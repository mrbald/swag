#include <QAbstractTableModel>
#include <QAbstractButton>
#include <QApplication>
#include <QCheckBox>
#include <QCloseEvent>
#include <QComboBox>
#include <QDate>
#include <QFont>
#include <QFormLayout>
#include <QGroupBox>
#include <QHeaderView>
#include <QHBoxLayout>
#include <QHash>
#include <QJsonArray>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QLineEdit>
#include <QListWidget>
#include <QMainWindow>
#include <QMessageBox>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QRegularExpressionValidator>
#include <QSaveFile>
#include <QScrollArea>
#include <QSignalBlocker>
#include <QSplitter>
#include <QTableView>
#include <QTimer>
#include <QUrl>
#include <QUrlQuery>
#include <QVBoxLayout>

#include <cerrno>
#include <cstdio>
#include <cstring>
#include <functional>
#include <memory>
#include <utility>
#include <fcntl.h>
#include <sys/file.h>
#include <unistd.h>

namespace {

QString displayValue(const QJsonValue &value) {
    if (value.isNull() || value.isUndefined())
        return {};
    if (value.isBool())
        return value.toBool() ? QStringLiteral("Yes") : QStringLiteral("No");
    if (value.isDouble())
        return QString::number(value.toDouble(), 'g', 16);
    return value.toString();
}

struct ApiResult {
    int status{};
    QJsonObject body;
    QString error;
    bool uncertain{};
    bool ok() const { return status >= 200 && status < 300 && error.isEmpty(); }
};

class ApiClient final : public QObject {
public:
    explicit ApiClient(QObject *parent)
        : QObject(parent), network_(this),
          base_(qEnvironmentVariable("SWAG_BACKEND_URL", "http://127.0.0.1:8081")),
          token_(qgetenv("SWAG_SERVICE_TOKEN")), user_(qgetenv("SWAG_USER")) {
        while (base_.endsWith(QLatin1Char('/')))
            base_.chop(1);
    }

    void request(const QByteArray &method, const QString &path, const QUrlQuery &query,
                 const QJsonObject &body, std::function<void(ApiResult)> callback) {
        QUrl url(base_ + QStringLiteral("/api/") + path);
        url.setQuery(query);
        QNetworkRequest request(url);
        request.setRawHeader("X-Swag-Service-Token", token_);
        request.setRawHeader("X-Swag-User", user_);
        request.setHeader(QNetworkRequest::ContentTypeHeader, QStringLiteral("application/json"));
        request.setRawHeader("Accept", "application/json");
        const auto bytes = QJsonDocument(body).toJson(QJsonDocument::Compact);
        QNetworkReply *reply = method == "GET" ? network_.get(request)
            : network_.sendCustomRequest(request, method, bytes);
        auto *timer = new QTimer(reply);
        timer->setSingleShot(true);
        QObject::connect(timer, &QTimer::timeout, reply, [reply] {
            reply->setProperty("swag_timeout", true);
            reply->abort();
        });
        timer->start(15000);
        QObject::connect(reply, &QNetworkReply::finished, this,
                         [reply, timer, callback = std::move(callback)]() mutable {
            timer->stop();
            ApiResult result;
            result.status = reply->attribute(QNetworkRequest::HttpStatusCodeAttribute).toInt();
            const bool successStatus = result.status >= 200 && result.status < 300;
            result.uncertain = result.status == 0 || reply->property("swag_timeout").toBool()
                || (successStatus && reply->error() != QNetworkReply::NoError);
            const auto data = reply->readAll();
            QJsonParseError parseError;
            const auto document = QJsonDocument::fromJson(data, &parseError);
            if (document.isObject())
                result.body = document.object();
            if (result.status < 200 || result.status >= 300) {
                const auto error = result.body.value(QStringLiteral("error")).toObject();
                result.error = error.value(QStringLiteral("message")).toString();
                if (result.error.isEmpty())
                    result.error = reply->property("swag_timeout").toBool()
                        ? QStringLiteral("Request timed out. The result of a write may be unknown; reload before retrying.")
                        : reply->errorString();
                const auto fields = error.value(QStringLiteral("fields")).toObject();
                QStringList details;
                for (auto it = fields.begin(); it != fields.end(); ++it)
                    details << QStringLiteral("%1: %2").arg(it.key(), displayValue(it.value()));
                if (!details.isEmpty())
                    result.error += QStringLiteral(" · ") + details.join(QStringLiteral("; "));
            } else if (result.status != 204 && (!document.isObject() || parseError.error != QJsonParseError::NoError)) {
                result.error = QStringLiteral("The service returned invalid JSON.");
                result.uncertain = true;
            }
            if (result.uncertain && result.error.isEmpty())
                result.error = QStringLiteral("The response was interrupted before its result could be confirmed.");
            reply->deleteLater();
            callback(std::move(result));
        });
    }

private:
    QNetworkAccessManager network_;
    QString base_;
    const QByteArray token_;
    const QByteArray user_;
};

class EntityModel final : public QAbstractTableModel {
public:
    explicit EntityModel(QObject *parent) : QAbstractTableModel(parent) {}
    int rowCount(const QModelIndex &parent = {}) const override {
        return parent.isValid() ? 0 : rows_.size();
    }
    int columnCount(const QModelIndex &parent = {}) const override {
        return parent.isValid() ? 0 : fields_.size();
    }
    QVariant data(const QModelIndex &index, int role = Qt::DisplayRole) const override {
        if (!index.isValid() || index.row() >= rows_.size() || index.column() >= fields_.size())
            return {};
        const auto field = fields_.at(index.column()).toObject();
        const auto value = rows_.at(index.row()).toObject().value(field.value(QStringLiteral("name")).toString());
        if (role == Qt::DisplayRole || role == Qt::ToolTipRole)
            return displayValue(value);
        if (role == Qt::TextAlignmentRole) {
            const auto type = field.value(QStringLiteral("type")).toString();
            return int((type == QStringLiteral("decimal") || type == QStringLiteral("integer"))
                           ? Qt::AlignRight | Qt::AlignVCenter : Qt::AlignLeft | Qt::AlignVCenter);
        }
        return {};
    }
    QVariant headerData(int section, Qt::Orientation orientation, int role) const override {
        if (role != Qt::DisplayRole)
            return {};
        if (orientation == Qt::Vertical)
            return section + 1;
        const auto field = fields_.at(section).toObject();
        return field.value(QStringLiteral("label")).toString(field.value(QStringLiteral("name")).toString());
    }
    void setEntity(const QJsonArray &fields) {
        beginResetModel();
        fields_ = fields;
        rows_ = {};
        endResetModel();
    }
    void setRows(const QJsonArray &rows) {
        beginResetModel();
        rows_ = rows;
        endResetModel();
    }
    QJsonObject item(int row) const {
        return row >= 0 && row < rows_.size() ? rows_.at(row).toObject() : QJsonObject{};
    }
    const QJsonArray &rows() const { return rows_; }
    const QJsonArray &fields() const { return fields_; }

private:
    QJsonArray fields_;
    QJsonArray rows_;
};

class DesktopWindow final : public QMainWindow {
public:
    DesktopWindow()
        : api_(this), user_(qEnvironmentVariable("SWAG_USER", "local diagnostic")),
          generation_(qEnvironmentVariable("SWAG_GENERATION", "local")),
          stateFile_(qEnvironmentVariable("SWAG_STATE_FILE")) {
        setWindowTitle(QStringLiteral("SWAG · Banking workspace"));
        setObjectName(QStringLiteral("desktop_window"));
        resize(1280, 800);
        auto *body = new QWidget(this);
        auto *layout = new QVBoxLayout(body);
        layout->setContentsMargins(12, 10, 12, 10);
        layout->setSpacing(9);
        auto *header = new QHBoxLayout;
        auto *title = new QLabel(QStringLiteral("SWAG  /  Banking workspace"), body);
        auto font = title->font();
        font.setBold(true);
        font.setPointSize(14);
        title->setFont(font);
        header->addWidget(title);
        header->addStretch();
        header->addWidget(new QLabel(QStringLiteral("Signed in: %1").arg(user_), body));
        quit_ = button(QStringLiteral("Quit"), QStringLiteral("quit_button"), body);
        header->addWidget(quit_);
        layout->addLayout(header);
        auto *draftRow = new QHBoxLayout;
        draftRow->addWidget(new QLabel(QStringLiteral("Unsaved workspace note"), body));
        draft_ = new QLineEdit(QStringLiteral("Draft: café 東京"), body);
        draft_->setObjectName(QStringLiteral("draft"));
        draft_->setPlaceholderText(QStringLiteral("This note remains in this running workspace when you reconnect"));
        draftRow->addWidget(draft_, 1);
        layout->addLayout(draftRow);

        auto *splitter = new QSplitter(Qt::Horizontal, body);
        entities_ = new QListWidget(splitter);
        entities_->setObjectName(QStringLiteral("entity_list"));
        entities_->setMinimumWidth(160);
        entities_->setMaximumWidth(205);
        auto *workspace = new QWidget(splitter);
        auto *center = new QVBoxLayout(workspace);
        center->setContentsMargins(3, 0, 3, 0);
        entityTitle_ = new QLabel(QStringLiteral("Connecting to bank service…"), workspace);
        font = entityTitle_->font();
        font.setBold(true);
        entityTitle_->setFont(font);
        center->addWidget(entityTitle_);
        auto *toolbar = new QHBoxLayout;
        filter_ = new QLineEdit(workspace);
        filter_->setObjectName(QStringLiteral("filter"));
        filter_->setPlaceholderText(QStringLiteral("Filter rows, then press Enter"));
        toolbar->addWidget(filter_, 1);
        reload_ = button(QStringLiteral("Reload"), QStringLiteral("reload_button"), workspace);
        new_ = button(QStringLiteral("New"), QStringLiteral("new_button"), workspace);
        save_ = button(QStringLiteral("Save"), QStringLiteral("save_button"), workspace);
        delete_ = button(QStringLiteral("Delete"), QStringLiteral("delete_button"), workspace);
        toolbar->addWidget(reload_);
        toolbar->addWidget(new_);
        toolbar->addWidget(save_);
        toolbar->addWidget(delete_);
        center->addLayout(toolbar);
        model_ = new EntityModel(this);
        grid_ = new QTableView(workspace);
        grid_->setObjectName(QStringLiteral("grid"));
        grid_->setModel(model_);
        grid_->setSelectionBehavior(QAbstractItemView::SelectRows);
        grid_->setSelectionMode(QAbstractItemView::SingleSelection);
        grid_->setEditTriggers(QAbstractItemView::NoEditTriggers);
        grid_->setAlternatingRowColors(true);
        grid_->setWordWrap(false);
        grid_->setSortingEnabled(false);
        grid_->verticalHeader()->setDefaultSectionSize(30);
        grid_->horizontalHeader()->setDefaultSectionSize(135);
        grid_->horizontalHeader()->setSectionsClickable(true);
        grid_->horizontalHeader()->setSortIndicatorShown(true);
        grid_->horizontalHeader()->setSortIndicator(0, Qt::AscendingOrder);
        grid_->horizontalHeader()->setStretchLastSection(false);
        grid_->setHorizontalScrollMode(QAbstractItemView::ScrollPerPixel);
        center->addWidget(grid_, 1);
        auto *paging = new QHBoxLayout;
        previous_ = button(QStringLiteral("Previous"), QStringLiteral("previous_button"), workspace);
        next_ = button(QStringLiteral("Next"), QStringLiteral("next_button"), workspace);
        page_ = new QLabel(QStringLiteral("No rows"), workspace);
        page_->setObjectName(QStringLiteral("paging"));
        paging->addWidget(previous_);
        paging->addWidget(page_, 1);
        paging->addWidget(next_);
        center->addLayout(paging);

        details_ = new QGroupBox(QStringLiteral("Row details"), splitter);
        details_->setMinimumWidth(325);
        details_->setMaximumWidth(410);
        auto *detailLayout = new QVBoxLayout(details_);
        formTitle_ = new QLabel(QStringLiteral("Select a row or create a new one."), details_);
        formTitle_->setWordWrap(true);
        detailLayout->addWidget(formTitle_);
        auto *scroll = new QScrollArea(details_);
        scroll->setWidgetResizable(true);
        scroll->setFrameShape(QFrame::NoFrame);
        formBody_ = new QWidget(scroll);
        formLayout_ = new QFormLayout(formBody_);
        formLayout_->setContentsMargins(2, 2, 8, 2);
        formLayout_->setLabelAlignment(Qt::AlignLeft | Qt::AlignVCenter);
        formLayout_->setRowWrapPolicy(QFormLayout::WrapLongRows);
        formLayout_->setFieldGrowthPolicy(QFormLayout::AllNonFixedFieldsGrow);
        formLayout_->setVerticalSpacing(8);
        scroll->setWidget(formBody_);
        detailLayout->addWidget(scroll, 1);
        auto *formButtons = new QHBoxLayout;
        formSave_ = button(QStringLiteral("Save changes"), QStringLiteral("form_save_button"), details_);
        formCancel_ = button(QStringLiteral("Cancel"), QStringLiteral("form_cancel_button"), details_);
        formButtons->addWidget(formSave_, 1);
        formButtons->addWidget(formCancel_);
        detailLayout->addLayout(formButtons);
        splitter->setSizes({170, 730, 350});
        layout->addWidget(splitter, 1);
        status_ = new QLabel(QStringLiteral("Loading schema…"), body);
        status_->setObjectName(QStringLiteral("status"));
        status_->setMinimumHeight(40);
        status_->setWordWrap(true);
        status_->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(status_);
        setCentralWidget(body);

        connect(quit_, &QPushButton::clicked, this, &QWidget::close);
        connect(draft_, &QLineEdit::textChanged, this, [this] { record(QStringLiteral("draft_changed")); });
        connect(entities_, &QListWidget::currentRowChanged, this, [this](int row) { selectEntity(row); });
        connect(grid_->selectionModel(), &QItemSelectionModel::currentRowChanged, this,
                [this](const QModelIndex &current, const QModelIndex &) {
            if (changingSelection_ || loading_ || !current.isValid())
                return;
            if (dirty_ || writing_) {
                restoreGridSelection();
                setError(QStringLiteral("Save or Cancel the current form before selecting another row."));
                return;
            }
            openForm(model_->item(current.row()), false);
        });
        connect(grid_, &QTableView::clicked, this, [this](const QModelIndex &index) {
            if (index.isValid() && !formOpen_ && !loading_ && !writing_)
                openForm(model_->item(index.row()), false);
        });
        connect(grid_->horizontalHeader(), &QHeaderView::sectionClicked, this, [this](int section) {
            if (!canNavigate())
                return;
            const auto name = model_->fields().at(section).toObject().value(QStringLiteral("name")).toString();
            descending_ = sort_ == name && !descending_;
            sort_ = name;
            grid_->horizontalHeader()->setSortIndicator(section, descending_ ? Qt::DescendingOrder : Qt::AscendingOrder);
            offset_ = 0;
            loadRows();
        });
        connect(reload_, &QPushButton::clicked, this, [this] {
            if (uncertainWrite_) { reconcileWrite(); return; }
            if (schema_.isEmpty()) { loadSchema(); return; }
            if (canNavigate()) loadRows();
        });
        connect(filter_, &QLineEdit::returnPressed, this, [this] {
            if (canNavigate()) { appliedFilter_ = filter_->text(); offset_ = 0; loadRows(); }
        });
        connect(new_, &QPushButton::clicked, this, [this] {
            if (canNavigate()) {
                grid_->clearSelection();
                openForm({}, true);
            }
        });
        connect(save_, &QPushButton::clicked, this, [this] { saveForm(); });
        connect(formSave_, &QPushButton::clicked, this, [this] { saveForm(); });
        connect(formCancel_, &QPushButton::clicked, this, [this] { cancelForm(); });
        connect(delete_, &QPushButton::clicked, this, [this] { confirmDelete(); });
        connect(previous_, &QPushButton::clicked, this, [this] {
            if (canNavigate()) { offset_ = qMax(0, offset_ - pageSize_); loadRows(); }
        });
        connect(next_, &QPushButton::clicked, this, [this] {
            if (canNavigate()) { offset_ += pageSize_; loadRows(); }
        });
        connect(qApp, &QCoreApplication::aboutToQuit, this, [this] {
            ready_ = false;
            record(QStringLiteral("quit"));
        });
        auto *timer = new QTimer(this);
        timer->setInterval(200);
        connect(timer, &QTimer::timeout, this, [this] { writeState(); });
        timer->start();
        updateControls();
        QTimer::singleShot(0, this, [this] {
            ready_ = true;
            record(QStringLiteral("ready"));
            loadSchema();
        });
    }

protected:
    void closeEvent(QCloseEvent *event) override {
        if (allowClose_ || (!dirty_ && !writing_ && !uncertainWrite_)) {
            event->accept();
            return;
        }
        event->ignore();
        if (closePrompt_)
            return;
        auto *box = new QMessageBox(QMessageBox::Warning, QStringLiteral("Quit workspace"),
            writing_ || uncertainWrite_
                ? QStringLiteral("A write has an unconfirmed result. Quit and discard the retained form?")
                : QStringLiteral("This form has unsaved changes. Quit and discard them?"),
            QMessageBox::Discard | QMessageBox::Cancel, this);
        closePrompt_ = box;
        box->setDefaultButton(QMessageBox::Cancel);
        box->setAttribute(Qt::WA_DeleteOnClose);
        connect(box, &QMessageBox::finished, this, [this](int result) {
            if (result == QMessageBox::Discard) { allowClose_ = true; close(); }
        });
        box->open();
    }

private:
    static QPushButton *button(const QString &label, const QString &name, QWidget *parent) {
        auto *result = new QPushButton(label, parent);
        result->setObjectName(name);
        return result;
    }
    QString entityName() const { return entity_.value(QStringLiteral("name")).toString(); }
    QJsonArray fields() const { return entity_.value(QStringLiteral("fields")).toArray(); }
    bool canNavigate() {
        if (uncertainWrite_) {
            setError(uncertainGuidance());
            return false;
        }
        if (dirty_ || writing_) {
            setError(QStringLiteral("Save or Cancel the current form before changing the view."));
            return false;
        }
        return !loading_;
    }
    void setError(const QString &message) {
        error_ = message;
        status_->setStyleSheet(QStringLiteral("color:#a51c20; font-weight:bold;"));
        status_->setText(message);
        record(QStringLiteral("error"));
    }
    void setStatus(const QString &message) {
        error_.clear();
        status_->setStyleSheet(QStringLiteral("color:#31465e;"));
        status_->setText(message);
        record(QStringLiteral("status"));
    }
    void apiError(const ApiResult &result) {
        if (uncertainWrite_) {
            setError(result.error + QStringLiteral(" ") + uncertainGuidance());
            return;
        }
        setError(result.status == 409
            ? QStringLiteral("Conflict: %1 Your form is retained. Cancel, then Reload to get the current row before editing again.").arg(result.error)
            : QStringLiteral("%1%2").arg(result.status ? QStringLiteral("HTTP %1 · ").arg(result.status) : QString(), result.error));
    }
    static QString uncertainGuidance() {
        return QStringLiteral("Write outcome unknown. Form values are retained; Save and Delete are blocked. "
                              "Use Reload or Cancel to explicitly discard the retained form and reconcile with the service before retrying.");
    }
    void latchUncertainWrite(const QString &reason) {
        uncertainWrite_ = true;
        updateControls();
        setError(reason + QStringLiteral(" ") + uncertainGuidance());
    }
    void reconcileWrite() {
        if (!uncertainWrite_ || writing_ || loading_ || reconcilePrompt_) return;
        auto *box = new QMessageBox(QMessageBox::Warning, QStringLiteral("Reconcile unknown write"),
            QStringLiteral("The write may already have succeeded. Discard these retained form values and reload the current service rows? "
                           "Check the refreshed rows before creating or saving again. Your workspace note is kept."),
            QMessageBox::Discard | QMessageBox::Cancel, this);
        reconcilePrompt_ = box;
        box->setDefaultButton(QMessageBox::Cancel);
        box->setAttribute(Qt::WA_DeleteOnClose);
        const auto id = selectedId_;
        connect(box, &QMessageBox::finished, this, [this, id](int result) {
            if (result != QMessageBox::Discard) return;
            // Discard is explicit; a failed read still leaves the mutation latch set.
            dirty_ = false;
            loadRows(id);
        });
        box->open();
        record(QStringLiteral("reconcile_confirmation"));
    }
    void loadSchema() {
        if (loading_ || writing_)
            return;
        loading_ = true;
        updateControls();
        api_.request("GET", QStringLiteral("schema"), {}, {}, [this](ApiResult result) {
            loading_ = false;
            if (!result.ok()) { apiError(result); updateControls(); return; }
            schema_ = result.body.value(QStringLiteral("entities")).toArray();
            if (schema_.isEmpty()) { setError(QStringLiteral("The service returned an empty schema.")); updateControls(); return; }
            const QSignalBlocker blocker(entities_);
            entities_->clear();
            for (const auto &value : schema_) {
                const auto entity = value.toObject();
                auto *item = new QListWidgetItem(entity.value(QStringLiteral("label")).toString(entity.value(QStringLiteral("name")).toString()), entities_);
                item->setData(Qt::UserRole, entity.value(QStringLiteral("name")).toString());
                item->setSizeHint(QSize(150, 36));
            }
            entities_->setCurrentRow(0);
            selectEntity(0);
        });
    }
    void selectEntity(int row) {
        if (row < 0 || row >= schema_.size())
            return;
        if (!canNavigate()) {
            const QSignalBlocker blocker(entities_);
            entities_->setCurrentRow(entityIndex_);
            return;
        }
        entityIndex_ = row;
        entity_ = schema_.at(row).toObject();
        entityTitle_->setText(entity_.value(QStringLiteral("label")).toString(entityName()));
        offset_ = 0;
        sort_ = QStringLiteral("id");
        descending_ = false;
        appliedFilter_.clear();
        filter_->clear();
        clearForm();
        model_->setEntity(fields());
        grid_->horizontalHeader()->setSortIndicator(0, Qt::AscendingOrder);
        loadRows();
    }
    void loadRows(const QJsonValue &preferId = {}) {
        if (entityName().isEmpty() || loading_ || writing_)
            return;
        loading_ = true;
        clearForm();
        updateControls();
        setStatus(QStringLiteral("Loading %1…").arg(entityTitle_->text()));
        QUrlQuery query;
        query.addQueryItem(QStringLiteral("offset"), QString::number(offset_));
        query.addQueryItem(QStringLiteral("limit"), QString::number(pageSize_));
        query.addQueryItem(QStringLiteral("sort"), sort_);
        query.addQueryItem(QStringLiteral("direction"), descending_ ? QStringLiteral("desc") : QStringLiteral("asc"));
        query.addQueryItem(QStringLiteral("filter"), appliedFilter_);
        const auto requestedEntity = entityName();
        api_.request("GET", requestedEntity, query, {}, [this, requestedEntity, preferId](ApiResult result) {
            if (entityName() != requestedEntity)
                return;
            loading_ = false;
            if (!result.ok()) { apiError(result); updateControls(); return; }
            if (!result.body.value(QStringLiteral("items")).isArray()
                    || !result.body.value(QStringLiteral("total")).isDouble()) {
                setError(QStringLiteral("The service returned an invalid row list.")
                         + (uncertainWrite_ ? QStringLiteral(" ") + uncertainGuidance() : QString()));
                updateControls();
                return;
            }
            uncertainWrite_ = false;
            const auto rows = result.body.value(QStringLiteral("items")).toArray();
            total_ = result.body.value(QStringLiteral("total")).toInt();
            changingSelection_ = true;
            model_->setRows(rows);
            int selected = -1;
            for (int i = 0; i < rows.size(); ++i)
                if (rows.at(i).toObject().value(QStringLiteral("id")) == preferId)
                    selected = i;
            if (selected < 0 && !rows.isEmpty())
                selected = 0;
            if (selected >= 0)
                grid_->setCurrentIndex(model_->index(selected, 0));
            changingSelection_ = false;
            if (selected >= 0)
                openForm(model_->item(selected), false);
            page_->setText(total_ == 0 ? QStringLiteral("0 rows")
                : QStringLiteral("%1–%2 of %3").arg(offset_ + 1).arg(offset_ + rows.size()).arg(total_));
            setStatus(QStringLiteral("%1 loaded. Select a row to edit; Save writes to the shared bank service.").arg(entityTitle_->text()));
            updateControls();
            record(QStringLiteral("rows_loaded"));
        });
    }
    void clearForm() {
        ++formGeneration_;
        buildingForm_ = true;
        while (formLayout_->rowCount() > 0)
            formLayout_->removeRow(0);
        editors_.clear();
        original_ = {};
        creating_ = false;
        formOpen_ = false;
        dirty_ = false;
        selectedId_ = QJsonValue();
        formTitle_->setText(QStringLiteral("Select a row or create a new one."));
        buildingForm_ = false;
    }
    void openForm(const QJsonObject &item, bool creating) {
        clearForm();
        original_ = item;
        creating_ = creating;
        formOpen_ = true;
        buildingForm_ = true;
        selectedId_ = creating ? QJsonValue() : item.value(QStringLiteral("id"));
        formTitle_->setText(creating ? QStringLiteral("New %1 · * required").arg(entityTitle_->text())
            : QStringLiteral("Row %1 · version %2 · * required").arg(displayValue(selectedId_), displayValue(item.value(QStringLiteral("version")))));
        QStringList references;
        for (const auto &value : fields()) {
            const auto field = value.toObject();
            const auto name = field.value(QStringLiteral("name")).toString();
            const auto type = field.value(QStringLiteral("type")).toString();
            const bool editable = field.value(QStringLiteral("editable")).toBool();
            const bool nullable = field.value(QStringLiteral("nullable")).toBool();
            QJsonValue initial = item.value(name);
            if (creating && field.contains(QStringLiteral("default")))
                initial = field.value(QStringLiteral("default"));
            QWidget *editor;
            if (!editable) {
                auto *line = new QLineEdit(displayValue(initial), formBody_);
                line->setReadOnly(true);
                line->setFocusPolicy(Qt::ClickFocus);
                editor = line;
                if (creating) line->setPlaceholderText(QStringLiteral("Assigned by service"));
            } else if (type == QStringLiteral("enum") || type == QStringLiteral("reference")) {
                auto *combo = new QComboBox(formBody_);
                combo->addItem(nullable ? QStringLiteral("(none)") : QStringLiteral("Choose…"), QVariant());
                if (type == QStringLiteral("enum")) {
                    for (const auto &choice : field.value(QStringLiteral("choices")).toArray())
                        combo->addItem(choice.toString(), choice.toString());
                } else if (!initial.isNull() && !initial.isUndefined()) {
                    combo->addItem(QStringLiteral("ID %1 · loading…").arg(displayValue(initial)), initial.toVariant());
                }
                const int index = combo->findData(initial.toVariant());
                if (index >= 0) combo->setCurrentIndex(index);
                if (type == QStringLiteral("reference")) references << name;
                connect(combo, QOverload<int>::of(&QComboBox::currentIndexChanged), this, [this] { formChanged(); });
                editor = combo;
            } else if (type == QStringLiteral("boolean")) {
                auto *check = new QCheckBox(QStringLiteral("Enabled"), formBody_);
                check->setChecked(initial.toBool());
                connect(check, &QCheckBox::toggled, this, [this] { formChanged(); });
                editor = check;
            } else {
                auto *line = new QLineEdit(displayValue(initial), formBody_);
                if (type == QStringLiteral("decimal")) {
                    const int scale = field.value(QStringLiteral("scale")).toInt(2);
                    line->setPlaceholderText(QStringLiteral("Exact decimal · %1 places").arg(scale));
                    line->setValidator(new QRegularExpressionValidator(QRegularExpression(
                        QStringLiteral("[+-]?[0-9]*(?:\\.[0-9]{0,%1})?").arg(scale)), line));
                } else if (type == QStringLiteral("integer")) {
                    line->setPlaceholderText(QStringLiteral("Whole number"));
                    line->setValidator(new QRegularExpressionValidator(QRegularExpression(QStringLiteral("[+-]?[0-9]*")), line));
                } else if (type == QStringLiteral("date")) {
                    line->setPlaceholderText(QStringLiteral("YYYY-MM-DD"));
                }
                connect(line, &QLineEdit::textChanged, this, [this] { formChanged(); });
                editor = line;
            }
            editor->setObjectName(QStringLiteral("field_") + name);
            editors_.insert(name, editor);
            QString label = field.value(QStringLiteral("label")).toString(name);
            if (editable && !nullable) label += QStringLiteral(" *");
            formLayout_->addRow(label, editor);
        }
        buildingForm_ = false;
        dirty_ = creating;
        baseline_ = formValues();
        updateControls();
        record(QStringLiteral("form_opened"));
        for (const auto &name : references)
            loadReference(name);
    }
    QJsonObject formValues() const {
        QJsonObject values;
        for (const auto &value : fields()) {
            const auto field = value.toObject();
            const auto name = field.value(QStringLiteral("name")).toString();
            auto *editor = editors_.value(name);
            if (!editor) continue;
            const auto type = field.value(QStringLiteral("type")).toString();
            if (!field.value(QStringLiteral("editable")).toBool()) {
                values.insert(name, original_.value(name));
            } else if (auto *combo = qobject_cast<QComboBox *>(editor)) {
                values.insert(name, combo->currentData().isValid() ? QJsonValue::fromVariant(combo->currentData()) : QJsonValue(QJsonValue::Null));
            } else if (auto *check = qobject_cast<QCheckBox *>(editor)) {
                values.insert(name, check->isChecked());
            } else if (auto *line = qobject_cast<QLineEdit *>(editor)) {
                const auto text = line->text();
                if (text.isEmpty() && field.value(QStringLiteral("nullable")).toBool()) {
                    values.insert(name, QJsonValue(QJsonValue::Null));
                } else if (type == QStringLiteral("integer")) {
                    bool ok;
                    const qlonglong integer = text.toLongLong(&ok);
                    // JSON integers remain exact only inside its safe integer range.
                    ok = ok && integer >= -9007199254740991LL && integer <= 9007199254740991LL;
                    values.insert(name, ok ? QJsonValue(static_cast<double>(integer)) : QJsonValue(text));
                } else {
                    // Decimal values deliberately remain JSON strings.
                    values.insert(name, text);
                }
            }
        }
        return values;
    }
    void formChanged() {
        if (buildingForm_) return;
        dirty_ = creating_ || formValues() != baseline_;
        updateControls();
        record(QStringLiteral("form_changed"));
    }
    void loadReference(const QString &fieldName) {
        QJsonObject metadata;
        for (const auto &value : fields())
            if (value.toObject().value(QStringLiteral("name")).toString() == fieldName)
                metadata = value.toObject();
        const auto target = metadata.value(QStringLiteral("reference")).toString();
        if (target.isEmpty()) return;
        auto *combo = qobject_cast<QComboBox *>(editors_.value(fieldName));
        if (!combo) return;
        combo->setEnabled(false);
        const int generation = formGeneration_;
        auto allRows = std::make_shared<QJsonArray>();
        fetchReferencePage(target, fieldName, generation, 0, allRows);
    }
    void fetchReferencePage(const QString &target, const QString &fieldName, int generation,
                            int offset, const std::shared_ptr<QJsonArray> &allRows) {
        QUrlQuery query;
        query.addQueryItem(QStringLiteral("offset"), QString::number(offset));
        query.addQueryItem(QStringLiteral("limit"), QString::number(100));
        query.addQueryItem(QStringLiteral("sort"), QStringLiteral("id"));
        query.addQueryItem(QStringLiteral("direction"), QStringLiteral("asc"));
        api_.request("GET", target, query, {}, [this, target, fieldName, generation, offset, allRows](ApiResult result) {
            if (generation != formGeneration_) return;
            auto *combo = qobject_cast<QComboBox *>(editors_.value(fieldName));
            if (!combo) return;
            if (!result.ok()) {
                combo->setEnabled(!writing_);
                apiError(result);
                return;
            }
            const auto page = result.body.value(QStringLiteral("items")).toArray();
            for (const auto &value : page) allRows->append(value);
            if (!page.isEmpty() && offset + page.size() < result.body.value(QStringLiteral("total")).toInt()) {
                fetchReferencePage(target, fieldName, generation, offset + page.size(), allRows);
                return;
            }
            QString titleField = QStringLiteral("id");
            for (const auto &value : schema_)
                if (value.toObject().value(QStringLiteral("name")).toString() == target)
                    titleField = value.toObject().value(QStringLiteral("title_field")).toString(QStringLiteral("id"));
            const auto selected = combo->currentData();
            const QSignalBlocker blocker(combo);
            const auto empty = combo->itemText(0);
            combo->clear();
            combo->addItem(empty, QVariant());
            for (const auto &value : *allRows) {
                const auto row = value.toObject();
                const auto id = row.value(QStringLiteral("id"));
                const auto label = displayValue(row.value(titleField));
                combo->addItem(titleField == QStringLiteral("id") ? QStringLiteral("ID %1").arg(displayValue(id))
                    : QStringLiteral("%1 · ID %2").arg(label, displayValue(id)), id.toVariant());
            }
            int index = selected.isValid() ? combo->findData(selected) : 0;
            if (index < 0 && selected.isValid()) {
                combo->addItem(QStringLiteral("ID %1 · unavailable").arg(selected.toString()), selected);
                index = combo->count() - 1;
            }
            combo->setCurrentIndex(qMax(0, index));
            combo->setEnabled(!writing_);
            record(QStringLiteral("reference_loaded"));
        });
    }
    bool validateForm(const QJsonObject &values) {
        for (const auto &value : fields()) {
            const auto field = value.toObject();
            if (!field.value(QStringLiteral("editable")).toBool()) continue;
            const auto name = field.value(QStringLiteral("name")).toString();
            const auto label = field.value(QStringLiteral("label")).toString(name);
            const auto current = values.value(name);
            const auto type = field.value(QStringLiteral("type")).toString();
            QString error;
            if (!field.value(QStringLiteral("nullable")).toBool() && (current.isNull() || current.isUndefined()
                    || (current.isString() && current.toString().trimmed().isEmpty())))
                error = QStringLiteral("%1 is required.").arg(label);
            if (error.isEmpty() && !current.isNull() && type == QStringLiteral("date")) {
                const auto text = current.toString();
                const auto date = QDate::fromString(text, Qt::ISODate);
                if (!date.isValid() || date.toString(Qt::ISODate) != text)
                    error = QStringLiteral("%1 must be a valid date in YYYY-MM-DD format.").arg(label);
            }
            if (error.isEmpty() && !current.isNull() && type == QStringLiteral("integer") && !current.isDouble())
                error = QStringLiteral("%1 must be a whole number.").arg(label);
            if (error.isEmpty() && !current.isNull() && type == QStringLiteral("decimal")) {
                const int scale = field.value(QStringLiteral("scale")).toInt(2);
                const QRegularExpression decimal(scale == 0 ? QStringLiteral("^[+-]?[0-9]+$")
                    : QStringLiteral("^[+-]?[0-9]+(?:\\.[0-9]{1,%1})?$").arg(scale));
                if (!decimal.match(current.toString()).hasMatch())
                    error = QStringLiteral("%1 must be an exact decimal with at most %2 places.").arg(label).arg(scale);
            }
            if (!error.isEmpty()) {
                setError(error);
                editors_.value(name)->setFocus();
                return false;
            }
        }
        return true;
    }
    void saveForm() {
        if (!formOpen_ || !dirty_ || writing_ || loading_ || uncertainWrite_) return;
        const auto values = formValues();
        if (!validateForm(values)) return;
        QJsonObject payload;
        for (const auto &value : fields()) {
            const auto field = value.toObject();
            const auto name = field.value(QStringLiteral("name")).toString();
            if (field.value(QStringLiteral("editable")).toBool() && (creating_ || values.value(name) != baseline_.value(name)))
                payload.insert(name, values.value(name));
        }
        if (!creating_) payload.insert(QStringLiteral("version"), original_.value(QStringLiteral("version")));
        const auto path = entityName() + (creating_ ? QString() : QLatin1Char('/') + displayValue(selectedId_));
        const auto method = creating_ ? QByteArray("POST") : QByteArray("PATCH");
        writing_ = true;
        updateControls();
        setStatus(QStringLiteral("Saving…"));
        api_.request(method, path, {}, payload, [this](ApiResult result) {
            writing_ = false;
            if (!result.ok()) {
                if (result.uncertain) latchUncertainWrite(result.error);
                else { apiError(result); updateControls(); }
                return;
            }
            const auto item = result.body.value(QStringLiteral("item")).toObject();
            if (!item.value(QStringLiteral("id")).isDouble() || !item.value(QStringLiteral("version")).isDouble()) {
                latchUncertainWrite(QStringLiteral("The service did not return a valid saved row."));
                return;
            }
            dirty_ = false;
            loadRows(item.value(QStringLiteral("id")));
        });
    }
    void cancelForm() {
        if (uncertainWrite_) { reconcileWrite(); return; }
        if (!formOpen_ || writing_) return;
        if (creating_) {
            clearForm();
            const auto current = grid_->currentIndex();
            if (current.isValid()) {
                openForm(model_->item(current.row()), false);
                restoreGridSelection();
            }
        } else {
            const auto original = original_;
            openForm(original, false);
        }
        setStatus(QStringLiteral("Changes cancelled. Reload to fetch the latest service values."));
        updateControls();
    }
    void restoreGridSelection() {
        const QSignalBlocker blocker(grid_->selectionModel());
        if (selectedId_.isUndefined() || selectedId_.isNull()) {
            grid_->clearSelection();
            return;
        }
        for (int row = 0; row < model_->rowCount(); ++row)
            if (model_->item(row).value(QStringLiteral("id")) == selectedId_) {
                grid_->setCurrentIndex(model_->index(row, 0));
                grid_->selectRow(row);
                return;
            }
    }
    void confirmDelete() {
        if (!formOpen_ || creating_ || dirty_ || writing_ || loading_ || uncertainWrite_ || deletePrompt_) return;
        auto *box = new QMessageBox(QMessageBox::Question, QStringLiteral("Delete row"),
            QStringLiteral("Delete %1 row %2 from the shared bank service?").arg(entityTitle_->text(), displayValue(selectedId_)),
            QMessageBox::Yes | QMessageBox::Cancel, this);
        deletePrompt_ = box;
        box->setDefaultButton(QMessageBox::Cancel);
        box->setAttribute(Qt::WA_DeleteOnClose);
        const auto id = selectedId_;
        const auto version = original_.value(QStringLiteral("version"));
        const auto entity = entityName();
        connect(box, &QMessageBox::finished, this, [this, id, version, entity](int result) {
            if (result != QMessageBox::Yes || entityName() != entity || selectedId_ != id || dirty_ || writing_ || loading_ || uncertainWrite_) return;
            writing_ = true;
            updateControls();
            setStatus(QStringLiteral("Deleting…"));
            api_.request("DELETE", entity + QLatin1Char('/') + displayValue(id), {}, {{QStringLiteral("version"), version}}, [this](ApiResult response) {
                writing_ = false;
                if (!response.ok()) {
                    if (response.uncertain) latchUncertainWrite(response.error);
                    else { apiError(response); updateControls(); }
                    return;
                }
                if (response.status != 204) {
                    latchUncertainWrite(QStringLiteral("The service did not confirm deletion with the expected response."));
                    return;
                }
                if (offset_ > 0 && model_->rowCount() == 1) offset_ = qMax(0, offset_ - pageSize_);
                loadRows();
            });
        });
        box->open();
        record(QStringLiteral("delete_confirmation"));
    }
    void updateControls() {
        const bool idle = !loading_ && !writing_;
        const bool reconciled = idle && !uncertainWrite_;
        const bool hasEntity = !entityName().isEmpty();
        entities_->setEnabled(reconciled);
        filter_->setEnabled(reconciled && hasEntity);
        reload_->setEnabled(idle);
        new_->setEnabled(reconciled && hasEntity);
        save_->setEnabled(reconciled && dirty_ && formOpen_);
        formSave_->setEnabled(save_->isEnabled());
        formCancel_->setEnabled(idle && formOpen_);
        delete_->setEnabled(reconciled && formOpen_ && !creating_ && !dirty_);
        grid_->setEnabled(reconciled);
        previous_->setEnabled(reconciled && offset_ > 0);
        next_->setEnabled(reconciled && offset_ + pageSize_ < total_);
        formBody_->setEnabled(!writing_);
        details_->setTitle(dirty_ ? QStringLiteral("Row details · Unsaved changes") : QStringLiteral("Row details"));
    }
    static QJsonArray bounds(const QWidget *widget) {
        const auto point = widget->mapToGlobal(QPoint(0, 0));
        return {point.x(), point.y(), widget->width(), widget->height()};
    }
    static QJsonArray rectBounds(const QWidget *widget, const QRect &rect) {
        const auto point = widget->mapToGlobal(rect.topLeft());
        return {point.x(), point.y(), rect.width(), rect.height()};
    }
    void record(const QString &event) {
        lastEvent_ = event;
        ++sequence_;
        writeState();
    }
    void writeState() {
        if (stateFile_.isEmpty()) return;
        QJsonObject geometry{
            {QStringLiteral("window"), bounds(this)},
            {QStringLiteral("entity_list"), bounds(entities_)},
            {QStringLiteral("grid_viewport"), bounds(grid_->viewport())},
            {QStringLiteral("filter"), bounds(filter_)},
            {QStringLiteral("new_button"), bounds(new_)},
            {QStringLiteral("save_button"), bounds(save_)},
            {QStringLiteral("delete_button"), bounds(delete_)},
            {QStringLiteral("reload_button"), bounds(reload_)},
            {QStringLiteral("draft"), bounds(draft_)},
            {QStringLiteral("quit_button"), bounds(quit_)},
            {QStringLiteral("paging"), bounds(page_)},
            {QStringLiteral("previous_button"), bounds(previous_)},
            {QStringLiteral("next_button"), bounds(next_)}
        };
        for (int i = 0; i < entities_->count(); ++i) {
            auto *item = entities_->item(i);
            const auto rect = entities_->visualItemRect(item);
            if (rect.intersects(entities_->viewport()->rect()))
                geometry.insert(QStringLiteral("entity_") + item->data(Qt::UserRole).toString(), rectBounds(entities_->viewport(), rect));
        }
        for (int row = 0; row < model_->rowCount(); ++row) {
            const auto rect = grid_->visualRect(model_->index(row, 0));
            if (rect.intersects(grid_->viewport()->rect()))
                geometry.insert(QStringLiteral("grid_row_%1").arg(row), rectBounds(grid_->viewport(), rect));
        }
        if (grid_->currentIndex().isValid())
            geometry.insert(QStringLiteral("grid_current_cell"), rectBounds(grid_->viewport(), grid_->visualRect(grid_->currentIndex())));
        if (formOpen_) {
            geometry.insert(QStringLiteral("form_save_button"), bounds(formSave_));
            geometry.insert(QStringLiteral("form_cancel_button"), bounds(formCancel_));
            for (auto it = editors_.begin(); it != editors_.end(); ++it) {
                if (it.value()->visibleRegion().isEmpty()) continue;
                geometry.insert(QStringLiteral("field_") + it.key(), bounds(it.value()));
                if (auto *combo = qobject_cast<QComboBox *>(it.value()))
                    if (combo->view()->isVisible())
                        geometry.insert(QStringLiteral("field_") + it.key() + QStringLiteral("_popup"), bounds(combo->view()));
            }
        }
        if (auto *modal = QApplication::activeModalWidget()) {
            geometry.insert(QStringLiteral("modal"), bounds(modal));
            if (auto *box = qobject_cast<QMessageBox *>(modal)) {
                for (auto *action : box->buttons())
                    geometry.insert(QStringLiteral("modal_button_%1").arg(int(box->standardButton(action))), bounds(action));
            }
        }
        QJsonObject state{
            {QStringLiteral("user"), user_},
            {QStringLiteral("generation"), generation_},
            {QStringLiteral("pid"), QCoreApplication::applicationPid()},
            {QStringLiteral("ready"), ready_},
            {QStringLiteral("entity"), entityName()},
            {QStringLiteral("entities"), schema_},
            {QStringLiteral("items"), model_->rows()},
            {QStringLiteral("selected_id"), selectedId_.isUndefined() ? QJsonValue(QJsonValue::Null) : selectedId_},
            {QStringLiteral("dirty"), dirty_},
            {QStringLiteral("error_text"), error_},
            {QStringLiteral("draft"), draft_->text()},
            {QStringLiteral("form_open"), formOpen_},
            {QStringLiteral("form_values"), formOpen_ ? formValues() : QJsonObject{}},
            {QStringLiteral("creating"), creating_},
            {QStringLiteral("loading"), loading_},
            {QStringLiteral("writing"), writing_},
            {QStringLiteral("uncertain_write"), uncertainWrite_},
            {QStringLiteral("offset"), offset_},
            {QStringLiteral("total"), total_},
            {QStringLiteral("last_event"), lastEvent_},
            {QStringLiteral("event_sequence"), sequence_},
            {QStringLiteral("focus_widget"), QApplication::focusWidget() ? QApplication::focusWidget()->objectName() : QString()},
            {QStringLiteral("geometry"), geometry}
        };
        QSaveFile file(stateFile_);
        if (!file.open(QIODevice::WriteOnly)) {
            if (!stateErrorReported_) qWarning("Cannot open desktop state file: %s", qPrintable(file.errorString()));
            stateErrorReported_ = true;
            return;
        }
        file.setPermissions(QFileDevice::ReadOwner | QFileDevice::WriteOwner);
        const auto bytes = QJsonDocument(state).toJson(QJsonDocument::Compact);
        if (file.write(bytes) != bytes.size() || !file.commit()) {
            if (!stateErrorReported_) qWarning("Cannot commit desktop state file: %s", qPrintable(file.errorString()));
            stateErrorReported_ = true;
        }
    }

    ApiClient api_;
    const QString user_;
    const QString generation_;
    const QString stateFile_;
    QJsonArray schema_;
    QJsonObject entity_;
    QJsonObject original_;
    QJsonObject baseline_;
    QHash<QString, QWidget *> editors_;
    QJsonValue selectedId_;
    EntityModel *model_{};
    QListWidget *entities_{};
    QTableView *grid_{};
    QLineEdit *filter_{};
    QLineEdit *draft_{};
    QGroupBox *details_{};
    QWidget *formBody_{};
    QFormLayout *formLayout_{};
    QLabel *entityTitle_{};
    QLabel *formTitle_{};
    QLabel *page_{};
    QLabel *status_{};
    QPushButton *quit_{};
    QPushButton *reload_{};
    QPushButton *new_{};
    QPushButton *save_{};
    QPushButton *delete_{};
    QPushButton *previous_{};
    QPushButton *next_{};
    QPushButton *formSave_{};
    QPushButton *formCancel_{};
    QPointer<QMessageBox> closePrompt_;
    QPointer<QMessageBox> deletePrompt_;
    QPointer<QMessageBox> reconcilePrompt_;
    int entityIndex_{-1};
    int offset_{};
    int total_{};
    static constexpr int pageSize_ = 25;
    QString sort_{QStringLiteral("id")};
    QString appliedFilter_;
    QString error_;
    QString lastEvent_;
    int formGeneration_{};
    int sequence_{};
    bool ready_{};
    bool loading_{};
    bool writing_{};
    bool uncertainWrite_{};
    bool formOpen_{};
    bool creating_{};
    bool dirty_{};
    bool descending_{};
    bool buildingForm_{};
    bool changingSelection_{};
    bool allowClose_{};
    bool stateErrorReported_{};
};

class SessionLock final {
public:
    int acquire() {
        const auto path = qgetenv("SWAG_SESSION_LOCK_FILE");
        if (path.isEmpty()) return 0;
        descriptor_ = ::open(path.constData(), O_CREAT | O_RDWR | O_CLOEXEC | O_NOFOLLOW, 0600);
        if (descriptor_ < 0) {
            std::fprintf(stderr, "Cannot open session lock: %s\n", std::strerror(errno));
            return 74;
        }
        if (::flock(descriptor_, LOCK_EX | LOCK_NB) < 0) {
            const int error = errno;
            std::fprintf(stderr, "Cannot acquire session lock: %s\n", std::strerror(error));
            return error == EWOULDBLOCK || error == EAGAIN ? 73 : 74;
        }
        return 0;
    }
    ~SessionLock() { if (descriptor_ >= 0) ::close(descriptor_); }
private:
    int descriptor_{-1};
};

} // namespace

int main(int argc, char **argv) {
    SessionLock lock;
    if (const int result = lock.acquire()) return result;
    QApplication app(argc, argv);
    app.setApplicationName(QStringLiteral("swag-desktop"));
    app.setStyle(QStringLiteral("Fusion"));
    app.setFont(QFont(QStringLiteral("DejaVu Sans"), 10));
    app.setStyleSheet(QStringLiteral(
        "QWidget { color:#152438; background-color:#ffffff; }"
        "QLineEdit,QComboBox { min-height:27px; padding:2px 5px; border:1px solid #7d8da2; border-radius:2px; }"
        "QLineEdit:read-only { background:#edf1f6; color:#4a596b; }"
        "QLineEdit:focus,QComboBox:focus { border:2px solid #215eac; }"
        "QPushButton { min-height:28px; padding:2px 9px; border:1px solid #7d8da2; background:#edf2f8; border-radius:2px; }"
        "QPushButton:focus { border:2px solid #215eac; }"
        "QPushButton:pressed { background:#cadbf2; }"
        "QPushButton:disabled { color:#8390a1; border-color:#bcc5d0; background:#f2f4f7; }"
        "QListWidget { border:1px solid #bac5d3; background:#f0f4f9; }"
        "QListWidget::item { padding:4px; }"
        "QListWidget::item:selected { background:#215eac; color:white; }"
        "QTableView { alternate-background-color:#f1f5fa; gridline-color:#ccd5e1; selection-background-color:#215eac; selection-color:white; }"
        "QHeaderView::section { background:#e3eaf4; padding:5px; border:1px solid #bcc8d8; }"
        "QGroupBox { border:1px solid #bac5d3; margin-top:9px; padding-top:10px; font-weight:bold; }"
        "QGroupBox::title { subcontrol-origin:margin; left:8px; padding:0 4px; }"));
    DesktopWindow window;
    window.showFullScreen();
    return app.exec();
}
