#ifndef QVNCWEBSOCKETSTREAM_H
#define QVNCWEBSOCKETSTREAM_H

#include <QtCore/QIODevice>
#include <QtCore/QByteArray>

QT_BEGIN_NAMESPACE

class QWebSocket;

// The RFB decoder consumes a byte stream. WebSocket message boundaries are
// deliberately hidden here; every outgoing write becomes one binary frame.
class QVncWebSocketStream final : public QIODevice
{
    Q_OBJECT
public:
    explicit QVncWebSocketStream(QWebSocket *socket, QObject *parent = nullptr);
    bool isSequential() const override { return true; }
    qint64 bytesAvailable() const override;
    qint64 queuedOutputBytes() const;
    void close() override;

signals:
    void disconnected();
    void outputDrained();

protected:
    qint64 readData(char *data, qint64 maxSize) override;
    qint64 writeData(const char *data, qint64 maxSize) override;

private:
    void finishDisconnect();
    QWebSocket *m_socket;
    QByteArray m_input;
    bool m_closed = false;
};

QT_END_NAMESPACE

#endif
