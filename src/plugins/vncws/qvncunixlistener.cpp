#include "qvncunixlistener.h"

#include <QtCore/QDir>
#include <QtCore/QFile>
#include <QtCore/QFileInfo>
#include <QtCore/QSocketNotifier>
#include <QtCore/QTimer>
#include <QtNetwork/QLocalSocket>

#include <cerrno>
#include <cstring>

#ifdef Q_OS_LINUX
#include <fcntl.h>
#include <stddef.h>
#include <sys/socket.h>
#include <sys/stat.h>
#include <sys/un.h>
#include <unistd.h>
#endif

QT_BEGIN_NAMESPACE

namespace {
#ifdef Q_OS_LINUX
QByteArray validatePath(const QString &path)
{
    const QByteArray encoded = QFile::encodeName(path);
    if (!QDir::isAbsolutePath(path) || QDir::cleanPath(path) != path ||
        path.endsWith(QLatin1Char('/')) || path.contains(QLatin1Char(':')) ||
        path.contains(QLatin1Char('=')) || encoded.contains('\0') ||
        encoded.size() >= int(sizeof(sockaddr_un::sun_path)))
        qFatal("vncws: socket requires a canonical absolute filesystem path without ':' or '='");
    const QString parent = QFileInfo(path).absolutePath();
    if (QFileInfo(parent).canonicalFilePath() != parent)
        qFatal("vncws: socket parent must exist and must not traverse symbolic links");
    const QByteArray encodedParent = QFile::encodeName(parent);
    struct stat state{};
    if (::lstat(encodedParent.constData(), &state) != 0 || !S_ISDIR(state.st_mode) ||
        state.st_uid != ::geteuid() || (state.st_mode & 07777) != 0700)
        qFatal("vncws: socket parent must be owned by the current UID with mode 0700");
    return encoded;
}

void markDescriptor(int fd)
{
    const int flags = ::fcntl(fd, F_GETFD);
    const int status = ::fcntl(fd, F_GETFL);
    if (flags < 0 || status < 0 || ::fcntl(fd, F_SETFD, flags | FD_CLOEXEC) < 0 ||
        ::fcntl(fd, F_SETFL, status | O_NONBLOCK) < 0)
        qFatal("vncws: cannot make Unix listener nonblocking and close-on-exec");
}
#endif
}

QString QVncUnixListener::validateDescriptor(int fd)
{
#ifdef Q_OS_LINUX
    int type = 0;
    socklen_t length = sizeof(type);
    if (::getsockopt(fd, SOL_SOCKET, SO_TYPE, &type, &length) != 0 ||
        length != sizeof(type) || type != SOCK_STREAM)
        qFatal("vncws: Unix fd must be a stream socket");
    int accepting = 0;
    length = sizeof(accepting);
    if (::getsockopt(fd, SOL_SOCKET, SO_ACCEPTCONN, &accepting, &length) != 0 ||
        length != sizeof(accepting) || accepting != 1)
        qFatal("vncws: Unix fd must already be listening");
    sockaddr_un address{};
    length = sizeof(address);
    if (::getsockname(fd, reinterpret_cast<sockaddr *>(&address), &length) != 0 ||
        address.sun_family != AF_UNIX || length <= offsetof(sockaddr_un, sun_path) ||
        length > sizeof(address) || address.sun_path[0] != '/')
        qFatal("vncws: Unix fd must bind a filesystem socket, not an abstract or unnamed socket");
    const size_t capacity = length - offsetof(sockaddr_un, sun_path);
    const size_t size = ::strnlen(address.sun_path, capacity);
    if (size == capacity || size + 1 != capacity)
        qFatal("vncws: Unix fd has an invalid socket pathname");
    const QByteArray encoded(address.sun_path, int(size));
    const QString path = QFile::decodeName(encoded);
    if (validatePath(path) != encoded)
        qFatal("vncws: Unix fd pathname cannot be represented exactly");
    struct stat state{};
    if (::lstat(encoded.constData(), &state) != 0 || !S_ISSOCK(state.st_mode) ||
        state.st_uid != ::geteuid() || (state.st_mode & 07777) != 0600)
        qFatal("vncws: Unix fd pathname must be a current-UID socket with mode 0600");
    markDescriptor(fd);
    return path;
#else
    Q_UNUSED(fd)
    qFatal("vncws: Unix socket transport currently requires Linux");
#endif
}

QVncUnixListener::QVncUnixListener(const QString &path, int inheritedFd, QObject *parent)
    : QObject(parent)
{
#ifdef Q_OS_LINUX
    const QByteArray encoded = validatePath(path);
    if (inheritedFd >= 0) {
        if (validateDescriptor(inheritedFd) != path)
            qFatal("vncws: inherited Unix listener pathname changed");
        m_fd = inheritedFd;
    } else {
        struct stat existing{};
        if (::lstat(encoded.constData(), &existing) == 0 || errno != ENOENT)
            qFatal("vncws: socket pathname already exists or cannot be inspected");
        m_fd = ::socket(AF_UNIX, SOCK_STREAM | SOCK_CLOEXEC | SOCK_NONBLOCK, 0);
        if (m_fd < 0)
            qFatal("vncws: cannot create Unix listener: %s", strerror(errno));
        sockaddr_un address{};
        address.sun_family = AF_UNIX;
        std::memcpy(address.sun_path, encoded.constData(), size_t(encoded.size()) + 1);
        // The parent is private; this additionally avoids a group/world-accessible
        // socket inode even between bind() and the exact mode-0600 chmod().
        const mode_t previousMask = ::umask(0077);
        const int result = ::bind(m_fd, reinterpret_cast<const sockaddr *>(&address),
                                  socklen_t(offsetof(sockaddr_un, sun_path) + encoded.size() + 1));
        const int bindError = errno;
        ::umask(previousMask);
        if (result != 0)
            qFatal("vncws: cannot bind Unix socket: %s", strerror(bindError));
        struct stat bound{};
        if (::lstat(encoded.constData(), &bound) != 0 || !S_ISSOCK(bound.st_mode) ||
            bound.st_uid != ::geteuid())
            qFatal("vncws: cannot inspect newly bound Unix socket");
        m_ownedPath = encoded;
        m_ownedDevice = quint64(bound.st_dev);
        m_ownedInode = quint64(bound.st_ino);
        if (::chmod(encoded.constData(), 0600) != 0 || ::listen(m_fd, 16) != 0) {
            const int error = errno;
            removeOwnedPath();
            qFatal("vncws: cannot secure/listen on Unix socket: %s", strerror(error));
        }
    }
    m_notifier = new QSocketNotifier(m_fd, QSocketNotifier::Read, this);
    connect(m_notifier, &QSocketNotifier::activated, this,
            [this] { acceptConnections(); });
#else
    Q_UNUSED(path)
    Q_UNUSED(inheritedFd)
    qFatal("vncws: Unix socket transport currently requires Linux");
#endif
}

QVncUnixListener::~QVncUnixListener()
{
    if (m_notifier)
        m_notifier->setEnabled(false);
#ifdef Q_OS_LINUX
    if (m_fd >= 0)
        ::close(m_fd);
    removeOwnedPath();
#endif
}

void QVncUnixListener::removeOwnedPath()
{
#ifdef Q_OS_LINUX
    if (m_ownedPath.isEmpty())
        return;
    struct stat state{};
    if (::lstat(m_ownedPath.constData(), &state) == 0 && S_ISSOCK(state.st_mode) &&
        quint64(state.st_dev) == m_ownedDevice && quint64(state.st_ino) == m_ownedInode)
        ::unlink(m_ownedPath.constData());
    m_ownedPath.clear();
#endif
}

void QVncUnixListener::acceptConnections()
{
#ifdef Q_OS_LINUX
    // Bound work per activation; accepted clients retain the existing takeover
    // policy rather than accumulating an unbounded list of pending connections.
    for (int attempt = 0; attempt < 16; ++attempt) {
        const int accepted = ::accept4(m_fd, nullptr, nullptr, SOCK_NONBLOCK | SOCK_CLOEXEC);
        if (accepted < 0) {
            if (errno == EINTR)
                continue;
            if (errno == EAGAIN || errno == EWOULDBLOCK)
                return;
            qWarning("vncws: Unix accept failed: %s", strerror(errno));
            m_notifier->setEnabled(false);
            QTimer::singleShot(100, this, [this] { m_notifier->setEnabled(true); });
            return;
        }
        auto *socket = new QLocalSocket;
        if (!socket->setSocketDescriptor(accepted, QLocalSocket::ConnectedState,
                                         QIODevice::ReadWrite)) {
            ::close(accepted);
            delete socket;
            continue;
        }
        emit newConnection(socket);
    }
#endif
}

QT_END_NAMESPACE
