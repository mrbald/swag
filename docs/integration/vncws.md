# Building the direct Qt plugin with a client application

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

In TCP mode the plugin serves binary WebSocket/RFB; the reverse proxy serves noVNC and
terminates TLS/authentication. The launcher supplies application identity through
the application's own contract. The plugin does not interpret a user ID or
authenticate viewers. Every WebSocket connection must be authorized and routed
to the correct private session endpoint; identity in an environment variable is
not a viewer access control. Existing open-connection revocation is an explicit
deployment policy.

Current limits: Qt 5.15/Linux raster widgets; one viewer with takeover; one
resizable screen (64..4096 per dimension, at most 4,953,600 pixels, including
native 3440x1440); 32-bit framebuffer;
RFB 3.3 with little-endian RGB888 in either tested channel order; negotiated
lossless zlib framebuffer encoding with Raw fallback;
negotiated UTF-8 clipboard with Latin-1 fallback. The RFB handshake deadline is 10 seconds. Input messages/queue
are capped at 1 MiB, output queue at 32 MiB, encoding hints and distinct held
keys at 256. Successive pixel updates are spaced by 16 ms; after idle the next
update is queued immediately. Updates pause above 1 MiB of local
transport output backlog, resuming below 512 KiB. Compressed updates are staged
under a 24 MiB cap before sending. The 32 MiB hard output cap still disconnects
an overflowing viewer; this is bounded local-transport pacing, not adaptive WAN
congestion control.

## Unix socket transport

Select `qt-unix` to run the same plugin and Qt executable over raw RFB on a
filesystem Unix socket. The browser still uses authenticated binary WebSocket;
the session manager bridges it directly to the socket, with no websockify helper
in the application path. The isolated probe uses websockify as its test bridge.

```sh
python3.12 tools/app.py build --render-backend qt-unix
python3.12 tools/app.py up --ui-mode mdi --render-backend qt-unix
# Existing image: isolated protocol and Chrome/noVNC functional checks
.venv/bin/python tools/check_vncws.py --backend qt-unix
# Running authenticated application
cmake --build --preset container --target app-unix-check
```

`socket=/absolute/path` creates a standalone listener; `fd=N` adopts an already
listening Unix or loopback TCP descriptor. Neither may be combined with `addr`
or `port`, and `socket` and `fd` are mutually exclusive. The Unix parent must
exist, have mode `0700`, belong to the current UID, and use a canonical path
without symlinks. The socket must fit Linux's pathname limit and contain neither
`:` nor `=`. Occupied paths are rejected, never removed to make startup succeed.
A new socket has mode `0600`. Standalone shutdown removes only its own inode;
an inherited listener never removes the supervisor's path.

The manager reserves `/run/swag/users/{user}/{generation}/rfb.sock`, retains the
listener through locked teardown, and passes `SWAG_QT_WS_LISTEN_FD` through the
launcher to Qt. That environment name is retained for compatibility with TCP.
Only Qt accepts connections, without calling `listen()` again. Admission checks
the generation, process identities and socket device/inode before and after
dialing; Linux `SO_PEERCRED` must identify the manager that created the listener.
Teardown closes the viewer and stops the acceptor before releasing the listener
and unlinking its matching inode. Replacement paths are preserved.

Qt has no TCP listener in this mode. Other UIDs are excluded by filesystem
permissions; this does not isolate users who share the service UID. Authentication,
Origin checks, TLS and session lifetime still belong to the manager and proxy.
The existing framing limits, clipboard, input, compression, resizing, takeover
and bounded output pacing apply to both transports. The standalone integration
example is also in the [top-level README](../../README.md).


For native Linux socket acceptance (including a real second UID):

```sh
mkdir -p artifacts/unix-native
podman run --rm --name swag-unix-acceptance --user 0 --entrypoint python3.12 \
  -v "$PWD/tests/vncws_unix.py:/tmp/check.py:ro,Z" \
  -v "$PWD/artifacts/unix-native:/artifacts:Z" \
  localhost/swag-probe-ws:dev /tmp/check.py --output /artifacts
```

The default Unix functional gate has 125 checks. The TCP/WebSocket gate has 126:
its slow-browser receiver check directly pressures Qt, while the Unix probe's
websockify bridge buffers frames. Unix backpressure is measured separately by
`paused_reader_defers_and_recovers` in the native suite; the bridge run does not
claim that result.

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
The manager reserves a loopback listening socket for each workspace generation,
passes it to the launcher using `SWAG_QT_WS_LISTEN_FD` and an inherited descriptor,
and retains its own copy until generation teardown. The launcher passes the
descriptor through the existing child-process trampoline and gives Qt
`vncws:fd=N`, then closes its copy. Qt adopts the listener; only Qt accepts
connections. The descriptor option excludes `addr` and
`port`, and requires a listening IPv4 TCP socket bound to `127.0.0.1`.

The manager checks the ready manifest against the authenticated user's current
generation, launcher/Qt PIDs and reserved port. It holds the workspace lock through
viewer connection and reservation release, so a Qt exit cannot let another
workspace reuse the port between validation and connection. A standalone launcher
without the inherited descriptor still reserves its own listener; a custom
supervisor must provide equivalent endpoint ownership for authenticated routing.
The browser cannot
select a host or port. The manager relays binary WebSocket data to Qt while
retaining Origin checks, browser heartbeats, viewer leases and takeover. The private
Qt connection has no heartbeat timeout: a temporarily blocked GUI thread can resume
without losing its viewer. Process exit still closes the connection and ends the
workspace. New connections retain the bounded startup/upgrade timeout.
No viewer port is published
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

## Raw baseline network limits

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
use. These results motivated the framebuffer compression and bounded pacing
increment below. Clipboard zlib alone does not reduce framebuffer traffic.

This is an application-stream delay/rate model with 16 KiB pacing chunks, not
packet-loss, congestion, TLS, Windows/RDP or concurrent-user qualification. The
measured Linux image is ARM64 EL8, Qt Base 5.15.3-9.el8_10, Qt WebSockets
5.15.3-1.el8 and GCC Toolset 15.2.1. Evidence and exact samples:
`artifacts/qt-ws-network/results.json`, `environment.txt` and profile screenshots.

```sh
.venv/bin/python tests/browser/network.py --samples 12 \
  --output artifacts/qt-ws-network
```

This command measures the currently deployed plugin. The retained Raw baseline
was recorded at commit `e2e0469` before framebuffer compression was added.

The harness temporarily reconfigures the local demo and recreates Alice's
workspace. Run it without concurrent demo users. It restores the original NGINX
configuration on failure too; if restoration itself fails, it retains the test
sidecar and reports that failure rather than leaving a route to a removed service.

## Framebuffer compression and pacing

The plugin implements the standard [RFB zlib encoding (6)](https://github.com/rfbproto/rfbproto/blob/master/rfbproto.rst#zlib-encoding).
It chooses the first supported encoding in the viewer's preference list (zlib or
Raw), defaulting to Raw when neither is requested. One zlib stream lasts for the
connection, including encoding withdrawal/re-enablement, pixel-format changes and
resize. Level 1 compression runs on the Qt GUI thread; no additional service or
dependency is needed beyond the zlib already used for clipboard text.

Independent wire checks decode pixels with Python's zlib and compare against Raw
for both supported RGB orders and depths. They also verify stream continuity,
maximum-size resize, fallback, fragmented negotiation and actual TCP slow-reader
recovery. The latter pauses receiving, submits 20 spaced full-frame requests and
then resumes: queued requests coalesced, the final requested area arrived and the
connection remained usable. Runtime logs independently confirm queue deferral and
resumption. Dirty regions are retained during deferral, with no queued stale frame
snapshots.

```sh
.venv/bin/python tests/vncws_compression.py --port 6116 \
  --output artifacts/qt-ws-compression/protocol
```

The same three-profile workload was repeated with the identical application
executable and noVNC client (hashes in `artifacts/qt-ws-compression/image-proof.json`).
All profiles completed without viewer disconnects or payload-forwarding errors;
the relay accounted for all closed connections before accepting byte totals.

| Added RTT; download/upload cap | Raw selection median | Zlib selection median / p95 | Raw → zlib resize paint |
| --- | --- | --- | --- |
| 0 ms; unlimited | 35 ms | 98 / 103 ms | 0.09 → 0.07 s |
| 50 ms; 20 / 5 Mbps | 917 ms | 142 / 233 ms | 3.04 → 0.27 s |
| 100 ms; 5 / 1 Mbps | 3797 ms | 244 / 386 ms | 11.71 → 0.51 s |

Total downstream traffic fell from 43–48 MiB to 0.94–0.96 MiB per profile
(97.75–98.06% reduction). Qt RSS was 66–67 MiB after selections and their CPU
cost was 0.11–0.18 seconds. The slower-link improvement is substantial; local
selection latency increased by about 63 ms with this compressed/paced path.
That regression motivated the follow-up below. These twelve-sample observations do not qualify
production tail latency, concurrent users or a real Windows/RDP network path.

Verification passed: 14 new compression/pressure wire checks, 26 existing protocol,
10 resize and 23 clipboard wire checks; 14 probe browser, 10 original MDI,
8 resize and 5 Unicode browser checks. Both the standalone copied-source build
and combined C++23 application/C++17 plugin build passed. The MDI checks exposed
an existing observer race: modal coordinates are published before queued centering.
The browser harness now waits for stable modal geometry before clicking. A relay
teardown fix treats only `ENOTCONN` from half-closing an already drained write side
as harmless; payload failures and other shutdown errors still fail the run.

Accepted evidence: `artifacts/qt-ws-compression/verified-summary.json`,
`comparison.json`, `network-final/results.json`, `mdi-final/` and the other
per-suite results. Earlier `mdi/`, `network/` and `network-diagnostic/` retain the
diagnosed harness failures and are not the accepted qualification runs.

```sh
.venv/bin/python tests/browser/network.py --samples 12 \
  --output artifacts/qt-ws-compression/network-final
```

### Local-latency follow-up

On 2026-11-02, the compressed baseline reproduced at 95.83 ms local median.
Browser WebSocket timing located most of the delay before the framebuffer arrived,
with no cursor-only updates during measured selections. Enabling Linux
`TCP_NODELAY` alone reduced the median to 51.34 ms; pointer-send-to-frame-arrival
fell from 73.89 to 29.96 ms. The plugin sets this on the listening socket through
Qt's public native descriptor; Linux copies the option to accepted connections.
Failure logs a warning and retains functional startup. No new dependency or
deployment setting is required.

The scheduler now measures the 16 ms interval from the previous emitted update,
allowing an idle update on the next event-loop turn while retaining burst pacing
and transport backpressure. The final unchanged browser workload measured:

| Added RTT; download/upload cap | Previous compressed median | New median / p95 | Resize paint |
| --- | --- | --- | --- |
| 0 ms; unlimited | 98 ms | 32 / 55 ms | 0.07 s |
| 50 ms; 20 / 5 Mbps | 142 ms | 130 / 159 ms | 0.24 s |
| 100 ms; 5 / 1 Mbps | 244 ms | 275 / 362 ms | 0.52 s |

The local regression is removed. The slowest profile's median increased while
its p95 decreased; twelve samples are insufficient to establish tail-latency
guarantees. Total downstream traffic was 1.05–1.07 MiB per profile, still about
98% below Raw, but modestly higher than the earlier compressed run. Earlier
painting can result in more separate updates. Qt RSS remained 66–67 MiB, and
selection-workload CPU use was 0.12–0.25 seconds.

All 73 focused checks passed: 26 startup, 15 compression/protocol, 14 probe browser,
10 MDI and 8 resize browser checks. The new startup check observes the option on
the listener configured by Qt and verifies an accepted connection inherits it.
Twelve tiny updates took 189 ms from first to last, confirming burst limiting;
the paused-reader recovery gate also passed. Application and noVNC hashes match
the previous compressed image, and both application and standalone plugin image
builds passed. All three network profiles passed with complete byte accounting.

Evidence: `artifacts/qt-ws-latency/verified-summary.json`, `comparison.json`,
`network-final/results.json`, `baseline-frame-timing.json`, `baseline/`, `nodelay/`
and `image-proof.json`. The retained pre-change image is
`localhost/swag-app-ws:compressed-baseline`.

## Review fixes: input ordering and endpoint ownership

The Opus 5.5 review identified four actionable defects, independently reproduced
before correction. Mouse and wheel events now use modifiers derived from held RFB
keys, including overlapping left/right modifiers, instead of Qt's delayed event
state. Horizontal wheel signs now match Qt's xcb convention.

The manager retains a listening descriptor per generation through viewer admission
and teardown. Regression checks close the renderer while leaving stale ready
metadata, pause the connection after validation, and attempt a real replacement
bind. They also cover failed launches and repeated cancellation during spawn,
child reaping and viewer teardown. The private Qt heartbeat is removed while the
browser heartbeat and leases remain. An authenticated Chrome viewer survived a
20.28-second Qt process pause, retained its original MDI frame and opened another
frame through real noVNC input after resuming.

The focused gate passed 42 Python tests on both the development host and EL8,
7 input wire checks, 26 protocol checks, 15 Chrome probe checks on each of qt-ws
and Tiger, 10 original MDI checks, 8 resize/takeover checks and 8 Linux ownership
checks, plus the authenticated GUI-stall check. The probe executable and noVNC
client are identical between renderers. The original MDI application executable
and noVNC client also match the pre-fix image. Standalone plugin and application
image builds passed. Evidence: `artifacts/qt-ws-review-fixes/`.

Initial diagnostic runs are retained separately: two wire assertions read a
transient event label overwritten by a later scroll notification; the horizontal
browser test initially tracked a cell omitted from observation when off-screen.
The final tests use retained wheel modifiers and the current cell's geometry.
Tiger's color assertion allows an eight-level per-channel tolerance for its lossy
encoding while still rejecting swapped channels; qt-ws retains exact RGB checks.
A container-runtime mount failure occurred before one browser fixture started.
None of these initial runs count as passing evidence.

These review fixes did not include a new network benchmark. Their earlier WAN
measurements remain historical evidence.

## Cursor batching and browser clipboard preflight

A requested update now includes both pending cursor and dirty pixel rectangles
in one framebuffer response. Raw and zlib retain their negotiation and stream
semantics; failed or partial writes close the connection. The existing pacing
and backpressure gates remain. Cursor rectangles have a 1 MiB wire bound and
are included in the compressed response's staging bound (12 MiB at that
checkpoint; now 24 MiB for the larger desktop envelope).

The supplied workspace and probe pages reject oversized/unsupported clipboard
text locally with an accessible error, retaining the entered text and connection.
Preflight uses the pinned noVNC UTF-8 encoder and fresh Deflator: normalized
UTF-8 including terminal NUL must fit 1 MiB, and the compressed stream plus its
four flag bytes must fit 1 MiB. Embedded NUL and malformed UTF-16 are rejected.
The same conservative policy applies before legacy fallback. Plugin-side limits
and disconnect policy remain unchanged; oversized outbound-copy feedback remained
future work. An independently embedded viewer must supply its own validation.

Standalone and combined application/plugin builds passed on aarch64 EL8,
Qt 5.15.3 and GCC 15.2.1. The original application/probe executables and noVNC
core hashes match the baseline. Functional evidence includes 151 wire/browser
suite checks, four Node boundary cases, ten checks using the pinned noVNC
compressor and two synthetic blank/oversized cursor cases. The cursor wire
test fails on the baseline and passes on the candidate with byte-exact pixels.
The standalone JavaScript checks used Node 26.8.2; run them with
`node --test tests/test_clipboard_preflight.mjs` (Node with ES-module syntax
detection is required). Browser checks use installed Chrome, not bundled engines.

The final paired network run adds 12 cursor-transition samples to the existing
12 row-selection samples per profile. Values below are baseline -> candidate
median/p95 milliseconds; this is one ordered comparison with small samples,
not a stable-tail or production latency claim.

| Profile | Row selection | Cursor transition and row paint |
| --- | --- | --- |
| Local | 33.48/53.36 -> 32.79/37.29 | 52.51/54.42 -> 52.59/53.03 |
| +50 ms RTT, 20/5 Mbps | 131.99/165.04 -> 131.69/186.11 | 166.42/185.87 -> 186.09/190.72 |
| +100 ms RTT, 5/1 Mbps | 240.88/341.41 -> 250.34/366.56 | 425.82/451.03 -> 335.50/364.66 |

All six profile runs passed with complete traffic accounting. The slow profile's
cursor-transition improvement comes with a worse row-selection result; the
50 ms cursor result also regressed. This establishes no universal speedup.
Downstream traffic was 1.71-1.74 MB baseline and 1.71-1.72 MB candidate for the
expanded workload. Candidate Qt CPU was 0.25-0.39 seconds and RSS 67,984-68,152 KiB.
Evidence: `artifacts/qt-ws-polish/verified-summary.json`, `comparison.json`,
`network-baseline/`, and `network-candidate/`.

The final two-user soak passed 1,800.35 seconds and 177 cycles: 354 clipboard
round trips, 118 reconnects and 70 viewer takeovers. Both original Qt processes
and unsaved forms survived the workload. No unexpected viewer closures or
browser errors were recorded. Final teardown removed both GUI processes and
released their reserved listener ports. Sampled RSS ended at 68,372/68,192 KiB
for Alice/Bob; kernel peak RSS was 83,268/79,216 KiB. Sampled descriptor counts
ranged from 12 to 15, with both ending at 12; CPU increased by 22.86/22.46 seconds
over the samples. This is alternating real input to two live local users,
not simultaneous-throughput or production capacity qualification. Resource
sampling is periodic, and queue evidence comes from the independent paused-reader
check rather than a continuous queue high-water measurement. Details:
`artifacts/qt-ws-polish/soak/results.json` and `verified-summary.json`.

Rejected harness runs remain separate: an initial probe port collision, stale
session observation after Start, excessive textarea line layout in a boundary
test, and a fullscreen test that pasted before ExtendedClipboard Provide.
The accepted tests use dedicated ports, refresh the observed session, preserve
the normalization boundary with fewer lines, and passively observe Provide
before sending paste keys. None of those rejected runs count as passing evidence.

## Follow-up input fixes and source transplant

A bounded local review reproduced two input defects: noVNC's `Super_L/R`
keysyms were dropped instead of becoming Qt Meta, and one RFB pointer packet
changing several buttons could leave Qt's held-button state incorrect. Both are
fixed. Pointer changes now emit individual releases before individual presses;
intermediate event order was reviewed in source, while final held state and
disconnect cleanup were observed at runtime.

The baseline failed seven focused assertions; the fixed plugin passed all 17
input checks, 26 protocol checks, 10 original MDI checks and eight resize checks.
Installed Chrome/noVNC passed 16 browser checks on each of Qt WebSocket and
TigerVNC. The browser shortcut check respects pinned noVNC's macOS mapping
(left Command to Alt, right Command to Super) and verifies an ordinary key after
release. This does not qualify Windows/RDP interception or IME/AltGr layouts.

The top-level [transplant quickstart](../../README.md#transplant-the-qt-websocket-plugin-into-your-application)
was exercised by copying the complete plugin directory into a minimal C++23 Qt
Widgets project, building the C++17 plugin with it, installing it, and observing
a successful WebSocket upgrade and RFB greeting. This native EL8/Qt 5.15.3 fixture
is not the actual client application. The application executable and noVNC core
hashes remain unchanged in the regression images.

Evidence and final nonfunctional results are under `artifacts/qt-ws-hunt/`, indexed
by `verified-summary.json` and `REVIEW.md`. The earlier 30-minute soak belongs to
the preceding checkpoint and was not repeated for these input-only changes.
The delegated session failure-path audit was blocked by an automated model
cybersecurity filter before returning a review; that coverage remains open.

## Everyday interactions and ultrawide displays

The desktop envelope now includes native 3440x1440, with an independent 4096
per-dimension limit and a 4,953,600-pixel area limit. Browser fullscreen uses the
full framebuffer; ordinary browser mode leaves space for the toolbar. Above the
envelope, the supplied viewer retains and scales the existing desktop and shows
a notice. Input, unsaved edits and return to a supported size remain functional.

The workspace clipboard panel now uses a textarea. A two-row TSV value, including
its trailing newline, passed both directions through the authenticated shell and
a Qt multiline widget. The former single-line control altered row boundaries.
Keypad Enter now arrives as Qt Enter rather than Return, with keypad metadata
attached to the individual event. Distinct Qt shortcuts and actual QListWidget
item reordering passed through noVNC on both renderers with the same probe binary.

The dense-frame acceptance test exposed and fixed a further cursor bug: an opaque
QPixmap could lose its alpha channel, causing the plugin to omit the required RFB
mask. RGB cursors now receive a complete opaque mask; alpha masks are validated
before emitting the rectangle. A 3440x1440 frame with a 495x499 opaque cursor passed
Raw/zlib pixel comparison and paused-reader recovery, with actual queue defer and
resume evidence. Existing alpha-cursor checks remain part of the regression gate.

Full evidence, relevant final nonfunctional results, rejected harness runs and
the next bounded review candidates are indexed under `artifacts/qt-ws-batch1/`.
These are native EL8 and installed macOS Chrome observations, not qualification
of the client's Windows/RDP path. Clean runtime packaging remains a separate task.

## Outbound clipboard availability

A real noVNC reproduction copied 10,000 characters successfully, then grew the
selection inside Qt to 1,280,000 characters using copy/paste. Previously the
browser retained an older copy without warning. The plugin now sends the standard
Extended Clipboard Notify-without-Text signal when it cannot transfer the text.
The supplied pages clear the stale value, explain that the clipboard is unavailable
or exceeds transfer limits, and recover on the next successful copy. The limit
stays 1 MiB; text is not truncated or replaced with an error message as clipboard data.

Pinned noVNC 1.7.0 ignores that availability signal upstream, so the image build
applies `tools/patch_novnc.py`. It verifies the exact upstream `rfb.js` hash before
adding a `clipboardunavailable` event and clearing the client's cached clipboard.
Changed upstream source fails the build for review. Both renderer comparisons use
the same patched client. An independently embedded viewer needs equivalent handling;
legacy clients cannot express this state and retain silent omission.

The isolated probe and authenticated shell both pass small/oversized/recovered
copy sequences. The 126-check functional gate, 16 Tiger reference checks and
26 original-app MDI/workspace/Unicode clipboard checks pass, including ordinary
empty clipboard behavior. Evidence: `artifacts/qt-ws-followups/clipboard-verified.json`.
The original application and probe binaries are unchanged; the plugin, noVNC hook
and HTML shell changed. The four-frame demo was restored afterward.

## Clean-runtime transplant qualification

The installed probe and plugin passed on a fresh pinned AlmaLinux 8.10 aarch64
image containing explicit runtime RPMs, without inheriting the build image's layers
or environment. Qt base/gui `5.15.3-9.el8_10` and WebSockets `5.15.3-1.el8` match
the build. The image contains no compiler, CMake/Ninja, development/static packages,
X server or window manager, and no `LD_LIBRARY_PATH` override. Distribution Qt GUI
still pulls in X11/OpenGL client libraries. These probe/plugin binaries resolve
against the system libstdc++; a different application may need additional runtime
libraries from its own toolchain.

Artifact hashes match the qualified source image. `ldd` has no unresolved libraries
and `/proc/1/maps` confirms the plugin is loaded. Four real noVNC checks pass:
first frame, keyboard edit, clipboard roundtrip and Context Menu/Escape/typing.
The initial screenshot was visually reviewed, including Japanese fallback glyphs.
Evidence: `artifacts/qt-ws-followups/runtime/verified.json`; reproduce with
[`tests/runtime/README.md`](../../tests/runtime/README.md). This proves the local
installed-artifact path, not the unavailable client application's dependencies.

## Client qualification

Repeated key-down messages now retain Qt's autorepeat flag. This honors
`QShortcut::setAutoRepeat(false)` while repeating shortcuts and ordinary text
still repeat. A release followed by a press is a new action; disconnect/replacement
clears held-key state. The baseline fired a non-repeating action three times;
the fixed plugin fired once and matched Tiger in the real noVNC fixture.
Evidence: `artifacts/qt-ws-slices/repeat-browser-*`, `repeat-wire`, `repeat-smoke`
and `repeat-mdi` (65 passing checks). RFB carries no explicit repeat flag, so
clients sending release/press pairs retain their distinct-press semantics.

The Context Menu key now maps to `Qt::Key_Menu` and delivers the separate keyboard
context-menu event, following [Qt's XCB backend](https://raw.githubusercontent.com/qt/qtbase/v5.15.2/src/plugins/platforms/xcb/qxcbkeyboard.cpp).
Mapping alone delivered the key but did not open the editor menu. The full fix
passes actual noVNC popup, Escape and subsequent typing checks against Tiger,
22 wire input checks, 16 probe smoke checks and 10 original-app MDI checks.
Evidence: `artifacts/qt-ws-slices/menu-verified.json`. The earlier Tiger autorepeat
comparison passed, but a later run intermittently classified duplicated key-downs
as fresh presses; this reference limitation remains recorded in the evidence.

The supplied EL8 image now includes `google-noto-sans-cjk-jp-fonts`
(`20230817-1.el8_10.noarch` in this run). With the probe's default DejaVu Sans 10,
Qt previously produced two missing glyphs for `東京`; it now falls back to
Noto Sans CJK JP with two nonzero glyphs. The same probe/noVNC client visibly
renders both characters through vncws and Tiger. The glyph diagnostic and 38
browser checks passed; evidence is `artifacts/qt-ws-slices/fonts-verified.json`.
Qualification used font-package overlays on the verified images; the shared
Containerfile records the dependency for subsequent full builds. The original
application executable is unchanged. This covers the demonstrated Japanese
fallback gap; client font selection, other scripts and IME still need validation.

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
