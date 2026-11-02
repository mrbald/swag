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
#include <QtGui/QClipboard>
#include <QtGui/QWindow>
#include <zlib.h>
#include <utility>

#ifdef Q_OS_WIN
#include <Winsock2.h>
#else
#include <arpa/inet.h>
#endif

QT_BEGIN_NAMESPACE

namespace {
constexpr int MaxClipboardBytes = 1024 * 1024;
constexpr quint32 ClipboardText = 1;
constexpr quint32 ClipboardCaps = 1u << 24;
constexpr quint32 ClipboardRequest = 1u << 25;
constexpr quint32 ClipboardPeek = 1u << 26;
constexpr quint32 ClipboardNotify = 1u << 27;
constexpr quint32 ClipboardProvide = 1u << 28;
constexpr quint32 ClipboardActions = ClipboardCaps | ClipboardRequest |
                                     ClipboardPeek | ClipboardNotify | ClipboardProvide;
constexpr quint32 ExtendedClipboardEncoding = 0xc0a1e5ceu;

bool encodeClipboardText(const QString &text, QByteArray *encoded)
{
    if (text.size() >= MaxClipboardBytes)
        return false;
    QString normalized = text;
    normalized.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    normalized.replace(QLatin1Char('\r'), QLatin1Char('\n'));
    normalized.replace(QLatin1Char('\n'), QStringLiteral("\r\n"));
    QByteArray bytes = normalized.toUtf8();
    if (bytes.size() >= MaxClipboardBytes || bytes.contains('\0'))
        return false;
    bytes.append('\0');
    *encoded = std::move(bytes);
    return true;
}

bool decodeClipboardText(const QByteArray &compressed, QString *text)
{
    if (compressed.isEmpty() || compressed.size() > MaxClipboardBytes - 4)
        return false;
    QByteArray inflated(MaxClipboardBytes + 4, '\0');
    z_stream stream{};
    stream.next_in = reinterpret_cast<Bytef *>(const_cast<char *>(compressed.constData()));
    stream.avail_in = uInt(compressed.size());
    stream.next_out = reinterpret_cast<Bytef *>(inflated.data());
    stream.avail_out = uInt(inflated.size());
    if (inflateInit(&stream) != Z_OK)
        return false;
    int result = Z_OK;
    for (;;) {
        const uInt beforeIn = stream.avail_in;
        const uInt beforeOut = stream.avail_out;
        result = inflate(&stream, Z_SYNC_FLUSH);
        if (result == Z_STREAM_END || stream.avail_in == 0)
            break;
        if (result != Z_OK || stream.avail_out == 0 ||
            (beforeIn == stream.avail_in && beforeOut == stream.avail_out))
            break;
    }
    bool extraOutput = false;
    if (stream.avail_out == 0 && result != Z_STREAM_END) {
        uchar probe = 0;
        stream.next_out = &probe;
        stream.avail_out = 1;
        result = inflate(&stream, Z_SYNC_FLUSH);
        extraOutput = stream.avail_out == 0;
    }
    const bool complete = stream.avail_in == 0 &&
        (result == Z_STREAM_END ||
         ((result == Z_OK || result == Z_BUF_ERROR) &&
          compressed.endsWith(QByteArray("\0\0\xff\xff", 4))));
    const int produced = int(stream.total_out) - (extraOutput ? 1 : 0);
    inflateEnd(&stream);
    if (!complete || extraOutput || produced < 5)
        return false;

    const quint32 declared = qFromBigEndian<quint32>(
        reinterpret_cast<const uchar *>(inflated.constData()));
    if (declared == 0 || declared > MaxClipboardBytes ||
        produced != 4 + int(declared) || inflated[produced - 1] != '\0')
        return false;
    const QByteArray utf8 = inflated.mid(4, int(declared) - 1);
    if (utf8.contains('\0'))
        return false;
    QString decoded = QString::fromUtf8(utf8.constData(), utf8.size());
    if (decoded.toUtf8() != utf8)
        return false;
    *text = decoded.replace(QStringLiteral("\r\n"), QStringLiteral("\n"));
    return true;
}
} // namespace

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
    connect(static_cast<QVncWebSocketStream *>(m_clientSocket),
            &QVncWebSocketStream::outputDrained, this, &QVncClient::onOutputDrained);
    m_updateTimer.setSingleShot(true);
    m_updateTimer.setTimerType(Qt::PreciseTimer);
    connect(&m_updateTimer, &QTimer::timeout, this, [this]() {
        m_updatePending = false;
        checkUpdate();
    });
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
    m_updateTimer.stop();
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
    if (m_dirtyRegion.rectCount() > 4096)
        m_dirtyRegion = m_dirtyRegion.boundingRect();
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
    const qint64 pending = static_cast<QVncWebSocketStream *>(m_clientSocket)->queuedOutputBytes();
    if (pending > 1024 * 1024) {
        if (!m_backpressureDeferred)
            qCDebug(lcVnc) << "framebuffer deferred" << pending << "queued bytes";
        m_backpressureDeferred = true;
        return;
    }
    const QRegion sendRegion = dirtyRegion();
    bool includeCursor = false;
#if QT_CONFIG(cursor)
    includeCursor = m_dirtyCursor && m_supportCursor && m_server->screen()->clientCursor;
#endif
    if (!sendRegion.isEmpty()) {
        if (!m_encoder || !m_encoder->write(includeCursor) || m_state != Connected) {
            close();
            return;
        }
    } else if (includeCursor) {
#if QT_CONFIG(cursor)
        if (!m_server->screen()->clientCursor->writeUpdate(this) || m_state != Connected) {
            close();
            return;
        }
#endif
    } else {
        return; // An incremental request waits for the next dirty rectangle.
    }
    m_lastFramebufferUpdate.start();
    m_dirtyRegion -= sendRegion;
    if (includeCursor)
        m_dirtyCursor = false;
    m_wantUpdate = false;
}

void QVncClient::sendEmptyUpdate()
{
    const char header[4] = {0, 0, 0, 0};
    if (m_clientSocket->write(header, sizeof(header)) == sizeof(header) &&
        m_state == Connected)
        m_lastFramebufferUpdate.start();
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

void QVncClient::sendExtendedClipboard(quint32 flags, const QByteArray &data)
{
    if (m_state != Connected || data.size() > MaxClipboardBytes - 4)
        return;
    char header[12] = {3, 0, 0, 0}; // ServerCutText
    const quint32 length = quint32(4 + data.size());
    qToBigEndian(0u - length, reinterpret_cast<uchar *>(header + 4));
    qToBigEndian(flags, reinterpret_cast<uchar *>(header + 8));
    m_clientSocket->write(header, sizeof(header));
    if (!data.isEmpty())
        m_clientSocket->write(data);
}

void QVncClient::sendExtendedClipboardProvide(const QString &text)
{
    QByteArray utf8;
    if (!encodeClipboardText(text, &utf8)) {
        if (m_clientClipboardActions & ClipboardNotify)
            sendExtendedClipboard(ClipboardNotify);
        return;
    }
    QByteArray plain(4, '\0');
    qToBigEndian(quint32(utf8.size()), reinterpret_cast<uchar *>(plain.data()));
    plain.append(utf8);
    QByteArray compressed(int(compressBound(uLong(plain.size()))), '\0');
    uLongf compressedSize = uLongf(compressed.size());
    if (compress2(reinterpret_cast<Bytef *>(compressed.data()), &compressedSize,
                  reinterpret_cast<const Bytef *>(plain.constData()),
                  uLong(plain.size()), Z_DEFAULT_COMPRESSION) != Z_OK ||
        compressedSize > uLongf(MaxClipboardBytes - 4)) {
        if (m_clientClipboardActions & ClipboardNotify)
            sendExtendedClipboard(ClipboardNotify);
        return;
    }
    compressed.resize(int(compressedSize));
    sendExtendedClipboard(ClipboardProvide | ClipboardText, compressed);
}

void QVncClient::sendClipboardText(const QString &text)
{
    if (m_state != Connected)
        return;
    if (m_supportExtendedClipboard) {
        QByteArray utf8;
        if ((m_clientClipboardActions & ClipboardNotify) && m_clientClipboardText)
            sendExtendedClipboard(ClipboardNotify |
                (encodeClipboardText(text, &utf8) ? ClipboardText : 0u));
        return;
    }
    if (text.size() > MaxClipboardBytes)
        return;
    const QByteArray payload = text.toLatin1();
    if (payload.size() > MaxClipboardBytes)
        return;
    char header[8] = {3, 0, 0, 0};
    qToBigEndian(quint32(payload.size()), reinterpret_cast<uchar *>(header + 4));
    m_clientSocket->write(header, sizeof(header));
    m_clientSocket->write(payload);
}

void QVncClient::scheduleUpdate()
{
    if (!m_updatePending && !m_backpressureDeferred && m_state == Connected) {
        m_updatePending = true;
        // Space emitted frames by 16 ms while allowing the first frame after
        // an idle period to run on the next event-loop turn.
        const qint64 elapsed = m_lastFramebufferUpdate.isValid()
            ? m_lastFramebufferUpdate.elapsed() : 16;
        m_updateTimer.start(elapsed < 16 ? int(16 - elapsed) : 0);
    }
}

void QVncClient::onOutputDrained()
{
    if (!m_backpressureDeferred || m_state != Connected)
        return;
    const qint64 pending = static_cast<QVncWebSocketStream *>(m_clientSocket)->queuedOutputBytes();
    if (pending > 512 * 1024)
        return;
    m_backpressureDeferred = false;
    qCDebug(lcVnc) << "framebuffer resumed" << pending << "queued bytes";
    scheduleUpdate();
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
    bool wantsClipboard = false;
    int preferredPixels = -1;
    for (int i = 0; i < m_encodingsPending; ++i) {
        qint32 encoding;
        m_clientSocket->read(reinterpret_cast<char *>(&encoding), sizeof(encoding));
        encoding = qint32(ntohl(quint32(encoding)));
        if (encoding == -239) // Cursor pseudo-encoding.
            wantsCursor = true;
        if (encoding == -308) // ExtendedDesktopSize pseudo-encoding.
            wantsDesktopSize = true;
        if (quint32(encoding) == ExtendedClipboardEncoding)
            wantsClipboard = true;
        if ((encoding == 0 || encoding == 6) && preferredPixels < 0)
            preferredPixels = encoding;
        // Other entries are preference hints. Raw is mandatory in RFB.
    }
    if (wantsCursor && !m_supportCursor)
        m_server->screen()->enableClientCursor(this);
    else if (!wantsCursor && m_supportCursor) {
        m_server->screen()->disableClientCursor(this);
        m_dirtyCursor = false;
    }
    m_supportCursor = wantsCursor;
    const bool useZlib = preferredPixels == 6;
    if (m_useZlib != useZlib)
        qCDebug(lcVnc) << "framebuffer encoding" << (useZlib ? 6 : 0);
    m_useZlib = useZlib;
    m_supportDesktopSize = wantsDesktopSize;
    if (!wantsDesktopSize)
        m_sentDesktopSize = false;
    m_supportExtendedClipboard = wantsClipboard;
    if (wantsClipboard) {
        // The extension requires a fresh Caps reply for every matching
        // SetEncodings message. A zero size requests Notify/Request transfer.
        m_clientClipboardActions = ClipboardRequest | ClipboardPeek |
                                   ClipboardNotify | ClipboardProvide;
        m_clientClipboardText = true;
        sendExtendedClipboard(ClipboardCaps | ClipboardRequest | ClipboardPeek |
                              ClipboardNotify | ClipboardProvide | ClipboardText,
                              QByteArray(4, '\0'));
    } else {
        m_clientClipboardActions = 0;
        m_clientClipboardText = false;
    }
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
        scheduleUpdate();
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
    const bool validSize = QVncGeometryLimits::valid(width, height);
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
        const int oldButtons = int(m_buttonState);
        const int newButtons = int(ev.buttons);
        if (oldButtons == newButtons) {
            QWindowSystemInterface::handleMouseEvent(nullptr, pos, pos, ev.buttons,
                                                     Qt::NoButton, QEvent::MouseMove, m_keymod);
        } else {
            // A single RFB mask can release and press different buttons.
            // Qt expects one changed button and the matching intermediate mask
            // per event; release the old buttons before pressing new ones.
            for (const Qt::MouseButton button : {Qt::LeftButton, Qt::MiddleButton, Qt::RightButton}) {
                if ((oldButtons & int(button)) && !(newButtons & int(button))) {
                    m_buttonState = Qt::MouseButtons(int(m_buttonState) & ~int(button));
                    QWindowSystemInterface::handleMouseEvent(nullptr, pos, pos,
                        m_buttonState, button, QEvent::MouseButtonRelease, m_keymod);
                }
            }
            for (const Qt::MouseButton button : {Qt::LeftButton, Qt::MiddleButton, Qt::RightButton}) {
                if (!(oldButtons & int(button)) && (newButtons & int(button))) {
                    m_buttonState |= button;
                    QWindowSystemInterface::handleMouseEvent(nullptr, pos, pos,
                        m_buttonState, button, QEvent::MouseButtonPress, m_keymod);
                }
            }
        }
        if (ev.wheelMask) {
            QWindow *target = QGuiApplication::topLevelAt(pos.toPoint());
            const QPointF local = target ? target->mapFromGlobal(pos.toPoint()) : pos;
            const QPoint delta((ev.wheelMask & 0x20 ? 120 : 0) +
                               (ev.wheelMask & 0x40 ? -120 : 0),
                               (ev.wheelMask & 0x08 ? 120 : 0) +
                               (ev.wheelMask & 0x10 ? -120 : 0));
            QWindowSystemInterface::handleWheelEvent(
                target, local, pos,
                QPoint(), delta, m_keymod);
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
        if (ev.unicode || ev.keycode) {
            const bool autoRepeat = ev.down && m_heldKeys.contains(ev.keysym);
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
            // RFB distinguishes left and right keysyms, while Qt combines
            // their modifier bits. Derive the bits from held wire keys so a
            // release of one side does not clear the other side.
            m_keymod = Qt::NoModifier;
            for (const HeldKey &held : qAsConst(m_heldKeys)) {
                switch (held.keycode) {
                case Qt::Key_Shift: m_keymod |= Qt::ShiftModifier; break;
                case Qt::Key_Control: m_keymod |= Qt::ControlModifier; break;
                case Qt::Key_Alt: m_keymod |= Qt::AltModifier; break;
                case Qt::Key_Meta: m_keymod |= Qt::MetaModifier; break;
                default: break;
                }
            }
            // Keypad is a property of this wire key, not a held modifier.
            // Keep m_keymod derived only from physically held modifier keys.
            Qt::KeyboardModifiers eventModifiers = m_keymod;
            if (ev.keysym >= 0xff80 && ev.keysym <= 0xffbd)
                eventModifiers |= Qt::KeypadModifier;
#ifndef QT_NO_CONTEXTMENU
            // Like Qt's XCB backend, a Menu press delivers both the keyboard
            // context-menu request and the ordinary key event.
            if (ev.down && ev.keycode == Qt::Key_Menu) {
                if (QWindow *window = QGuiApplication::focusWindow()) {
                    const QPoint globalPos = m_lastPointerPos.toPoint();
                    QWindowSystemInterface::handleContextMenuEvent(window, false,
                        window->mapFromGlobal(globalPos), globalPos, eventModifiers);
                }
            }
#endif
            QWindowSystemInterface::handleKeyEvent(nullptr,
                ev.down ? QEvent::KeyPress : QEvent::KeyRelease,
                ev.keycode, eventModifiers, text, autoRepeat);
        }
        m_handleMsg = false;
    }
}

void QVncClient::clientCutText()
{
    QRfbClientCutText ev;

    if (m_cutTextPending == 0 && ev.read(m_clientSocket)) {
        m_cutTextExtended = (ev.length & 0x80000000u) != 0;
        const quint32 length = m_cutTextExtended ? 0u - ev.length : ev.length;
        if (length > MaxClipboardBytes || (m_cutTextExtended && length < 4)) {
            qWarning("vncws: invalid clipboard payload length");
            close();
            return;
        }
        m_cutTextPending = int(length);
        if (!m_cutTextPending) {
            m_server->setRemoteClipboardText(QString());
            m_handleMsg = false;
            return;
        }
    }

    if (m_cutTextPending && m_clientSocket->bytesAvailable() >= m_cutTextPending) {
        const QByteArray payload = m_clientSocket->read(m_cutTextPending);
        if (m_cutTextExtended)
            handleExtendedClipboard(payload);
        else
            m_server->setRemoteClipboardText(QString::fromLatin1(payload));
        m_cutTextPending = 0;
        m_cutTextExtended = false;
        m_handleMsg = false;
    }
}

void QVncClient::handleExtendedClipboard(const QByteArray &message)
{
    const auto invalid = [this]() {
        qWarning("vncws: invalid Extended Clipboard message");
        close();
    };
    if (!m_supportExtendedClipboard || message.size() < 4) {
        invalid();
        return;
    }
    const quint32 flags = qFromBigEndian<quint32>(
        reinterpret_cast<const uchar *>(message.constData()));
    const quint32 formats = flags & 0xffffu;
    const quint32 actions = flags & 0xff000000u;
    if ((flags & 0x00ff0000u) || (actions & ~ClipboardActions)) {
        invalid();
        return;
    }

    if (actions & ClipboardCaps) {
        int formatCount = 0;
        for (int bit = 0; bit < 16; ++bit)
            formatCount += (formats >> bit) & 1u;
        if (message.size() != 4 + 4 * formatCount) {
            invalid();
            return;
        }
        m_clientClipboardActions = actions & ~ClipboardCaps;
        m_clientClipboardText = (formats & ClipboardText) != 0;
        if (m_clientClipboardText &&
            !QGuiApplication::clipboard()->text().isEmpty())
            sendClipboardText(QGuiApplication::clipboard()->text());
        return;
    }

    if (message.size() != 4) {
        if (actions != ClipboardProvide) {
            invalid();
            return;
        }
    }
    switch (actions) {
    case ClipboardRequest:
        if (message.size() != 4) { invalid(); return; }
        if ((formats & ClipboardText) && m_clientClipboardText &&
            (m_clientClipboardActions & ClipboardProvide))
            sendExtendedClipboardProvide(QGuiApplication::clipboard()->text());
        return;
    case ClipboardPeek: {
        if (message.size() != 4) { invalid(); return; }
        if (!(m_clientClipboardActions & ClipboardNotify))
            return;
        QByteArray encoded;
        const bool available = encodeClipboardText(QGuiApplication::clipboard()->text(), &encoded);
        sendExtendedClipboard(ClipboardNotify | (available ? ClipboardText : 0u));
        return;
    }
    case ClipboardNotify:
        if (message.size() != 4) { invalid(); return; }
        if (formats & ClipboardText) {
            if (m_clientClipboardActions & ClipboardRequest)
                sendExtendedClipboard(ClipboardRequest | ClipboardText);
        } else {
            m_server->setRemoteClipboardText(QString());
        }
        return;
    case ClipboardProvide: {
        if (formats != ClipboardText || message.size() <= 4) { invalid(); return; }
        QString text;
        if (!decodeClipboardText(message.mid(4), &text)) { invalid(); return; }
        m_server->setRemoteClipboardText(text);
        return;
    }
    default:
        invalid();
        return;
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
