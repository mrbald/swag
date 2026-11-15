// Direct tests of the actual QPA factory's backing store, not a test replica.
// Link Qt5::GuiPrivate and Qt5::FbSupportPrivate; C++23; no moc/Widgets needed.
// Run under an EXTERNAL timeout (also for the baseline, which deadlocks):
//   timeout 20s env QT_PLUGIN_PATH=/path/to/plugins \
//     ./vncws_backingstore -platform vncws:size=800x600:port=5901 --case all
// Build both test and plugin with -fsanitize=address -fno-omit-frame-pointer
// and the plugin with QT_FORCE_ASSERTS for ASan/assertion qualification.
#include <QBackingStore>
#include <QGuiApplication>
#include <QImage>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QWindow>
#include <QtFbSupport/private/qfbbackingstore_p.h>
#include <climits>
#include <cstdio>
#include <functional>
#include <stdexcept>

namespace {
void require(bool condition, const char *message)
{
    if (!condition)
        throw std::runtime_error(message);
}

struct Store {
    QWindow window;
    QBackingStore backing{&window};
    QFbBackingStore *fb;
    explicit Store(QSize size = QSize(8, 6))
    {
        window.setSurfaceType(QSurface::RasterSurface);
        window.resize(size);
        window.create();
        // This fixture runs only on the framebuffer plugin, whose factory
        // returns QFbBackingStore (baseline) or QVncBackingStore (candidate).
        fb = static_cast<QFbBackingStore *>(backing.handle());
        fb->resize(size, {});
        require(image().size() == size, "initial resize failed");
        image().fill(QColor(40, 100, 180, 128));
    }
    QImage &image() { return *static_cast<QImage *>(fb->paintDevice()); }
};

void pixelsEqual(const QImage &actual, const QImage &before)
{
    for (int y = 0; y < actual.height(); ++y)
        for (int x = 0; x < actual.width(); ++x)
            require(actual.pixel(x, y) == (before.rect().contains(x, y) ? before.pixel(x, y) : 0),
                    "preserved/exposed pixel mismatch");
}

void nested()
{
    Store s;
    const QRgb untouched = s.image().pixel(7, 5);
    const QRegion region(QRect(1, 1, 2, 2));
    s.fb->beginPaint(region);
    require(s.image().pixel(1, 1) == 0, "outer alpha clear missing");
    require(s.image().pixel(7, 5) == untouched, "alpha clear escaped region");
    QPainter painter(&s.image());
    painter.fillRect(QRect(1, 1, 1, 1), Qt::red);
    s.fb->beginPaint(region); // Baseline self-deadlocks here.
    require(s.image().pixelColor(1, 1) == QColor(Qt::red), "nested begin cleared outer pixels");
    // Exercise exactly the unchanged compositor's lock/image/unlock sequence.
    s.fb->lock();
    const QImage snapshot = s.fb->image();
    s.fb->unlock();
    require(snapshot.constBits() != s.image().constBits(), "active-painter image copy was shallow");
    s.fb->endPaint();
    painter.fillRect(QRect(1, 1, 1, 1), Qt::green);
    require(snapshot.pixelColor(1, 1) == QColor(Qt::red), "snapshot changed after painter resumed");
    painter.end();
    s.fb->endPaint();
    require(s.image().pixelColor(1, 1) == QColor(Qt::green), "outer painter did not resume");
}

void resizeNested()
{
    Store s;
    s.fb->beginPaint({});
    const uchar *storage = s.image().constBits();
    QPainter painter(&s.image());
    s.fb->resize(QSize(12, 10), {});
    require(s.image().size() == QSize(8, 6) && s.image().constBits() == storage,
            "resize replaced active painter storage");
    s.fb->beginPaint(QRegion(QRect(0, 0, 8, 6)));
    s.fb->resize(QSize(17, 13), {});
    s.fb->endPaint();
    require(s.image().size() == QSize(8, 6), "inner end applied pending resize");
    painter.fillRect(QRect(7, 5, 1, 1), QColor(8, 72, 144, 128));
    painter.end();
    const QImage before = s.image().copy();
    s.fb->endPaint();
    require(s.image().size() == QSize(17, 13), "last deferred size not applied");
    pixelsEqual(s.image(), before);
}

void cancel()
{
    Store s;
    s.fb->beginPaint({});
    const uchar *storage = s.image().constBits();
    QPainter painter(&s.image());
    s.fb->resize(QSize(17, 13), {});
    s.fb->resize(QSize(0, 0), {});
    s.fb->resize(QSize(8, 6), {});
    painter.fillRect(QRect(7, 5, 1, 1), Qt::yellow);
    painter.end();
    s.fb->endPaint();
    require(s.image().size() == QSize(8, 6) && s.image().constBits() == storage,
            "last-size cancellation replaced storage");
    require(s.image().pixelColor(7, 5) == QColor(Qt::yellow), "cancel lost resumed paint");
    s.fb->beginPaint(QRegion(QRect(0, 0, 1, 1)));
    s.fb->endPaint();
    require(s.image().pixel(0, 0) == 0 && s.image().pixelColor(7, 5) == QColor(Qt::yellow),
            "paint after cancellation failed");
}

void empty()
{
    for (QSize size : {QSize(0, 0), QSize(0, 7), QSize(-1, -1), QSize(-1, 7)}) {
        Store s;
        s.fb->beginPaint({});
        QPainter painter(&s.image());
        s.fb->resize(size, {});
        require(s.image().size() == QSize(8, 6), "empty resize freed active storage");
        painter.fillRect(QRect(7, 5, 1, 1), Qt::cyan);
        painter.end();
        s.fb->endPaint();
        require(s.image().isNull(), "empty/invalid request not applied");
        s.fb->beginPaint({});
        s.fb->resize(QSize(9, 7), {});
        s.fb->endPaint();
        require(s.image().size() == QSize(9, 7), "deferred recovery from empty failed");
        pixelsEqual(s.image(), QImage());
    }
}

void preservation()
{
    Store s;
    for (QSize size : {QSize(17, 13), QSize(3, 2), QSize(12, 10)}) {
        const QImage before = s.image().copy();
        s.fb->beginPaint({});
        s.fb->resize(size, {});
        s.fb->endPaint();
        require(s.image().size() == size, "deferred size mismatch");
        pixelsEqual(s.image(), before); // Includes every exposed pixel and alpha.
    }
    // Shrinking translucent content must still clear only the next dirty area.
    const QRgb outside = s.image().pixel(1, 1);
    s.fb->beginPaint(QRegion(QRect(0, 0, 1, 1)));
    s.fb->endPaint();
    require(s.image().pixel(0, 0) == 0 && s.image().pixel(1, 1) == outside,
            "alpha clear after deferred resize changed semantics");
    s.fb->resize(QSize(5, 4), {}); // Ordinary path still delegates to Qt.
    require(s.image().size() == QSize(5, 4), "ordinary resize failed");
}

void allocationFailure()
{
    Store s;
    const QImage before = s.image().copy();
    const uchar *storage = s.image().constBits();
    s.fb->beginPaint({});
    // Image parameter arithmetic overflows before any giant allocation. This
    // deterministically exercises the same null-image result as allocation OOM.
    s.fb->resize(QSize(INT_MAX, INT_MAX), {});
    s.fb->endPaint();
    require(s.image().constBits() == storage, "allocation failure discarded old storage");
    pixelsEqual(s.image(), before);
    s.fb->beginPaint({});
    s.fb->endPaint(); // Retry must also leave the old image safe.
    require(s.image().constBits() == storage, "failed retry discarded old storage");
    s.fb->beginPaint({});
    s.fb->resize(QSize(11, 9), {}); // A later request supersedes the failed one.
    s.fb->endPaint();
    require(s.image().size() == QSize(11, 9), "failed resize could not be superseded");
    pixelsEqual(s.image(), before);
}

void redirected()
{
    Store s;
    s.fb->beginPaint({});
    QImage wrapper(s.image().bits(), s.image().width(), s.image().height(),
                   s.image().bytesPerLine(), s.image().format());
    QPainter painter(&wrapper);
    require(!s.image().paintingActive() && wrapper.paintingActive(), "wrapper test setup failed");
    painter.fillRect(QRect(1, 1, 1, 1), Qt::magenta);
    s.fb->beginPaint(QRegion(QRect(0, 0, 8, 6)));
    require(s.image().pixelColor(1, 1) == QColor(Qt::magenta), "redirected nested begin cleared pixels");
    s.fb->resize(QSize(13, 9), {});
    s.fb->endPaint();
    require(s.image().size() == QSize(8, 6), "redirected painter storage resized early");
    painter.fillRect(QRect(7, 5, 1, 1), Qt::yellow);
    painter.end();
    const QImage before = s.image().copy();
    s.fb->endPaint();
    require(s.image().size() == QSize(13, 9), "redirected resize was not applied");
    pixelsEqual(s.image(), before);
}
} // namespace

int main(int argc, char **argv)
{
    QGuiApplication::setAttribute(Qt::AA_DisableHighDpiScaling);
    QGuiApplication app(argc, argv);
    if (app.platformName() != QStringLiteral("vncws")) {
        std::fprintf(stderr, "requires -platform vncws\n");
        return 2;
    }
    const QStringList arguments = app.arguments();
    QString selected = QStringLiteral("all");
    if (arguments.size() == 3 && arguments[1] == QStringLiteral("--case"))
        selected = arguments[2];
    else if (arguments.size() != 1)
        return 2;
    const std::pair<const char *, std::function<void()>> tests[] = {
        {"nested", nested}, {"resize", resizeNested}, {"cancel", cancel},
        {"empty", empty}, {"preservation", preservation},
        {"allocation-failure", allocationFailure}, {"redirected", redirected}
    };
    QJsonArray results;
    bool failed = false;
    for (const auto &test : tests) {
        if (selected != QStringLiteral("all") && selected != QLatin1String(test.first))
            continue;
        std::fprintf(stderr, "starting %s\n", test.first);
        std::fflush(stderr);
        QJsonObject result{{"check", test.first}, {"status", "pass"}};
        try {
            test.second();
        } catch (const std::exception &error) {
            result["status"] = "fail";
            result["error"] = error.what();
            failed = true;
        }
        results.append(result);
    }
    if (results.isEmpty())
        return 2;
    std::puts(QJsonDocument(results).toJson(QJsonDocument::Compact).constData());
    return failed ? 1 : 0;
}
