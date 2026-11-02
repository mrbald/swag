# Ultrawide framebuffer fixture

`vncws_entropy.cpp` is the static Qt Widgets fixture for `tests/vncws_ultrawide.py`:
3440x1440 deterministic high-entropy pixels and an opaque 495x499 custom cursor.
Keep the expected cursor bytes and dimensions in the Python test independent
of the fixture implementation. The opaque cursor is intentional: replacing it
with a translucent image would hide the missing-alpha-channel regression.

Build with the same Qt development packages as the plugin (the native EL8 probe
image supplies these). From the repository root:

```sh
c++ -std=c++17 -fPIC tests/fixtures/vncws_entropy.cpp -o /tmp/vncws-entropy \
  $(pkg-config --cflags --libs Qt5Widgets)
QT_PLUGIN_PATH=/opt/swag/plugins \
QT_QPA_PLATFORM=vncws:size=3440x1440:port=5900 \
QT_FORCE_STDERR_LOGGING=1 QT_LOGGING_RULES='qt.qpa.vnc.debug=true' \
  /tmp/vncws-entropy > /tmp/vncws-entropy.log 2>&1
```

In another terminal with the project Python environment, run:

```sh
.venv/bin/python tests/vncws_ultrawide.py \
  --url http://127.0.0.1:5900/ \
  --qt-log /tmp/vncws-entropy.log --output artifacts/ultrawide-wire
```

The test observes the image and sends protocol requests. It checks full Raw/zlib
pixel equivalence, exact opaque cursor pixels/mask, and paused-reader recovery.
Pass `--qt-log` for the acceptance gate: that additionally requires actual queue
deferral/resumption. When running in a container, publish the fixture port only
on host loopback and retain its Qt log in a mounted artifact directory.
