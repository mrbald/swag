# vncws Qt platform plugin

This experimental QPA plugin adapts Qt's VNC platform plugin to serve binary
RFB through a WebSocket listener inside the Qt process. Use
`QT_QPA_PLATFORM=vncws:size=1280x800:port=5900` with a noVNC client pointed at
port 5900. One viewer is supported; a new viewer takes over the connection.
RFB 3.3 has no authentication or encryption. The probe must bind/publish it
only on host loopback. Clipboard text uses RFB's Latin-1 encoding.

The files `main.cpp`, `qvnc.cpp`, `qvnc_p.h`, `qvncclient.cpp`,
`qvncclient.h`, `qvncintegration.cpp`, `qvncintegration.h`,
`qvncscreen.cpp`, `qvncscreen.h`, and `vnc.json` began from the official
Qt Base v5.15.2 tag at
`https://github.com/qt/qtbase/tree/v5.15.2/src/plugins/platforms/vnc`.
The source archive SHA-256 is
`30f6d86c574b0787d1c44f67b987d746b595f8c00ca2e659cf884728afa20023`.
The Qt-authored files retain their upstream copyright headers and are offered
under LGPLv3; see `LICENSE.LGPL3` and its incorporated `LICENSE.GPL3`.
The WebSocket stream and CMake integration
were added for SWAG. The plugin is linked against the Qt 5.15.3 EL8 packages;
the private QPA APIs require a matching Qt build for deployment.
