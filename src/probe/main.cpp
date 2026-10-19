#include <QAbstractTableModel>
#include <QAction>
#include <QApplication>
#include <QClipboard>
#include <QComboBox>
#include <QDialog>
#include <QDialogButtonBox>
#include <QFont>
#include <QGuiApplication>
#include <QHBoxLayout>
#include <QHash>
#include <QHeaderView>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QKeyEvent>
#include <QKeySequence>
#include <QLabel>
#include <QLineEdit>
#include <QMainWindow>
#include <QMenu>
#include <QMenuBar>
#include <QPointer>
#include <QPushButton>
#include <QSaveFile>
#include <QScrollBar>
#include <QTableView>
#include <QTimer>
#include <QVBoxLayout>
#include <QWheelEvent>

#include <algorithm>

namespace {

constexpr int RowCount = 10000;
constexpr int ColumnCount = 20;

class FixtureModel final : public QAbstractTableModel {
    Q_OBJECT

public:
    explicit FixtureModel(QObject *parent = nullptr) : QAbstractTableModel(parent) {}

    int rowCount(const QModelIndex &parent = {}) const override {
        return parent.isValid() ? 0 : RowCount;
    }

    int columnCount(const QModelIndex &parent = {}) const override {
        return parent.isValid() ? 0 : ColumnCount;
    }

    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override {
        if (!index.isValid() || (role != Qt::DisplayRole && role != Qt::EditRole))
            return {};
        const auto key = index.row() * ColumnCount + index.column();
        const auto it = edits_.constFind(key);
        if (it != edits_.cend())
            return it.value();
        return QStringLiteral("R%1C%2").arg(index.row() + 1).arg(index.column() + 1);
    }

    QVariant headerData(int section, Qt::Orientation orientation,
                        int role = Qt::DisplayRole) const override {
        if (role != Qt::DisplayRole)
            return {};
        return orientation == Qt::Horizontal
                   ? QStringLiteral("Column %1").arg(section + 1)
                   : QString::number(section + 1);
    }

    Qt::ItemFlags flags(const QModelIndex &index) const override {
        return QAbstractTableModel::flags(index) | Qt::ItemIsEditable;
    }

    bool setData(const QModelIndex &index, const QVariant &value,
                 int role = Qt::EditRole) override {
        if (!index.isValid() || role != Qt::EditRole)
            return false;
        const auto text = value.toString();
        if (data(index, Qt::EditRole).toString() == text)
            return true;
        edits_.insert(index.row() * ColumnCount + index.column(), text);
        emit dataChanged(index, index, {Qt::DisplayRole, Qt::EditRole});
        emit cellEdited(index.row(), index.column());
        return true;
    }

signals:
    void cellEdited(int row, int column);

private:
    QHash<int, QString> edits_;
};

class GridView final : public QTableView {
    Q_OBJECT

public:
    using QTableView::QTableView;

    bool isEditing() const { return state() == QAbstractItemView::EditingState; }

signals:
    void clipboardAction(const QString &action);

protected:
    void keyPressEvent(QKeyEvent *event) override {
        if (event->matches(QKeySequence::Copy)) {
            copySelection();
            event->accept();
            return;
        }
        if (event->matches(QKeySequence::Paste)) {
            pasteSelection();
            event->accept();
            return;
        }
        QTableView::keyPressEvent(event);
    }

private:
    void copySelection() {
        const auto indexes = selectionModel()->selectedIndexes();
        if (indexes.isEmpty())
            return;
        int top = RowCount, left = ColumnCount, bottom = -1, right = -1;
        for (const auto &index : indexes) {
            top = std::min(top, index.row());
            left = std::min(left, index.column());
            bottom = std::max(bottom, index.row());
            right = std::max(right, index.column());
        }
        QStringList rows;
        for (int row = top; row <= bottom; ++row) {
            QStringList cells;
            for (int column = left; column <= right; ++column) {
                const auto index = model()->index(row, column);
                cells.append(selectionModel()->isSelected(index)
                                 ? model()->data(index, Qt::EditRole).toString()
                                 : QString());
            }
            rows.append(cells.join(QLatin1Char('\t')));
        }
        QApplication::clipboard()->setText(rows.join(QLatin1Char('\n')));
        emit clipboardAction(QStringLiteral("grid_copy"));
    }

    void pasteSelection() {
        auto start = currentIndex();
        if (!start.isValid())
            start = model()->index(0, 0);
        auto text = QApplication::clipboard()->text();
        text.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
        text.replace(QLatin1Char('\r'), QLatin1Char('\n'));
        auto rows = text.split(QLatin1Char('\n'), Qt::KeepEmptyParts);
        if (rows.size() > 1 && rows.last().isEmpty())
            rows.removeLast();
        for (int row = 0; row < rows.size() && start.row() + row < model()->rowCount(); ++row) {
            const auto cells = rows.at(row).split(QLatin1Char('\t'), Qt::KeepEmptyParts);
            for (int column = 0;
                 column < cells.size() && start.column() + column < model()->columnCount();
                 ++column) {
                model()->setData(model()->index(start.row() + row, start.column() + column),
                                 cells.at(column), Qt::EditRole);
            }
        }
        emit clipboardAction(QStringLiteral("grid_paste"));
    }
};

class ProbeWindow final : public QMainWindow {
    Q_OBJECT

public:
    ProbeWindow()
        : user_(qEnvironmentVariable("SWAG_USER", "probe")),
          generation_(qEnvironmentVariable("SWAG_GENERATION", "local")),
          stateFile_(qEnvironmentVariable("SWAG_STATE_FILE")) {
        setWindowTitle(QStringLiteral("SWAG rendering probe"));
        setObjectName(QStringLiteral("probe_window"));
        resize(1280, 800);

        auto *body = new QWidget(this);
        auto *layout = new QVBoxLayout(body);
        layout->setContentsMargins(12, 8, 12, 10);
        layout->setSpacing(8);

        auto *title = new QLabel(QStringLiteral("SWAG rendering probe · 10,000 rows × 20 columns"), body);
        auto titleFont = title->font();
        titleFont.setBold(true);
        title->setFont(titleFont);
        layout->addWidget(title);

        auto *controls = new QHBoxLayout;
        controls->setSpacing(8);
        controls->addWidget(new QLabel(QStringLiteral("Draft:"), body));
        textEdit_ = new QLineEdit(QStringLiteral("Draft: café 東京"), body);
        textEdit_->setObjectName(QStringLiteral("text_edit"));
        textEdit_->setMinimumWidth(280);
        controls->addWidget(textEdit_, 1);
        combo_ = new QComboBox(body);
        combo_->setObjectName(QStringLiteral("combo"));
        combo_->addItems({QStringLiteral("Alpha"), QStringLiteral("Beta"), QStringLiteral("Gamma – 東京")});
        combo_->setMinimumWidth(150);
        controls->addWidget(combo_);
        dialogButton_ = new QPushButton(QStringLiteral("Open dialog"), body);
        dialogButton_->setObjectName(QStringLiteral("dialog_button"));
        controls->addWidget(dialogButton_);
        copyButton_ = new QPushButton(QStringLiteral("Copy text"), body);
        copyButton_->setObjectName(QStringLiteral("copy_button"));
        controls->addWidget(copyButton_);
        pasteButton_ = new QPushButton(QStringLiteral("Paste text"), body);
        pasteButton_->setObjectName(QStringLiteral("paste_button"));
        controls->addWidget(pasteButton_);
        quitButton_ = new QPushButton(QStringLiteral("Quit"), body);
        quitButton_->setObjectName(QStringLiteral("quit_button"));
        controls->addWidget(quitButton_);
        layout->addLayout(controls);

        layout->addWidget(new QLabel(
            QStringLiteral("Grid: arrows / Page Down · F2 or double-click to edit · Enter commits · Esc cancels · Ctrl+C / Ctrl+V transfers TSV"), body));
        model_ = new FixtureModel(this);
        grid_ = new GridView(body);
        grid_->setObjectName(QStringLiteral("grid"));
        grid_->setModel(model_);
        grid_->setSelectionBehavior(QAbstractItemView::SelectItems);
        grid_->setSelectionMode(QAbstractItemView::ExtendedSelection);
        grid_->setEditTriggers(QAbstractItemView::DoubleClicked | QAbstractItemView::EditKeyPressed |
                               QAbstractItemView::AnyKeyPressed);
        grid_->setAlternatingRowColors(true);
        grid_->setWordWrap(false);
        grid_->verticalHeader()->setDefaultSectionSize(25);
        grid_->horizontalHeader()->setDefaultSectionSize(115);
        grid_->setVerticalScrollMode(QAbstractItemView::ScrollPerItem);
        grid_->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
        grid_->setCurrentIndex(model_->index(0, 0));
        layout->addWidget(grid_, 1);

        marker_ = new QLabel(body);
        marker_->setObjectName(QStringLiteral("state_marker"));
        marker_->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(marker_);
        setCentralWidget(body);

        menu_ = menuBar()->addMenu(QStringLiteral("Actions"));
        menu_->setObjectName(QStringLiteral("probe_menu"));
        menu_->addAction(QStringLiteral("Open modal dialog"), this, &ProbeWindow::openDialog);
        menu_->addAction(QStringLiteral("Copy draft to clipboard"), this, &ProbeWindow::copyText);
        menu_->addSeparator();
        menu_->addAction(QStringLiteral("Quit probe"), qApp, &QApplication::quit);
        connect(menu_, &QMenu::aboutToShow, this, [this] {
            menuOpen_ = true;
            record(QStringLiteral("menu_open"));
        });
        connect(menu_, &QMenu::aboutToHide, this, [this] {
            menuOpen_ = false;
            record(QStringLiteral("menu_close"));
        });
        connect(dialogButton_, &QPushButton::clicked, this, &ProbeWindow::openDialog);
        connect(copyButton_, &QPushButton::clicked, this, &ProbeWindow::copyText);
        connect(pasteButton_, &QPushButton::clicked, this, [this] {
            textEdit_->setText(QApplication::clipboard()->text());
            record(QStringLiteral("paste_text"));
        });
        connect(quitButton_, &QPushButton::clicked, qApp, &QApplication::quit);
        connect(textEdit_, &QLineEdit::textChanged, this, [this] { record(QStringLiteral("text_changed")); });
        connect(combo_, QOverload<int>::of(&QComboBox::currentIndexChanged), this,
                [this] { record(QStringLiteral("combo_changed")); });
        connect(model_, &FixtureModel::cellEdited, this, [this](int row, int column) {
            record(QStringLiteral("cell_edited:%1,%2").arg(row).arg(column));
        });
        connect(grid_, &GridView::clipboardAction, this, &ProbeWindow::record);
        connect(grid_->verticalScrollBar(), &QScrollBar::valueChanged, this,
                [this] { record(QStringLiteral("scroll_changed")); });
        connect(grid_->selectionModel(), &QItemSelectionModel::currentChanged, this,
                [this] { record(QStringLiteral("current_changed")); });
        connect(QApplication::clipboard(), &QClipboard::dataChanged, this,
                [this] { record(QStringLiteral("clipboard_changed")); });
        connect(qApp, &QCoreApplication::aboutToQuit, this, [this] {
            ready_ = false;
            record(QStringLiteral("quit"));
        });

        qApp->installEventFilter(this);
        auto *timer = new QTimer(this);
        timer->setInterval(200);
        connect(timer, &QTimer::timeout, this, &ProbeWindow::writeState);
        timer->start();
        record(QStringLiteral("initial"));
        QTimer::singleShot(0, this, [this] {
            ready_ = true;
            grid_->setFocus();
            record(QStringLiteral("ready"));
        });
    }

protected:
    bool eventFilter(QObject *watched, QEvent *event) override {
        if (event->type() == QEvent::KeyPress) {
            const auto *key = static_cast<QKeyEvent *>(event);
            ++keyEvents_;
            lastKey_ = QKeySequence(key->modifiers() | key->key()).toString(QKeySequence::PortableText);
            record(QStringLiteral("key:%1").arg(lastKey_));
        } else if (event->type() == QEvent::Wheel) {
            const auto *wheel = static_cast<QWheelEvent *>(event);
            ++wheelEvents_;
            record(QStringLiteral("wheel:%1,%2").arg(wheel->angleDelta().x()).arg(wheel->angleDelta().y()));
        }
        return QMainWindow::eventFilter(watched, event);
    }

private:
    void copyText() {
        QApplication::clipboard()->setText(textEdit_->text());
        record(QStringLiteral("copy_text"));
    }

    void openDialog() {
        if (dialog_)
            return;
        auto *dialog = new QDialog(this);
        dialog_ = dialog;
        dialog->setObjectName(QStringLiteral("probe_dialog"));
        dialog->setWindowTitle(QStringLiteral("Probe modal dialog"));
        dialog->setAttribute(Qt::WA_DeleteOnClose);
        dialog->setWindowModality(Qt::WindowModal);
        dialog->setMinimumWidth(430);
        auto *layout = new QVBoxLayout(dialog);
        layout->addWidget(new QLabel(QStringLiteral("Modal dialog — keyboard focus is inside this dialog."), dialog));
        auto *draft = new QLineEdit(QStringLiteral("Dialog draft"), dialog);
        draft->setObjectName(QStringLiteral("dialog_editor"));
        dialogEditor_ = draft;
        layout->addWidget(draft);
        auto *buttons = new QDialogButtonBox(dialog);
        auto *close = buttons->addButton(QStringLiteral("Close dialog"), QDialogButtonBox::AcceptRole);
        close->setObjectName(QStringLiteral("dialog_close_button"));
        close->setDefault(true);
        dialogClose_ = close;
        layout->addWidget(buttons);
        connect(buttons, &QDialogButtonBox::accepted, dialog, &QDialog::accept);
        connect(dialog, &QDialog::finished, this, [this] {
            dialogOpen_ = false;
            record(QStringLiteral("dialog_close"));
        });
        dialogOpen_ = true;
        dialog->open(); // Modal to this window, without a nested event loop.
        draft->setFocus();
        record(QStringLiteral("dialog_open"));
    }

    static QJsonArray bounds(const QWidget *widget) {
        const auto position = widget->mapToGlobal(QPoint(0, 0));
        return {position.x(), position.y(), widget->width(), widget->height()};
    }

    static QJsonArray localBounds(const QWidget *widget, const QRect &rect) {
        const auto position = widget->mapToGlobal(rect.topLeft());
        return {position.x(), position.y(), rect.width(), rect.height()};
    }

    void record(const QString &event) {
        ++sequence_;
        lastEvent_ = event;
        writeState();
    }

    void writeState() {
        const int scrollRow = std::max(0, grid_->rowAt(0));
        marker_->setText(QStringLiteral("User: %1 | Generation: %2 | Top row: %3 | Selected: R%4C%5")
                             .arg(user_, generation_).arg(scrollRow + 1)
                             .arg(grid_->currentIndex().row() + 1)
                             .arg(grid_->currentIndex().column() + 1));
        if (stateFile_.isEmpty())
            return;
        QJsonObject geometry{
            {QStringLiteral("grid_viewport"), bounds(grid_->viewport())},
            {QStringLiteral("text_edit"), bounds(textEdit_)},
            {QStringLiteral("combo"), bounds(combo_)},
            {QStringLiteral("dialog_button"), bounds(dialogButton_)},
            {QStringLiteral("copy_button"), bounds(copyButton_)},
            {QStringLiteral("paste_button"), bounds(pasteButton_)},
            {QStringLiteral("quit_button"), bounds(quitButton_)},
            {QStringLiteral("menu_bar"), bounds(menuBar())},
            {QStringLiteral("menu_action"), localBounds(menuBar(), menuBar()->actionGeometry(menu_->menuAction()))}
        };
        geometry.insert(QStringLiteral("window"), bounds(this));
        if (dialogOpen_ && dialogClose_) {
            geometry.insert(QStringLiteral("dialog_close_button"), bounds(dialogClose_));
            geometry.insert(QStringLiteral("dialog"), bounds(dialog_));
            geometry.insert(QStringLiteral("dialog_editor"), bounds(dialogEditor_));
        }
        if (menuOpen_)
            geometry.insert(QStringLiteral("menu_popup"), bounds(menu_));
        const bool comboPopupOpen = combo_->view()->isVisible();
        if (comboPopupOpen)
            geometry.insert(QStringLiteral("combo_popup"), bounds(combo_->view()));
        const auto cell = grid_->visualRect(model_->index(0, 1));
        if (cell.intersects(grid_->viewport()->rect()))
            geometry.insert(QStringLiteral("grid_cell_0_1"), localBounds(grid_->viewport(), cell));
        if (grid_->currentIndex().isValid())
            geometry.insert(QStringLiteral("grid_current_cell"),
                            localBounds(grid_->viewport(), grid_->visualRect(grid_->currentIndex())));
        QJsonObject state{
            {QStringLiteral("user"), user_},
            {QStringLiteral("generation"), generation_},
            {QStringLiteral("pid"), QCoreApplication::applicationPid()},
            {QStringLiteral("platform"), QGuiApplication::platformName()},
            {QStringLiteral("ready"), ready_},
            {QStringLiteral("event_sequence"), sequence_},
            {QStringLiteral("last_event"), lastEvent_},
            {QStringLiteral("scroll_row"), scrollRow},
            {QStringLiteral("editor_text"), textEdit_->text()},
            {QStringLiteral("cell_0_1"), model_->data(model_->index(0, 1), Qt::EditRole).toString()},
            {QStringLiteral("combo_value"), combo_->currentText()},
            {QStringLiteral("combo_popup_open"), comboPopupOpen},
            {QStringLiteral("dialog_open"), dialogOpen_},
            {QStringLiteral("dialog_text"), dialogEditor_ ? dialogEditor_->text() : QString()},
            {QStringLiteral("active_modal"), QApplication::activeModalWidget() != nullptr},
            {QStringLiteral("focus_widget"), QApplication::focusWidget()
                                                   ? QApplication::focusWidget()->objectName()
                                                   : QString()},
            {QStringLiteral("menu_open"), menuOpen_},
            {QStringLiteral("clipboard_text"), QApplication::clipboard()->text()},
            {QStringLiteral("geometry"), geometry},
            {QStringLiteral("current_row"), grid_->currentIndex().row()},
            {QStringLiteral("current_column"), grid_->currentIndex().column()},
            {QStringLiteral("current_value"), model_->data(grid_->currentIndex(), Qt::EditRole).toString()},
            {QStringLiteral("grid_editing"), grid_->isEditing()},
            {QStringLiteral("key_events"), keyEvents_},
            {QStringLiteral("wheel_events"), wheelEvents_},
            {QStringLiteral("last_key"), lastKey_}
        };
        QSaveFile file(stateFile_);
        if (!file.open(QIODevice::WriteOnly)) {
            if (!stateErrorReported_)
                qWarning("Cannot open probe state file: %s", qPrintable(file.errorString()));
            stateErrorReported_ = true;
            return;
        }
        file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        const auto bytes = QJsonDocument(state).toJson(QJsonDocument::Compact);
        if (file.write(bytes) != bytes.size() || !file.commit()) {
            if (!stateErrorReported_)
                qWarning("Cannot commit probe state file: %s", qPrintable(file.errorString()));
            stateErrorReported_ = true;
        }
    }

    QString user_;
    QString generation_;
    QString stateFile_;
    FixtureModel *model_{};
    GridView *grid_{};
    QLineEdit *textEdit_{};
    QComboBox *combo_{};
    QPushButton *dialogButton_{};
    QPushButton *copyButton_{};
    QPushButton *pasteButton_{};
    QPushButton *quitButton_{};
    QLabel *marker_{};
    QMenu *menu_{};
    QPointer<QDialog> dialog_;
    QPointer<QPushButton> dialogClose_;
    QPointer<QLineEdit> dialogEditor_;
    bool ready_{};
    bool dialogOpen_{};
    bool menuOpen_{};
    bool stateErrorReported_{};
    int sequence_{};
    int keyEvents_{};
    int wheelEvents_{};
    QString lastEvent_;
    QString lastKey_;
};

} // namespace

int main(int argc, char **argv) {
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("swag-probe"));
    app.setStyle(QStringLiteral("Fusion"));
    app.setFont(QFont(QStringLiteral("DejaVu Sans"), 10));
    app.setStyleSheet(QStringLiteral(
        "QWidget { color: #111111; background-color: #ffffff; }"
        "QLineEdit, QComboBox { min-height: 28px; padding: 2px 5px; border: 1px solid #666666; }"
        "QPushButton { min-height: 30px; padding: 2px 10px; border: 1px solid #666666; background: #eeeeee; }"
        "QPushButton:focus { border: 2px solid #145ac5; }"
        "QPushButton:pressed { background: #d0e0ff; }"
        "QTableView { alternate-background-color: #f1f4f8; gridline-color: #b7c0ca; selection-background-color: #195fc2; selection-color: #ffffff; }"
        "QHeaderView::section { background: #e1e7ef; padding: 4px; border: 1px solid #b7c0ca; }"
        "QMenu::item:selected, QMenuBar::item:selected { background: #195fc2; color: #ffffff; }"));
    ProbeWindow window;
    window.showFullScreen();
    return app.exec();
}

#include "main.moc"
