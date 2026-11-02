# Scope

Build a Qt 5 application hosted on an EL8-compatible Linux container and operated
through a browser canvas. CMake is the top-level build; application code is C++23
with GCC Toolset 15 (no newer compiler), and Python 3.12. Use nanobind if native
Python bindings become useful. Boost is available when needed.

The client reports RHEL 8 with its distribution Qt 5 packages and a C++23 build
using GCC Toolset 15. The browser client is understood to be Chrome on a Windows
VM, probably hosted in Azure (hosting unconfirmed). Exact Linux architecture,
Qt package versions and compatibility of the actual client application remain
qualification inputs; the current plugin requires Qt 5.15.

Delivery uses the client's RPM workflow: commit plugin sources alongside the
application, build with its Qt/toolchain, and include the plugin in the application
RPM. The user owns the separate standard infrastructure package and site-specific
NGINX/OAuth2 Proxy setup. Containers here are a deployable-behavior test harness;
manufacturing RPMs or a production infrastructure installer is outside this work.

One shared service account hosts strictly zero or one Qt GUI process per user.
Usernames are stable, short, unique within the configured identity realm, and
alphanumeric. The authenticated proxy supplies identity; the session manager binds
it to the process. Reconnection rejoins the same live process. Application exit or
crash ends that application session; reconnect must not silently recreate it.
For a second viewer, use takeover: disconnect the earlier viewer.

The real banking backend is shared. The mock will use a domain model driving CRUD
APIs and Qt grids/forms, with one SQLite database owned by the shared backend.
Initial entities: portfolios, mandates, instruments, positions, cash, prices, FX,
and user-to-portfolio permissions. Qt clients access the service, not the DB file.

# First milestone: choose the renderer

Compare Qt's built-in VNC QPA plugin with Qt/xcb on TigerVNC's Xvnc. Prefer the
X11-free path if it provides the required interactions at a bounded maintenance
cost. Use one executable, fonts, data, geometry, and noVNC client for both paths.

First test wheel scrolling, keyboard navigation, edit commit/cancel, clipboard
text transfer, Unicode, popup/menu/dialog visibility and focus. A fixed framebuffer
with browser scaling is acceptable. Test reconnect with unsaved state, normal exit,
and process death. Application state and the relevant canvas region are separate
assertions; a socket or changed log alone is not success.

Do the initial comparison over private TCP. Assess Unix-socket feasibility early,
but implement the Qt transport patch only if the feature gate justifies it. Limit
Qt-specific functional repairs to half an engineer-day before reconsidering.
If both candidates are viable, compare representative grid traffic and total
session resources, including Xvnc and any window manager. Do not extrapolate local
or emulated measurements to production.

The rendering probe is private and deliberately precedes real authentication,
session ownership, the CRUD generator, and banking workflows. Passing it proves
only the tested rendering behavior. The selected path must subsequently pass the
two-user authenticated topology and Unix-socket checks.

The first runtime gate selected TigerVNC; see `docs/decisions/rendering.md`.
The unmodified Qt VNC plugin remains an explicit comparison mode, not the default.

## Experimental direct WebSocket probe

Add an opt-in `qt-ws` probe mode: the same Qt Widgets executable with an
in-process VNC QPA plugin serving RFB over WebSocket, without X11, Xvnc or
websockify. A separate static HTTP server may serve the unchanged noVNC client.
Keep a fixed 1280x800 screen and one viewer. The MVP gate is real browser basic
controls, wheel scrolling and clipboard in both directions, using the existing
probe checks and retaining screenshots. Record Unicode, popup, reconnect and
exit behavior too. No authenticated-workspace integration, production claims,
dynamic resize or performance work in this spike. TigerVNC stays the default.

The next increment makes `src/plugins/vncws/` a self-contained source directory
that builds with the client's Qt 5.15/private development packages, independently
of SWAG's toolchain and application. Add explicit listener configuration and
startup failures, bounded protocol validation, framebuffer clipping and safe
disconnect/takeover cleanup. Verify with independent wire-level fault probes and
real noVNC input; retain the existing working demo while checking candidate
images. Authentication, compression, dynamic resize, Qt 6 and publication remain
separate increments. Commit verified checkpoints without unrelated user edits.

The final application-level gate is the original MDI portfolio-management
application with an opt-in Qt WebSocket renderer. Preserve existing identity,
ownership, generation, lease and takeover controls and the TigerVNC default.
Exercise real forms/grids, multiple and duplicate frames, dialogs, clipboard,
reconnection and unsaved edits through the authenticated browser path. The probe
alone is not evidence of client-application compatibility.

For that gate, the manager reserves a loopback listener for the workspace generation
and passes its file descriptor through the launcher to Qt. It retains its reservation
until generation teardown under the same lock used for viewer admission, preventing
port reuse between endpoint validation and connection. The manager checks trusted
current-generation metadata against its reservation and bridges authenticated binary WebSockets. No additional public viewer
port or plugin-side identity/authentication is introduced. The experimental mode
requires MDI without an external window manager and keeps a fixed 1280x800 screen;
the TigerVNC resize qualification does not apply to it.

The approved next increment adds bounded single-screen remote resizing and
negotiated Unicode text clipboard support to the Qt WebSocket plugin. Preserve
ordinary Qt application behavior, current process identity, unsaved MDI forms,
input/focus and takeover while exercising real noVNC input. Then measure the
existing deployment path under explicit network profiles, recording bandwidth,
input/visual responsiveness, resource use and failures before selecting further
compression or pacing work. Keep TigerVNC as the fallback and commit verified
checkpoints. Production packaging and site configuration remain with the user.

The measured Raw framebuffer path is too slow on the shaped WAN profiles. The
next increment adds negotiated lossless RFB zlib encoding with Raw fallback,
bounded update coalescing and transport-drain pacing. Preserve the same client,
application executable and source-copy build contract. Independently verify
decoded pixels, stream lifetime, negotiation changes and slow-reader recovery;
rerun the original browser workflows and the same three network profiles before
claiming improvement. Keep bounded queues and existing authentication unchanged.

Follow up the measured local-latency regression with controlled timing evidence.
Remove avoidable transport/scheduling delay where demonstrated, retaining lossless
compression, bounded queues and WAN savings. Compare the same real-browser workload
and keep the application executable unchanged; client qualification remains separate.

## Approved transport polish and qualification

Batch a pending client cursor and dirty pixels into one framebuffer response,
preserving Raw/zlib negotiation, bounded pacing and backpressure. Add browser
clipboard preflight to the workspace and probe, with a serialization-aware size
bound and accessible local errors that preserve text, focus and the connection.
Keep the plugin's existing clipboard limits and rejection policy; outbound
oversized-copy feedback is separate future scope.

Implement these independent changes in separate worktrees with focused tests.
Root reviews and integrates them, then runs the combined functional browser gates.
Final nonfunctional qualification compares the accepted baseline and candidate
under the existing three network profiles and runs a two-user, 30-minute soak
covering edits, clipboard, resize, reconnect and takeover. Retain timing, bytes,
CPU/RSS, available queue evidence, failures and cleanup results. This bounded
local exercise establishes no production SLO or capacity claim. Exact client Qt,
application and Windows/RDP qualification remain external gates.

Before client integration, perform one bounded local defect hunt across transport
feature interactions and session failure paths. Fix only reproduced defects with
focused regressions; classify remaining candidates as ruled out or requiring
client evidence. Keep functional checks with each fix and relevant nonfunctional
checks last. Add a short top-level README source-transplant/build/install/run guide,
preserving the separate application and infrastructure packaging responsibilities.

# Authenticated topology and bank mock

One app-host process manager owns every user's launcher and private RFB socket.
NGINX authenticates through OAuth2 Proxy and a local Keycloak OIDC realm, overwrites
the username/header credential, and sends requests to that manager. The manager
deterministically chooses the user's generation/socket; no raw browser-supplied
username or socket path selects another workspace. Same-origin CSRF protects
mutations; authenticated viewer leases expire unless renewed. Manager death fails
closed; restart begins with no running workspaces. IdP SSO is independent.

The application and shared Python bank backend are separate services. SQLAlchemy
models drive schema, CRUD validation and Qt grids/forms. Backend entitlements,
exact decimal strings/scaled integers and version-checked writes apply regardless
of UI choices. SQLite persists beyond GUI or bank-process restarts. This is a
single-host topology proof; shared UID, local HTTP and synthetic IdP identities
are explicit demo constraints. Endpoint details: `docs/interfaces.md`.

# Optional MDI desktop

Retain the single-view mode and add a startup-selected MDI mode. One Qt process
per user owns a QMdiArea with several independently editable child frames,
including duplicate views of the same entity. Support tile/cascade, move/resize,
maximize/restore and guarded child/workspace close. Reconnect retains the live
frames, unsaved forms and layout. Qualify this mode without Openbox; Xvnc/xcb and
the clipboard helper remain. Native Qt popups/dialogs require explicit focus and
stacking checks in that configuration.

The authenticated desktop adapts its framebuffer and Qt root to the available
browser canvas on connect, browser resize and viewer takeover. Preserve live
process identity, child frames and unsaved forms. Keep the original renderer
comparison fixed-size for repeatable results.

Minimize transport-shell overhead with one compact connection bar and on-demand
clipboard/account controls. Support user-initiated browser fullscreen with
revealable overlay controls and a reliable Escape exit. Preserve authentication,
rejoin/explicit-start semantics, clipboard focus and unsaved Qt state.
