# Building the direct WebSocket plugin with a client application

Copy `src/plugins/vncws/` as a directory and build it against the client's Qt
development packages. Its standalone CMake build requires CMake 3.16+, C++17,
Qt 5.15 Core/Gui/Network/WebSockets, Qt's matching private headers and static
platform-support libraries, pkg-config, libxkbcommon and zlib. It does not build SWAG
or require SWAG's GCC 15/C++23 configuration. See the plugin README for EL8
package names and the preserved upstream/SWAG license boundaries.

For the client's deployment, these sources live alongside the application and the
built plugin ships in its RPM. The user owns the separate infrastructure package
and site-specific NGINX/OAuth2 Proxy configuration. The container commands below
exercise equivalent application, process and proxy boundaries; they do not require
a container-based production deployment. RPM manufacturing is outside this work.

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

Current limits: Qt 5.15/Linux raster widgets; one viewer with takeover; one
resizable screen (64..2048 per dimension, at most 2,073,600 pixels); 32-bit framebuffer;
RFB 3.3 with little-endian RGB888 in either tested channel order; Raw encoding;
negotiated UTF-8 clipboard with Latin-1 fallback. The RFB handshake deadline is 10 seconds. Input messages/queue
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
users. The renderer starts at 1280x800 and negotiates remote resizing with noVNC.
The Qt screen and application resize to the browser without replacing the process.
Below the browser shell's usable-size threshold, or above the plugin's size cap,
the existing framebuffer is scaled locally. Unsupported layouts are rejected
without disconnecting the viewer.

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

The subsequent autoresize gate passed 10 independent resize protocol cases and
8 authenticated browser cases, including rapid resize, rejected oversize requests,
small-window scaling, open dialogs, dirty forms, reconnect and viewer takeover.
The original MDI suite passed 10 checks and existing protocol regressions passed
26 checks. Evidence: `artifacts/qt-ws-resize/`. Reproduce the new gates with:

```sh
.venv/bin/python tests/vncws_resize.py --port 6114
.venv/bin/python tests/browser/resize.py --render-backend qt-ws \
  --output artifacts/qt-ws-resize/browser
```

The Unicode clipboard gate passed 23 independent protocol checks, 5 authenticated
MDI browser checks and all 14 existing probe browser checks. It covers multilingual
text in both directions, empty clipboard, reconnect, legacy Latin-1 clients,
asymmetric capabilities and malformed/oversized compressed payloads. Extended
Clipboard uses bounded zlib streams: compressed payload and expanded text are
each limited to 1 MiB. This compresses clipboard text, not framebuffer updates.
The combined C++23 application/C++17 plugin source build also passed with GCC 15
and the same EL8 Qt packages. Evidence: `artifacts/qt-ws-clipboard/`.

```sh
.venv/bin/python tests/vncws_clipboard.py
.venv/bin/python tests/browser/clipboard.py
```

Browser checks use the viewer's clipboard panel and real Qt copy/paste shortcuts.
Native Windows clipboard permissions, remote-desktop forwarding, installed font
coverage and IME behavior still need client-side checks.

## Measured network limits

On 2026-11-01, the original authenticated MDI app completed three synthetic
network profiles without disconnects. A test-only TCP relay between NGINX and
the session manager added delay and per-connection directional byte-rate caps;
authentication, viewer leases and the Qt WebSocket relay stayed in the path.
All connection records drained before byte totals were accepted, and the test
restored the normal NGINX route afterward.

| Added RTT; download/upload cap | Selection median / p95 | Resize paint |
| --- | --- | --- |
| 0 ms; unlimited | 35 / 40 ms | 0.09 s |
| 50 ms; 20 / 5 Mbps | 917 / 1112 ms | 3.04 s |
| 100 ms; 5 / 1 Mbps | 3797 / 4270 ms | 11.71 s |

Each profile measured 12 alternating row selections using real noVNC mouse input.
Timing starts before Playwright's click command and ends when the selected-row
pixel appears; it includes automation overhead and 20 ms polling. Qt state
independently confirms the selected record afterward. Resize timing observes
the new bottom-right pixel after growing the browser from 1440x1040 to 1680x1100.
These small-sample percentiles describe this workload, not a service guarantee.

Qt RSS after selection was 67–68 MiB; the selection workload used 0.09–0.64 CPU
seconds across the profiles. Total downstream traffic was 43–48 MiB per profile,
including connection setup, warmup, selections and resize, plus session/lease
HTTP and viewer WebSocket overhead. Authentication and static assets are outside
these byte totals. Raw framebuffer traffic is the clear next optimization target:
the slower profiles remain functional but are too sluggish for ordinary interactive
use. Framebuffer compression and bounded update coalescing/pacing should precede
WAN acceptance. Clipboard zlib does not reduce framebuffer traffic.

This is an application-stream delay/rate model with 16 KiB pacing chunks, not
packet-loss, congestion, TLS, Windows/RDP or concurrent-user qualification. The
measured Linux image is ARM64 EL8, Qt Base 5.15.3-9.el8_10, Qt WebSockets
5.15.3-1.el8 and GCC Toolset 15.2.1. Evidence and exact samples:
`artifacts/qt-ws-network/results.json`, `environment.txt` and profile screenshots.

```sh
.venv/bin/python tests/browser/network.py --samples 12 \
  --output artifacts/qt-ws-network
```

The harness temporarily reconfigures the local demo and recreates Alice's
workspace. Run it without concurrent demo users. It restores the original NGINX
configuration on failure too; if restoration itself fails, it retains the test
sidecar and reports that failure rather than leaving a route to a removed service.

## Client qualification

The client reports RHEL 8 with distribution Qt 5, C++23 and GCC Toolset 15.
The browser client is understood to be Chrome on a Windows VM, probably in Azure;
Azure hosting is unconfirmed. This is close to the tested EL8 stack, but the exact
Qt RPM versions, Linux CPU architecture and actual client application remain
unverified. The plugin currently requires Qt 5.15 and matching private development
packages; its C++17 target can be built alongside the C++23 application.

Capture the target package versions and architecture before the client build:

```sh
uname -m
rpm -q qt5-qtbase qt5-qtbase-devel qt5-qtbase-private-devel \
  qt5-qtbase-static qt5-qtwebsockets-devel
```

Chrome's host OS does not change the Linux plugin build. Qualification must cover
the actual browser-to-server network path and Windows display scaling/keyboard
behavior. If the Windows VM is accessed through remote desktop, also check its
additional clipboard and keyboard path.

Native X11 handles, OpenGL/Qt Quick/WebEngine, IME and WAN/concurrency
envelopes are not qualified. TigerVNC remains the
default and fallback renderer.
