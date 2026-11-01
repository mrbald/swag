# Building the direct WebSocket plugin with a client application

Copy `src/plugins/vncws/` as a directory and build it against the client's Qt
development packages. Its standalone CMake build requires CMake 3.16+, C++17,
Qt 5.15 Core/Gui/Network/WebSockets, Qt's matching private headers and static
platform-support libraries, pkg-config and libxkbcommon. It does not build SWAG
or require SWAG's GCC 15/C++23 configuration. See the plugin README for EL8
package names and the preserved upstream/SWAG license boundaries.

```sh
cmake -S vncws -B vncws-build -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_PREFIX_PATH=/path/to/client/qt
cmake --build vncws-build --parallel
cmake --install vncws-build --prefix /opt/vncws
QT_PLUGIN_PATH=/opt/vncws/plugins \
QT_FORCE_STDERR_LOGGING=1 \
QT_QPA_PLATFORM=vncws:size=1280x800:port=5900 \
  /path/to/client-application
```

The default listener is IPv4 loopback. Add `addr=0.0.0.0` only when an external
access boundary, such as the probe's loopback-published container ports, supplies
the intended isolation. Qt logging can otherwise go to the system journal on
EL8; `QT_FORCE_STDERR_LOGGING=1` directs startup errors to the captured log.
Invalid configuration and bind failures terminate startup.

The plugin serves binary WebSocket/RFB only; the reverse proxy serves noVNC and
terminates TLS/authentication. The launcher supplies application identity through
the application's own contract. The plugin does not interpret a user ID or
authenticate viewers. Every WebSocket connection must be authorized and routed
to the correct private session endpoint; identity in an environment variable is
not a viewer access control. Existing open-connection revocation is an explicit
deployment policy.

Current limits: Qt 5.15/Linux raster widgets; one viewer with takeover; fixed
screen (64..2048 per dimension, at most 2,073,600 pixels); 32-bit framebuffer;
RFB 3.3 with little-endian RGB888 in either tested channel order; Raw encoding;
Latin-1 clipboard. The RFB handshake deadline is 10 seconds. Input messages/queue
are capped at 1 MiB, output queue at 16 MiB, encoding hints and distinct held
keys at 256. Slow viewers exceeding the output cap are disconnected; adaptive
backpressure/compression remains future work.

## Verification retained in this repository

The image's `probe-ws` stage copies only the plugin directory and builds it as a
standalone project. All browser actions use real noVNC input; Qt state observation
reports outcomes and geometry only. Protocol fault probes inspect bytes and
survival independently of the implementation.

```sh
python3.12 tools/probe.py build --backend qt-ws
.venv/bin/python tests/vncws_protocol.py
.venv/bin/python tests/browser/smoke.py --backend qt-ws --port 6106
.venv/bin/python tests/browser/vncws_lifecycle.py --port 6112
podman run --rm --name swag-ws-startup-check --ulimit core=0:0 \
  --entrypoint python3.12 \
  -v "$PWD/tests/vncws_startup.py:/tmp/check.py:ro" \
  localhost/swag-probe-ws:dev /tmp/check.py
```

On 2026-11-01: 14 startup, 26 wire-protocol, 14 browser and 3 lifecycle checks
passed. Evidence: `artifacts/qt-ws-hardening/verified-summary.json`, `startup.json`,
`final-protocol/`, `final-browser/`, and `verified-lifecycle/`. The browser tests
include correct RGB color, Ctrl release on takeover/disconnect and recovery from
disconnect during a drag. Startup checks verify loopback binding and immediate
failure for bad options or an occupied port. Wire checks include fragmented and
coalesced messages, clipped/empty requests, invalid formats, cursor withdrawal,
viewer replacement and handshake expiry. This is not a fuzzing/security audit.

## Original authenticated MDI application

The optional application image runs the original `swag-desktop` executable and
shared bank service through the existing Keycloak/OAuth2 Proxy/NGINX path:

```sh
python3.12 tools/app.py build --render-backend qt-ws
python3.12 tools/app.py up --ui-mode mdi --render-backend qt-ws
.venv/bin/python tests/browser/mdi.py --render-backend qt-ws \
  --output artifacts/qt-ws-mdi/mdi
.venv/bin/python tests/browser/workspace.py --render-backend qt-ws \
  --output artifacts/qt-ws-mdi/workspace
.venv/bin/python tests/browser/login_state.py
.venv/bin/python tests/linux_ownership.py --output artifacts/qt-ws-mdi/ownership
PYTHONPATH=python .venv/bin/python -m unittest tests.test_launcher tests.test_sessions -q
```

The browser entry point is `http://localhost:6081`, with synthetic logins
`alice / demo` and `bob / demo`. The `up` command recreates the demo stack and ends
existing workspaces; bank data persists. To return to TigerVNC, run
`python3.12 tools/app.py up --ui-mode mdi`.

`SWAG_RENDER_BACKEND=qt-ws` requires MDI mode without an external window manager.
The launcher reserves a loopback listening socket, passes its descriptor through
the existing child-process trampoline, and gives Qt `vncws:fd=N`. Qt adopts that
listener; the parent closes its copy. The descriptor option excludes `addr` and
`port`, and requires a listening IPv4 TCP socket bound to `127.0.0.1`.

The manager resolves the internal port from the ready manifest matching the
authenticated user's current generation and launcher/Qt PIDs. The browser cannot
select a host or port. The manager relays binary WebSocket data to Qt while
retaining Origin checks, viewer leases and takeover. No viewer port is published
from the application container. Qt is the only GUI child: no Xvnc, Openbox or
vncconfig. This remains shared-UID process ownership, not OS isolation between
users. The new renderer keeps a fixed 1280x800 framebuffer with browser scaling;
TigerVNC's dynamic remote resize is not available in this mode.

On 2026-11-01 the Qt WebSocket application path passed 10 MDI, 11 two-user
workspace, 5 login-recovery and 8 Linux-ownership checks, plus 22 focused Python
unit tests. The expanded plugin startup suite passed 25 checks in both the probe
and application images, including inherited-FD validation and a real
WebSocket/RFB greeting after the production exec trampoline and parent close.
The inherited descriptor has close-on-exec restored inside Qt. Both application
images contain the identical `swag-desktop` executable; the image's Python files
match the reviewed sources. Evidence is under `artifacts/qt-ws-mdi/`, including
`image-proof.json`, screenshots and per-suite results/logs.
The same 10 MDI checks also passed on the TigerVNC fallback, using that identical
Qt executable; see `artifacts/qt-ws-mdi/tiger-mdi/`.

The client's exact Qt/OS/architecture and actual application remain unverified.
Native X11 handles, OpenGL/Qt Quick/WebEngine, IME, dynamic resizing, Unicode
clipboard and WAN/concurrency envelopes are not qualified. TigerVNC remains the
default and fallback renderer.
