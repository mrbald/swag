// Diagnose actual Qt fallback glyphs, independently of clipboard/codepoint tests.
#include <QApplication>
#include <QFont>
#include <QGlyphRun>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QTextLayout>
#include <cstdio>

int main(int argc, char **argv)
{
    QApplication app(argc, argv);
    const QString sample = QStringLiteral("東京");
    QTextLayout layout(sample, QFont(QStringLiteral("DejaVu Sans"), 10));
    layout.beginLayout();
    auto line = layout.createLine();
    line.setLineWidth(400);
    layout.endLayout();
    QJsonArray runs;
    int glyphCount = 0;
    int missing = 0;
    for (const auto &run : layout.glyphRuns()) {
        QJsonArray glyphs;
        for (const auto glyph : run.glyphIndexes()) {
            glyphs.append(int(glyph));
            ++glyphCount;
            missing += glyph == 0;
        }
        runs.append(QJsonObject{{"family", run.rawFont().familyName()},
                                {"glyphs", glyphs}});
    }
    const bool covered = glyphCount == sample.size() && missing == 0;
    const QJsonObject result{{"sample", sample}, {"runs", runs},
                             {"glyph_count", glyphCount}, {"missing", missing},
                             {"covered", covered}};
    const auto bytes = QJsonDocument(result).toJson();
    std::fwrite(bytes.constData(), 1, size_t(bytes.size()), stdout);
    return covered ? 0 : 1;
}
