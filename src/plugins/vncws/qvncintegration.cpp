/****************************************************************************
**
** Copyright (C) 2017 The Qt Company Ltd.
** Contact: https://www.qt.io/licensing/
**
** This file is part of the plugins of the Qt Toolkit.
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

#include "qvncintegration.h"
#include "qvncscreen.h"
#include "qvnc_p.h"

#include <QtFontDatabaseSupport/private/qgenericunixfontdatabase_p.h>
#include <QtServiceSupport/private/qgenericunixservices_p.h>
#include <QtEventDispatcherSupport/private/qgenericunixeventdispatcher_p.h>

#include <QtFbSupport/private/qfbbackingstore_p.h>
#include <QtFbSupport/private/qfbwindow_p.h>
#include <QtFbSupport/private/qfbcursor_p.h>

#include <QtGui/private/qguiapplication_p.h>
#include <qpa/qplatforminputcontextfactory_p.h>
#include <private/qinputdevicemanager_p_p.h>
#include <qpa/qwindowsysteminterface.h>

#include <QtCore/QRegularExpression>
#include <QtCore/QSet>
#include <QtNetwork/QHostAddress>

QT_BEGIN_NAMESPACE

QVncIntegration::QVncIntegration(const QStringList &paramList)
    : m_fontDb(new QGenericUnixFontDatabase),
      m_services(new QGenericUnixServices)
{
    QSize size(1024, 768);
    QHostAddress address(QHostAddress::LocalHost);
    quint16 port = 5900;
    QSet<QString> seen;
    const QRegularExpression sizeRx(QStringLiteral("^([0-9]+)x([0-9]+)$"));
    const QRegularExpression ipv4Rx(QStringLiteral("^([0-9]{1,3}\\.){3}[0-9]{1,3}$"));
    const QRegularExpression numberRx(QStringLiteral("^[0-9]+$"));
    for (const QString &arg : paramList) {
        const int separator = arg.indexOf(QLatin1Char('='));
        if (separator <= 0 || separator == arg.size() - 1 ||
            arg.indexOf(QLatin1Char('='), separator + 1) >= 0)
            qFatal("vncws: invalid platform parameter '%s'", qPrintable(arg));
        const QString key = arg.left(separator);
        const QString value = arg.mid(separator + 1);
        if (seen.contains(key))
            qFatal("vncws: duplicate platform parameter '%s'", qPrintable(key));
        seen.insert(key);
        if (key == QLatin1String("size")) {
            const auto match = sizeRx.match(value);
            bool widthOk = false, heightOk = false;
            const int width = match.captured(1).toInt(&widthOk);
            const int height = match.captured(2).toInt(&heightOk);
            if (!match.hasMatch() || !widthOk || !heightOk ||
                width < 64 || width > 2048 || height < 64 || height > 2048 ||
                qint64(width) * height > 2073600)
                qFatal("vncws: size must be 64..2048 in each dimension and at most 2073600 pixels");
            size = QSize(width, height);
        } else if (key == QLatin1String("depth")) {
            if (value != QLatin1String("32"))
                qFatal("vncws: only depth=32 is supported");
        } else if (key == QLatin1String("port")) {
            bool valid = false;
            const uint number = value.toUInt(&valid);
            if (!numberRx.match(value).hasMatch() || !valid || number == 0 || number > 65535)
                qFatal("vncws: port must be 1..65535");
            port = quint16(number);
        } else if (key == QLatin1String("addr")) {
            if (!ipv4Rx.match(value).hasMatch() || !address.setAddress(value) ||
                address.protocol() != QAbstractSocket::IPv4Protocol)
                qFatal("vncws: addr must be a dotted-decimal IPv4 address");
        } else {
            qFatal("vncws: unknown platform parameter '%s'", qPrintable(key));
        }
    }

    m_primaryScreen = new QVncScreen(size);
    m_server = new QVncServer(m_primaryScreen, address, port);
    m_primaryScreen->vncServer = m_server;
}

QVncIntegration::~QVncIntegration()
{
    delete m_server;
    QWindowSystemInterface::handleScreenRemoved(m_primaryScreen);
}

void QVncIntegration::initialize()
{
    if (m_primaryScreen->initialize())
        QWindowSystemInterface::handleScreenAdded(m_primaryScreen);
    else
        qFatal("vncws: failed to initialize screen");

    m_inputContext = QPlatformInputContextFactory::create();

    m_nativeInterface.reset(new QPlatformNativeInterface);

    // we always have exactly one mouse and keyboard
    QInputDeviceManagerPrivate::get(QGuiApplicationPrivate::inputDeviceManager())->setDeviceCount(
        QInputDeviceManager::DeviceTypePointer, 1);
    QInputDeviceManagerPrivate::get(QGuiApplicationPrivate::inputDeviceManager())->setDeviceCount(
        QInputDeviceManager::DeviceTypeKeyboard, 1);

}

bool QVncIntegration::hasCapability(QPlatformIntegration::Capability cap) const
{
    switch (cap) {
    case ThreadedPixmaps: return true;
    case WindowManagement: return false;
    default: return QPlatformIntegration::hasCapability(cap);
    }
}

QPlatformBackingStore *QVncIntegration::createPlatformBackingStore(QWindow *window) const
{
    return new QFbBackingStore(window);
}

QPlatformWindow *QVncIntegration::createPlatformWindow(QWindow *window) const
{
    return new QFbWindow(window);
}

QAbstractEventDispatcher *QVncIntegration::createEventDispatcher() const
{
    return createUnixEventDispatcher();
}

QList<QPlatformScreen *> QVncIntegration::screens() const
{
    QList<QPlatformScreen *> list;
    list.append(m_primaryScreen);
    return list;
}

QPlatformFontDatabase *QVncIntegration::fontDatabase() const
{
    return m_fontDb.data();
}

QPlatformServices *QVncIntegration::services() const
{
    return m_services.data();
}

QPlatformNativeInterface *QVncIntegration::nativeInterface() const
{
    return m_nativeInterface.data();
}

QT_END_NAMESPACE
