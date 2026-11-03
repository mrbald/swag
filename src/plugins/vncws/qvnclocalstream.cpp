#include "qvnclocalstream.h"

#include <QtCore/QDebug>
#include <QtNetwork/QLocalSocket>

QT_BEGIN_NAMESPACE

namespace {
constexpr qint64 MaxInputBytes = 1 * 1024 * 1024;
constexpr qint64 MaxOutputBytes = 32 * 1024 * 1024;
}

QVncLocalStream::QVncLocalStream(QLocalSocket *socket, QObject *parent)
    : QVncStream(parent), m_socket(socket)
{
    m_socket->setParent(this);
    // The decoder reads directly from this bounded Qt buffer; there is no
    // second application-side input queue for the local transport.
    m_socket->setReadBufferSize(MaxInputBytes);
    open(QIODevice::ReadWrite | QIODevice::Unbuffered);
    connect(m_socket, &QLocalSocket::readyRead, this, [this] {
        if (!m_closed)
            emit readyRead();
    });
    connect(m_socket, &QLocalSocket::disconnected, this,
            &QVncLocalStream::finishDisconnect);
    connect(m_socket, &QLocalSocket::bytesWritten, this, [this](qint64) {
        if (!m_closed && m_socket->bytesToWrite() <= 512 * 1024)
            emit outputDrained();
    });
}

qint64 QVncLocalStream::bytesAvailable() const
{
    return m_socket->bytesAvailable() + QIODevice::bytesAvailable();
}

qint64 QVncLocalStream::queuedOutputBytes() const
{
    return m_socket->bytesToWrite();
}

void QVncLocalStream::close()
{
    if (m_closed)
        return;
    m_closed = true;
    QIODevice::close();
    m_socket->abort();
    emit disconnected();
}

void QVncLocalStream::finishDisconnect()
{
    if (m_closed)
        return;
    m_closed = true;
    QIODevice::close();
    emit disconnected();
}

qint64 QVncLocalStream::readData(char *data, qint64 maxSize)
{
    return m_socket->read(data, maxSize);
}

qint64 QVncLocalStream::writeData(const char *data, qint64 maxSize)
{
    if (m_closed || maxSize < 0 || maxSize > MaxOutputBytes ||
        m_socket->bytesToWrite() > MaxOutputBytes - maxSize) {
        qWarning("vncws: output queue limit exceeded");
        close();
        return -1;
    }
    return m_socket->write(data, maxSize);
}

QT_END_NAMESPACE
