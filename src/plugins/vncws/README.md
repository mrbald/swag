# vncws Qt platform plugin

This Qt 5.15 QPA plugin adapts Qt's VNC platform plugin to serve binary RFB
through a WebSocket listener inside the Qt process. The directory builds on
its own; no SWAG application source or GCC 15 toolchain is needed.

On AlmaLinux 8, install `cmake`, a C++17 compiler, `pkgconf-pkg-config`,
`qt5-qtbase-devel`, `qt5-qtbase-private-devel`, `qt5-qtbase-static`,
`qt5-qtwebsockets-devel`, `libxkbcommon-devel`, and `zlib-devel`. Build against the same
Qt packages used by the host application:

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

Accepted parameters are `size=WxH` (each dimension 64–2048, area at most
2,073,600 pixels), `depth=32`, `port=1..65535`, and `addr=` followed by a
dotted-decimal IPv4 address. Supervised Linux launches may instead use `fd=N`
to adopt an inherited, already listening TCP socket bound to `127.0.0.1`;
`fd` cannot be combined with `addr` or `port`. The plugin restores
close-on-exec on that descriptor. Defaults are 1024x768, depth 32, port 5900,
and 127.0.0.1. Invalid, duplicate, or unknown parameters fail startup. The RFB
handshake is limited to 10 seconds. RFB 3.3 has no authentication or encryption;
expose this listener only within a trusted access boundary. The Extended
Clipboard pseudo-encoding transfers UTF-8 text through a bounded zlib payload
when the viewer supports it. Unnegotiated viewers use RFB's Latin-1 text path.
Clipboard text and compressed messages are limited to 1 MiB.

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
renegotiation. Raw remains the fallback. Framebuffer updates are coalesced for
about 16 ms and deferred while the WebSocket output queue is congested;
clipboard and size replies retain their stream order.

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
