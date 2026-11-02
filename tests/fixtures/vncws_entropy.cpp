#include <QApplication>
#include <QCursor>
#include <QImage>
#include <QPainter>
#include <QPixmap>
#include <QWidget>
#include <cstdint>

class EntropyWindow final : public QWidget
{
public:
    EntropyWindow()
        : image(3440, 1440, QImage::Format_RGB32)
    {
        uint32_t state = 0x6d2b79f5U;
        for (int y = 0; y < image.height(); ++y) {
            auto *row = reinterpret_cast<uint32_t *>(image.scanLine(y));
            for (int x = 0; x < image.width(); ++x) {
                state ^= state << 13;
                state ^= state >> 17;
                state ^= state << 5;
                row[x] = 0xff000000U | (state & 0x00ffffffU);
            }
        }
        QImage cursorImage(495, 499, QImage::Format_ARGB32);
        cursorImage.fill(0xff123456U);
        setCursor(QCursor(QPixmap::fromImage(cursorImage), 0, 0));
        resize(image.size());
    }

protected:
    void paintEvent(QPaintEvent *) override
    {
        QPainter painter(this);
        painter.drawImage(QPoint(0, 0), image);
    }

private:
    QImage image;
};

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    EntropyWindow window;
    window.show();
    return app.exec();
}
