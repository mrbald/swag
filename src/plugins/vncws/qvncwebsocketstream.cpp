#include "qvncwebsocketstream.h"

#include <QtWebSockets/QWebSocket>
#include <QtCore/QDebug>

#include <algorithm>
#include <cstring>

QT_BEGIN_NAMESPACE

namespace {
constexpr qint64 MaxInputBytes = 1 * 1024 * 1024;
constexpr qint64 MaxOutputBytes = 16 * 1024 * 1024;
}

QVncWebSocketStream::QVncWebSocketStream(QWebSocket *socket, QObject *parent)
    : QIODevice(parent), m_socket(socket)
{
    m_socket->setParent(this);
    m_socket->setMaxAllowedIncomingFrameSize(MaxInputBytes);
    m_socket->setMaxAllowedIncomingMessageSize(MaxInputBytes);
    open(QIODevice::ReadWrite | QIODevice::Unbuffered);
    connect(m_socket, &QWebSocket::binaryMessageReceived, this,
            [this](const QByteArray &message) {
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
            &QVncWebSocketStream::disconnected);
}

qint64 QVncWebSocketStream::bytesAvailable() const
{
    return m_input.size() + QIODevice::bytesAvailable();
}

void QVncWebSocketStream::close()
{
    if (m_socket)
        m_socket->close();
    QIODevice::close();
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
    if (maxSize < 0 || maxSize > MaxOutputBytes ||
        m_socket->bytesToWrite() > MaxOutputBytes - maxSize) {
        qWarning("vncws: output queue limit exceeded");
        close();
        return -1;
    }
    return m_socket->sendBinaryMessage(QByteArray(data, int(maxSize)));
}

QT_END_NAMESPACE
