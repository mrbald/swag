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

#include "qvncclient.h"
#include "qvnc_p.h"

#include "qvncwebsocketstream.h"
#include <QtCore/QIODevice>
#include <QtCore/QCoreApplication>
#include <QtCore/QtEndian>

#include <qpa/qwindowsysteminterface.h>
#include <QtGui/qguiapplication.h>
#include <QtGui/QWindow>

#ifdef Q_OS_WIN
#include <Winsock2.h>
#else
#include <arpa/inet.h>
#endif

QT_BEGIN_NAMESPACE

QVncClient::QVncClient(QIODevice *clientSocket, QVncServer *server)
    : QObject(server)
    , m_server(server)
    , m_clientSocket(clientSocket)
    , m_encoder(nullptr)
    , m_state(Protocol)
    , m_msgType(0)
    , m_handleMsg(false)
    , m_encodingsPending(-1)
    , m_cutTextPending(0)
    , m_supportCopyRect(false)
    , m_supportRRE(false)
    , m_supportCoRRE(false)
    , m_supportHextile(false)
    , m_supportZRLE(false)
    , m_supportCursor(false)
    , m_supportDesktopSize(false)
    , m_wantUpdate(false)
    , m_keymod(Qt::NoModifier)
    , m_dirtyCursor(false)
    , m_updatePending(false)
{
    m_clientSocket->setParent(this);
    connect(m_clientSocket,SIGNAL(readyRead()),this,SLOT(readClient()));
    connect(m_clientSocket,SIGNAL(disconnected()),this,SLOT(discardClient()));
    m_handshakeTimer.setSingleShot(true);
    connect(&m_handshakeTimer, &QTimer::timeout, this, [this]() {
        qWarning("vncws: RFB handshake timed out");
        close();
    });
    m_handshakeTimer.start(10000);

    // send protocol version
    const char *proto = "RFB 003.003\n";
    m_clientSocket->write(proto, 12);
}

QVncClient::~QVncClient()
{
    delete m_encoder;
}

QIODevice *QVncClient::clientSocket() const
{
    return m_clientSocket;
}

void QVncClient::close()
{
    if (m_state == Disconnected)
        return;
    m_state = Disconnected;
    m_handshakeTimer.stop();
    releaseInput();
    static_cast<QVncWebSocketStream *>(m_clientSocket)->close();
}

void QVncClient::releaseInput()
{
    for (auto it = m_heldKeys.cbegin(); it != m_heldKeys.cend(); ++it)
        QWindowSystemInterface::handleKeyEvent(nullptr, QEvent::KeyRelease,
            it.value().keycode, Qt::NoModifier, it.value().text);
    m_heldKeys.clear();
    m_keymod = Qt::NoModifier;
    for (const Qt::MouseButton button : {Qt::LeftButton, Qt::MiddleButton, Qt::RightButton}) {
        if (m_buttonState.testFlag(button)) {
            m_buttonState = Qt::MouseButtons(int(m_buttonState) & ~int(button));
            QWindowSystemInterface::handleMouseEvent(nullptr,
                m_lastPointerPos, m_lastPointerPos, m_buttonState, button,
                QEvent::MouseButtonRelease, Qt::NoModifier);
        }
    }
}

void QVncClient::setDirty(const QRegion &region)
{
    m_dirtyRegion += region.intersected(m_server->screen()->geometry());
    if (m_state == Connected &&
        ((m_server->dirtyMap()->numDirty > 0) || m_dirtyCursor)) {
        scheduleUpdate();
    }
}

void QVncClient::convertPixels(char *dst, const char *src, int count) const
{
    const int screendepth = m_server->screen()->depth();

    // cutoffs
#if Q_BYTE_ORDER == Q_BIG_ENDIAN
    if (!m_swapBytes)
#endif
    if (m_sameEndian) {
        if (screendepth == m_pixelFormat.bitsPerPixel) { // memcpy cutoffs

            switch (screendepth) {
            case 32:
                if (m_pixelFormat.redShift == 16 && m_pixelFormat.greenShift == 8 &&
                    m_pixelFormat.blueShift == 0) {
                    memcpy(dst, src, count * sizeof(quint32));
                    return;
                }
                break;
            case 16:
                if (m_pixelFormat.redBits == 5
                    && m_pixelFormat.greenBits == 6
                    && m_pixelFormat.blueBits == 5)
                {
                    memcpy(dst, src, count * sizeof(quint16));
                    return;
                }
            }
        }
    }

    const int bytesPerPixel = (m_pixelFormat.bitsPerPixel + 7) / 8;

    for (int i = 0; i < count; ++i) {
        int r, g, b;

        switch (screendepth) {
        case 8: {
            QRgb rgb = m_server->screen()->image()->colorTable()[int(*src)];
            r = qRed(rgb);
            g = qGreen(rgb);
            b = qBlue(rgb);
            src++;
            break;
        }
        case 16: {
            quint16 p = *reinterpret_cast<const quint16*>(src);
#if Q_BYTE_ORDER == Q_BIG_ENDIAN
            if (m_swapBytes)
                p = ((p & 0xff) << 8) | ((p & 0xff00) >> 8);
#endif
            r = (p >> 11) & 0x1f;
            g = (p >> 5) & 0x3f;
            b = p & 0x1f;
            r <<= 3;
            g <<= 2;
            b <<= 3;
            src += sizeof(quint16);
            break;
        }
        case 32: {
            quint32 p = *reinterpret_cast<const quint32*>(src);
            r = (p >> 16) & 0xff;
            g = (p >> 8) & 0xff;
            b = p & 0xff;
            src += sizeof(quint32);
            break;
        }
        default: {
            r = g = b = 0;
            qWarning("QVNCServer: don't support %dbpp display", screendepth);
            return;
        }
        }

#if Q_BYTE_ORDER == Q_BIG_ENDIAN
        if (m_swapBytes)
            qSwap(r, b);
#endif

        r >>= (8 - m_pixelFormat.redBits);
        g >>= (8 - m_pixelFormat.greenBits);
        b >>= (8 - m_pixelFormat.blueBits);

        int pixel = (r << m_pixelFormat.redShift) |
                    (g << m_pixelFormat.greenShift) |
                    (b << m_pixelFormat.blueShift);

        if (m_sameEndian || m_pixelFormat.bitsPerPixel == 8) {
            memcpy(dst, &pixel, bytesPerPixel);
            dst += bytesPerPixel;
            continue;
        }


        if (QSysInfo::ByteOrder == QSysInfo::BigEndian) {
            switch (m_pixelFormat.bitsPerPixel) {
            case 16:
                pixel = (((pixel & 0x0000ff00) << 8)  |
                         ((pixel & 0x000000ff) << 24));
                break;
            case 32:
                pixel = (((pixel & 0xff000000) >> 24) |
                         ((pixel & 0x00ff0000) >> 8)  |
                         ((pixel & 0x0000ff00) << 8)  |
                         ((pixel & 0x000000ff) << 24));
                break;
            default:
                qWarning("Cannot handle %d bpp client", m_pixelFormat.bitsPerPixel);
            }
        } else { // QSysInfo::ByteOrder == QSysInfo::LittleEndian
            switch (m_pixelFormat.bitsPerPixel) {
            case 16:
                pixel = (((pixel & 0xff000000) >> 8) |
                         ((pixel & 0x00ff0000) << 8));
                break;
            case 32:
                pixel = (((pixel & 0xff000000) >> 24) |
                         ((pixel & 0x00ff0000) >> 8)  |
                         ((pixel & 0x0000ff00) << 8)  |
                         ((pixel & 0x000000ff) << 24));
                break;
            default:
                qWarning("Cannot handle %d bpp client",
                       m_pixelFormat.bitsPerPixel);
                break;
            }
        }
        memcpy(dst, &pixel, bytesPerPixel);
        dst += bytesPerPixel;
    }
}

void QVncClient::readClient()
{
    // Consume every complete message in the stream. Partial messages remain in
    // QVncWebSocketStream until the next binary WebSocket frame arrives.
    while (m_state != Disconnected) {
        if (m_state == Protocol) {
            if (m_clientSocket->bytesAvailable() < 12)
                return;
            if (m_clientSocket->read(12) != QByteArrayLiteral("RFB 003.003\n")) {
                qWarning("vncws: unsupported RFB protocol version");
                close();
                return;
            }
            const quint32 auth = htonl(1); // RFB 3.3 security type None.
            m_clientSocket->write(reinterpret_cast<const char *>(&auth), sizeof(auth));
            m_state = Init;
            continue;
        }
        if (m_state == Init) {
            if (m_clientSocket->bytesAvailable() < 1)
                return;
            char shared;
            m_clientSocket->read(&shared, 1);
            if (shared != 0 && shared != 1) {
                qWarning("vncws: invalid ClientInit shared flag");
                close();
                return;
            }
            QRfbServerInit init;
            auto &format = init.format;
            format.bitsPerPixel = 32;
            format.depth = 32;
            format.bigEndian = 0;
            format.trueColor = true;
            format.redBits = format.greenBits = format.blueBits = 8;
            format.redMax = format.greenMax = format.blueMax = 255;
            format.redShift = 16;
            format.greenShift = 8;
            format.blueShift = 0;
            init.width = m_server->screen()->geometry().width();
            init.height = m_server->screen()->geometry().height();
            init.setName("SWAG vncws");
            init.write(m_clientSocket);
            initializePixelFormat(format);
            m_encoder = new QRfbRawEncoder(this);
            m_state = Connected;
            m_handshakeTimer.stop();
            continue;
        }
        if (!m_handleMsg) {
            if (m_clientSocket->bytesAvailable() < 1)
                return;
            m_clientSocket->read(reinterpret_cast<char *>(&m_msgType), 1);
            m_handleMsg = true;
        }
        const qint64 before = m_clientSocket->bytesAvailable();
        switch (m_msgType) {
        case SetPixelFormat:
            setPixelFormat();
            break;
        case SetEncodings:
            setEncodings();
            break;
        case FramebufferUpdateRequest:
            frameBufferUpdateRequest();
            break;
        case KeyEvent:
            keyEvent();
            break;
        case PointerEvent:
            pointerEvent();
            break;
        case ClientCutText:
            clientCutText();
            break;
        case SetDesktopSize:
            setDesktopSize();
            break;
        default:
            qWarning("vncws: unsupported RFB message type %u", unsigned(m_msgType));
            close();
            return;
        }
        if (m_state == Disconnected)
            return;
        if (m_handleMsg && m_clientSocket->bytesAvailable() == before)
            return;
    }
}

void QVncClient::initializePixelFormat(const QRfbPixelFormat &format)
{
    m_pixelFormat = format;
    m_sameEndian = (QSysInfo::ByteOrder == QSysInfo::BigEndian) == bool(format.bigEndian);
#if Q_BYTE_ORDER == Q_BIG_ENDIAN
    m_swapBytes = server()->screen()->swapBytes();
#endif
    m_needConversion = pixelConversionNeeded();
}

bool QVncClient::validPixelFormat(const QRfbPixelFormat &format) const
{
    return format.bitsPerPixel == 32 &&
           (format.depth == 24 || format.depth == 32) &&
           format.bigEndian == 0 && format.trueColor == 1 &&
           format.redMax == 255 && format.greenMax == 255 &&
           format.blueMax == 255 &&
           format.greenShift == 8 &&
           ((format.redShift == 16 && format.blueShift == 0) ||
            (format.redShift == 0 && format.blueShift == 16));
}

void QVncClient::discardClient()
{
    if (m_discarded)
        return;
    m_discarded = true;
    m_state = Disconnected;
    m_handshakeTimer.stop();
    releaseInput();
    m_server->discardClient(this);
}

void QVncClient::checkUpdate()
{
    if (m_state != Connected || !m_wantUpdate)
        return;
    if (m_requestRect.isEmpty()) {
        sendEmptyUpdate();
        m_wantUpdate = false;
        return;
    }
#if QT_CONFIG(cursor)
    if (m_dirtyCursor && m_supportCursor && m_server->screen()->clientCursor) {
        m_server->screen()->clientCursor->write(this);
        m_dirtyCursor = false;
        m_wantUpdate = false;
        return;
    }
#endif
    const QRegion sendRegion = dirtyRegion();
    if (!sendRegion.isEmpty()) {
        if (m_encoder)
            m_encoder->write();
        m_wantUpdate = false;
        m_dirtyRegion -= sendRegion;
    }
}

void QVncClient::sendEmptyUpdate()
{
    const char header[4] = {0, 0, 0, 0};
    m_clientSocket->write(header, sizeof(header));
}

void QVncClient::sendExtendedDesktopSize(quint16 reason, quint16 status)
{
    // A pseudo-rectangle must be the only rectangle in this update.
    char update[36] = {};
    const auto put16 = [&update](int offset, quint16 value) {
        qToBigEndian(value, reinterpret_cast<uchar *>(update + offset));
    };
    const auto put32 = [&update](int offset, quint32 value) {
        qToBigEndian(value, reinterpret_cast<uchar *>(update + offset));
    };
    const QVncScreen *screen = m_server->screen();
    const QSize size = screen->geometry().size();
    put16(2, 1);                    // one rectangle
    put16(4, reason);
    put16(6, status);
    put16(8, quint16(size.width()));
    put16(10, quint16(size.height()));
    put32(12, quint32(-308));      // ExtendedDesktopSize
    update[16] = 1;                // one screen
    put32(20, screen->screenId);
    put16(28, quint16(size.width()));
    put16(30, quint16(size.height()));
    put32(32, screen->screenFlags);
    m_clientSocket->write(update, sizeof(update));
}

void QVncClient::scheduleUpdate()
{
    if (!m_updatePending) {
        m_updatePending = true;
        QCoreApplication::postEvent(this, new QEvent(QEvent::UpdateRequest));
    }
}

bool QVncClient::event(QEvent *event)
{
    if (event->type() == QEvent::UpdateRequest) {
        m_updatePending = false;
        checkUpdate();
        return true;
    }
    return QObject::event(event);
}

void QVncClient::setPixelFormat()
{
    if (m_clientSocket->bytesAvailable() >= 19) {
        char buf[3];
        m_clientSocket->read(buf, 3); // just padding
        QRfbPixelFormat format;
        format.read(m_clientSocket);
        if (!validPixelFormat(format)) {
            qWarning("vncws: unsupported pixel format (need 32bpp little-endian RGB888 or BGR888)");
            close();
            return;
        }
        initializePixelFormat(format);
        m_handleMsg = false;
    }
}

void QVncClient::setEncodings()
{
    if (m_encodingsPending < 0) {
        QRfbSetEncodings header;
        if (!header.read(m_clientSocket))
            return;
        if (header.count > 256) {
            qWarning("vncws: too many encoding hints");
            close();
            return;
        }
        m_encodingsPending = header.count;
    }
    if (m_clientSocket->bytesAvailable() < qint64(m_encodingsPending) * 4)
        return;

    bool wantsCursor = false;
    bool wantsDesktopSize = false;
    for (int i = 0; i < m_encodingsPending; ++i) {
        qint32 encoding;
        m_clientSocket->read(reinterpret_cast<char *>(&encoding), sizeof(encoding));
        encoding = qint32(ntohl(quint32(encoding)));
        if (encoding == -239) // Cursor pseudo-encoding.
            wantsCursor = true;
        if (encoding == -308) // ExtendedDesktopSize pseudo-encoding.
            wantsDesktopSize = true;
        // Other entries are preference hints. Raw is mandatory in RFB.
    }
    if (wantsCursor && !m_supportCursor)
        m_server->screen()->enableClientCursor(this);
    else if (!wantsCursor && m_supportCursor) {
        m_server->screen()->disableClientCursor(this);
        m_dirtyCursor = false;
    }
    m_supportCursor = wantsCursor;
    m_supportDesktopSize = wantsDesktopSize;
    if (!wantsDesktopSize)
        m_sentDesktopSize = false;
    m_encodingsPending = -1;
    m_handleMsg = false;
}

void QVncClient::frameBufferUpdateRequest()
{
    qCDebug(lcVnc) << "FramebufferUpdateRequest";
    QRfbFrameBufferUpdateRequest ev;

    if (ev.read(m_clientSocket)) {
        if (ev.incremental != 0 && ev.incremental != 1) {
            qWarning("vncws: invalid FramebufferUpdateRequest incremental flag");
            close();
            return;
        }
        if (!ev.incremental && m_supportDesktopSize) {
            // The layout reply completes this request; pixels follow a later
            // request, after the client has processed the descriptor.
            sendExtendedDesktopSize(0, 0);
            m_sentDesktopSize = true;
            m_dirtyRegion = m_server->screen()->geometry();
            m_requestRect = m_server->screen()->geometry();
            m_wantUpdate = false;
            m_handleMsg = false;
            return;
        }
        m_requestRect = QRect(ev.rect.x, ev.rect.y, ev.rect.w, ev.rect.h)
                            .intersected(m_server->screen()->geometry());
        if (m_requestRect.isEmpty()) {
            sendEmptyUpdate();
            m_wantUpdate = false;
            m_handleMsg = false;
            return;
        }
        if (!ev.incremental) {
            setDirty(m_requestRect);
        }
        m_wantUpdate = true;
        checkUpdate();
        m_handleMsg = false;
    }
}

void QVncClient::setDesktopSize()
{
    if (!m_supportDesktopSize || !m_sentDesktopSize) {
        qWarning("vncws: SetDesktopSize without ExtendedDesktopSize negotiation");
        close();
        return;
    }

    // The seven-byte header contains a bounded screen count. Do not consume
    // any part of a fragmented request until all screen records have arrived.
    if (m_clientSocket->bytesAvailable() < 7)
        return;
    const QByteArray header = m_clientSocket->peek(7);
    const int count = quint8(header[5]);
    const int length = 7 + 16 * count;
    if (m_clientSocket->bytesAvailable() < length)
        return;
    const QByteArray message = m_clientSocket->read(length);
    const auto get16 = [&message](int offset) {
        return qFromBigEndian<quint16>(reinterpret_cast<const uchar *>(message.constData() + offset));
    };
    const auto get32 = [&message](int offset) {
        return qFromBigEndian<quint32>(reinterpret_cast<const uchar *>(message.constData() + offset));
    };
    const int width = get16(1);
    const int height = get16(3);
    const bool validSize = width >= 64 && width <= 2048 &&
                           height >= 64 && height <= 2048 &&
                           qint64(width) * height <= 2073600;
    const bool validLayout = count == 1 &&
                             get32(7) == m_server->screen()->screenId &&
                             get16(11) == 0 && get16(13) == 0 &&
                             get16(15) == width && get16(17) == height;
    if (!validSize || !validLayout) {
        sendExtendedDesktopSize(1, 3); // invalid screen layout
    } else if (!m_server->screen()->resizeScreen(QSize(width, height))) {
        sendExtendedDesktopSize(1, 2); // out of resources
    } else {
        m_server->screen()->screenFlags = get32(19);
        m_requestRect = m_server->screen()->geometry();
        m_dirtyRegion = m_requestRect;
        m_wantUpdate = false;
        sendExtendedDesktopSize(1, 0);
    }
    m_handleMsg = false;
}

void QVncClient::pointerEvent()
{
    QRfbPointerEvent ev;
    if (ev.read(m_clientSocket)) {
        if (ev.rawButtons & 0x80) {
            qWarning("vncws: unsupported pointer button mask");
            close();
            return;
        }
        const QRect screen = m_server->screen()->geometry();
        const QPointF pos = screen.topLeft() + QPoint(
            qBound(0, int(ev.x), screen.width() - 1),
            qBound(0, int(ev.y), screen.height() - 1));
        m_lastPointerPos = pos;
        const int buttonStateChange = int(m_buttonState ^ ev.buttons);
        QEvent::Type type = QEvent::MouseMove;
        if (int(ev.buttons) > int(m_buttonState))
            type = QEvent::MouseButtonPress;
        else if (int(ev.buttons) < int(m_buttonState))
            type = QEvent::MouseButtonRelease;
        QWindowSystemInterface::handleMouseEvent(nullptr, pos, pos, ev.buttons, Qt::MouseButton(buttonStateChange),
                                                 type, QGuiApplication::keyboardModifiers());
        m_buttonState = ev.buttons;
        if (ev.wheelMask) {
            QWindow *target = QGuiApplication::topLevelAt(pos.toPoint());
            const QPointF local = target ? target->mapFromGlobal(pos.toPoint()) : pos;
            const QPoint delta((ev.wheelMask & 0x20 ? -120 : 0) +
                               (ev.wheelMask & 0x40 ? 120 : 0),
                               (ev.wheelMask & 0x08 ? 120 : 0) +
                               (ev.wheelMask & 0x10 ? -120 : 0));
            QWindowSystemInterface::handleWheelEvent(
                target, local, pos,
                QPoint(), delta, QGuiApplication::keyboardModifiers());
        }
        m_handleMsg = false;
    }
}

void QVncClient::keyEvent()
{
    QRfbKeyEvent ev;

    if (ev.read(m_clientSocket)) {
        if (ev.down != 0 && ev.down != 1) {
            qWarning("vncws: invalid key down flag");
            close();
            return;
        }
        if (ev.keycode == Qt::Key_Shift)
            m_keymod = ev.down ? m_keymod | Qt::ShiftModifier :
                                 m_keymod & ~Qt::ShiftModifier;
        else if (ev.keycode == Qt::Key_Control)
            m_keymod = ev.down ? m_keymod | Qt::ControlModifier :
                                 m_keymod & ~Qt::ControlModifier;
        else if (ev.keycode == Qt::Key_Alt)
            m_keymod = ev.down ? m_keymod | Qt::AltModifier :
                                 m_keymod & ~Qt::AltModifier;
        if (ev.unicode || ev.keycode) {
            const uint codepoint = uint(ev.unicode);
            const QString text = ev.unicode ? QString::fromUcs4(&codepoint, 1) : QString();
            if (ev.down && !m_heldKeys.contains(ev.keysym) && m_heldKeys.size() >= 256) {
                qWarning("vncws: too many held keys");
                close();
                return;
            }
            if (ev.down)
                m_heldKeys.insert(ev.keysym, HeldKey{ev.keycode, text});
            else
                m_heldKeys.remove(ev.keysym);
            QWindowSystemInterface::handleKeyEvent(nullptr,
                ev.down ? QEvent::KeyPress : QEvent::KeyRelease,
                ev.keycode, m_keymod, text);
        }
        m_handleMsg = false;
    }
}

void QVncClient::clientCutText()
{
    QRfbClientCutText ev;

    if (m_cutTextPending == 0 && ev.read(m_clientSocket)) {
        if (ev.length > 1024 * 1024) {
            close();
            return;
        }
        m_cutTextPending = ev.length;
        if (!m_cutTextPending) {
            m_server->setRemoteClipboardText(QString());
            m_handleMsg = false;
        }
    }

    if (m_cutTextPending && m_clientSocket->bytesAvailable() >= m_cutTextPending) {
        const QByteArray text = m_clientSocket->read(m_cutTextPending);
        m_server->setRemoteClipboardText(QString::fromLatin1(text));
        m_cutTextPending = 0;
        m_handleMsg = false;
    }
}

bool QVncClient::pixelConversionNeeded() const
{
    if (!m_sameEndian)
        return true;

#if Q_BYTE_ORDER == Q_BIG_ENDIAN
    if (server()->screen()->swapBytes())
        return true;
#endif

    const int screendepth = m_server->screen()->depth();
    if (screendepth != m_pixelFormat.bitsPerPixel)
        return true;

    switch (screendepth) {
    case 32:
        return m_pixelFormat.redShift != 16 || m_pixelFormat.greenShift != 8 ||
               m_pixelFormat.blueShift != 0;
    case 24:
        return true;
    case 16:
        return (m_pixelFormat.redBits == 5
                && m_pixelFormat.greenBits == 6
                && m_pixelFormat.blueBits == 5);
    }
    return true;
}

QT_END_NAMESPACE
