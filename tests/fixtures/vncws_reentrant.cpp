// Standalone Qt 5.15 Widgets regression fixture; no moc or private Qt headers.
// Build (GCC 15):
//   g++ -std=c++23 -O2 -fPIC tests/fixtures/vncws_reentrant.cpp \
//       $(pkg-config --cflags --libs Qt5Widgets) -o /tmp/vncws_reentrant
// Run with the plugin directory containing platforms/libqvncws.so:
//   QT_PLUGIN_PATH=/path/to/plugins SWAG_STATE_FILE=/tmp/state.json \
//       /tmp/vncws_reentrant -platform vncws:size=800x600:addr=127.0.0.1:port=5901
// All actions come from ordinary Qt input. The timer/JSON only observe.
#include <QAbstractTableModel>
#include <QApplication>
#include <QCommandLineParser>
#include <QDialog>
#include <QElapsedTimer>
#include <QHeaderView>
#include <QJsonDocument>
#include <QJsonObject>
#include <QPainter>
#include <QPushButton>
#include <QResizeEvent>
#include <QSaveFile>
#include <QTableView>
#include <QThread>
#include <QTimer>
#include <QWidget>
#include <functional>

namespace {

// Independent consumers hardcode this palette; JSON reports revision only.
const QColor parentColors[] = {
    QColor(36, 90, 156), QColor(38, 142, 82),
    QColor(224, 178, 48), QColor(128, 80, 180)
};
const QColor dialogColor(210, 90, 54);
const QColor bandColor(40, 120, 220);

QJsonObject rectangle(const QRect &rect)
{
    return {{"x", rect.x()}, {"y", rect.y()},
            {"width", rect.width()}, {"height", rect.height()}};
}

struct Observation {
    QString path;
    QElapsedTimer elapsed;
    QWidget *root = nullptr;
    QRect tableCellInterior;
    bool alphaDialog = false;
    bool maximized = false;
    bool allowParentResize = false;
    int dialogsPerPaint = 1;
    int dialogsThisArm = 0;
    int outerTablePaintId = 0;
    int modalTriggerPaintId = 0;
    int parentResizeEvents = 0;
    int resizeRequests = 0;
    int updateRequests = 0;
    bool armed = false;
    bool modalActive = false;
    int heartbeat = 0;
    int armCount = 0;
    int modalEntered = 0;
    int modalExited = 0;
    int followUpCount = 0;
    int parentRevision = 0;
    int tablePaintEntered = 0;
    int tablePaintExited = 0;
    int tablePaintDepth = 0;
    int dataCalls = 0;
    int paintDataCalls = 0;
    int dialogPaints = 0;
    int parentPaints = 0;
    int modalTriggerPaintDepth = 0;
    bool modalTriggerThreadMatchesGui = false;

    QJsonObject rect(int x, int y, int width, int height) const
    {
        return rectangle(QRect(root->mapToGlobal(QPoint(x, y)), QSize(width, height)));
    }

    void publish() const
    {
        if (path.isEmpty() || !root)
            return;
        const QJsonObject controls{
            {"arm", rect(24, 20, 180, 44)},
            {"follow_up", rect(224, 20, 180, 44)},
            {"dismiss", rect(324, 456, 152, 48)},
            {"resize_parent", rect(244, 328, 144, 40)},
            {"update_parent", rect(412, 328, 144, 40)}
        };
        const QJsonObject geometry{
            {"root", rect(0, 0, root->width(), root->height())},
            {"table", rect(24, 88, 752, 264)},
            {"dialog", rect(220, 236, 360, 292)}
        };
        const QJsonObject samples{
            {"parent", rect(56, 432, 48, 32)},
            {"dialog_body", rect(244, 260, 48, 32)},
            {"stacking", rect(280, 420, 48, 24)},
            {"alpha", rect(280, 384, 48, 24)},
            {"table_cell", rectangle(tableCellInterior)}
        };
        const QJsonObject state{
            {"schema_version", 1}, {"fixture", "vncws_reentrant"},
            {"pid", double(QCoreApplication::applicationPid())},
            {"elapsed_ms", double(elapsed.elapsed())},
            {"ready", tablePaintExited > 0 && parentPaints > 0 && heartbeat > 0},
            {"heartbeat", heartbeat}, {"armed", armed},
            {"modal_active", modalActive}, {"alpha_dialog", alphaDialog},
            {"maximized", maximized}, {"allow_parent_resize", allowParentResize},
            {"dialogs_per_paint", dialogsPerPaint},
            {"dialogs_this_arm", dialogsThisArm},
            {"modal_trigger_paint_id", modalTriggerPaintId},
            {"parent_resize_events", parentResizeEvents},
            {"resize_requests", resizeRequests}, {"update_requests", updateRequests},
            {"arm_count", armCount}, {"modal_entered", modalEntered},
            {"modal_exited", modalExited}, {"follow_up_count", followUpCount},
            {"parent_revision", parentRevision},
            {"table_paint_entered", tablePaintEntered},
            {"table_paint_exited", tablePaintExited},
            {"table_paint_depth", tablePaintDepth},
            {"data_calls", dataCalls}, {"paint_data_calls", paintDataCalls},
            {"dialog_paints", dialogPaints}, {"parent_paints", parentPaints},
            {"modal_trigger_paint_depth", modalTriggerPaintDepth},
            {"modal_trigger_thread_matches_gui", modalTriggerThreadMatchesGui},
            {"controls", controls}, {"geometry", geometry}, {"samples", samples}
        };
        QSaveFile file(path);
        if (!file.open(QIODevice::WriteOnly))
            qFatal("cannot open SWAG_STATE_FILE: %s", qPrintable(file.errorString()));
        const QByteArray bytes = QJsonDocument(state).toJson(QJsonDocument::Compact);
        if (file.write(bytes) != bytes.size() || !file.commit())
            qFatal("cannot publish SWAG_STATE_FILE: %s", qPrintable(file.errorString()));
    }
};

class ParentPatch final : public QWidget {
public:
    ParentPatch(Observation &state, QWidget *parent) : QWidget(parent), s(state)
    {
        setAttribute(Qt::WA_OpaquePaintEvent);
    }
protected:
    void paintEvent(QPaintEvent *) override
    {
        {
            QPainter painter(this);
            painter.fillRect(rect(), parentColors[s.parentRevision % 4]);
        }
        ++s.parentPaints;
        s.publish();
    }
private:
    Observation &s;
};

class ModalDialog final : public QDialog {
public:
    ModalDialog(Observation &state, QWidget *parent)
        : QDialog(parent, Qt::Dialog | Qt::FramelessWindowHint), s(state)
    {
        setObjectName(QStringLiteral("reentrant_dialog"));
        setFixedSize(360, 292);
        move(parent->mapToGlobal(QPoint(220, 236)));
        if (s.alphaDialog)
            setAttribute(Qt::WA_TranslucentBackground);
        auto *dismiss = new QPushButton(QStringLiteral("Dismiss modal"), this);
        dismiss->setObjectName(QStringLiteral("dismiss"));
        dismiss->setGeometry(104, 220, 152, 48);
        connect(dismiss, &QPushButton::clicked, this, &QDialog::accept);
        auto *resizeParent = new QPushButton(QStringLiteral("Resize parent"), this);
        resizeParent->setGeometry(24, 92, 144, 40);
        resizeParent->setEnabled(s.allowParentResize);
        connect(resizeParent, &QPushButton::clicked, this, [this, parent] {
            ++s.resizeRequests;
            parent->resize(parent->size() == QSize(800, 600) ? QSize(1000, 700) : QSize(800, 600));
            s.publish();
        });
        auto *updateParent = new QPushButton(QStringLiteral("Repaint parent"), this);
        updateParent->setGeometry(192, 92, 144, 40);
        connect(updateParent, &QPushButton::clicked, this, [this, parent] {
            ++s.updateRequests;
            parent->repaint(); // A real input action deliberately requests nested sync.
            s.publish();
        });
    }
protected:
    void paintEvent(QPaintEvent *) override
    {
        {
            QPainter painter(this);
            painter.setCompositionMode(QPainter::CompositionMode_Source);
            painter.fillRect(rect(), dialogColor);
            QColor band = bandColor;
            band.setAlpha(s.alphaDialog ? 128 : 255);
            // Source replaces this band's alpha; the screen compositor must
            // blend it over the parent. Other dialog samples remain opaque.
            painter.fillRect(QRect(24, 140, 312, 36), band);
        }
        ++s.dialogPaints;
        s.publish();
    }
private:
    Observation &s;
};

class PaintTable final : public QTableView {
public:
    PaintTable(Observation &state, QWidget *parent) : QTableView(parent), s(state) {}
protected:
    void paintEvent(QPaintEvent *event) override
    {
        ++s.tablePaintEntered;
        if (s.tablePaintDepth == 0)
            s.outerTablePaintId = s.tablePaintEntered;
        ++s.tablePaintDepth;
        s.publish();
        // data() can enter QDialog::exec() only inside this exact base call.
        QTableView::paintEvent(event);
        --s.tablePaintDepth;
        ++s.tablePaintExited;
        const QRect cell = visualRect(model()->index(0, 0)).adjusted(4, 4, -4, -4);
        s.tableCellInterior = QRect(viewport()->mapToGlobal(cell.topLeft()), cell.size());
        s.publish();
    }
private:
    Observation &s;
};

class ModalModel final : public QAbstractTableModel {
public:
    ModalModel(Observation &state, QObject *parent) : QAbstractTableModel(parent), s(state) {}
    std::function<void()> enterModal;
    int rowCount(const QModelIndex &parent = QModelIndex()) const override
    {
        return parent.isValid() ? 0 : 8;
    }
    int columnCount(const QModelIndex &parent = QModelIndex()) const override
    {
        return parent.isValid() ? 0 : 4;
    }
    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override
    {
        ++s.dataCalls;
        if (s.tablePaintDepth > 0)
            ++s.paintDataCalls;
        if (!index.isValid() || role != Qt::DisplayRole)
            return {};
        if (s.armed && !s.modalActive && s.tablePaintDepth > 0 &&
            index.row() == 0 && index.column() == s.dialogsThisArm) {
            // Two different cells can fail sequentially in one base paintEvent.
            // Advance before exec so re-requests for this cell cannot retrigger.
            ++s.dialogsThisArm;
            s.armed = s.dialogsThisArm < s.dialogsPerPaint;
            s.modalTriggerPaintDepth = s.tablePaintDepth;
            s.modalTriggerPaintId = s.outerTablePaintId;
            s.modalTriggerThreadMatchesGui = QThread::currentThread() == qApp->thread();
            enterModal(); // Synchronous: preserve the active QTableView painter.
        }
        return QStringLiteral("row %1 / column %2").arg(index.row()).arg(index.column());
    }
private:
    Observation &s;
};

class Fixture final : public QWidget {
public:
    explicit Fixture(Observation &state)
        : QWidget(nullptr, Qt::Window | Qt::FramelessWindowHint), s(state)
    {
        s.root = this;
        setWindowTitle(QStringLiteral("vncws paint reentrancy"));
        if (s.maximized || s.allowParentResize) {
            setMinimumSize(800, 600);
            resize(800, 600);
        } else {
            setFixedSize(800, 600);
        }
        move(0, 0);
        auto *arm = new QPushButton(QStringLiteral("Arm next paint"), this);
        arm->setObjectName(QStringLiteral("arm"));
        arm->setGeometry(24, 20, 180, 44);
        auto *followUp = new QPushButton(QStringLiteral("Follow-up action"), this);
        followUp->setObjectName(QStringLiteral("follow_up"));
        followUp->setGeometry(224, 20, 180, 44);
        auto *table = new PaintTable(s, this);
        table->setGeometry(24, 88, 752, 264);
        auto *model = new ModalModel(s, table);
        table->setModel(model);
        table->setEditTriggers(QAbstractItemView::NoEditTriggers);
        table->setSelectionMode(QAbstractItemView::NoSelection);
        table->horizontalHeader()->setSectionResizeMode(QHeaderView::Fixed);
        table->horizontalHeader()->setDefaultSectionSize(170);
        table->verticalHeader()->setSectionResizeMode(QHeaderView::Fixed);
        table->verticalHeader()->setDefaultSectionSize(27);
        auto *patch = new ParentPatch(s, this);
        patch->setGeometry(24, 372, 752, 204);
        connect(arm, &QPushButton::clicked, this, [this, table] {
            if (s.armed || s.modalActive)
                return;
            ++s.armCount;
            s.dialogsThisArm = 0;
            s.armed = true;
            s.publish();
            table->viewport()->update();
        });
        connect(followUp, &QPushButton::clicked, this, [this, patch, table] {
            ++s.followUpCount;
            ++s.parentRevision;
            patch->update();
            table->viewport()->update();
            s.publish();
        });
        model->enterModal = [this, patch] {
            ModalDialog dialog(s, this);
            ++s.modalEntered;
            s.modalActive = true;
            s.publish();
            dialog.exec(); // Deliberately enter a nested loop from model data().
            s.modalActive = false;
            ++s.modalExited;
            ++s.parentRevision;
            patch->update();
            s.publish();
        };
        auto *timer = new QTimer(this);
        timer->setInterval(100);
        connect(timer, &QTimer::timeout, this, [this] {
            ++s.heartbeat;
            s.publish();
        });
        timer->start();
    }
protected:
    void resizeEvent(QResizeEvent *event) override
    {
        QWidget::resizeEvent(event);
        ++s.parentResizeEvents;
        s.publish();
    }
private:
    Observation &s;
};

} // namespace

int main(int argc, char **argv)
{
    QApplication::setAttribute(Qt::AA_DisableHighDpiScaling);
    QApplication app(argc, argv);
    QApplication::setStyle(QStringLiteral("Fusion"));
    QCommandLineParser parser;
    parser.setApplicationDescription(QStringLiteral(
        "Real input -> QTableView paint -> model data -> modal exec regression"));
    parser.addHelpOption();
    parser.addOption({QStringLiteral("state-file"), QStringLiteral("Passive observation JSON"),
                      QStringLiteral("path")});
    parser.addOption({QStringLiteral("alpha-dialog"),
                      QStringLiteral("Use an alpha-128 band in the modal window")});
    parser.addOption({QStringLiteral("dialogs-per-paint"),
                      QStringLiteral("Fail one or two different cells in the same table paint"),
                      QStringLiteral("count"), QStringLiteral("1")});
    parser.addOption({QStringLiteral("allow-parent-resize"),
                      QStringLiteral("Enable the modal's real-input parent resize button")});
    parser.addOption({QStringLiteral("maximized"),
                      QStringLiteral("Maximize the parent; minimum size is 800x600")});
    parser.process(app);
    Observation state;
    state.path = parser.isSet(QStringLiteral("state-file"))
        ? parser.value(QStringLiteral("state-file")) : qEnvironmentVariable("SWAG_STATE_FILE");
    state.alphaDialog = parser.isSet(QStringLiteral("alpha-dialog"));
    state.maximized = parser.isSet(QStringLiteral("maximized"));
    state.allowParentResize = parser.isSet(QStringLiteral("allow-parent-resize"));
    bool valid = false;
    state.dialogsPerPaint = parser.value(QStringLiteral("dialogs-per-paint")).toInt(&valid);
    if (!valid || state.dialogsPerPaint < 1 || state.dialogsPerPaint > 2)
        parser.showHelp(2);
    state.elapsed.start();
    Fixture fixture(state);
    if (state.maximized)
        fixture.showMaximized();
    else
        fixture.show();
    state.publish();
    return app.exec();
}
