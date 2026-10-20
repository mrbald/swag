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
#include <QMdiArea>
#include <QMdiSubWindow>
#include <QResizeEvent>
#include <QShortcut>
#include <QNetworkAccessManager>
#include <QNetworkReply>
#include <QNetworkRequest>
#include <QPointer>
#include <QPushButton>
#include <QRegularExpression>
#include <QRegularExpressionValidator>
#include <QSaveFile>
#include <QScreen>
#include <QScrollArea>
#include <QSignalBlocker>
#include <QSplitter>
#include <QTableView>
#include <QTimer>
#include <QUrl>
#include <QUrlQuery>
#include <QVBoxLayout>
#include <QWindow>

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

    void cancelAll() {
        // Disconnect callbacks before abort: abort can synchronously emit finished.
        // Each pane owns its API client, so a closed read-only pane cannot leave
        // callbacks referring to its destroyed form/model.
        for (auto *reply : network_.findChildren<QNetworkReply *>()) {
            QObject::disconnect(reply, nullptr, this, nullptr);
            reply->abort();
        }
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

QPushButton *makeButton(const QString &label, const QString &name, QWidget *parent) {
    auto *result = new QPushButton(label, parent);
    result->setObjectName(name);
    return result;
}

QJsonArray widgetBounds(const QWidget *widget) {
    const auto point = widget->mapToGlobal(QPoint(0, 0));
    return {point.x(), point.y(), widget->width(), widget->height()};
}

void openMessageBox(QMessageBox *box) {
    // Qt manages the entire dialog, including its border: no Openbox dependency.
    box->setWindowFlags(Qt::Dialog | Qt::FramelessWindowHint | Qt::BypassWindowManagerHint);
    box->setWindowModality(Qt::WindowModal);
    const QPointer<QWidget> previousFocus(QApplication::focusWidget());
    const QPointer<QWidget> owner(box->parentWidget() ? box->parentWidget()->window() : nullptr);
    QObject::connect(box, &QMessageBox::finished, owner, [owner, previousFocus] {
        if (!owner) return;
        QTimer::singleShot(0, owner, [owner, previousFocus] {
            if (!owner) return;
            owner->raise();
            owner->activateWindow();
            if (previousFocus && previousFocus->isVisible()) previousFocus->setFocus();
        });
    });
    box->open();
    QTimer::singleShot(0, box, [box] {
        box->adjustSize();
        if (auto *owner = box->parentWidget()) {
            const auto center = owner->window()->mapToGlobal(owner->window()->rect().center());
            box->move(center - box->rect().center());
        }
        box->raise();
        box->activateWindow();
        if (box->defaultButton()) box->defaultButton()->setFocus();
    });
}

class EntityPane final : public QWidget {
public:
    explicit EntityPane(QWidget *parent = nullptr) : QWidget(parent), api_(this) {
        setObjectName(QStringLiteral("entity_pane"));
        auto *body = this;
        auto *layout = new QVBoxLayout(this);
        layout->setSizeConstraint(QLayout::SetNoConstraint);
        layout->setContentsMargins(3, 0, 3, 0);
        layout->setSpacing(7);
        setMinimumSize(430, 300);
        auto *splitter = new QSplitter(Qt::Horizontal, this);
        auto *workspace = new QWidget(splitter);
        auto *center = new QVBoxLayout(workspace);
        center->setContentsMargins(3, 0, 3, 0);
        entityTitle_ = new QLabel(QStringLiteral("Connecting to bank service…"), workspace);
        auto font = entityTitle_->font();
        font.setBold(true);
        entityTitle_->setFont(font);
        layout->addWidget(entityTitle_);
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
        layout->addLayout(toolbar);
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
        details_->setMinimumWidth(245);
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
        splitter->setSizes({700, 330});
        contentSplitter_ = splitter;
        layout->addWidget(splitter, 1);
        status_ = new QLabel(QStringLiteral("Loading schema…"), body);
        status_->setObjectName(QStringLiteral("status"));
        status_->setMinimumHeight(40);
        status_->setWordWrap(true);
        status_->setTextInteractionFlags(Qt::TextSelectableByMouse);
        layout->addWidget(status_);
        entityTitle_->setWordWrap(true);
        grid_->setMinimumHeight(65);
        scroll->setMinimumHeight(60);

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
        updateControls();
    }
    ~EntityPane() override { changed_ = {}; api_.cancelAll(); }
    void setChanged(std::function<void(const QString &)> callback) { changed_ = std::move(callback); }
    bool setSchema(const QJsonArray &schema, int index) { schema_ = schema; return selectEntity(index); }
    bool dirty() const { return dirty_; }
    bool writing() const { return writing_; }
    bool uncertain() const { return uncertainWrite_; }
    bool loading() const { return loading_; }
    int entityIndex() const { return entityIndex_; }
    QString label() const { return entityTitle_->text(); }
    QString name() const { return entityName(); }
    void focusGrid() { grid_->setFocus(); }
    QJsonObject snapshot() const { return observation(); }
    bool changeEntity(int row) { return selectEntity(row); }
    void showFailure(const QString &text) { setError(text); }

protected:
    void resizeEvent(QResizeEvent *event) override {
        QWidget::resizeEvent(event);
        // Keep grid rows visible beside the scrollable form in tiled frames.
        // The title and toolbar span the pane rather than narrowing the grid.
        const bool narrow = width() < 850;
        details_->setMinimumWidth(narrow ? 0 : 245);
        details_->setMaximumWidth(narrow ? QWIDGETSIZE_MAX : 410);
        contentSplitter_->setOrientation(Qt::Horizontal);
    }

private:
    static QPushButton *button(const QString &label, const QString &name, QWidget *parent) {
        return makeButton(label, name, parent);
    }
    void notifyChanged() { if (changed_) changed_(QStringLiteral("pane_changed")); }
    void record(const QString &event) { if (changed_) changed_(event); }
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
        openMessageBox(box);
        record(QStringLiteral("reconcile_confirmation"));
    }
    bool selectEntity(int row) {
        if (row < 0 || row >= schema_.size())
            return false;
        if (!canNavigate()) {
            return false;
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
        return true;
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
        openMessageBox(box);
        record(QStringLiteral("delete_confirmation"));
    }
    void updateControls() {
        const bool idle = !loading_ && !writing_;
        const bool reconciled = idle && !uncertainWrite_;
        const bool hasEntity = !entityName().isEmpty();
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
        notifyChanged();
    }
    static QJsonArray bounds(const QWidget *widget) {
        const auto point = widget->mapToGlobal(QPoint(0, 0));
        return {point.x(), point.y(), widget->width(), widget->height()};
    }
    static QJsonArray rectBounds(const QWidget *widget, const QRect &rect) {
        const auto point = widget->mapToGlobal(rect.topLeft());
        return {point.x(), point.y(), rect.width(), rect.height()};
    }
    QJsonObject observation() const {
        QJsonObject geometry{
            {QStringLiteral("window"), bounds(this)},
            {QStringLiteral("grid_viewport"), bounds(grid_->viewport())},
            {QStringLiteral("filter"), bounds(filter_)},
            {QStringLiteral("new_button"), bounds(new_)},
            {QStringLiteral("save_button"), bounds(save_)},
            {QStringLiteral("delete_button"), bounds(delete_)},
            {QStringLiteral("reload_button"), bounds(reload_)},
            {QStringLiteral("paging"), bounds(page_)},
            {QStringLiteral("previous_button"), bounds(previous_)},
            {QStringLiteral("next_button"), bounds(next_)}
        };
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
            {QStringLiteral("entity"), entityName()},
            {QStringLiteral("entities"), schema_},
            {QStringLiteral("items"), model_->rows()},
            {QStringLiteral("selected_id"), selectedId_.isUndefined() ? QJsonValue(QJsonValue::Null) : selectedId_},
            {QStringLiteral("dirty"), dirty_},
            {QStringLiteral("error_text"), error_},
            {QStringLiteral("form_open"), formOpen_},
            {QStringLiteral("form_values"), formOpen_ ? formValues() : QJsonObject{}},
            {QStringLiteral("creating"), creating_},
            {QStringLiteral("loading"), loading_},
            {QStringLiteral("writing"), writing_},
            {QStringLiteral("uncertain_write"), uncertainWrite_},
            {QStringLiteral("offset"), offset_},
            {QStringLiteral("total"), total_},
            {QStringLiteral("focus_widget"), QApplication::focusWidget() ? QApplication::focusWidget()->objectName() : QString()},
            {QStringLiteral("geometry"), geometry}
        };
        return state;
    }
    ApiClient api_;
    QSplitter *contentSplitter_{};
    std::function<void(const QString &)> changed_;
    QJsonArray schema_;
    QJsonObject entity_;
    QJsonObject original_;
    QJsonObject baseline_;
    QHash<QString, QWidget *> editors_;
    QJsonValue selectedId_;
    EntityModel *model_{};
    QTableView *grid_{};
    QLineEdit *filter_{};
    QGroupBox *details_{};
    QWidget *formBody_{};
    QFormLayout *formLayout_{};
    QLabel *entityTitle_{};
    QLabel *formTitle_{};
    QLabel *page_{};
    QLabel *status_{};
    QPushButton *reload_{};
    QPushButton *new_{};
    QPushButton *save_{};
    QPushButton *delete_{};
    QPushButton *previous_{};
    QPushButton *next_{};
    QPushButton *formSave_{};
    QPushButton *formCancel_{};
    QPointer<QMessageBox> deletePrompt_;
    QPointer<QMessageBox> reconcilePrompt_;
    int entityIndex_{-1};
    int offset_{};
    int total_{};
    static constexpr int pageSize_ = 25;
    QString sort_{QStringLiteral("id")};
    QString appliedFilter_;
    QString error_;
    int formGeneration_{};
    bool loading_{};
    bool writing_{};
    bool uncertainWrite_{};
    bool formOpen_{};
    bool creating_{};
    bool dirty_{};
    bool descending_{};
    bool buildingForm_{};
    bool changingSelection_{};
};

class GuardedSubWindow final : public QMdiSubWindow {
public:
    using QMdiSubWindow::QMdiSubWindow;
    std::function<bool()> mayClose;
    bool discardApproved{};
protected:
    void closeEvent(QCloseEvent *event) override {
        if (discardApproved || !mayClose || mayClose()) QMdiSubWindow::closeEvent(event);
        else event->ignore();
    }
};

class DesktopWindow final : public QMainWindow {
public:
    DesktopWindow()
        : api_(this), user_(qEnvironmentVariable("SWAG_USER", "local diagnostic")),
          generation_(qEnvironmentVariable("SWAG_GENERATION", "local")),
          stateFile_(qEnvironmentVariable("SWAG_STATE_FILE")),
          mdiMode_(qEnvironmentVariable("SWAG_UI_MODE", "single") == QStringLiteral("mdi")) {
        setWindowTitle(QStringLiteral("SWAG · Banking workspace"));
        setObjectName(QStringLiteral("desktop_window"));
        if (mdiMode_) setWindowFlags(Qt::Window | Qt::FramelessWindowHint);
        if (const auto *screen = QGuiApplication::primaryScreen()) setGeometry(screen->geometry());
        auto *body = new QWidget(this);
        auto *layout = new QVBoxLayout(body);
        layout->setContentsMargins(6, 4, 6, 4);
        layout->setSpacing(4);
        auto *header = new QHBoxLayout;
        header->setSpacing(8);
        header->addWidget(new QLabel(QStringLiteral("User: %1").arg(user_), body));
        auto *noteLabel = new QLabel(QStringLiteral("Note"), body);
        header->addWidget(noteLabel);
        draft_ = new QLineEdit(body);
        draft_->setObjectName(QStringLiteral("draft"));
        draft_->setMinimumWidth(120);
        draft_->setAccessibleName(QStringLiteral("Unsaved workspace note"));
        draft_->setPlaceholderText(QStringLiteral("Workspace note"));
        draft_->setToolTip(QStringLiteral("Unsaved note retained in this running workspace when you reconnect."));
        noteLabel->setBuddy(draft_);
        header->addWidget(draft_, 1);
        quit_ = makeButton(QStringLiteral("Quit"), QStringLiteral("quit_button"), body);
        quit_->setToolTip(QStringLiteral("Quit the application and end this workspace."));
        header->addWidget(quit_);
        layout->addLayout(header);
        auto *splitter = new QSplitter(Qt::Horizontal, body);
        auto *navigation = new QWidget(splitter);
        auto *navigationLayout = new QVBoxLayout(navigation);
        navigationLayout->setContentsMargins(0, 0, 0, 0);
        entities_ = new QListWidget(navigation);
        entities_->setObjectName(QStringLiteral("entity_list"));
        entities_->setMinimumWidth(160);
        entities_->setToolTip(mdiMode_
            ? QStringLiteral("Select an entity to open or activate a frame. Ctrl+Tab switches frames; Ctrl+Shift+Tab switches back. * marks unsaved changes.")
            : QStringLiteral("Select the entity to view and edit."));
        navigation->setMaximumWidth(205);
        navigationLayout->addWidget(entities_, 1);
        if (mdiMode_) {
            open_ = makeButton(QStringLiteral("Open new window"), QStringLiteral("mdi_open_button"), navigation);
            open_->setToolTip(QStringLiteral("Open another independent frame for the selected entity (Ctrl+N)"));
            navigationLayout->addWidget(open_);
            tile_ = makeButton(QStringLiteral("Tile"), QStringLiteral("mdi_tile_button"), navigation);
            cascade_ = makeButton(QStringLiteral("Cascade"), QStringLiteral("mdi_cascade_button"), navigation);
            maximize_ = makeButton(QStringLiteral("Maximize active"), QStringLiteral("mdi_maximize_button"), navigation);
            close_ = makeButton(QStringLiteral("Close active"), QStringLiteral("mdi_close_button"), navigation);
            tile_->setToolTip(QStringLiteral("Arrange all frames in tiles."));
            cascade_->setToolTip(QStringLiteral("Arrange overlapping frames with their title bars visible."));
            maximize_->setToolTip(QStringLiteral("Maximize or restore the active frame."));
            close_->setToolTip(QStringLiteral("Close the active frame (Ctrl+W). Unsaved changes require confirmation."));
            navigationLayout->addWidget(tile_);
            navigationLayout->addWidget(cascade_);
            navigationLayout->addWidget(maximize_);
            navigationLayout->addWidget(close_);
            mdi_ = new QMdiArea(splitter);
            mdi_->setObjectName(QStringLiteral("mdi_area"));
            mdi_->setViewMode(QMdiArea::SubWindowView);
            mdi_->setActivationOrder(QMdiArea::ActivationHistoryOrder);
            mdi_->setOption(QMdiArea::DontMaximizeSubWindowOnActivation, true);
            mdi_->setHorizontalScrollBarPolicy(Qt::ScrollBarAsNeeded);
            mdi_->setVerticalScrollBarPolicy(Qt::ScrollBarAsNeeded);
            connect(open_, &QPushButton::clicked, this, [this] { openPane(entities_->currentRow()); });
            connect(tile_, &QPushButton::clicked, mdi_, &QMdiArea::tileSubWindows);
            connect(cascade_, &QPushButton::clicked, mdi_, &QMdiArea::cascadeSubWindows);
            connect(maximize_, &QPushButton::clicked, this, [this] {
                if (auto *window = mdi_->activeSubWindow()) {
                    if (window->isMaximized()) window->showNormal();
                    else window->showMaximized();
                }
            });
            connect(close_, &QPushButton::clicked, this, [this] { closeActivePane(); });
            connect(mdi_, &QMdiArea::subWindowActivated, this, [this](QMdiSubWindow *window) {
                if (window) {
                    if (auto *pane = static_cast<EntityPane *>(window->widget())) {
                        const QSignalBlocker blocker(entities_);
                        entities_->setCurrentRow(pane->entityIndex());
                    }
                }
                updateShellControls();
                record(QStringLiteral("window_activated"));
            });
            auto *next = new QShortcut(QKeySequence(QStringLiteral("Ctrl+Tab")), this);
            connect(next, &QShortcut::activated, mdi_, &QMdiArea::activateNextSubWindow);
            auto *previous = new QShortcut(QKeySequence(QStringLiteral("Ctrl+Shift+Tab")), this);
            connect(previous, &QShortcut::activated, mdi_, &QMdiArea::activatePreviousSubWindow);
            // QMdiSubWindow already installs QKeySequence::Close (Ctrl+W on
            // Linux). A second shell shortcut makes the focused frame's native
            // action ambiguous, suppressing both rather than closing it.
            auto *newShortcut = new QShortcut(QKeySequence(QStringLiteral("Ctrl+N")), this);
            connect(newShortcut, &QShortcut::activated, this, [this] { openPane(entities_->currentRow()); });
        } else {
            single_ = new EntityPane(splitter);
            single_->setChanged([this](const QString &event) { updateShellControls(); record(event); });
        }
        splitter->setSizes({170, 1080});
        layout->addWidget(splitter, 1);
        shellStatus_ = new QLabel(QStringLiteral("Loading schema…"), body);
        shellStatus_->setWordWrap(true);
        layout->addWidget(shellStatus_);
        setCentralWidget(body);
        connect(quit_, &QPushButton::clicked, this, &QWidget::close);
        connect(draft_, &QLineEdit::textChanged, this, [this] { record(QStringLiteral("draft_changed")); });
        connect(entities_, &QListWidget::currentRowChanged, this, [this](int row) {
            if (row < 0 || row >= schema_.size()) return;
            if (mdiMode_) {
                selectFrame(row);
            } else if (single_ && !single_->changeEntity(row)) {
                const QSignalBlocker blocker(entities_); entities_->setCurrentRow(single_->entityIndex());
            }
        });
        connect(entities_, &QListWidget::itemClicked, this, [this](QListWidgetItem *) {
            // currentRowChanged does not fire when the last frame for the
            // already-selected entity was closed. Selection and click share
            // the same activate-or-open operation, so one click creates once.
            if (mdiMode_) selectFrame(entities_->currentRow());
        });
        connect(entities_, &QListWidget::itemDoubleClicked, this, [this](QListWidgetItem *) {
            if (mdiMode_) openPane(entities_->currentRow());
        });
        connect(qApp, &QCoreApplication::aboutToQuit, this, [this] { ready_ = false; record(QStringLiteral("quit")); });
        auto *timer = new QTimer(this);
        timer->setInterval(200);
        connect(timer, &QTimer::timeout, this, [this] { writeState(); });
        timer->start();
        updateShellControls();
        QTimer::singleShot(0, this, [this] { ready_ = true; record(QStringLiteral("ready")); loadSchema(); });
    }

    void followScreen() {
        if (!followingScreen_) {
            followingScreen_ = true;
            if (auto *handle = windowHandle())
                connect(handle, &QWindow::screenChanged, this, [this](QScreen *screen) { watchScreen(screen); });
            connect(qApp, &QGuiApplication::primaryScreenChanged, this, [this](QScreen *screen) {
                if (!screen_) watchScreen(screen);
            });
        }
        watchScreen(windowHandle() ? windowHandle()->screen() : QGuiApplication::primaryScreen());
    }

    ~DesktopWindow() override {
        QObject::disconnect(screenGeometryConnection_);
        api_.cancelAll();
        if (single_) single_->setChanged({});
        for (const auto &window : windows_) {
            if (!window) continue;
            window->disconnect(this);
            window->mayClose = {};
            if (window->widget()) static_cast<EntityPane *>(window->widget())->setChanged({});
        }
        if (mdi_) mdi_->disconnect(this);
    }

protected:
    void closeEvent(QCloseEvent *event) override {
        if (allowClose_) { event->accept(); return; }
        bool dirty = false, writing = false, uncertain = false;
        for (auto *pane : panes()) {
            dirty |= pane->dirty(); writing |= pane->writing(); uncertain |= pane->uncertain();
        }
        if (!dirty && !writing && !uncertain) { event->accept(); return; }
        event->ignore();
        if (closePrompt_) return;
        if (writing) {
            showCloseBlocked(QStringLiteral("Quit workspace"),
                QStringLiteral("A frame is saving or deleting. Wait for its result before quitting the workspace."));
            return;
        }
        auto *box = new QMessageBox(QMessageBox::Warning, QStringLiteral("Quit workspace"),
            uncertain
                ? QStringLiteral("A frame has an unconfirmed write result. Quit the entire workspace and discard all retained forms?")
                : QStringLiteral("One or more frames have unsaved changes. Quit the entire workspace and discard all forms?"),
            QMessageBox::Discard | QMessageBox::Cancel, this);
        closePrompt_ = box;
        box->setDefaultButton(QMessageBox::Cancel);
        box->setAttribute(Qt::WA_DeleteOnClose);
        connect(box, &QMessageBox::finished, this, [this](int result) {
            // Preflight the entire collection: cancellation never closes any pane.
            if (result == QMessageBox::Discard) { allowClose_ = true; close(); }
        });
        openMessageBox(box);
        record(QStringLiteral("quit_confirmation"));
    }

private:
    void watchScreen(QScreen *screen) {
        if (!screen) screen = QGuiApplication::primaryScreen();
        if (screen_ != screen) {
            QObject::disconnect(screenGeometryConnection_);
            screen_ = screen;
            if (screen_)
                screenGeometryConnection_ = connect(screen_, &QScreen::geometryChanged, this, [this] {
                    // Apply after Qt has processed the RandR screen update.
                    QTimer::singleShot(0, this, [this] { fitScreen(); });
                });
        }
        fitScreen();
    }
    void fitScreen() {
        if (!screen_) return;
        // Fullscreen geometry must be explicit when Xvnc has no window manager.
        // A browser remote-resize changes QScreen without replacing the app.
        setGeometry(screen_->geometry());
        QTimer::singleShot(0, this, [this] {
            if (mdi_) keepFrameVisible(mdi_->activeSubWindow());
            if (auto *modal = QApplication::activeModalWidget())
                modal->move(mapToGlobal(rect().center()) - modal->rect().center());
            record(QStringLiteral("screen_geometry_changed"));
        });
    }
    QList<EntityPane *> panes() const {
        if (single_) return {single_};
        QList<EntityPane *> result;
        for (const auto &window : windows_) if (window && window->widget()) result << static_cast<EntityPane *>(window->widget());
        return result;
    }
    EntityPane *activePane() const {
        if (single_) return single_;
        return mdi_ && mdi_->activeSubWindow() ? static_cast<EntityPane *>(mdi_->activeSubWindow()->widget()) : nullptr;
    }
    void showCloseBlocked(const QString &title, const QString &message) {
        auto *box = new QMessageBox(QMessageBox::Warning, title, message, QMessageBox::Ok, this);
        closePrompt_ = box;
        box->setDefaultButton(QMessageBox::Ok);
        box->setAttribute(Qt::WA_DeleteOnClose);
        openMessageBox(box);
        record(QStringLiteral("close_blocked"));
    }
    bool mayClosePane(GuardedSubWindow *window, EntityPane *pane) {
        if (pane->writing()) {
            if (!closePrompt_) showCloseBlocked(QStringLiteral("Close frame"),
                QStringLiteral("This frame is saving or deleting. Wait for its result before closing it."));
            return false;
        }
        if (!pane->dirty() && !pane->uncertain()) return true;
        if (closePrompt_) return false;
        auto *box = new QMessageBox(QMessageBox::Warning, QStringLiteral("Close frame"),
            pane->uncertain()
                ? QStringLiteral("This frame has an unconfirmed write result. Close it and discard the retained form?")
                : QStringLiteral("This frame has unsaved changes. Close it and discard the form?"),
            QMessageBox::Discard | QMessageBox::Cancel, this);
        closePrompt_ = box;
        box->setDefaultButton(QMessageBox::Cancel);
        box->setAttribute(Qt::WA_DeleteOnClose);
        const QPointer<GuardedSubWindow> guarded(window);
        connect(box, &QMessageBox::finished, this, [guarded](int result) {
            if (result == QMessageBox::Discard && guarded) { guarded->discardApproved = true; guarded->close(); }
        });
        openMessageBox(box);
        record(QStringLiteral("frame_close_confirmation"));
        return false;
    }
    void closeActivePane() {
        if (mdi_ && mdi_->activeSubWindow()) mdi_->activeSubWindow()->close();
    }
    void keepFrameVisible(QMdiSubWindow *window) {
        if (!window || window->isMaximized() || window->isMinimized()) return;
        const auto available = mdi_->viewport()->size();
        window->resize(qMin(window->width(), available.width()), qMin(window->height(), available.height()));
        window->move(qBound(0, window->pos().x(), qMax(0, available.width() - window->width())),
                     qBound(0, window->pos().y(), qMax(0, available.height() - window->height())));
    }
    void selectFrame(int index) {
        if (!mdi_ || index < 0 || index >= schema_.size()) return;
        if (auto *active = mdi_->activeSubWindow()) {
            if (active->widget() && static_cast<EntityPane *>(active->widget())->entityIndex() == index) {
                if (active->isMinimized()) active->showNormal();
                keepFrameVisible(active);
                active->raise();
                return;
            }
        }
        for (const auto &window : windows_) {
            if (!window || !window->widget() || static_cast<EntityPane *>(window->widget())->entityIndex() != index) continue;
            if (window->isMinimized()) window->showNormal();
            keepFrameVisible(window);
            mdi_->setActiveSubWindow(window);
            window->raise();
            return;
        }
        openPane(index);
    }
    void openPane(int index) {
        if (!mdi_ || index < 0 || index >= schema_.size()) return;
        const bool firstFrame = mdi_->subWindowList().isEmpty();
        auto *window = new GuardedSubWindow;
        const QString id = QString::number(++windowSequence_);
        window->setObjectName(QStringLiteral("mdi_window_") + id);
        window->setProperty("swag_id", id);
        auto *pane = new EntityPane;
        window->setWidget(pane);
        window->setAttribute(Qt::WA_DeleteOnClose);
        window->mayClose = [this, window, pane] { return mayClosePane(window, pane); };
        mdi_->addSubWindow(window);
        windows_ << window;
        pane->setChanged([this, window, pane](const QString &event) {
            QString title = pane->label() + QStringLiteral(" · ") + window->property("swag_id").toString();
            if (pane->dirty()) title += QStringLiteral(" *");
            if (pane->writing()) title += QStringLiteral(" · Saving…");
            if (pane->uncertain()) title += QStringLiteral(" · Write outcome unknown");
            window->setWindowTitle(title);
            updateShellControls(); record(event);
        });
        connect(window, &QObject::destroyed, this, [this] {
            for (int i = windows_.size() - 1; i >= 0; --i) if (!windows_.at(i)) windows_.removeAt(i);
            updateShellControls(); record(QStringLiteral("window_closed"));
        });
        connect(window, &QMdiSubWindow::windowStateChanged, this, [this] {
            updateShellControls(); record(QStringLiteral("window_state_changed"));
        });
        pane->setSchema(schema_, index);
        window->resize(qMin(990, mdi_->viewport()->width() - 20), qMin(615, mdi_->viewport()->height() - 20));
        window->show();
        // QMdiArea's default placement may put a large child mostly outside
        // its viewport. Cascade only within the available horizontal/vertical
        // slack, wrapping offsets so every new frame is fully reachable.
        const int offset = (windows_.size() - 1) * 24;
        const int horizontalSlack = qMax(0, mdi_->viewport()->width() - window->width());
        const int verticalSlack = qMax(0, mdi_->viewport()->height() - window->height());
        window->move(horizontalSlack ? offset % (horizontalSlack + 1) : 0,
                     verticalSlack ? offset % (verticalSlack + 1) : 0);
        keepFrameVisible(window);
        if (firstFrame) window->showMaximized();
        mdi_->setActiveSubWindow(window);
        pane->focusGrid();
        record(QStringLiteral("window_opened"));
    }
    void loadSchema() {
        if (schemaLoading_) return;
        schemaLoading_ = true;
        shellStatus_->setText(QStringLiteral("Loading schema…"));
        shellStatus_->show();
        updateShellControls();
        api_.request("GET", QStringLiteral("schema"), {}, {}, [this](ApiResult result) {
            schemaLoading_ = false;
            schema_ = result.body.value(QStringLiteral("entities")).toArray();
            if (!result.ok() || schema_.isEmpty()) {
                shellError_ = result.error.isEmpty() ? QStringLiteral("The service returned an empty schema.") : result.error;
                shellStatus_->setText(shellError_ + QStringLiteral(" Retrying…"));
                shellStatus_->setStyleSheet(QStringLiteral("color:#a51c20;"));
                if (single_) single_->showFailure(shellError_);
                updateShellControls(); record(QStringLiteral("schema_failed"));
                QTimer::singleShot(3000, this, [this] { loadSchema(); });
                return;
            }
            shellError_.clear();
            shellStatus_->setStyleSheet({});
            shellStatus_->clear();
            shellStatus_->hide();
            {
                const QSignalBlocker blocker(entities_);
                entities_->clear();
                for (const auto &value : schema_) {
                    const auto entity = value.toObject();
                    auto *item = new QListWidgetItem(entity.value(QStringLiteral("label")).toString(entity.value(QStringLiteral("name")).toString()), entities_);
                    item->setData(Qt::UserRole, entity.value(QStringLiteral("name")).toString());
                    item->setSizeHint(QSize(150, 36));
                }
                entities_->setCurrentRow(0);
            }
            if (single_) single_->setSchema(schema_, 0);
            else openPane(0);
            updateShellControls(); record(QStringLiteral("schema_loaded"));
        });
    }
    void updateShellControls() {
        const bool available = !schema_.isEmpty() && !schemaLoading_;
        entities_->setEnabled(available && (mdiMode_ || !single_ || (!single_->loading() && !single_->writing() && !single_->uncertain())));
        if (open_) open_->setEnabled(available);
        if (tile_) tile_->setEnabled(mdi_ && !mdi_->subWindowList().isEmpty());
        if (cascade_) cascade_->setEnabled(tile_->isEnabled());
        if (maximize_) {
            const auto *active = mdi_->activeSubWindow();
            maximize_->setEnabled(active);
            maximize_->setText(active && active->isMaximized() ? QStringLiteral("Restore active") : QStringLiteral("Maximize active"));
        }
        if (close_) close_->setEnabled(mdi_ && mdi_->activeSubWindow());
    }
    void record(const QString &event) { lastEvent_ = event; ++sequence_; writeState(); }
    void writeState() {
        if (stateFile_.isEmpty()) return;
        QJsonObject state = activePane() ? activePane()->snapshot() : QJsonObject{
            {QStringLiteral("entity"), QString()}, {QStringLiteral("items"), QJsonArray{}},
            {QStringLiteral("selected_id"), QJsonValue(QJsonValue::Null)}, {QStringLiteral("dirty"), false},
            {QStringLiteral("form_open"), false}, {QStringLiteral("form_values"), QJsonObject{}},
            {QStringLiteral("creating"), false}, {QStringLiteral("loading"), schemaLoading_},
            {QStringLiteral("writing"), false}, {QStringLiteral("uncertain_write"), false},
            {QStringLiteral("offset"), 0}, {QStringLiteral("total"), 0}, {QStringLiteral("error_text"), shellError_}
        };
        QJsonObject geometry = state.value(QStringLiteral("geometry")).toObject();
        geometry.insert(QStringLiteral("window"), widgetBounds(this));
        if (screen_) {
            const auto screenRect = screen_->geometry();
            const QJsonArray screenBounds{screenRect.x(), screenRect.y(), screenRect.width(), screenRect.height()};
            geometry.insert(QStringLiteral("screen"), screenBounds);
            state.insert(QStringLiteral("screen_geometry"), screenBounds);
            state.insert(QStringLiteral("framebuffer_size"), QJsonArray{screenRect.width(), screenRect.height()});
        }
        geometry.insert(QStringLiteral("entity_list"), widgetBounds(entities_));
        geometry.insert(QStringLiteral("draft"), widgetBounds(draft_));
        geometry.insert(QStringLiteral("quit_button"), widgetBounds(quit_));
        for (int i = 0; i < entities_->count(); ++i) {
            auto *item = entities_->item(i);
            const auto rect = entities_->visualItemRect(item);
            if (rect.intersects(entities_->viewport()->rect())) {
                const auto point = entities_->viewport()->mapToGlobal(rect.topLeft());
                geometry.insert(QStringLiteral("entity_") + item->data(Qt::UserRole).toString(), QJsonArray{point.x(), point.y(), rect.width(), rect.height()});
            }
        }
        QJsonArray windows;
        QJsonValue activeId(QJsonValue::Null);
        if (mdi_) {
            geometry.insert(QStringLiteral("mdi_viewport"), widgetBounds(mdi_->viewport()));
            geometry.insert(QStringLiteral("mdi_open_button"), widgetBounds(open_));
            geometry.insert(QStringLiteral("mdi_tile_button"), widgetBounds(tile_));
            geometry.insert(QStringLiteral("mdi_cascade_button"), widgetBounds(cascade_));
            geometry.insert(QStringLiteral("mdi_maximize_button"), widgetBounds(maximize_));
            geometry.insert(QStringLiteral("mdi_close_button"), widgetBounds(close_));
            for (const auto &window : windows_) {
                if (!window || !window->widget()) continue;
                auto *pane = static_cast<EntityPane *>(window->widget());
                auto item = pane->snapshot();
                const auto id = window->property("swag_id").toString();
                const bool active = mdi_->activeSubWindow() == window;
                item.insert(QStringLiteral("window_id"), id);
                item.insert(QStringLiteral("title"), window->windowTitle());
                item.insert(QStringLiteral("active"), active);
                item.insert(QStringLiteral("maximized"), window->isMaximized());
                item.insert(QStringLiteral("minimized"), window->isMinimized());
                auto childGeometry = item.value(QStringLiteral("geometry")).toObject();
                childGeometry.insert(QStringLiteral("window"), widgetBounds(window));
                const auto titlePoint = window->mapToGlobal(QPoint(0, 0));
                const int titleHeight = qMax(20, pane->mapTo(window, QPoint(0, 0)).y());
                QJsonArray titleBounds{titlePoint.x() + 6, titlePoint.y() + 2, qMax(10, window->width() - 110), titleHeight - 2};
                childGeometry.insert(QStringLiteral("window_title"), titleBounds);
                item.insert(QStringLiteral("geometry"), childGeometry);
                geometry.insert(QStringLiteral("window_") + id, widgetBounds(window));
                geometry.insert(QStringLiteral("window_title_") + id, titleBounds);
                windows.append(item);
                if (active) activeId = id;
            }
        }
        if (auto *modal = QApplication::activeModalWidget()) {
            geometry.insert(QStringLiteral("modal"), widgetBounds(modal));
            if (auto *box = qobject_cast<QMessageBox *>(modal))
                for (auto *action : box->buttons())
                    geometry.insert(QStringLiteral("modal_button_%1").arg(int(box->standardButton(action))), widgetBounds(action));
        }
        state.insert(QStringLiteral("user"), user_);
        state.insert(QStringLiteral("generation"), generation_);
        state.insert(QStringLiteral("pid"), QCoreApplication::applicationPid());
        state.insert(QStringLiteral("ready"), ready_);
        state.insert(QStringLiteral("entities"), schema_);
        state.insert(QStringLiteral("draft"), draft_->text());
        state.insert(QStringLiteral("ui_mode"), mdiMode_ ? QStringLiteral("mdi") : QStringLiteral("single"));
        state.insert(QStringLiteral("windows"), windows);
        state.insert(QStringLiteral("active_window_id"), activeId);
        state.insert(QStringLiteral("last_event"), lastEvent_);
        state.insert(QStringLiteral("event_sequence"), sequence_);
        state.insert(QStringLiteral("focus_widget"), QApplication::focusWidget() ? QApplication::focusWidget()->objectName() : QString());
        state.insert(QStringLiteral("geometry"), geometry);
        QSaveFile file(stateFile_);
        if (!file.open(QIODevice::WriteOnly)) {
            if (!stateErrorReported_) qWarning("Cannot open desktop state file: %s", qPrintable(file.errorString()));
            stateErrorReported_ = true; return;
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
    const bool mdiMode_;
    QPointer<QScreen> screen_;
    QMetaObject::Connection screenGeometryConnection_;
    QJsonArray schema_;
    QList<QPointer<GuardedSubWindow>> windows_;
    EntityPane *single_{};
    QMdiArea *mdi_{};
    QListWidget *entities_{};
    QLineEdit *draft_{};
    QLabel *shellStatus_{};
    QPushButton *quit_{};
    QPushButton *open_{};
    QPushButton *tile_{};
    QPushButton *cascade_{};
    QPushButton *maximize_{};
    QPushButton *close_{};
    QPointer<QMessageBox> closePrompt_;
    QString shellError_;
    QString lastEvent_;
    int sequence_{};
    int windowSequence_{};
    bool ready_{};
    bool schemaLoading_{};
    bool allowClose_{};
    bool stateErrorReported_{};
    bool followingScreen_{};
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
    window.followScreen();
    QTimer::singleShot(0, &window, [&window] { window.raise(); window.activateWindow(); window.setFocus(); });
    return app.exec();
}
