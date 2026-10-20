# Scope

Build a Qt 5 application hosted on an EL8-compatible Linux container and operated
through a browser canvas. CMake is the top-level build; application code is C++23
with GCC Toolset 15 (no newer compiler), and Python 3.12. Use nanobind if native
Python bindings become useful. Boost is available when needed.

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
