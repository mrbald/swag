#include "qvncwebsocketstream.h"

#include <QtWebSockets/QWebSocket>
#include <QtCore/QDebug>

#include <algorithm>
#include <cstring>

QT_BEGIN_NAMESPACE

namespace {
constexpr qint64 MaxInputBytes = 1 * 1024 * 1024;
constexpr qint64 MaxOutputBytes = 32 * 1024 * 1024;
}

QVncWebSocketStream::QVncWebSocketStream(QWebSocket *socket, QObject *parent)
    : QVncStream(parent), m_socket(socket)
{
    m_socket->setParent(this);
    m_socket->setMaxAllowedIncomingFrameSize(MaxInputBytes);
    m_socket->setMaxAllowedIncomingMessageSize(MaxInputBytes);
    open(QIODevice::ReadWrite | QIODevice::Unbuffered);
    connect(m_socket, &QWebSocket::binaryMessageReceived, this,
            [this](const QByteArray &message) {
        if (m_closed)
            return;
        if (message.size() > MaxInputBytes - m_input.size()) {
            qWarning("vncws: input queue limit exceeded");
            close();
            return;
        }
        m_input.append(message);
        emit readyRead();
    });
    connect(m_socket, &QWebSocket::textMessageReceived, this,
            [this](const QString &) { close(); });
    connect(m_socket, &QWebSocket::disconnected, this,
            &QVncWebSocketStream::finishDisconnect);
    connect(m_socket, &QWebSocket::bytesWritten, this, [this](qint64) {
        if (!m_closed && m_socket->bytesToWrite() <= 512 * 1024)
            emit outputDrained();
    });
}

qint64 QVncWebSocketStream::bytesAvailable() const
{
    return m_input.size() + QIODevice::bytesAvailable();
}

qint64 QVncWebSocketStream::queuedOutputBytes() const
{
    return m_socket->bytesToWrite();
}

void QVncWebSocketStream::close()
{
    if (m_closed)
        return;
    m_closed = true;
    QIODevice::close();
    m_socket->abort();
    emit disconnected();
}

void QVncWebSocketStream::finishDisconnect()
{
    if (m_closed)
        return;
    m_closed = true;
    QIODevice::close();
    emit disconnected();
}

qint64 QVncWebSocketStream::readData(char *data, qint64 maxSize)
{
    const qint64 n = std::min(maxSize, qint64(m_input.size()));
    if (n > 0) {
        std::memcpy(data, m_input.constData(), size_t(n));
        m_input.remove(0, int(n));
    }
    return n;
}

qint64 QVncWebSocketStream::writeData(const char *data, qint64 maxSize)
{
    if (m_closed || maxSize < 0 || maxSize > MaxOutputBytes ||
        m_socket->bytesToWrite() > MaxOutputBytes - maxSize) {
        qWarning("vncws: output queue limit exceeded");
        close();
        return -1;
    }
    return m_socket->sendBinaryMessage(QByteArray(data, int(maxSize)));
}

QT_END_NAMESPACE
