#ifndef QVNCLOCALSTREAM_H
#define QVNCLOCALSTREAM_H

#include "qvncstream.h"

QT_BEGIN_NAMESPACE

class QLocalSocket;

class QVncLocalStream final : public QVncStream
{
    Q_OBJECT
public:
    explicit QVncLocalStream(QLocalSocket *socket, QObject *parent = nullptr);
    qint64 bytesAvailable() const override;
    qint64 queuedOutputBytes() const override;
    void close() override;

protected:
    qint64 readData(char *data, qint64 maxSize) override;
    qint64 writeData(const char *data, qint64 maxSize) override;

private:
    void finishDisconnect();
    QLocalSocket *m_socket;
    bool m_closed = false;
};

QT_END_NAMESPACE

#endif
