# Clean-runtime transplant check

This image starts from the pinned AlmaLinux 8.10 base and installs explicit
runtime RPMs. It copies the already built probe, platform plugin and pinned noVNC
client from a qualified image; it inherits none of that image's build layers or
environment variables. No rebuild occurs in the runtime image.

The default Qt RPM versions match the currently qualified EL8 build. A different
Qt build requires matching runtime RPMs and an explicitly rebuilt plugin. Do not
use this fixture's version defaults as a compatibility promise for arbitrary Qt.
The `qt5-qtbase-gui` RPM pulls in X11/OpenGL **client libraries**; the acceptance
condition is no X server/window manager, not an absence of all X-related libraries.
Python is included only to serve the static browser fixture.

From the repository root, use the accepted probe image ID or a retained tag:

```sh
podman build -f tests/runtime/Containerfile \
  --build-arg QUALIFIED_IMAGE=localhost/swag-probe-ws:dev \
  -t localhost/swag-runtime-check:dev .
mkdir -p artifacts/runtime-check
podman run --detach --name swag-runtime-check \
  --userns=keep-id:uid=1000,gid=1000 \
  --publish 127.0.0.1:6296:5900 --publish 127.0.0.1:6297:6080 \
  --volume "$PWD/artifacts/runtime-check:/artifacts:Z" \
  localhost/swag-runtime-check:dev
podman exec --detach swag-runtime-check python3.12 -m http.server 6080 \
  --bind 0.0.0.0 --directory /opt/novnc
```

Open `http://127.0.0.1:6297/probe.html?wsPort=6296` in installed Chrome. Keep these
unauthenticated test ports bound to loopback. The fixture launches the executable
directly using `QT_PLUGIN_PATH` and `QT_QPA_PLATFORM`; there is no launcher,
websockify, Xvnc or Openbox in the path. State observation remains available at
`artifacts/runtime-check/state.json` for geometry and assertions. Actual actions
must still use the browser/noVNC input path.

Run the scripted browser gate against that same mounted observation directory:

```sh
.venv/bin/python tests/browser/runtime.py --output artifacts/runtime-check
```

Qualification must retain:

- Exact probe/plugin/noVNC hashes matching the qualified source image.
- Runtime RPM list from `/opt/swag/runtime-packages.txt`, Qt RPM versions matching
  `/opt/swag/build-image-manifest.json`, and no `*-devel`, `*-static`, compiler,
  CMake, Ninja, Xvnc or Openbox packages/executables.
- `ldd` for the probe and plugin with no unresolved libraries, and `/proc/1/maps`
  showing the loaded plugin/runtime libraries. Do not provide a build-toolchain
  `LD_LIBRARY_PATH` to make this pass.
- Real noVNC first frame, editing, clipboard and context-menu checks, plus visual
  inspection of the rendered result. Startup/`ldd` alone is insufficient.

Retain logs before cleanup:

```sh
podman logs swag-runtime-check > artifacts/runtime-check/container.log 2>&1
podman cp swag-runtime-check:/opt/swag/runtime-packages.txt artifacts/runtime-check/
podman rm --force swag-runtime-check
```

This is a standalone runtime/source-copy qualification for the probe and plugin.
It does not qualify the client application's own dependencies, Qt build, CPU
architecture, Windows/RDP input, authentication or production deployment.

The same runtime image supports the Unix transport. To exercise that path without
adding a bridge or development tools, run the native acceptance harness as root
with `--entrypoint python3.12`, mounting `tests/vncws_unix.py` and an evidence
directory as in the [Unix integration guide](../../docs/integration/vncws.md#unix-socket-transport).
The harness launches Qt as UID 1000 and tests denied access as UID 1001.
Authenticated browser qualification remains a separate application gate.
