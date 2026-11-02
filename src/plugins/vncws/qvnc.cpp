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
#include "qvnc_p.h"
#include <QtCore/QBuffer>
#include "qvncscreen.h"
#include "qvncclient.h"
#include "qvncwebsocketstream.h"
#include <QtCore/QIODevice>
#include <QtGui/QClipboard>
#include <QtGui/QGuiApplication>
#include <QtNetwork/QHostAddress>
#include <QtWebSockets/QWebSocket>
#include <QtWebSockets/QWebSocketServer>
#include <qendian.h>
#include <qthread.h>
#include <cerrno>
#include <cstring>
#include <xkbcommon/xkbcommon.h>

#include <QtGui/qguiapplication.h>
#include <QtGui/QWindow>

#ifdef Q_OS_WIN
#include <Winsock2.h>
#else
#include <arpa/inet.h>
#endif
#ifdef Q_OS_LINUX
#include <netinet/tcp.h>
#include <sys/socket.h>
#endif

#include <QtCore/QDebug>
#include <new>

QT_BEGIN_NAMESPACE

Q_LOGGING_CATEGORY(lcVnc, "qt.qpa.vnc");

QVncDirtyMap::QVncDirtyMap(QVncScreen *screen, const QSize &size)
    : screen(screen), bytesPerPixel(0), numDirty(0), map(nullptr), buffer(nullptr)
{
    bytesPerPixel = (screen->depth() + 7) / 8;
    bufferWidth = size.width();
    bufferHeight = size.height();
    bufferStride = bufferWidth * bytesPerPixel;
    buffer = new (std::nothrow) uchar[bufferHeight * bufferStride];

    mapWidth = (bufferWidth + MAP_TILE_SIZE - 1) / MAP_TILE_SIZE;
    mapHeight = (bufferHeight + MAP_TILE_SIZE - 1) / MAP_TILE_SIZE;
    numTiles = mapWidth * mapHeight;
    map = new (std::nothrow) uchar[numTiles];
}

QVncDirtyMap::~QVncDirtyMap()
{
    delete[] map;
    delete[] buffer;
}

void QVncDirtyMap::reset()
{
    memset(map, 1, numTiles);
    memset(buffer, 0, bufferHeight * bufferStride);
    numDirty = numTiles;
}

inline bool QVncDirtyMap::dirty(int x, int y) const
{
    return map[y * mapWidth + x];
}

inline void QVncDirtyMap::setClean(int x, int y)
{
    map[y * mapWidth + x] = 0;
    --numDirty;
}

template <class T>
void QVncDirtyMapOptimized<T>::setDirty(int tileX, int tileY, bool force)
{
    static bool alwaysForce = qEnvironmentVariableIsSet("QT_VNC_NO_COMPAREBUFFER");
    if (alwaysForce)
        force = true;

    bool changed = false;

    if (!force) {
        const int lstep = bufferStride;
        const int startX = tileX * MAP_TILE_SIZE;
        const int startY = tileY * MAP_TILE_SIZE;
        const uchar *scrn = screen->image()->constBits()
                            + startY * lstep + startX * bytesPerPixel;
        uchar *old = buffer + startY * bufferStride + startX * sizeof(T);

        const int tileHeight = (startY + MAP_TILE_SIZE > bufferHeight ?
                                bufferHeight - startY : MAP_TILE_SIZE);
        const int tileWidth = (startX + MAP_TILE_SIZE > bufferWidth ?
                               bufferWidth - startX : MAP_TILE_SIZE);
        const bool doInlines = (tileWidth == MAP_TILE_SIZE);

        int y = tileHeight;

        if (doInlines) { // hw: memcmp/memcpy is inlined when using constants
            while (y) {
                if (memcmp(old, scrn, sizeof(T) * MAP_TILE_SIZE)) {
                    changed = true;
                    break;
                }
                scrn += lstep;
                old += bufferStride;
                --y;
            }

            while (y) {
                memcpy(old, scrn, sizeof(T) * MAP_TILE_SIZE);
                scrn += lstep;
                old += bufferStride;
                --y;
            }
        } else {
            while (y) {
                if (memcmp(old, scrn, sizeof(T) * tileWidth)) {
                    changed = true;
                    break;
                }
                scrn += lstep;
                old += bufferStride;
                --y;
            }

            while (y) {
                memcpy(old, scrn, sizeof(T) * tileWidth);
                scrn += lstep;
                old += bufferStride;
                --y;
            }
        }
    }

    const int mapIndex = tileY * mapWidth + tileX;
    if ((force || changed) && !map[mapIndex]) {
        map[mapIndex] = 1;
        ++numDirty;
    }
}

template class QVncDirtyMapOptimized<unsigned char>;
template class QVncDirtyMapOptimized<unsigned short>;
template class QVncDirtyMapOptimized<unsigned int>;

static const struct {
    int keysym;
    int keycode;
} keyMap[] = {
    { 0xff08, Qt::Key_Backspace },
    { 0xff09, Qt::Key_Tab       },
    { 0xff0d, Qt::Key_Return    },
    { 0xff1b, Qt::Key_Escape    },
    { 0xff63, Qt::Key_Insert    },
    { 0xffff, Qt::Key_Delete    },
    { 0xff50, Qt::Key_Home      },
    { 0xff57, Qt::Key_End       },
    { 0xff55, Qt::Key_PageUp    },
    { 0xff56, Qt::Key_PageDown  },
    { 0xff51, Qt::Key_Left      },
    { 0xff52, Qt::Key_Up        },
    { 0xff53, Qt::Key_Right     },
    { 0xff54, Qt::Key_Down      },
    { 0xffbe, Qt::Key_F1        },
    { 0xffbf, Qt::Key_F2        },
    { 0xffc0, Qt::Key_F3        },
    { 0xffc1, Qt::Key_F4        },
    { 0xffc2, Qt::Key_F5        },
    { 0xffc3, Qt::Key_F6        },
    { 0xffc4, Qt::Key_F7        },
    { 0xffc5, Qt::Key_F8        },
    { 0xffc6, Qt::Key_F9        },
    { 0xffc7, Qt::Key_F10       },
    { 0xffc8, Qt::Key_F11       },
    { 0xffc9, Qt::Key_F12       },
    { 0xffe1, Qt::Key_Shift     },
    { 0xffe2, Qt::Key_Shift     },
    { 0xffe3, Qt::Key_Control   },
    { 0xffe4, Qt::Key_Control   },
    { 0xffe7, Qt::Key_Meta      },
    { 0xffe8, Qt::Key_Meta      },
    { 0xffeb, Qt::Key_Meta      }, // noVNC Super_L
    { 0xffec, Qt::Key_Meta      }, // noVNC Super_R
    { 0xffe9, Qt::Key_Alt       },
    { 0xffea, Qt::Key_Alt       },

    { 0xffb0, Qt::Key_0         },
    { 0xffb1, Qt::Key_1         },
    { 0xffb2, Qt::Key_2         },
    { 0xffb3, Qt::Key_3         },
    { 0xffb4, Qt::Key_4         },
    { 0xffb5, Qt::Key_5         },
    { 0xffb6, Qt::Key_6         },
    { 0xffb7, Qt::Key_7         },
    { 0xffb8, Qt::Key_8         },
    { 0xffb9, Qt::Key_9         },

    { 0xff8d, Qt::Key_Enter     },
    { 0xffaa, Qt::Key_Asterisk  },
    { 0xffab, Qt::Key_Plus      },
    { 0xffad, Qt::Key_Minus     },
    { 0xffae, Qt::Key_Period    },
    { 0xffaf, Qt::Key_Slash     },

    { 0xff95, Qt::Key_Home      },
    { 0xff96, Qt::Key_Left      },
    { 0xff97, Qt::Key_Up        },
    { 0xff98, Qt::Key_Right     },
    { 0xff99, Qt::Key_Down      },
    { 0xff9a, Qt::Key_PageUp    },
    { 0xff9b, Qt::Key_PageDown  },
    { 0xff9c, Qt::Key_End       },
    { 0xff9e, Qt::Key_Insert    },
    { 0xff9f, Qt::Key_Delete    },

    { 0, 0 }
};

void QRfbRect::read(QIODevice *s)
{
    quint16 buf[4];
    s->read((char*)buf, 8);
    x = ntohs(buf[0]);
    y = ntohs(buf[1]);
    w = ntohs(buf[2]);
    h = ntohs(buf[3]);
}

bool QRfbRect::write(QIODevice *s) const
{
    quint16 buf[4];
    buf[0] = htons(x);
    buf[1] = htons(y);
    buf[2] = htons(w);
    buf[3] = htons(h);
    return s->write((char*)buf, 8) == 8;
}

void QRfbPixelFormat::read(QIODevice *s)
{
    uchar buf[16];
    s->read(reinterpret_cast<char *>(buf), sizeof(buf));
    bitsPerPixel = buf[0];
    depth = buf[1];
    bigEndian = buf[2];
    trueColor = buf[3];

    redMax = qFromBigEndian<quint16>(buf + 4);
    greenMax = qFromBigEndian<quint16>(buf + 6);
    blueMax = qFromBigEndian<quint16>(buf + 8);
    quint16 a = redMax;
    redBits = 0;
    while (a) { a >>= 1; redBits++; }

    a = greenMax;
    greenBits = 0;
    while (a) { a >>= 1; greenBits++; }

    a = blueMax;
    blueBits = 0;
    while (a) { a >>= 1; blueBits++; }

    redShift = buf[10];
    greenShift = buf[11];
    blueShift = buf[12];
}

void QRfbPixelFormat::write(QIODevice *s)
{
    uchar buf[16] = {};
    buf[0] = bitsPerPixel;
    buf[1] = depth;
    buf[2] = bigEndian;
    buf[3] = trueColor;

    quint16 a = 0;
    for (int i = 0; i < redBits; i++) a = (a << 1) | 1;
    qToBigEndian<quint16>(a, buf + 4);

    a = 0;
    for (int i = 0; i < greenBits; i++) a = (a << 1) | 1;
    qToBigEndian<quint16>(a, buf + 6);

    a = 0;
    for (int i = 0; i < blueBits; i++) a = (a << 1) | 1;
    qToBigEndian<quint16>(a, buf + 8);

    buf[10] = redShift;
    buf[11] = greenShift;
    buf[12] = blueShift;
    s->write(reinterpret_cast<const char *>(buf), sizeof(buf));
}


void QRfbServerInit::setName(const char *n)
{
    delete[] name;
    name = new char [strlen(n) + 1];
    strcpy(name, n);
}

void QRfbServerInit::read(QIODevice *s)
{
    s->read((char *)&width, 2);
    width = ntohs(width);
    s->read((char *)&height, 2);
    height = ntohs(height);
    format.read(s);

    quint32 len;
    s->read((char *)&len, 4);
    len = ntohl(len);

    name = new char [len + 1];
    s->read(name, len);
    name[len] = '\0';
}

void QRfbServerInit::write(QIODevice *s)
{
    quint16 t = htons(width);
    s->write((char *)&t, 2);
    t = htons(height);
    s->write((char *)&t, 2);
    format.write(s);
    quint32 len = strlen(name);
    len = htonl(len);
    s->write((char *)&len, 4);
    s->write(name, strlen(name));
}

bool QRfbSetEncodings::read(QIODevice *s)
{
    if (s->bytesAvailable() < 3)
        return false;

    char tmp;
    s->read(&tmp, 1);        // padding
    s->read((char *)&count, 2);
    count = ntohs(count);

    return true;
}

bool QRfbFrameBufferUpdateRequest::read(QIODevice *s)
{
    if (s->bytesAvailable() < 9)
        return false;

    s->read(&incremental, 1);
    rect.read(s);

    return true;
}

bool QRfbKeyEvent::read(QIODevice *s)
{
    if (s->bytesAvailable() < 7)
        return false;

    s->read(&down, 1);
    quint16 tmp;
    s->read((char *)&tmp, 2);  // padding

    quint32 key;
    s->read((char *)&key, 4);
    key = ntohl(key);
    keysym = key;

    unicode = 0;
    keycode = 0;
    int i = 0;
    while (keyMap[i].keysym && !keycode) {
        if (keyMap[i].keysym == (int)key)
            keycode = keyMap[i].keycode;
        i++;
    }

    if (keycode >= ' ' && keycode <= '~')
        unicode = keycode;

    if (!keycode) {
        const quint32 codepoint = xkb_keysym_to_utf32(key);
        if (codepoint <= 0x10ffffU &&
            !(codepoint >= 0xd800U && codepoint <= 0xdfffU)) {
            unicode = int(codepoint);
            if (codepoint >= 'a' && codepoint <= 'z')
                keycode = Qt::Key_A + int(codepoint - 'a');
            else if (codepoint >= ' ' && codepoint <= '~')
                keycode = Qt::Key_Space + int(codepoint - ' ');
        }
    }

    return true;
}

bool QRfbPointerEvent::read(QIODevice *s)
{
    if (s->bytesAvailable() < 5)
        return false;

    char buttonMask;
    s->read(&buttonMask, 1);
    rawButtons = quint8(buttonMask);
    wheelMask = quint8(buttonMask) & 0x78;
    buttons = Qt::NoButton;
    if (buttonMask & 1)
        buttons |= Qt::LeftButton;
    if (buttonMask & 2)
        buttons |= Qt::MiddleButton;
    if (buttonMask & 4)
        buttons |= Qt::RightButton;

    quint16 tmp;
    s->read((char *)&tmp, 2);
    x = ntohs(tmp);
    s->read((char *)&tmp, 2);
    y = ntohs(tmp);

    return true;
}

bool QRfbClientCutText::read(QIODevice *s)
{
    if (s->bytesAvailable() < 7)
        return false;

    char tmp[3];
    s->read(tmp, 3);        // padding
    s->read((char *)&length, 4);
    length = ntohl(length);

    return true;
}

QRfbRawEncoder::~QRfbRawEncoder()
{
    if (zlibInitialized)
        deflateEnd(&zlibStream);
}

bool QRfbRawEncoder::writeZlib(const QImage &screenImage, const QRegion &region,
                              bool includeCursor)
{
    constexpr int MaxStagedBytes = 24 * 1024 * 1024;
    try {
        if (!zlibInitialized) {
            if (deflateInit(&zlibStream, Z_BEST_SPEED) != Z_OK) {
                qWarning("vncws: cannot initialize framebuffer zlib stream");
                client->close();
                return false;
            }
            zlibInitialized = true;
        }

        QByteArray packet(4, '\0');
        qToBigEndian(quint16(region.rectCount() + int(includeCursor)),
                    reinterpret_cast<uchar *>(packet.data() + 2));
        for (const QRect &rect : region) {
            const int rowBytes = rect.width() * client->clientBytesPerPixel();
            const int pixelBytes = rowBytes * rect.height();
            QByteArray pixels(pixelBytes, '\0');
            for (int row = 0; row < rect.height(); ++row) {
                const char *source = reinterpret_cast<const char *>(screenImage.constScanLine(rect.y() + row))
                                     + rect.x() * screenImage.depth() / 8;
                char *destination = pixels.data() + row * rowBytes;
                if (client->doPixelConversion())
                    client->convertPixels(destination, source, rect.width());
                else
                    memcpy(destination, source, size_t(rowBytes));
            }

            zlibStream.next_in = reinterpret_cast<Bytef *>(pixels.data());
            zlibStream.avail_in = uInt(pixelBytes);
            QByteArray compressed(int(deflateBound(&zlibStream, uLong(pixelBytes)) + 6), '\0');
            zlibStream.next_out = reinterpret_cast<Bytef *>(compressed.data());
            zlibStream.avail_out = uInt(compressed.size());
            const int status = deflate(&zlibStream, Z_SYNC_FLUSH);
            if (status != Z_OK || zlibStream.avail_in != 0 || zlibStream.avail_out == 0) {
                qWarning("vncws: framebuffer zlib compression failed");
                client->close();
                return false;
            }
            compressed.resize(compressed.size() - int(zlibStream.avail_out));
            if (compressed.size() > MaxStagedBytes - packet.size() - 16) {
                qWarning("vncws: compressed framebuffer update exceeds staging limit");
                client->close();
                return false;
            }

            char header[16] = {};
            qToBigEndian(quint16(rect.x()), reinterpret_cast<uchar *>(header));
            qToBigEndian(quint16(rect.y()), reinterpret_cast<uchar *>(header + 2));
            qToBigEndian(quint16(rect.width()), reinterpret_cast<uchar *>(header + 4));
            qToBigEndian(quint16(rect.height()), reinterpret_cast<uchar *>(header + 6));
            qToBigEndian(quint32(6), reinterpret_cast<uchar *>(header + 8));
            qToBigEndian(quint32(compressed.size()), reinterpret_cast<uchar *>(header + 12));
            packet.append(header, 16);
            packet.append(compressed);
        }
#if QT_CONFIG(cursor)
        if (includeCursor) {
            const qint64 cursorBytes = client->server()->screen()->clientCursor->wireBytes(client);
            if (cursorBytes < 0 || cursorBytes > MaxStagedBytes - packet.size()) {
                qWarning("vncws: cursor exceeds framebuffer staging limit");
                client->close();
                return false;
            }
            QBuffer cursorBuffer(&packet);
            if (!cursorBuffer.open(QIODevice::Append) ||
                !client->server()->screen()->clientCursor->writeRect(client, &cursorBuffer)) {
                qWarning("vncws: cannot stage cursor in framebuffer update");
                client->close();
                return false;
            }
        }
#endif
        if (client->clientSocket()->write(packet) != packet.size()) {
            client->close();
            return false;
        }
        return true;
    } catch (const std::bad_alloc &) {
        qWarning("vncws: cannot allocate framebuffer update");
        client->close();
        return false;
    }
}

bool QRfbRawEncoder::write(bool includeCursor)
{
//    QVncDirtyMap *map = server->dirtyMap();
    QIODevice *socket = client->clientSocket();

    const int bytesPerPixel = client->clientBytesPerPixel();

    // create a region from the dirty rects and send the region's merged rects.
    // ### use the tile map again
    const QImage screenImage = client->server()->screenImage();
    QRegion rgn = client->dirtyRegion();
    rgn &= QRect(0, 0, screenImage.width(), screenImage.height());
    if (rgn.rectCount() > (includeCursor ? 4095 : 4096))
        rgn = rgn.boundingRect();
    qCDebug(lcVnc) << "QRfbRawEncoder::write()" << rgn;
    if (client->useZlib()) {
        return writeZlib(screenImage, rgn, includeCursor);
    }
//    if (map) {
//        for (int y = 0; y < map->mapHeight; ++y) {
//            for (int x = 0; x < map->mapWidth; ++x) {
//                if (!map->dirty(x, y))
//                    continue;
//                rgn += QRect(x * MAP_TILE_SIZE, y * MAP_TILE_SIZE,
//                             MAP_TILE_SIZE, MAP_TILE_SIZE);
//                map->setClean(x, y);
//            }
//        }

//        rgn &= QRect(0, 0, server->screen()->geometry().width(),
//                     server->screen()->geometry().height());
//    }

    const auto rectsInRegion = rgn.rectCount();

    {
        const char tmp[2] = { 0, 0 }; // msg type, padding
        if (socket->write(tmp, sizeof(tmp)) != sizeof(tmp)) {
            client->close();
            return false;
        }
    }

    {
        const quint16 count = htons(quint16(rectsInRegion + int(includeCursor)));
        if (socket->write((char *)&count, sizeof(count)) != sizeof(count)) {
            client->close();
            return false;
        }
    }

    for (const QRect &tileRect: rgn) {
        const QRfbRect rect(tileRect.x(), tileRect.y(),
                            tileRect.width(), tileRect.height());
        if (!rect.write(socket)) {
            client->close();
            return false;
        }

        const quint32 encoding = htonl(0); // raw encoding
        if (socket->write((char *)&encoding, sizeof(encoding)) != sizeof(encoding)) {
            client->close();
            return false;
        }

        int linestep = screenImage.bytesPerLine();
        const uchar *screendata = screenImage.scanLine(rect.y)
                                  + rect.x * screenImage.depth() / 8;

        if (client->doPixelConversion()) {
            const int bufferSize = rect.w * rect.h * bytesPerPixel;
            if (bufferSize > buffer.size())
                buffer.resize(bufferSize);

            // convert pixels
            char *b = buffer.data();
            const int bstep = rect.w * bytesPerPixel;
            for (int i = 0; i < rect.h; ++i) {
                client->convertPixels(b, (const char*)screendata, rect.w);
                screendata += linestep;
                b += bstep;
            }
            if (socket->write(buffer.constData(), bufferSize) != bufferSize) {
                client->close();
                return false;
            }
        } else {
            for (int i = 0; i < rect.h; ++i) {
                const int rowBytes = rect.w * bytesPerPixel;
                if (socket->write((const char*)screendata, rowBytes) != rowBytes) {
                    client->close();
                    return false;
                }
                screendata += linestep;
            }
        }
    }
#if QT_CONFIG(cursor)
    if (includeCursor &&
        !client->server()->screen()->clientCursor->writeRect(client, socket)) {
        client->close();
        return false;
    }
#endif
    return true;
}

#if QT_CONFIG(cursor)
QVncClientCursor::QVncClientCursor()
{
    QWindow *w = QGuiApplication::focusWindow();
    QCursor c = w ? w->cursor() : QCursor(Qt::ArrowCursor);
    changeCursor(&c, nullptr);
}

QVncClientCursor::~QVncClientCursor()
{
}

bool QVncClientCursor::writeUpdate(QVncClient *client) const
{
    if (wireBytes(client) < 0)
        return false;
    QIODevice *socket = client->clientSocket();
    const char header[4] = {0, 0, 0, 1};
    return socket->write(header, sizeof(header)) == sizeof(header) &&
           writeRect(client, socket);
}

qint64 QVncClientCursor::wireBytes(const QVncClient *client) const
{
    constexpr qint64 MaxCursorWireBytes = 1024 * 1024;
    const qint64 width = cursor.width();
    const qint64 height = cursor.height();
    if (width < 0 || height < 0 || width > 65535 || height > 65535)
        return -1;
    const qint64 payloadBytes = width * height * client->clientBytesPerPixel() +
                                ((width + 7) / 8) * height;
    if (payloadBytes > MaxCursorWireBytes - 12)
        return -1;
    return 12 + payloadBytes;
}

bool QVncClientCursor::writeRect(QVncClient *client, QIODevice *socket) const
{
    if (wireBytes(client) < 0)
        return false;
    const qint64 width = cursor.width();
    const qint64 height = cursor.height();
    const bool empty = cursor.isNull();
    const QImage img = empty ? QImage() : cursor.convertToFormat(client->server()->screen()->format());
    if (!empty && (img.isNull() || img.size() != cursor.size()))
        return false;
    const int maskWidth = (int(width) + 7) / 8;
    const bool hasAlpha = !empty && cursor.hasAlphaChannel();
    const QImage bitmap = hasAlpha
            ? cursor.createAlphaMask().convertToFormat(QImage::Format_Mono) : QImage();
    if (hasAlpha && (bitmap.isNull() || bitmap.depth() != 1 || bitmap.size() != img.size()))
        return false;
    // Qt can strip the alpha channel from an entirely opaque QPixmap.
    const QByteArray opaqueMask = !empty && !hasAlpha
            ? QByteArray(maskWidth, char(0xff)) : QByteArray();
    char rect[12];
    qToBigEndian(quint16(hotspot.x()), reinterpret_cast<uchar *>(rect));
    qToBigEndian(quint16(hotspot.y()), reinterpret_cast<uchar *>(rect + 2));
    qToBigEndian(quint16(width), reinterpret_cast<uchar *>(rect + 4));
    qToBigEndian(quint16(height), reinterpret_cast<uchar *>(rect + 6));
    qToBigEndian(quint32(-239), reinterpret_cast<uchar *>(rect + 8));
    if (socket->write(rect, sizeof(rect)) != sizeof(rect))
        return false;

    if (empty)
        return true;

    // write pixels
    const int n = client->clientBytesPerPixel() * img.width();
    char *buffer = new char[n];
    for (int i = 0; i < img.height(); ++i) {
        client->convertPixels(buffer, (const char*)img.scanLine(i), img.width());
        if (socket->write(buffer, n) != n) {
            delete[] buffer;
            return false;
        }
    }
    delete[] buffer;

    // write mask
    if (!hasAlpha) {
        for (int i = 0; i < img.height(); ++i) {
            if (socket->write(opaqueMask.constData(), maskWidth) != maskWidth)
                return false;
        }
    } else {
        for (int i = 0; i < bitmap.height(); ++i) {
            if (socket->write((const char*)bitmap.scanLine(i), maskWidth) != maskWidth)
                return false;
        }
    }
    return true;
}

void QVncClientCursor::changeCursor(QCursor *widgetCursor, QWindow *window)
{
    Q_UNUSED(window);
    const Qt::CursorShape shape = widgetCursor ? widgetCursor->shape() : Qt::ArrowCursor;

    if (shape == Qt::BitmapCursor) {
        // application supplied cursor
        hotspot = widgetCursor->hotSpot();
        cursor = widgetCursor->pixmap().toImage();
    } else {
        // system cursor
        QPlatformCursorImage platformImage(nullptr, nullptr, 0, 0, 0, 0);
        platformImage.set(shape);
        cursor = *platformImage.image();
        hotspot = platformImage.hotspot();
    }
    for (auto client : qAsConst(clients))
        client->setDirtyCursor();
}

void QVncClientCursor::addClient(QVncClient *client)
{
    if (!clients.contains(client)) {
        clients.append(client);
        // Force a cursor update when the client connects.
        client->setDirtyCursor();
    }
}

uint QVncClientCursor::removeClient(QVncClient *client)
{
    clients.removeOne(client);
    return clients.count();
}
#endif // QT_CONFIG(cursor)

QVncServer::QVncServer(QVncScreen *screen, const QHostAddress &address,
                       quint16 port, int inheritedFd)
    : qvnc_screen(screen)
    , m_address(address)
    , m_port(port)
    , m_inheritedFd(inheritedFd)
{
    QMetaObject::invokeMethod(this, "init", Qt::QueuedConnection);
}

void QVncServer::init()
{
    serverSocket = new QWebSocketServer(QStringLiteral("SWAG VNCWS"),
                                        QWebSocketServer::NonSecureMode, this);
    if (m_inheritedFd >= 0) {
        if (!serverSocket->setNativeDescriptor(qintptr(m_inheritedFd)))
            qFatal("vncws: cannot adopt listening fd %d: %s", m_inheritedFd,
                   qPrintable(serverSocket->errorString()));
    } else if (!serverSocket->listen(m_address, m_port)) {
        qFatal("vncws: cannot listen on %s:%u: %s",
               qPrintable(m_address.toString()), unsigned(m_port),
               qPrintable(serverSocket->errorString()));
    }
#ifdef Q_OS_LINUX
    // QWebSocket has no public socket option API in Qt 5.15. Linux copies this
    // TCP option from the listening socket to each accepted connection.
    const int noDelay = 1;
    if (::setsockopt(int(serverSocket->nativeDescriptor()), IPPROTO_TCP,
                     TCP_NODELAY, &noDelay, sizeof(noDelay)) != 0) {
        qWarning("vncws: cannot enable TCP_NODELAY: %s", strerror(errno));
    } else {
        qCDebug(lcVnc) << "TCP_NODELAY enabled on listener";
    }
#endif
    qInfo("vncws: WebSocket listening on %s:%u",
          qPrintable(m_address.toString()), unsigned(m_port));

    connect(serverSocket, &QWebSocketServer::newConnection,
            this, &QVncServer::newConnection);
    connect(QGuiApplication::clipboard(), &QClipboard::dataChanged, this,
            [this]() {
        if (!m_remoteClipboardChange)
            sendClipboardText(QGuiApplication::clipboard()->text());
    });

}

QVncServer::~QVncServer()
{
    qDeleteAll(clients);
}

void QVncServer::setDirty()
{
    for (auto client : qAsConst(clients))
        client->setDirty(qvnc_screen->dirtyRegion);

    qvnc_screen->clearDirty();
}


void QVncServer::newConnection()
{
    auto *socket = serverSocket->nextPendingConnection();
    const auto previousClients = clients;
    for (auto *previous : previousClients)
        previous->close();
    auto *stream = new QVncWebSocketStream(socket);
    clients.append(new QVncClient(stream, this));

    dirtyMap()->reset();

    qCDebug(lcVnc) << "new WebSocket connection from: " << socket->peerAddress();

    qvnc_screen->setPowerState(QPlatformScreen::PowerStateOn);
}

void QVncServer::discardClient(QVncClient *client)
{
    clients.removeOne(client);
    qvnc_screen->disableClientCursor(client);
    client->deleteLater();
    if (clients.isEmpty()) {
        qvnc_screen->setPowerState(QPlatformScreen::PowerStateOff);
    }
}

void QVncServer::setRemoteClipboardText(const QString &text)
{
    m_remoteClipboardChange = true;
    QGuiApplication::clipboard()->setText(text);
    m_remoteClipboardChange = false;
}

void QVncServer::sendClipboardText(const QString &text)
{
    for (auto *client : qAsConst(clients)) {
        client->sendClipboardText(text);
    }
}

inline QImage QVncServer::screenImage() const
{
    return *qvnc_screen->image();
}

QT_END_NAMESPACE
