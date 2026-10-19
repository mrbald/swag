# Renderer: TigerVNC

Selected after the first functional comparison on native ARM64 Linux, 2026-10-19.
Both candidates used the same C++23 Qt Widgets executable, 1280x800 framebuffer,
noVNC client and browser. Build/runtime: AlmaLinux 8.10, GCC 15.2.1, Qt 5.15.3,
Python 3.12.15, TigerVNC 1.15.0, noVNC 1.7.0, websockify 0.13.0, Chrome 155.

The packaged Qt VNC plugin passed ordinary ASCII input, grid edits, popups, dialogs,
reconnect, normal exit and SIGKILL cleanup. It failed wheel scrolling (no wheel
events), Unicode key input beyond Latin-1 (`é` arrived; `€Ж` did not), and clipboard
transfer in both directions. TigerVNC passed the corresponding checks. Relevant
canvas screenshots were also visually inspected. These findings apply to the
tested package, not every Qt VNC implementation.

| Interaction | Packaged Qt VNC plugin | TigerVNC |
| --- | --- | --- |
| ASCII input, grid edit/cancel/navigation | Passed | Passed |
| Wheel scrolling | Failed: no wheel events observed | Passed |
| Browser Unicode key events for `é€Ж` | `é` arrived; `€Ж` did not | Passed |
| Clipboard: browser to Qt | Failed | Passed |
| Clipboard: Qt to browser | Failed | Passed |
| Combo/menu popups and modal focus | Passed | Passed |
| Reconnect with draft, normal exit, Qt SIGKILL | Passed | Passed |

TigerVNC's selected path is Qt/xcb on Xvnc, with Openbox for window management and
vncconfig for X11 clipboard ownership. This accepts a headless X11 dependency in
exchange for the required grid interactions without a Qt plugin repair project.
Qt Unix-socket transport was not implemented or tested: the probe explicitly
rejects `qt` + `unix`. The functional failures ended Qt qualification before a
transport patch was warranted. No performance ranking between the two candidates
was made.

The Unix-socket run passed all 13 checks, including mode0600 on
`/run/swag/users/probe/vnc.sock`, RFB TCP disabled, browser input, popups/modal focus,
edit cancellation, unsaved draft reconnection, normal Qt exit and SIGKILL. Qt exit
stops its bridge/display helpers and ends the private probe container.

The final TCP comparison passed 13/13 checks on TigerVNC and 9/13 on Qt's plugin.

Evidence is retained locally under `artifacts/final-comparison/` and
`artifacts/tiger-unix/` (results, screenshots, traces, container metadata). The
packaged Xvnc Unix-socket check is under `artifacts/packaging/`. New runs get their
own folders with image IDs and complete package/plugin manifests. Full artifacts
are ignored by Git; this summary is the durable decision.

This decision records functional rendering checks. Later authentication,
concurrent-user ownership and CRUD/backend checks are summarized in
[Findings and limits](../findings.md). WAN, throughput and production sizing
remain unqualified.
Unicode browser key events and clipboard controls are tested; actual keyboard
layouts, IMEs and CJK font coverage need separate checks if required.
