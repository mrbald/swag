# vncws Qt platform plugin

This Qt 5.15 QPA plugin adapts Qt's VNC platform plugin to serve binary RFB
through a WebSocket listener or a Linux filesystem Unix-domain socket inside
the Qt process. The directory builds on
its own; no SWAG application source or GCC 15 toolchain is needed.

On AlmaLinux 8, install `cmake`, a C++17 compiler, `pkgconf-pkg-config`,
`qt5-qtbase-devel`, `qt5-qtbase-private-devel`, `qt5-qtbase-static`,
`qt5-qtwebsockets-devel`, `libxkbcommon-devel`, and `zlib-devel`. Build against the same
Qt packages used by the host application. QtNetwork must include `QLocalSocket`
(the `localserver` feature), also when selecting TCP at runtime:

```sh
cp -a src/plugins/vncws /tmp/vncws
cmake -S /tmp/vncws -B /tmp/vncws-build -DCMAKE_BUILD_TYPE=Release
cmake --build /tmp/vncws-build --parallel
cmake --install /tmp/vncws-build --prefix /opt/vncws
QT_PLUGIN_PATH=/opt/vncws/plugins \
  QT_QPA_PLATFORM=vncws:size=1280x800:port=5900 \
  /path/to/qt5-application
```

The installed plugin is `/opt/vncws/plugins/platforms/libqvncws.so`. Connect a
binary noVNC client to `ws://127.0.0.1:5900/`. For a container with an
explicitly controlled published port, add `addr=0.0.0.0` to the platform
string. Default bind address is `127.0.0.1`. One viewer is supported; a new
viewer takes over the connection.

Accepted parameters are `size=WxH` (each dimension 64–4096, area at most
4,953,600 pixels, including native 3440×1440), `depth=32`, `port=1..65535`, and `addr=` followed by a
dotted-decimal IPv4 address. Supervised Linux launches may instead use `fd=N`
to adopt an inherited, already listening TCP socket bound to `127.0.0.1` or a
filesystem Unix stream socket. The descriptor's family selects WebSocket/TCP
or raw RFB/Unix transport; `fd` cannot be combined with `addr`, `port` or `socket`.
The plugin restores
close-on-exec on that descriptor. Defaults are 1024x768, depth 32, port 5900,
and 127.0.0.1. Invalid, duplicate, or unknown parameters fail startup. The RFB
handshake is limited to 10 seconds. RFB 3.3 has no authentication or encryption;
expose this listener only within a trusted access boundary. The Extended
Clipboard pseudo-encoding transfers UTF-8 text through a bounded zlib payload
when the viewer supports it. Unnegotiated viewers use RFB's Latin-1 text path.
Clipboard text and compressed messages are limited to 1 MiB.
Glyph rendering uses fonts installed with the Qt application, regardless of
the browser's fonts. The supplied EL8 images include
`google-noto-sans-cjk-jp-fonts` for Japanese fallback; include appropriate fonts
in the client application's runtime image as well. Unicode clipboard support
does not establish font coverage or IME support.
The supplied SWAG browser pages validate normalized UTF-8 and the pinned noVNC
compressed payload before sending. Rejected text remains editable with a visible
error. The plugin still disconnects invalid/oversized inbound protocol messages;
oversized outbound text is not transferred. Extended Clipboard peers receive
the standard Notify-without-Text signal when text is unavailable. The supplied
noVNC client applies `tools/patch_novnc.py` to the pinned 1.7.0 source so the shell
can clear stale text and display an explanation; a later valid copy clears it.
Unpatched noVNC ignores that notification. Legacy viewers cannot report this
availability state and retain the previous silent-omission behavior.

For standalone Linux Unix-socket mode, provision `/run/client-app` for the
application UID with mode `0700`, then create a fresh private directory per start:

```sh
session_dir=$(mktemp -d /run/client-app/session.XXXXXX)
QT_PLUGIN_PATH=/opt/vncws/plugins \
  QT_QPA_PLATFORM="vncws:size=1280x800:socket=$session_dir/rfb.sock" \
  /path/to/qt5-application
```

This serves **raw RFB**, not WebSocket frames, and opens no TCP listener. The
browser still connects to an authenticated WebSocket gateway; that gateway forwards
RFB bytes to the Unix socket. The socket has mode `0600`; its immediate parent
must already have mode `0700` and belong to the application UID. Paths must be
absolute, canonical, shorter than 108 encoded bytes and contain neither `:` nor
`=` (Qt platform parameter delimiters). Abstract sockets and symlink traversal
are rejected. `socket` is mutually exclusive with `addr`, `port` and `fd`.

An occupied pathname is never removed, including a stale socket after a crash.
Standalone cleanup requires normal Qt application teardown (including destruction
of the QGuiApplication/QApplication). SIGTERM with its default action, SIGKILL,
`_exit` and crashes bypass that cleanup. Restarting on the same occupied path
fails safely. Use a fresh private generation directory per start, and have the
supervisor clean its own abandoned generation only after the process is dead.
The plugin does not install signal handlers in the client application.
A supervisor using `fd=N` owns pathname creation and cleanup: the
plugin closes its descriptor but never unlinks the inherited pathname. The
inherited socket and parent must have the same ownership/modes described above.
Keep listener reservation and generation cleanup under supervisor control.
These permissions restrict other UIDs; they do not isolate processes sharing
the application UID, or root.

The accepted client pixel formats are 32 bits per pixel, depth 24 or 32,
little-endian true color with 8-bit red/green/blue channels and shifts either
16/8/0 or 0/8/16. The latter is noVNC's default and is converted before
encoding.

Clients advertising ExtendedDesktopSize (`-308`) receive a one-screen layout
descriptor in response to each nonincremental framebuffer request. A
SetDesktopSize (`251`) request can resize that screen within the same size
limits as startup. Unsupported dimensions or screen layouts receive an RFB
status reply without disconnecting. The Qt process and its windows remain in
place. The viewer may prefer standard zlib encoding (`6`) over Raw (`0`);
one zlib stream is kept for the connection across resize and encoding
renegotiation. Raw remains the fallback. Pending pixels and cursor rectangles
share one framebuffer response when both are ready.
Cursor rectangles are bounded to 1 MiB including header, pixels and mask.
Pixel/cursor updates are spaced by at least 16 ms; the first update after idle runs on the next event-loop
turn. Updates are deferred while the transport output queue is congested;
clipboard and size replies retain their stream order. On Linux, the TCP listener
enables TCP_NODELAY for accepted connections to avoid delaying small frames.
Compressed responses are staged under a 24 MiB cap; each transport's output queue
is capped at 32 MiB. These bounds include the larger framebuffer while preserving
the 1 MiB defer / 512 KiB resume thresholds. The supplied workspace viewer scales
the existing desktop and displays a notice above the supported geometry limit.
Keypad Enter is delivered as Qt Enter with event-local keypad metadata, distinct
from ordinary Return. The workspace clipboard panel preserves multiline text.

The files `main.cpp`, `qvnc.cpp`, `qvnc_p.h`, `qvncclient.cpp`,
`qvncclient.h`, `qvncintegration.cpp`, `qvncintegration.h`,
`qvncscreen.cpp`, `qvncscreen.h`, and `vnc.json` began from the official
Qt Base v5.15.2 tag at
`https://github.com/qt/qtbase/tree/v5.15.2/src/plugins/platforms/vnc`.
The source archive SHA-256 is
`30f6d86c574b0787d1c44f67b987d746b595f8c00ca2e659cf884728afa20023`.
The Qt-authored files retain their upstream copyright headers and LGPLv3
licensing; see `LICENSE.LGPL3` and its incorporated `LICENSE.GPL3`. New SWAG
files and additions are under AGPLv3; see `LICENSE.SWAG`. This does not
relicense the Qt-authored source. The private QPA APIs require a matching Qt
build for deployment.
