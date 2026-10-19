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

The selected path uses Qt/xcb, Xvnc, Openbox and vncconfig inside the container.
It retains headless X11; no host X server or host X11 socket is required. The
framebuffer is fixed at 1280x800 and scaled in the browser. Dynamic server-side
resizing was not qualified. Qt Unix-socket support was not patched or tested.

The original probe uses websockify. The authenticated manager instead bridges
WebSocket binary frames directly to the selected RFB Unix socket. RFB TCP is
disabled, sockets have mode `0600` and per-user directories have mode `0700`. This
avoids adding a websockify process to every authenticated workspace.

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
| Bank/session service tests | 18 passed | `unit-tests.log` |
| Authenticated two-user browser/Qt workflow | 11/11 | `workspace-check/results.json` |
| Login cache headers, six logout/relogin cycles, expired-tab recovery, stale responses and history revalidation | 5/5 | `login-state/results.json` |
| Linux locks, launcher/manager death and restart | 8/8 | `linux-ownership/results.json` |
| Uncertain create response and reconciliation | Passed | `uncertain-write-check/results.json` |
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
