The private rendering probe runs the same `/opt/swag/bin/swag-probe` in two modes:

| `SWAG_BACKEND` | Qt platform | Supporting GUI processes |
| --- | --- | --- |
| `qt` | `vnc:size=1280x800:port=5900` | none |
| `tiger` | `xcb`, `DISPLAY=:99` | Xvnc, Openbox, vncconfig |

Both serve the same noVNC release and `probe.html` through Python 3.12 websockify
on container port 6080. Only publish that port, bound to host loopback. RFB port
5900 remains inside the container; TigerVNC binds it to container loopback.
There is no authentication or session manager in this probe.

`SWAG_RFB_TRANSPORT=tcp` is the comparison default. The selected TigerVNC path
also supports `SWAG_RFB_TRANSPORT=unix`: Xvnc disables its RFB TCP listener and
creates `/run/swag/users/$SWAG_USER/vnc.sock` with mode 0600 inside directories
with mode 0700. websockify connects through `--unix-target`; the browser URL stays
the same. For example, add `-e SWAG_BACKEND=tiger -e SWAG_RFB_TRANSPORT=unix` to
the run command below. `SWAG_BACKEND=qt` with `unix` fails configuration explicitly
because the packaged Qt VNC plugin is unpatched. This adds no authentication or
multiuser session manager.

Build from the repository root (native architecture, no emulation):

```sh
podman build --target deps -f containers/Containerfile -t localhost/swag-deps:el8 .
podman build --target probe -f containers/Containerfile -t localhost/swag-probe:dev .
```

The second command reuses the dependency layers. GCC Toolset 15 compiles C++23;
EL8 RPMs supply Qt 5.15, Python 3.12, CMake and TigerVNC. The image records actual
RPM versions and the executable/plugin hashes in `/opt/swag/image-manifest.json`.
noVNC 1.7.0 and websockify 0.13.0 are pinned and checked with SHA-256 at build time.
websockify's optional acceleration and authentication modules are omitted.

Use one writable artifact directory per container, owned through Podman's user
namespace mapping. The image service account is `swag`, UID/GID 1000. Example:

```sh
mkdir -p artifacts/qt
podman run --name swag-qt --userns keep-id:uid=1000,gid=1000 \
  -p 127.0.0.1:6080:6080 -v "$PWD/artifacts/qt:/artifacts:Z" \
  -e SWAG_BACKEND=qt -e SWAG_USER=probe -e SWAG_GENERATION=1 \
  localhost/swag-probe:dev
```

Open `http://127.0.0.1:6080/probe.html`. For the other backend, use a separate
artifact directory/container name and `SWAG_BACKEND=tiger`. The build and UID
mapping were verified with Podman; the Docker adapter remains unverified.

`SWAG_STATE_FILE` defaults to `/artifacts/state.json`; the probe owns this JSON.
`SWAG_ARTIFACT_DIR` defaults to `/artifacts` and holds `backend.json` and separate
Qt, Xvnc, Openbox, vncconfig and websockify logs. Configured paths must stay under
`/artifacts`. `SWAG_USER` is an alphanumeric observation label, not authentication.
`SWAG_GENERATION` distinguishes explicit launches. Container restart creates a
new application session, so run without an automatic restart policy.
`backend.json` records `rfbTransport` and `rfbEndpoint`. Readiness checks the
configured RFB listener or Unix socket plus the HTTP listener. An existing Unix
endpoint is refused; normal cleanup removes the socket after terminating helpers.

The supervisor waits for the actual Qt process. Browser disconnect leaves it
running. Normal Qt exit, crash or a forwarded termination signal ends the session;
helpers are terminated and the container exits with Qt's status (signals use
128 + signal). Helpers are not restarted. Failure of a helper terminates Qt and
records the helper failure. TigerVNC's `NeverShared`/`DisconnectClients` settings
disconnect an earlier viewer when a second viewer joins; authenticated ownership
and matching Qt takeover behavior remain outside this launcher.
Cleanup signals each helper's process group even if its original master has
already exited, so forked websockify workers also receive termination.

Collect the actual manifest and verify packaged Unix-socket support:

```sh
podman run --rm --name swag-manifest --userns keep-id:uid=1000,gid=1000 \
  -v "$PWD/artifacts/manifest:/artifacts:Z" --entrypoint python3.12 \
  localhost/swag-probe:dev /opt/swag/backends/manifest.py --output-dir /artifacts
podman run --rm --name swag-uds --userns keep-id:uid=1000,gid=1000 \
  -v "$PWD/artifacts/uds:/artifacts:Z" --entrypoint python3.12 \
  localhost/swag-probe:dev /opt/swag/backends/check-tiger-uds.py
```

The UDS check saves packaged `Xvnc -help`, launches Xvnc with `-rfbport -1` and
`-rfbunixpath`, reads an RFB banner through the Unix socket, verifies mode 0600,
and checks that no TCP listener appeared. This verifies the packaged server only;
the comparison itself uses private TCP. The optional Tiger Unix launch mode uses
that transport without adding authenticated routing; Qt UDS is not implemented.

This directory describes the original single-user rendering probe. The
authenticated stack uses `python/swag_session/` to bridge WebSocket binary frames
directly to each user's RFB Unix socket; it does not launch per-user websockify
processes. See [the integration contract](../../docs/interfaces.md) and
[findings](../../docs/findings.md).
