# Qt gotchas

## Do not run a modal event loop while painting

A table calls `model->data()` while painting. If error handling there calls
`QMessageBox::critical()` or `QDialog::exec()`, the nested event loop can redraw
before painting finishes. With `vncws`, Qt's framebuffer compositor can then
wait on the backing-store mutex already held by the same GUI thread.

The idiomatic fix is to queue the notification and return from `data()` with a
safe fallback. Open the dialog asynchronously:

```cpp
#include <QMessageBox>
#include <QTimer>

// On the GUI thread; owner is a live, non-null QWidget*, message is a QString.
QTimer::singleShot(0, owner, [owner, message] {
    auto *box = new QMessageBox(QMessageBox::Critical, QStringLiteral("Error"), message,
                               QMessageBox::Ok, owner);
    box->setTextFormat(Qt::PlainText);
    box->setAttribute(Qt::WA_DeleteOnClose);
    box->open();
});
// Return a safe fallback from data(), e.g. an empty QVariant.
```

The context (`owner`) cancels the callback if the widget is destroyed. Capture
`message` by value. The heap-allocated dialog survives the callback and deletes
itself on close. `open()` is window-modal and returns immediately without a
nested event loop. Plain-text rendering preserves error text containing markup.
Do not call `processEvents()` or enter another nested event loop before painting
unwinds: a zero-delay callback is deferred, not a guarantee against reentrancy.
If many cells report the same failure, keep one notification pending/visible
instead of scheduling one per `data()` call.

Diagnostic signature: `paintEvent -> data -> error dialog -> exec -> doRedraw
-> QBasicMutex::lockInternal`. The reported client stack matches this mechanism;
mutex ownership has not been verified in that deployed build. A recursive mutex
is not a fix for reentrant painting.

Sources: Qt 5.15.2 [backing-store locking](https://github.com/qt/qtbase/blob/v5.15.2/src/platformsupport/fbconvenience/qfbbackingstore.cpp)
and [compositor locking](https://github.com/qt/qtbase/blob/v5.15.2/src/platformsupport/fbconvenience/qfbscreen.cpp).

Pattern references: [context-bound singleShot](https://doc.qt.io/qt-5/qtimer.html#singleShot-4)
and [asynchronous QDialog::open](https://doc.qt.io/qt-5/qdialog.html#open).

## Backend containment in vncws

`QVncBackingStore` avoids holding Qt's image mutex across application painting,
so this nested dialog can be composited and dismissed. The standard Qt compositor
is unchanged. Its image copy may snapshot an interrupted paint; ordinary paints
do not acquire a new snapshot or maintain a second buffer. This relies on
backing-store access staying on the GUI thread, checked by debug assertions.

Resizes requested during painting wait until the outer paint ends, preserving
existing pixels and clearing newly allocated space. Newly exposed or reentrantly
painted content may remain blank or stale until the application explicitly
invalidates it; the plugin does not schedule a full QWidget repaint. Nested painting of the same widget, recursive
error-dialog chains, or deleting an actively painted widget remain application
reentrancy errors; use the asynchronous pattern above. For diagnostics, enable
`QT_LOGGING_RULES="vncws.paint.debug=true"` (exceptional messages are limited per
backing store).

[Validation and performance evidence](docs/qualification/framebuffer-reentrancy.md).
