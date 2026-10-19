# Current integration contract

This is a short implementation note, not another tracker.

## Shared bank service

Python 3.12, SQLAlchemy declarative domain models, SQLite local to the service.
Listen on 8081. Qt uses `SWAG_BACKEND_URL` (base, without `/api`),
`SWAG_SERVICE_TOKEN`, and immutable `SWAG_USER` from its process environment.
Each request sends `X-Swag-Service-Token` and `X-Swag-User`. The first authenticates
the trusted application service; the second chooses the user's bank entitlements.
Neither service port is published to the host.

- `GET /health` -> `{ "status": "ok" }`, unauthenticated private health check.
- `GET /api/schema` -> `{ "entities": [ { "name": "portfolios", "label": "Portfolios", "title_field": "name", "fields": [...] } ] }`.
- Fields: `name`, `label`, `type` (`string`, `integer`, `decimal`, `date`, `enum`,
  `reference`, `boolean`), `editable`, `nullable`, optional `choices`, `reference`
  (entity name), `scale`. `id` and `version` are read-only integer fields.
- `GET /api/{entity}?offset=0&limit=100&sort=id&direction=asc&filter=text`
  -> `{ "items": [...], "total": 10, "offset": 0, "limit": 100 }`.
- `POST /api/{entity}` with field values -> `{ "item": {...} }`.
- `PATCH /api/{entity}/{id}` with `{ "version": 1, ...changed fields }`
  -> `{ "item": {...} }`.
- `DELETE /api/{entity}/{id}` with `{ "version": 1 }` -> 204.
- Errors: `{ "error": { "code": "conflict", "message": "...", "fields": {} } }`.
  Use 409 for stale versions, 422 validation, 403 denied writes, and 404 for rows
  outside a user's entitlement. Monetary and quantity decimals are JSON strings;
  persist exact scaled integers, not binary floating point.

Seed demo users alice and bob, both entitled to one shared portfolio, with an
additional private portfolio each. Include mandates, instruments, positions,
cash balances, prices and FX rates. Schema metadata drives both API validation
and Qt grids/forms. System actor/version fields cannot be supplied by clients.

## Session service

One app-host container/service account, session manager on private port8080.
NGINX authenticates through OAuth2 Proxy/Keycloak and overwrites `X-Swag-User`
and `X-Swag-Proxy-Key`. The manager requires the configured proxy key and a short
alphanumeric username. `SWAG_PUBLIC_ORIGIN` is the exact browser origin.

- `GET /health`: private health check.
- `GET /session` -> `{ "user": "alice", "state": "absent|starting|running|ended", "generation": "...", "pid": 12, "csrf_token": "..." }` (nullable identity fields before launch).
- `POST /session`: explicitly start or rejoin; same result. Concurrent requests
  for the same username resolve to one process. Ending requires explicit restart.
- `DELETE /session`: terminate that user's workspace; return ended state.
- `POST /session/lease` with `{ "generation": "..." }`: renew viewer access.
- Mutations require `Origin == SWAG_PUBLIC_ORIGIN` and `X-Swag-CSRF` from GET.
- `GET /viewer/{generation}/websockify`: WebSocket to that user's existing
  generation only. Second viewer takes over. Enforce Origin and a renewable lease.
  New WebSocket requests never launch applications.

Per-user launchers own Xvnc/display, Openbox, vncconfig and Qt. The authenticated
manager bridges opaque binary WebSocket frames directly to the user's RFB Unix
socket, with bounded buffers and backpressure. Each display is unique. Runtime
root: `/run/swag/users/{user}`; files/logs: `/artifacts/users/{user}/{generation}`.
The manager passes `SWAG_USER`, `SWAG_GENERATION`, `SWAG_STATE_FILE`,
`SWAG_SESSION_LOCK_FILE`, `SWAG_BACKEND_URL`, and `SWAG_SERVICE_TOKEN` to Qt.
Qt holds the per-user flock for its entire lifetime, acquired before QApplication.
The manager fails closed on shutdown: stop every owned workspace. Its restart
policy must not leave old Qt processes running. Qt exit ends viewer grants and
helpers; the bank service/database survives. This does not globally end IdP SSO.

## Qt application

New `src/desktop/` defines `swag-desktop`, linking Qt5 Widgets and Network. Retain
the rendering probe. Root adds/install targets in top-level CMake and container.
The desktop is fullscreen1280x800, with entity navigation, generated grid/forms,
reload/new/save/delete controls, paging/filtering, explicit unsaved draft and Quit.
Use asynchronous QtNetwork calls. Show server errors and 409 conflicts without
silently overwriting edits. The app never opens SQLite.

Private observation JSON: `user`, `generation`, `pid`, `ready`, `entity`, `items`,
`selected_id`, `dirty`, `error_text`, `draft`, `geometry` (named widgets -> global
`[x,y,w,h]`). Form field geometry uses `field_<name>`. Browser tests only read it.
