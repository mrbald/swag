#ifndef QVNCBACKINGSTORE_H
#define QVNCBACKINGSTORE_H

#include <QtFbSupport/private/qfbbackingstore_p.h>

QT_BEGIN_NAMESPACE

// GUI-thread-only, including composition. QFbScreen still uses its existing
// short lock/image/unlock sequence; painting must not hold that mutex across
// application callbacks, which can enter a nested event loop.
class QVncBackingStore final : public QFbBackingStore
{
public:
    using QFbBackingStore::QFbBackingStore;
    void beginPaint(const QRegion &region) override;
    void endPaint() override;
    void resize(const QSize &size, const QRegion &staticContents) override;

private:
    int mPaintDepth = 0;
    bool mResizePending = false; // Separate flag: empty/invalid sizes are requests too.
    QSize mDeferredSize;
    bool mReportedNestedPaint = false;
    bool mReportedDeferredResize = false;
    bool mReportedAllocationFailure = false;
};

QT_END_NAMESPACE

#endif // QVNCBACKINGSTORE_H
