# Findings and limits

Durable findings from the 2026-10-19 topology proof. The agreed milestone is
complete; this note records what was learned and the limits of that result.
[README](../README.md) has build/run/check commands;
[interfaces](interfaces.md) defines the service contracts.

## Renderer choice

The [rendering decision](decisions/rendering.md) records package versions and the
interaction matrix. With the tested Qt 5.15.3 package, Qt's VNC plugin failed
wheel input, Unicode keys beyond Latin-1 and both clipboard directions: 9/13
checks passed. TigerVNC passed 13/13 over TCP and again over a private Unix socket.
These are findings about the tested package, not all Qt VNC implementations.

The selected path uses Qt/xcb, Xvnc and vncconfig inside the container. Single-view
mode also runs Openbox; the optional MDI mode omits it.
It retains headless X11; no host X server or host X11 socket is required. The
renderer comparison/private probe keep their fixed 1280x800 framebuffer.
The authenticated desktop uses browser-driven resizing as described below.
Qt Unix-socket support was not patched or tested.

The original probe uses websockify. The authenticated manager instead bridges
WebSocket binary frames directly to the selected RFB Unix socket. RFB TCP is
disabled, sockets have mode `0600` and per-user directories have mode `0700`. This
avoids adding a websockify process to every authenticated workspace.

## MDI without Openbox

`--ui-mode mdi` selects Qt's `QMdiArea`/`QMdiSubWindow` path. Child frames belong
to one fullscreen Qt root, so moving, resizing, tiling, cascading and switching
them does not need an external window manager. The mode remains one Qt process
per user, with independent pane models, filters, form values and network clients.
Duplicate entity views can retain different unsaved edits. They do not synchronize
automatically; reload fetches current backend data and stale saves receive 409.

Default Qt MDI placement initially left large children partly outside the fixed
canvas. New children now use bounded cascade offsets. The area also permits
scrolling when users move frames beyond the viewport. Pane-wide controls leave
room for a grid beside the scrollable form, including in four-frame tiles.

Qt already installs the active MDI child's Close shortcut. Adding another
shell-level Ctrl+W binding caused ambiguous shortcuts and no close action while
editing. The desktop uses Qt's native action, which enters the same close guard.

Qt message boxes remain native top-level windows even with MDI child parents.
The desktop explicitly places, raises and focuses them and restores the previous
focus on dismissal. This matters without Openbox. The tested combo popup and
application message boxes are the qualified scope; arbitrary file dialogs,
third-party native windows and integration into the client's existing application
are untested. The internal X11 dependency remains.

Closing a dirty or uncertain child requires explicit discard. Closing during a
write is blocked until a result is known. Quit preflights every pane, including
inactive ones, before closing anything; cancelling leaves every pane open.
Clean panes can close while reads are pending, cancelling their callbacks. An
empty MDI shell remains a running workspace. Reconnect retains live frame IDs,
layout and unsaved state; process exit still ends the whole user's workspace.

## Browser-sized desktop

The initial fixed desktop made renderer comparisons repeatable. Authenticated
noVNC now enables its `resizeSession` API: on connection and container-size
changes it requests an Xvnc framebuffer matching the available browser area in
CSS pixels. Its built-in resize observer and rate limiting handle subsequent
changes. Local scaling remains enabled during negotiation or if a resize fails.
This does not change the private renderer comparison.

Qt widgets have minimum layout sizes. When the available canvas is smaller than
800×600, the client disables remote resizing and scales the existing framebuffer
in full. This avoids clipping Qt controls outside a too-small server display.
The check covers shrinking to 800×600/640×480 browser windows, reconnecting while
small, and resuming remote resizing after expansion. The threshold applies to
the canvas, excluding browser-page controls.

Without a window manager, Qt fullscreen alone did not establish a screen-resize
contract. The desktop explicitly follows `QScreen::geometryChanged` and fits its
root to the current screen, including after reconnect/takeover. Open message boxes
recenter; the active normal MDI child is kept reachable after a shrink. Other
normal children retain their layout and may need scrolling or Tile; maximized
children follow the MDI viewport. Resizing preserves pane forms and the process.
The active viewer controls display size, consistent with the existing takeover
policy. This is logical CSS-pixel sizing; HiDPI sharpness is not qualified.

## UI/UX space review (2026-10-20)

The review prioritized the remote-desktop conventions of a compact connection
bar, on-demand transfer/session controls, and video-style browser fullscreen.

| Finding | Change |
| --- | --- |
| Two browser bars, permanent clipboard input and wide margins consumed space before the application started. | One 36px bar, no canvas margins; clipboard and session controls open on demand. The redundant disabled “Workspace running” button is hidden once connected. |
| Qt repeated branding, used a second permanent note row, and kept instructions below the workspace. | One compact user/note/Quit row; shortcuts and frame guidance use tooltips. Loading/errors remain visible. Business grids/forms keep their original controls and target sizes. |
| A single MDI frame left much of the canvas gray. | The first frame opens maximized. Additional frames remain independent; Restore/Tile/Cascade are explicit controls. |
| Fullscreen needs an exit and recovery path without wasting permanent space. | Fullscreen uses the browser API, overlays an auto-hiding toolbar and keeps a small Controls tab. Escape exits before the key reaches Qt; disconnected/expired/error states and open panels keep recovery controls visible. |

At a 1440×1040 browser viewport, measured canvas dimensions changed from
1408×896 to 1440×1004: 108 additional vertical pixels, the full width, and 14.6%
more canvas area. These measurements exclude browser-owned window decorations.
Fullscreen reserves no toolbar height; its 88×28 Controls tab overlays the app.
Revealing controls does not change framebuffer dimensions, so Qt controls do not
move under the pointer. The RFB connection and Qt process remain mounted.

Fullscreen is requested directly from a click using
[`requestFullscreen`](https://developer.mozilla.org/en-US/docs/Web/API/Element/requestFullscreen).
The browser requires user activation and can reject it; rejection leaves the
workspace usable and retryable. Browser policy or embedding can disable the API.
No keyboard lock is requested and normal Qt typing shortcuts remain available.
Escape is reserved for exiting browser fullscreen; use a Qt dialog's Cancel
button to dismiss it while staying fullscreen. Other browsers remain unqualified.
Native Chrome was checked separately through the desktop UI: the fullscreen
button removed the visible address bar/tab strip, Chrome showed its native exit
notice, and Escape restored browser controls. The test tab was then closed.
Headless API/layout checks alone are not treated as proof of OS-level fullscreen.

## Integration lessons and resolved findings

| Finding | Resolution and evidence |
| --- | --- |
| NGINX generated a login redirect containing internal port 8082 instead of published port 6081. | `absolute_redirect off` preserves relative redirects. Fresh browser OIDC login passes. |
| Keycloak generated links without its public `/auth` prefix, causing login/resource routing failures. | Both `KC_HOSTNAME` and `KC_HTTP_RELATIVE_PATH` include the public context. OAuth's issuer/login URL is public; token/JWKS/profile requests use private service DNS. |
| Logout/relogin could leave “Sign in” displayed. Protected HTML lost the intended `no-store` header through NGINX header inheritance, and the client stopped checking after HTTP 401. | Protected locations explicitly retain cache/security headers. Sign-in links start OAuth directly. Valid session checks clear expired state; restored pages reload. A lifecycle revision rejects old poll/lease responses during sign-out or start. Six logout/relogin cycles and expired-tab recovery pass. |
| Restarting a Podman container changed its IP while NGINX retained the old address, producing HTTP 502 responses. | Variable upstreams and the container-network resolver refresh DNS every 2 s. Fresh browser login passes after host restart; live-session reconnect is tested separately. |
| Existing Qt clients timed out after the bank container acquired a new address on restart. | The demo gives the bank a stable private address, modeling a shared-service VIP. The browser persistence check restarts the bank and reloads through the existing Qt process. Arbitrary backend IP changes are not qualified. |
| Startup could report success when only Keycloak was ready. | Startup now requires every container running, bank/manager/OAuth health responses, public discovery and an unauthenticated `/session` returning HTTP 401. Killing each application component during startup is rejected. |
| A create could commit before its response timed out; immediately retrying Save could repeat the operation. | Qt latches an uncertain outcome, retains form/draft, and blocks mutations/navigation. Explicit discard and a successful reload are required. A committed create with its response delayed by 20 s produced one POST despite repeated Save/Enter. |
| A process registry alone does not protect ownership across failures; helpers can outlive a supervisor. | Serialized starts, stable manager/launcher/Qt locks, process-group cleanup and Linux parent-death signals enforce ownership. Twelve simultaneous starts per user resolve to one Qt process. Duplicate direct Qt launch exits with status 73; launcher/manager SIGKILL checks pass. The probe also cleans forked websockify workers after their master exits. |

The relevant implementations are [the stack driver](../tools/app.py),
[NGINX configuration](../containers/nginx.conf.in),
[session manager](../python/swag_session/server.py),
[launcher](../python/swag_session/launcher.py),
[process primitives](../python/swag_session/processes.py) and
[Qt desktop](../src/desktop/main.cpp).

The uncertain-write guard prevents blind retry; it provides no server-side
idempotency or exactly-once guarantee. A successful reload does not prove that a
previous timed-out operation has stopped; a late commit could still arrive.
Optimistic version conflicts are separate: HTTP 409 keeps the current edit intact
instead of silently overwriting another user's change.

## Session and trust boundaries

- All GUI processes share UID 1000. This enforces application ownership, with no
  OS isolation between users. The Qt code and shared-account processes are trusted.
- NGINX overwrites identity after authentication; the manager requires its private
  proxy credential. The IdP must supply the agreed stable alphanumeric username
  (currently 1–32 characters). Browser headers cannot select another user's socket.
- A second viewer takes over the same live process. Disconnecting or losing a
  viewer lease leaves Qt running. The default lease is 30 s, renewed every 10 s by
  the browser; inbound WebSocket messages are capped at 256 KiB.
- Qt exit/crash ends its generation and helpers. Manager death ends all owned
  workspaces. Restart begins without running desktops; opening a viewer never
  implicitly creates another Qt process. Unsaved GUI state survives reconnect,
  but not process death. Shared database records persist separately.
- “End session & sign out” stops Qt and removes the app login. IdP SSO is
  independent. Use the advertised `http://localhost:6081` origin: callback and
  CSRF/Origin checks depend on that exact origin. The private rendering probe uses
  `http://127.0.0.1:6080/probe.html` and has no authentication.
- A tab that observed logout keeps checking and can recover after another tab
  signs in. Recovery updates its login state without automatically taking viewer
  control. Deliberate sign-out suppresses recovery until navigation finishes.

## Verification and evidence

Results below were checked from local artifacts. Full artifacts are Git-ignored;
these summaries and the test programs remain available to a fresh checkout.

| Check | Recorded result | Local evidence under `artifacts/` |
| --- | --- | --- |
| Qt/Tiger TCP comparison | Qt 9/13, Tiger 13/13 | `final-comparison/{qt,tiger}/results.json` |
| Tiger Unix-socket renderer | 13/13 | `tiger-unix/tiger/results.json` |
| Bank/session/launcher service tests | 24 passed | `mdi-unit-tests.log` |
| Authenticated two-user browser/Qt workflow in MDI mode without Openbox | 11/11 | `workspace-check/results.json` |
| Single-view compatibility after pane extraction | 11/11 | `mdi-single-workspace-check/results.json` |
| MDI independent forms, four-frame layout, focus, clipboard, reconnect and lifecycle without Openbox | 10/10 | `mdi-check/results.json` |
| Browser resize, small-window scaling, dialogs, reconnect and takeover in MDI mode | 6/6 | `resize-mdi-check/results.json` |
| Same resize workflow in single-view/Openbox mode | 6/6 | `resize-single-check/results.json` |
| Compact geometry, clipboard focus, fullscreen API/reveal/Escape, rejection/retry and recovery | 7/7 | `presentation-check/results.json` |
| Native Chrome display fullscreen and Escape | Visually checked through desktop UI | `presentation-check/native-observation.json` |
| Login cache headers, six logout/relogin cycles, expired-tab recovery, stale responses and history revalidation | 5/5 | `login-state/results.json` |
| Linux locks, launcher/manager death and restart (single-view) | 8/8 | `linux-ownership/results.json` |
| Uncertain create response and reconciliation | Passed | `uncertain-write-check/results.json` |
| MDI close during a read, blocked close/Quit during a write, uncertain-form retention and reconciliation | Passed | `mdi-uncertain-write-check/results.json` |
| Startup faults | Three rejected components plus healthy restoration | `startup-check/results.json` |

The [renderer harness](../tests/browser/smoke.py) uses browser key events for
`é€Ж` and visible clipboard controls. The
[workspace harness](../tests/browser/workspace.py) drives the canvas with real
mouse/keyboard input; application instrumentation observes state and geometry.
Immediate typing after a click initially raced remote focus. The harness waits
for focus to settle before typing; this does not establish a latency bound.
The [uncertain-write fixture](../tests/browser/uncertain_write.py) intentionally
bypasses OIDC to isolate write handling; authentication is tested in the separate
workspace checks. [Linux ownership](../tests/linux_ownership.py) and
[startup](../tests/startup.py) checks deliberately kill demo processes.
The [MDI harness](../tests/browser/mdi.py) additionally checks full child-frame
containment/non-overlap and visible grid rows, and types after cancelling a close
dialog without another mouse click. The tested four-frame tile was also visually
inspected. Keyboard tests focus the canvas again after browser reload; Qt's
activation observation can precede the child's raise/focus events, so the harness
allows those events to settle before interacting.
The [login checks](../tests/browser/login_state.py) use real OIDC login and inject
delayed old poll/lease responses during logout. They exercise browser Back and
the persisted `pageshow` handler; a synthetic event covers that handler when
Chrome declines BFCache. This does not qualify actual BFCache admission.

## Remaining limits

- Verified environment: native ARM64 AlmaLinux 8.10 containers under Podman, with
  Chrome 155 on macOS. x86_64, Docker and other browsers remain unverified. The
  workspace harness explicitly selects Chrome; only the renderer harness has a
  Linux Playwright Chromium default.
- Authentication uses real OIDC with synthetic Keycloak identities over local
  HTTP. Actual Entra tenant integration and external HTTPS are unqualified.
  The application, bank and edge use EL8; authentication sidecars use pinned
  upstream images.
- Ownership is single-host. Multi-host routing/failover, capacity, concurrent-user
  resource costs, WAN traffic and latency have not been measured or qualified.
  The renderer was selected on functional requirements, without a performance ranking.
- Unicode key-event tests do not qualify native keyboard layouts, IMEs or CJK
  font coverage. Clipboard checks cover text.
- CRUD and grids derive at runtime from SQLAlchemy metadata and the API schema;
  there is no separate C++ code-generation step. Portfolio permissions are
  provisioned by trusted backend code and excluded from generic public CRUD.
- SQLite belongs to the shared bank service; Qt never opens it. Exact decimals
  use scaled integers and JSON strings. The mock initializes tables with
  `create_all`; schema migrations are not implemented. Model changes need an
  explicit migration or an intentional mock-database reset.
