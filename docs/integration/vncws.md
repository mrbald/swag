# Integrate the direct Qt plugin

Choose a transport and a process owner before following a recipe. The plugin
renders Linux Qt Widgets directly; it does not serve HTML, authenticate a viewer,
or decide which user owns an application process.

| You need | Follow | Browser-to-Qt path |
| --- | --- | --- |
| One local application, simplest smoke test | [Standalone WebSocket](#standalone-websocket) | noVNC → Qt WebSocket listener |
| One local application with a filesystem access boundary | [Standalone Unix socket](#unix-socket-transport) | noVNC → WebSocket bridge → raw-RFB Unix socket → Qt |
| Your supervisor creates and retains the endpoint | [Inherited descriptor](#inherited-descriptor) | Either path above, with a parent-owned listener |
| Authenticated per-user workspaces using this repository | [Reuse the session stack](#reuse-the-session-stack) | noVNC → authenticated manager → private Qt endpoint |
| Existing top-level windows, native X11 dependencies or a window manager | [TigerVNC porting path](README.md) | noVNC → manager → Xvnc/X11 → Qt/xcb |

Both direct backends use the **same tuned renderer**: zlib compression, update
pacing, bounded queues and cursor batching. The original tuning was measured on
`qt-ws`; `qt-unix` retains it and has its own passing latency/network checks.
Choose `qt-unix` for a private same-host endpoint when using the managed bridge;
this is a filesystem access-control choice, **not a measured performance win**
over `qt-ws`. There is no controlled benchmark establishing a transport winner.
See the [measured qualification history](../qualification/vncws.md) for workloads
and limits. TigerVNC is the compatibility/default path for the other requirements.

**Building and loading this plugin needs no application code change.** Reusing our
session manager also requires the application's identity, lifetime-lock and
readiness contracts. MDI is a requirement of this repository's direct-Qt launcher,
not an automatic transformation performed by the plugin. An arbitrary client
application still needs its own window/dialog qualification.

## Build and install

All manual application recipes below run on the **Linux application host**, as
the application service UID. Replace `/opt/client-app` and the executable name
with your installation. Use a matching Qt 5.15 build: the plugin depends on private
QPA headers and static platform-support libraries, so Qt version/ABI compatibility
must be established against the actual client build.

Requirements: CMake 3.16+, C++17 compiler, Qt5 Core/Gui/Network/WebSockets,
QtNetwork `localserver` support (`QLocalSocket`), matching private Qt headers and
platform-support archives, pkg-config, libxkbcommon and zlib. On the tested EL8
build the development packages are `qt5-qtbase-devel`, `qt5-qtbase-private-devel`,
`qt5-qtbase-static`, `qt5-qtwebsockets-devel`, `libxkbcommon-devel`, `zlib-devel`
and `pkgconf-pkg-config`. Use your distribution's enabled repositories; do not
mix private headers/static archives from a different Qt build.

Copy the **whole** `src/plugins/vncws/` directory, including license files, into
the application repository as `third_party/vncws`. Choose one build method:

| Build method | Configuration |
| --- | --- |
| Existing CMake project | Add `add_subdirectory(third_party/vncws)` after `project(... LANGUAGES CXX)`. Build the normal application and target `qvncws`. |
| Standalone plugin project | Run the commands below from the application repository. |

```sh
cmake -S third_party/vncws -B build/vncws -DCMAKE_BUILD_TYPE=Release \
  -DCMAKE_INSTALL_PREFIX=/opt/client-app
cmake --build build/vncws --parallel
cmake --install build/vncws
```

For a non-system Qt, add `-DCMAKE_PREFIX_PATH=/path/to/client/qt` when configuring.
Installation writes `/opt/client-app/plugins/platforms/libqvncws.so`; the install
step needs permission to write that prefix. The application can keep its C++23
configuration while the plugin target compiles as C++17. No link against
`qvncws` or call in `main()` is needed: Qt loads it through `QT_QPA_PLATFORM` and
`QT_PLUGIN_PATH`.

Ship the module in the **application RPM**, with the application's matching Qt
runtime libraries, fonts and other dependencies. The supervisor, proxy and site
configuration belong to the separate infrastructure deployment. This guide does
not manufacture either RPM or require containers in the client deployment.
[The clean-runtime fixture](../../tests/runtime/README.md) supplies a tested
runtime package reference and verification procedure, not a universal package list.

## Prepare the local viewer

The standalone and inherited-descriptor recipes use the same fixed-size test
viewer. These are **unauthenticated, loopback-only local checks**; use the
[session-stack recipe](#reuse-the-session-stack) for authenticated workspaces.

From this repository root, on the Linux application host, install Python 3.12 with venv/pip,
`curl`, `tar`, `patch` and `sha256sum`. Fetch the exact noVNC release used by our images:

```sh
(
  set -eu
  mkdir -p artifacts/client-viewer
  curl --fail --location \
    https://codeload.github.com/novnc/noVNC/tar.gz/refs/tags/v1.7.0 \
    --output artifacts/novnc-1.7.0.tar.gz
  echo 'b1003a11b6e6e8d8f7f5e5586daae7f8ca651d8aee0aa155ff9ac841c48f52c6  artifacts/novnc-1.7.0.tar.gz' | sha256sum --check -
  tar -xzf artifacts/novnc-1.7.0.tar.gz --strip-components=1 -C artifacts/client-viewer
  python3.12 tools/patch_novnc.py artifacts/client-viewer/core/rfb.js
  cp web/probe.html web/clipboard.js artifacts/client-viewer/
)
```

The upstream JavaScript is readable, non-minified ES modules; no bundling or npm
build is needed. Our [tracked unified patch](../../patches/novnc/0001-clipboard-unavailable.patch)
is applied by the standard `patch` tool through a checksum-checking wrapper.
[Patch maintenance and manual application](../../patches/novnc/README.md) describe
the exact inputs and expected output.

Extract a fresh upstream tree before reapplying the patch: the wrapper deliberately rejects
an already-patched or different `rfb.js`. The page and `clipboard.js` are both
needed for clipboard limits and unavailable-copy feedback. Copy the resulting
viewer directory to your local test host if preparing it elsewhere. An embedded
viewer needs equivalent `clipboardunavailable` handling; copying only the plugin
does not change an existing browser client.

The test page uses a 1280×800 framebuffer and disables remote resizing. The
production [workspace shell](../../web/workspace.html) enables negotiated resizing,
clipboard controls, session recovery and fullscreen; it also requires the session
API. Do not substitute it for the standalone page without providing that API.

## Standalone WebSocket

1. In terminal A, start the client application:

   ```sh
   QT_PLUGIN_PATH=/opt/client-app/plugins \
   QT_QPA_PLATFORM=vncws:size=1280x800:port=5900 \
   QT_FORCE_STDERR_LOGGING=1 /opt/client-app/bin/client-app
   ```

2. In terminal B, from the repository root, serve the prepared viewer:

   ```sh
   python3.12 -m http.server 6080 --bind 127.0.0.1 \
     --directory artifacts/client-viewer
   ```

3. Open **http://127.0.0.1:6080/probe.html?wsPort=5900**. Expect `Connected`,
   application pixels, working typing and clipboard transfer through the panel.
   Port 6080 serves HTML; port 5900 speaks binary WebSocket/RFB. No websockify
   bridge is used in this pattern.

Quit the application normally and stop the HTTP server with Ctrl-C. A viewer
reload reconnects to the same live process; it does not restart the application.
For a browser on another machine, keep these listeners private and forward both
ports with `ssh -N -L 6080:127.0.0.1:6080 -L 5900:127.0.0.1:5900 user@app-host`,
then open the same browser URL on that machine. That tunnel is a local-test
convenience, not a multi-user session router.

## Unix socket transport

A Unix listener carries **raw RFB**, not WebSocket frames. An ordinary HTTP
WebSocket reverse proxy cannot simply forward an upgraded connection to it;
a WebSocket-to-byte-stream bridge must be present.

1. Provision a canonical directory owned by the application account. As an
   administrator, replace `APPUSER`/`APPGROUP` with that account and run:

   ```sh
   sudo install -d -m 0700 -o APPUSER -g APPGROUP /run/client-app
   ```

   Recreate it after a host reboot; `/run` is temporary. Then run the following
   **as the application UID** in terminal A:

   ```sh
   session_dir=$(mktemp -d /run/client-app/session.XXXXXX)
   printf 'Socket for terminal B: %s/rfb.sock\n' "$session_dir"
   QT_PLUGIN_PATH=/opt/client-app/plugins \
   QT_QPA_PLATFORM="vncws:size=1280x800:socket=$session_dir/rfb.sock" \
   QT_FORCE_STDERR_LOGGING=1 /opt/client-app/bin/client-app
   ```

2. In terminal B, **as the same application UID**, from the repository root,
   prepare the local bridge once. Another UID cannot traverse the private socket
   directory. Use the same hash-verified wheel as the container images:

   ```sh
   (
     set -eu
     python3.12 -m venv artifacts/viewer-venv
     curl --fail --location \
       https://files.pythonhosted.org/packages/19/e8/6476cd85a71f7cfb04ca1e199809b75bc8580d075d4338ffcde4c761d620/websockify-0.13.0-py3-none-any.whl \
       --output artifacts/websockify-0.13.0-py3-none-any.whl
     echo 'ac497a8dafc7f51d28ca989f01cdf5f8add663a497d425a56b159dcb8d6a314c  artifacts/websockify-0.13.0-py3-none-any.whl' | sha256sum --check -
     artifacts/viewer-venv/bin/python -m pip install --no-deps \
       artifacts/websockify-0.13.0-py3-none-any.whl
   )
   ```

   Set `rfb_socket` to the **actual path printed by terminal A**, then serve the
   viewer and bridge together (no separate HTTP server on port 6080):

   ```sh
   rfb_socket=/run/client-app/session.REPLACE/rfb.sock
   artifacts/viewer-venv/bin/python -m websockify \
     --web artifacts/client-viewer --unix-target "$rfb_socket" 127.0.0.1:6080
   ```

3. Open **http://127.0.0.1:6080/probe.html**; there is no `wsPort` override.
   Expect `Connected`, application pixels, typing and bidirectional clipboard.
   `stat -c '%a %U %n' "$rfb_socket"` should show mode `600` and the application
   owner. Qt opens no TCP listener; websockify owns the local browser port.
   For a remote browser, forward **only** port 6080 using SSH as above.

Quit Qt normally, stop the bridge with Ctrl-C, then `rmdir "$session_dir"` in
terminal A. Normal Qt teardown removes its own socket inode. Default SIGTERM,
SIGKILL, `_exit` and crashes bypass that cleanup and leave the socket behind.
Restarting on the same path is refused. A supervisor must track its owned inode
and clean its abandoned generation only after process death; do not blindly
remove an occupied socket. Use a fresh directory per start, or managed `fd=N`.

The socket parent must be canonical, owned by the current UID and mode `0700`.
The full socket path must fit 107 encoded bytes on Linux and contain no `:` or
`=`. Abstract sockets, symlink traversal and occupied paths are rejected. Mode
`0600` excludes other UIDs; processes sharing the service UID remain trusted.
The probe bridge buffers frames; it is a connectivity example, not the bounded
production bridge or a backpressure qualification.

## Inherited descriptor

Use `fd=N` when the parent owns the endpoint. The descriptor must actually be
inherited by the child; putting an arbitrary number in the environment is not
sufficient. It must already be a listening loopback IPv4 TCP socket or a private
filesystem Unix socket. `fd`, `socket`, `addr` and `port` are mutually exclusive
as described in the [parameter reference](../../src/plugins/vncws/README.md).

[The runnable stdlib example](examples/inherited_listener.py) demonstrates binding,
`pass_fds`, listener retention and cleanup on Linux. It does **not** implement
authentication, per-user admission, application readiness or recovery. Its JSON
output reports an endpoint reservation, not proof that Qt initialized. Production
integration should use the contracts below and the actual manager/launcher code.
For Unix, first provision `/run/client-app` as in Unix step 1, and run both the
example and its bridge as that application UID.

```sh
# Terminal A: TCP, then use the HTTP server and URL from Standalone WebSocket.
python3.12 docs/integration/examples/inherited_listener.py \
  --transport tcp --port 5900 --plugin-path /opt/client-app/plugins \
  -- /opt/client-app/bin/client-app

# Alternative terminal A: Unix; use the printed socket with the Unix bridge recipe.
python3.12 docs/integration/examples/inherited_listener.py \
  --transport unix --runtime-dir /run/client-app \
  --plugin-path /opt/client-app/plugins -- /opt/client-app/bin/client-app
```

Use the Qt Quit action or Ctrl-C in terminal A, then stop the viewer server/bridge.
The example also cleans up on SIGTERM, terminal hangup (SIGHUP), and SIGQUIT.
The example retains the listener while stopping/reaping the child and removes
only its matching Unix socket. Parent SIGKILL and helpers escaping the child's
process group are outside this demonstration; it is not a replacement for a
production process supervisor.

For a custom supervisor, preserve this sequence:

1. Bind an endpoint for one authenticated user/generation and retain the listener.
2. Pass its descriptor explicitly through every exec boundary; launch Qt with
   `QT_QPA_PLATFORM=vncws:size=1280x800:fd=N`. Only Qt accepts connections.
3. Authorize each browser connection against that generation. TCP relays binary
   WebSocket to Qt; Unix decodes browser WebSocket to raw RFB and applies bounded
   flow control in both directions.
4. Serialize admission and teardown. Retain the listener until viewer closure
   and child teardown finish; remove only the owned Unix inode. Do not reuse a
   generation or recreate Qt implicitly on reconnect.

Our manager additionally validates manifest identity and Unix device/inode before
and after dialing. Linux `SO_PEERCRED` must identify the manager that called
`listen()`; the child must not call `listen()` again, because it changes that
credential. The compatibility environment name `SWAG_QT_WS_LISTEN_FD` is used for
both TCP and Unix descriptors. These are contracts of **our manager**; a different
supervisor must establish equivalent ownership rather than copy only `fd=N`.

## Reuse the session stack

This is the complete repository demo, with Keycloak/OAuth2 Proxy/NGINX and the
manager's direct Unix bridge. It needs a running native Linux Podman engine (or
Podman VM), host Python 3.12, `uv`, CMake ≥3.25, Ninja, and installed Google Chrome
for browser checks. Run from the repository root:

```sh
uv sync --locked
cmake --preset container
python3.12 tools/app.py build --render-backend qt-unix
python3.12 tools/app.py up --ui-mode mdi --render-backend qt-unix
```

Open **http://localhost:6081**; log in as **alice / demo** or **bob / demo** and
start a workspace. `up` recreates the demo stack and ends active workspaces while
preserving bank data. Use separate browser profiles for two users. For TCP mode,
replace `qt-unix` with `qt-ws` in both commands. Both modes use MDI and no external
window manager. The default `tools/app.py up` remains TigerVNC; mode selection
must be explicit.

To replace the demo executable, follow the [application porting contracts](README.md#application-contracts-when-reusing-this-manager):
change the launcher executable, supply immutable identity, acquire its lifetime
lock **before QApplication**, and publish atomic readiness JSON. The remaining
observation fields are for testing. Do not copy the demo's bank/database API into
an unrelated client application merely to satisfy the renderer.

`SWAG_RUNTIME_DIR` for `qt-unix` must be absolute/canonical, without symlinks,
`:` or `=` and at most **26 encoded bytes on Linux**. The remaining 81 bytes are
reserved for the longest username and generation suffix. This manager-root limit
is distinct from the plugin's 107-byte full-path limit. The manager rejects an
invalid configuration before launching workspaces.

The browser cannot select an arbitrary backend host/path. The production boundary
is the authenticated manager: [server.py](../../python/swag_session/server.py),
[launcher.py](../../python/swag_session/launcher.py), and
[processes.py](../../python/swag_session/processes.py). For site-specific
NGINX/OIDC deployment, preserve the [HTTP/session contract](../interfaces.md) and
[proxy template](../../containers/nginx.conf.in); the synthetic Keycloak mapping
is not an Entra qualification. TLS, identity mapping and infrastructure packaging
remain site responsibilities.

## Verify the selected pattern

For a copied client application, first verify pixels, typing, clipboard and Quit
using its actual executable and the selected local recipe. Then exercise its
real dialogs, reconnect/takeover, resize, lifetime and identity behavior through
[the application acceptance checklist](README.md#porting-gate). Our probe tests
do not discover or qualify arbitrary application widgets.

For repository regression checks, **build each image the gate uses**. From the
repository root, with the host dependencies above installed:

```sh
uv sync --locked
# Isolated probe image, separate from the application image:
python3.12 tools/probe.py build --backend qt-unix
.venv/bin/python tools/check_vncws.py --backend qt-unix
# The running application must already be qt-unix, from the session-stack recipe:
.venv/bin/python tools/check_vncws.py --backend qt-unix \
  --suite app-mdi --suite app-workspace --suite app-resize --suite app-clipboard
```

For TCP, select `qt-ws` consistently in build, launch and gate commands. Shared-demo
checks change records and end workspaces; run them sequentially on an idle demo.
The gate must exit zero and report `status: pass` with no failed checks in its
JSON summary; check counts can change as coverage grows. Unix backpressure and
filesystem ownership are verified separately with the native Linux harness:

```sh
mkdir -p artifacts/unix-native
podman run --rm --name swag-unix-acceptance --user 0 --entrypoint python3.12 \
  -v "$PWD/tests/vncws_unix.py:/tmp/check.py:ro,Z" \
  -v "$PWD/artifacts/unix-native:/artifacts:Z" \
  localhost/swag-probe-ws:dev /tmp/check.py --output /artifacts
```

The native harness runs Qt as UID 1000 and denied clients as UID 1001. Runtime
package/ABI checks are in [the clean-runtime recipe](../../tests/runtime/README.md).
Network and soak checks are separate, last-stage qualification; their scripts
accept `--render-backend qt-unix` or `qt-ws`. Existing local results and historical
changes are in [the qualification record](../qualification/vncws.md), not setup
prerequisites. Ignored `artifacts/` evidence is not included in a fresh clone.

## Diagnose a failed integration

| Symptom | First check |
| --- | --- |
| Qt cannot find/load `vncws` | `QT_PLUGIN_PATH` points to the directory containing `platforms/`. Add `QT_DEBUG_PLUGINS=1`; inspect `ldd …/platforms/libqvncws.so` and match private Qt/runtime versions. |
| Missing `Qt5FbSupport` or a static support archive at configure time | Install the matching Qt private-development and static-support packages; check the selected Qt prefix. |
| Browser gets HTML or disconnects immediately | TCP 5900 is WebSocket/RFB; the viewer page is on HTTP 6080. Unix RFB requires a WebSocket bridge. Check `wsPort` only for the TCP recipe. |
| Local page works on Linux but not on the remote browser machine | `127.0.0.1` refers to the browser machine; establish the documented SSH forwards or use the authenticated deployment. |
| TCP `Address already in use` | Stop the previous listener or choose an unused port and update `wsPort`/SSH forwards. A recently closed TCP connection can delay fixed-port reuse. The inherited-FD example accepts `--port 0` and prints the selected port. |
| Unix listener rejects the path | Check canonical path/length, current-UID ownership, parent `0700`/socket `0600`, and whether a previous process left an occupied socket. Do not blindly unlink it. |
| `invalid qt-unix runtime directory` | Use a canonical short manager root such as `/run/swag`; `/var/run` is commonly a symlink. |
| Workspace returns `launch_failed` despite an open socket | Inspect generation-specific `launcher.log`, `qt.log` and `launcher.json`; confirm executable, lock and readiness contracts. A bound endpoint is not GUI readiness. |
| Clipboard displays a previous copy after an unavailable copy | Deploy the pinned noVNC patch **and** the page event handling; rebuilding/restarting only Qt cannot update browser code. |
| Application opens but a custom window/dialog behaves incorrectly | Qualify the actual widget/window type; direct Qt has no external window manager. Use the Tiger path where native X11 behavior is required. |

## Supported scope

Linux Qt 5.15 raster Widgets, one controlling viewer with takeover, one resizable
screen, and 32-bit pixels. Sizes are 64..4096 per dimension with at most 4,953,600
pixels, including native **3440×1440**. Negotiated lossless zlib framebuffer and
UTF-8 clipboard have Raw/Latin-1 fallbacks. Clipboard transfer is bounded to 1 MiB;
see the [plugin reference](../../src/plugins/vncws/README.md) for protocol and queue
limits. The browser controls size; small/unsupported layouts scale locally.

Actual client Qt ABI/app, Windows/RDP input and scaling, IMEs, native X11 handles,
OpenGL/Qt Quick/WebEngine, and production capacity need their own qualification.
The measured local environment and successive test results are kept separately in
[the qualification history](../qualification/vncws.md).
