#ifndef QVNCSTREAM_H
#define QVNCSTREAM_H

#include <QtCore/QIODevice>

QT_BEGIN_NAMESPACE

// Transport-independent, bounded byte stream consumed by the RFB decoder.
class QVncStream : public QIODevice
{
    Q_OBJECT
public:
    explicit QVncStream(QObject *parent = nullptr) : QIODevice(parent) {}
    bool isSequential() const override { return true; }
    virtual qint64 queuedOutputBytes() const = 0;

signals:
    void disconnected();
    void outputDrained();
};

QT_END_NAMESPACE

#endif
