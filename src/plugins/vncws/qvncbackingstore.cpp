/****************************************************************************
**
** Copyright (C) 2016 The Qt Company Ltd.
** Contact: https://www.qt.io/licensing/
**
** This file is part of the QtGui module of the Qt Toolkit.
**
** $QT_BEGIN_LICENSE:LGPL$
** Commercial License Usage
** Licensees holding valid commercial Qt licenses may use this file in
** accordance with the commercial license agreement provided with the
** Software or, alternatively, in accordance with the terms contained in
** a written agreement between you and The Qt Company. For licensing terms
** and conditions see https://www.qt.io/terms-conditions. For further
** information use the contact form at https://www.qt.io/contact-us.
**
** GNU Lesser General Public License Usage
** Alternatively, this file may be used under the terms of the GNU Lesser
** General Public License version 3 as published by the Free Software
** Foundation and appearing in the file LICENSE.LGPL3 included in the
** packaging of this file. Please review the following information to
** ensure the GNU Lesser General Public License version 3 requirements
** will be met: https://www.gnu.org/licenses/lgpl-3.0.html.
**
** GNU General Public License Usage
** Alternatively, this file may be used under the terms of the GNU
** General Public License version 2.0 or (at your option) the GNU General
** Public license version 3 or any later version approved by the KDE Free
** Qt Foundation. The licenses are as published by the Free Software
** Foundation and appearing in the file LICENSE.GPL2 and LICENSE.GPL3
** included in the packaging of this file. Please review the following
** information to ensure the GNU General Public License requirements will
** be met: https://www.gnu.org/licenses/gpl-2.0.html and
** https://www.gnu.org/licenses/gpl-3.0.html.
**
** $QT_END_LICENSE$
**
****************************************************************************/

#include "qvncbackingstore.h"

#include <QtCore/QLoggingCategory>
#include <QtCore/QThread>
#include <QtGui/QGuiApplication>
#include <QtGui/QPainter>
#include <QtGui/QScreen>
#include <QtGui/QWindow>
#include <qpa/qplatformscreen.h>

QT_BEGIN_NAMESPACE

Q_LOGGING_CATEGORY(lcVncPaint, "vncws.paint", QtWarningMsg)

void QVncBackingStore::beginPaint(const QRegion &region)
{
    Q_ASSERT(QThread::currentThread() == qGuiApp->thread());
    if (mPaintDepth++ > 0) {
        if (!mReportedNestedPaint) {
            mReportedNestedPaint = true;
            qCDebug(lcVncPaint) << "nested beginPaint: skip alpha clear";
        }
        return;
    }

    // QFbBackingStore's alpha clear, without its long-lived mutex. A nested
    // painter would conflict with the suspended outer widget painter.
    if (mImage.hasAlphaChannel()) {
        QPainter p(&mImage);
        p.setCompositionMode(QPainter::CompositionMode_Source);
        for (const QRect &r : region)
            p.fillRect(r, Qt::transparent);
    }
}

void QVncBackingStore::endPaint()
{
    Q_ASSERT(QThread::currentThread() == qGuiApp->thread());
    Q_ASSERT(mPaintDepth > 0);
    if (--mPaintDepth != 0 || !mResizePending)
        return;

    if (mDeferredSize.isEmpty()) {
        mImage = QImage();
    } else {
        QImage resized(mDeferredSize, window()->screen()->handle()->format());
        if (resized.isNull()) {
            // Keep the old storage and retry on the next outer endPaint.
            // QBackingStore has already cached the requested size, so it may
            // never issue this resize again. A later resize can supersede it.
            if (!mReportedAllocationFailure) {
                mReportedAllocationFailure = true;
                qCDebug(lcVncPaint) << "deferred resize allocation failed; retaining image";
            }
            return;
        }
        resized.fill(Qt::transparent);
        {
            QPainter p(&resized);
            p.setCompositionMode(QPainter::CompositionMode_Source);
            p.drawImage(QPoint(0, 0), mImage);
        }
        // QWidgetRepaintManager has ended its painters before endPaint.
        // Do not test mImage.paintingActive(): high-DPI painting can redirect
        // through a separate QImage wrapper over this same storage.
        mImage.swap(resized);
    }
    mResizePending = false;
}

void QVncBackingStore::resize(const QSize &size, const QRegion &staticContents)
{
    Q_ASSERT(QThread::currentThread() == qGuiApp->thread());
    if (mPaintDepth > 0) {
        mDeferredSize = size;
        mResizePending = size != mImage.size(); // Last request can cancel a resize.
        if (mResizePending && !mReportedDeferredResize) {
            mReportedDeferredResize = true;
            qCDebug(lcVncPaint) << "resize deferred until outer endPaint";
        }
        return;
    }
    mResizePending = false;
    QFbBackingStore::resize(size, staticContents);
}

QT_END_NAMESPACE
