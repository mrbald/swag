// Synthetic GUI-thread raster-paint/composite benchmark, not a WAN benchmark.
#include <QApplication>
#include <QCoreApplication>
#include <QElapsedTimer>
#include <QEvent>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPaintEvent>
#include <QPainter>
#include <QScreen>
#include <QWidget>
#include <cstdio>
#include <ctime>

class PaintWindow final : public QWidget
{
public:
    int frame = 0;
    int paints = 0;
protected:
    void paintEvent(QPaintEvent *event) override
    {
        ++paints;
        QPainter painter(this);
        painter.fillRect(event->rect(), QColor((frame * 37) & 255,
                                              (frame * 61) & 255,
                                              (frame * 17) & 255));
    }
};

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    const auto arguments = app.arguments();
    if (arguments.size() != 3 ||
        (arguments[1] != QStringLiteral("small") && arguments[1] != QStringLiteral("full"))) {
        std::fprintf(stderr, "usage: vncws-paint-benchmark small|full iterations\n");
        return 2;
    }
    bool valid = false;
    const int iterations = arguments[2].toInt(&valid);
    if (!valid || iterations < 1 || iterations > 1000000)
        return 2;
    PaintWindow window;
    window.resize(1280, 800);
    window.setAttribute(Qt::WA_OpaquePaintEvent);
    window.show();
    app.processEvents();
    const QRect region = arguments[1] == QStringLiteral("small")
        ? QRect(100, 100, 128, 64) : window.rect();
    auto frame = [&] {
        ++window.frame;
        window.repaint(region);
        // Dispatch the QFbScreen update posted by the backing-store flush.
        QCoreApplication::sendPostedEvents(nullptr, QEvent::UpdateRequest);
    };
    for (int i = 0; i < 100; ++i)
        frame();
    const int paintStart = window.paints;
    QElapsedTimer timer;
    const std::clock_t cpuStart = std::clock();
    timer.start();
    for (int i = 0; i < iterations; ++i)
        frame();
    const qint64 wallNs = timer.nsecsElapsed();
    const double cpuSeconds = double(std::clock() - cpuStart) / CLOCKS_PER_SEC;
    const QColor expected((window.frame * 37) & 255, (window.frame * 61) & 255,
                          (window.frame * 17) & 255);
    const QImage pixel = app.primaryScreen()->grabWindow(0, 110, 110, 1, 1).toImage();
    if (window.paints - paintStart != iterations || pixel.isNull() ||
        pixel.pixelColor(0, 0) != expected) {
        std::fprintf(stderr, "benchmark did not paint/composite every requested update\n");
        return 1;
    }
    const QJsonObject result{{"workload", arguments[1]}, {"iterations", iterations},
        {"wall_ns", double(wallNs)}, {"cpu_seconds", cpuSeconds},
        {"width", window.width()}, {"height", window.height()},
        {"dirty_pixels", region.width() * region.height()},
        {"scope", "synthetic repaint+composite, no VNC client or encoding"}};
    const QByteArray json = QJsonDocument(result).toJson(QJsonDocument::Compact);
    std::puts(json.constData());
}
