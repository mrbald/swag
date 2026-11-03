#ifndef QVNCUNIXLISTENER_H
#define QVNCUNIXLISTENER_H

#include <QtCore/QByteArray>
#include <QtCore/QObject>
#include <QtCore/QString>

QT_BEGIN_NAMESPACE

class QLocalSocket;
class QSocketNotifier;

// Owns its descriptor, but owns the pathname only when it performed bind().
// QLocalServer::listen(fd) cannot express that distinction on Qt 5.15/Linux:
// its closeServer() unconditionally removes the inherited pathname.
class QVncUnixListener final : public QObject
{
    Q_OBJECT
public:
    QVncUnixListener(const QString &path, int inheritedFd, QObject *parent);
    ~QVncUnixListener() override;
    static QString validateDescriptor(int fd);

signals:
    void newConnection(QLocalSocket *socket);

private:
    void acceptConnections();
    void removeOwnedPath();
    int m_fd = -1;
    QSocketNotifier *m_notifier = nullptr;
    QByteArray m_ownedPath;
    quint64 m_ownedDevice = 0;
    quint64 m_ownedInode = 0;
};

QT_END_NAMESPACE

#endif
