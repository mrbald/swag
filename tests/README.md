# Testing design

The Playwright checks in this repository are fully scripted. They locate Qt
widgets using application-reported geometry, send real browser mouse and keyboard
input through noVNC, and check the resulting state. Running the checks and deciding
pass/fail requires neither an LLM nor image recognition. Screenshots provide
additional evidence for separate visual review.

## How Playwright interacts with Qt

The browser contains two different surfaces:

| Surface | How the tests address it |
| --- | --- |
| HTML login page and workspace toolbar | Ordinary Playwright DOM locators, such as `page.locator("#username")`. |
| Qt application inside the noVNC canvas | Named widget rectangles reported by Qt, converted to canvas coordinates for mouse input; keyboard events go to the focused Qt widget. |

Qt buttons, fields, menus and grid cells do not become DOM elements. A Playwright
`getByRole("button")` locator cannot find a Qt button inside the canvas. The
application supplies the information needed to target it without recognizing
pixels.

In [workspace.py](browser/workspace.py), `Actor.state()` reads the current user's
generation-specific `state.json`. Its `geometry` maps names such as `field_name`
or `form_save_button` to remote `[x, y, width, height]` rectangles.
`Actor.canvas_point()` scales a point in that rectangle using the actual
framebuffer size and the canvas's browser bounding box. `Actor.click()` sends
`page.mouse.click()` at that point; `Actor.edit()` clicks, selects the existing
text and types with Playwright's keyboard API.

The input path depends on the renderer:

```text
TigerVNC: Playwright -> browser -> noVNC/RFB -> Xvnc/X11 -> Qt widget
vncws:    Playwright -> browser -> noVNC/RFB over WebSocket -> Qt QPA -> Qt widget
```

For example, with an actor connected and a form open:

```python
actor.edit("field_name", "Browser draft")
actor.wait(lambda s: s.get("dirty")
           and s.get("form_values", {}).get("name") == "Browser draft")
```

This exercises Qt focus, selection, keyboard input and form-change handling.
`edit()` does not set the Qt field through a test API. The fixed-size renderer
probe uses the same approach in [smoke.py](browser/smoke.py), with its known
1280×800 framebuffer; workspace checks use the current framebuffer dimensions.

## Observation and assertions

The Qt [desktop](../src/desktop/main.cpp) and [probe](../src/probe/main.cpp) publish
observation JSON through `SWAG_STATE_FILE`, atomically with `QSaveFile`. The desktop
reports identity, PID/generation, readiness, loaded records, form values, dirty
and error state, focus, geometry and, in MDI mode, individual child windows.
The [interface contract](../docs/interfaces.md) describes these fields.

Instrumentation observes the application; tests only read its state file.
Application interactions under test must travel through browser input, rather
than invoking Qt slots or changing widget values through instrumentation. API
requests and container commands are also used for setup, teardown, protocol
boundary checks and deliberate fault injection, such as killing a process or
delaying a backend response.

| Evidence | What a scripted assertion can establish |
| --- | --- |
| Application observation JSON | Expected edit/selection, conflict or retained draft, active window, focus and process identity. Tests poll for the expected state with a timeout. |
| Browser DOM, canvas dimensions and HTTP/API responses | Shell state, framebuffer fit, authentication/CSRF rejection and session lifecycle. |
| Canvas screenshots | Captured appearance for review; the probe also compares before/after SHA-256 hashes for editing and scrolling. |

The screenshot hash checks establish that the captured region changed. They do
not compare against an approved image or establish that text, colors or layout
are correct. The probe records its scope as “functional rendering only; visual
screenshots require review; no WAN/performance claim”.

A green run establishes the scripted functional and geometry assertions.
Clipped text, obscured controls, incorrect painting and other visual defects can
remain despite correct reported state. Review the relevant screenshots separately;
a person can do that, and an LLM can optionally assist, but neither is part of the
automated verdict. Native browser fullscreen also needs a desktop-level check;
headless Fullscreen API and layout assertions alone do not qualify it.

This design requires application-specific observation fields and stable target
names. Porting the harness to another Qt application means adding/adapting that
observation and its assertions. noVNC alone does not supply semantic widget
discovery.

## Check suites and entry points

Run commands from the repository root. Build targets are defined in
[CMakeLists.txt](../CMakeLists.txt).

| Checks | Entry point | Scope |
| --- | --- | --- |
| Service and launcher tests | `PYTHONPATH=python .venv/bin/python -m unittest discover -s tests -p 'test_*.py'` | Bank validation/permissions/versioning, session management and launcher contracts; no browser. |
| [Rendering probe](browser/smoke.py) | `probe-check`; `probe-compare` | Input, scrolling, edit commit/cancel, Unicode, clipboard, popups/dialogs, reconnect, exit and Qt death. Comparison uses the same executable/client over TCP; the selected TigerVNC check uses a Unix socket. |
| [Direct Qt WebSocket probe](browser/smoke.py) | `probe-ws-check` | Builds the vncws probe image and runs its browser smoke checks. This target does not run all protocol or everyday-interaction regressions. |
| [Everyday interactions](browser/everyday.py) | `.venv/bin/python tests/browser/everyday.py --output artifacts/everyday-check` | Autorepeat, keyboard Context Menu, Return/keypad Enter, item drag-and-drop and multiline clipboard through real noVNC. `--backend tiger --context-menu-only` isolates the menu reference comparison. |
| [Wire input](vncws_input.py) and [protocol boundaries](vncws_protocol.py) | `.venv/bin/python tests/vncws_input.py`; `.venv/bin/python tests/vncws_protocol.py` | RFB input semantics, malformed requests, connection ownership and transport boundaries. Direct protocol inputs complement browser tests; they do not prove browser key delivery. |
| [Specialized fixtures](fixtures/README.md) | See fixture build/run instructions | Exact ultrawide pixels/cursor framing and Qt Japanese fallback glyphs; rendering screenshots still require review. |
| [Authenticated workspace](browser/workspace.py) and [login recovery](browser/login_state.py) | `app-check`; `app-login-check` for login only | Two-user ownership and permissions, browser CRUD/conflicts, reconnect, lifecycle and login regressions. |
| [MDI](browser/mdi.py), [resize](browser/resize.py) and [presentation](browser/presentation.py) | `app-mdi-check`, `app-resize-check`, `app-presentation-check` | Independent frames, close guards, focus, state-preserving resize/takeover, clipboard and fullscreen controls. |
| [Ownership](linux_ownership.py), [uncertain writes](browser/uncertain_write.py) and [startup](startup.py) | `.venv/bin/python tests/linux_ownership.py`, `.venv/bin/python tests/browser/uncertain_write.py` (add `--ui-mode mdi` for MDI), `.venv/bin/python tests/startup.py` | Process death, committed writes with delayed responses, and failed startup. The uncertain-write fixture bypasses OIDC and qualifies write reconciliation only. |

For prerequisites and stack setup, follow the [main README](../README.md).
`uv sync --locked` installs Python dependencies. Workspace browser harnesses
explicitly select installed Google Chrome with `channel="chrome"`; installing
Playwright's bundled Chromium alone does not satisfy that selection. The renderer
probe defaults to Chrome on macOS and bundled Chromium on Linux.

On an idle running demo, run the browser checks sequentially:

```sh
cmake --build --preset container --target app-check
cmake --build --preset container --target app-resize-check
cmake --build --preset container --target app-presentation-check
```

For frame checks, start MDI mode with `app-up-mdi`, then run `app-mdi-check`.
For vncws integration, use `app-ws-build`, `app-ws-up`, then `app-ws-check`;
the latter runs MDI and workspace checks. For existing qualified images, direct
Python harness commands avoid rebuilding images as a side effect of a CMake
probe-check target. Protocol and everyday harnesses require the vncws probe image
(`probe-ws-image`) first. Use distinct ports and output directories if running
independent probes concurrently; checks against the shared demo remain sequential.
These checks change demo data and end test workspaces. Switching UI mode recreates
the stack and ends existing workspaces. Fault checks also operate on the local
demo; the startup fixture recreates the stack and restores a healthy instance.

## Unified vncws functional gate

Use the existing qualified `localhost/swag-probe-ws:dev` image and installed
Google Chrome, then run:

```sh
.venv/bin/python tools/check_vncws.py --list
.venv/bin/python tools/check_vncws.py --port 6300
```

The equivalent CMake target is `vncws-functional-check`; unlike the older probe
check targets, it does not rebuild images. Pass runner options directly with the
Python command when selecting suites, ports or output locations.

The default gate runs protocol, input, clipboard, compression and resize wire
checks, followed by real noVNC smoke, everyday interactions and outbound clipboard
failure/recovery checks. It runs suites
sequentially and reuses the chosen port pair. It does not build images or run
network/soak measurements. Standalone cursor/ultrawide fixture tests and visual
review remain separate gates.

`--list` prints the available suites as JSON without running anything. Repeat
`--suite` to select a subset; duplicate names are ignored and isolated probes
always precede shared-demo suites. Shared-demo checks are opt-in because they
change data and end workspaces. For example, with the local vncws MDI stack up:

```sh
.venv/bin/python tools/check_vncws.py --suite input --suite everyday
.venv/bin/python tools/check_vncws.py --suite app-mdi --suite app-workspace \
  --origin http://localhost:6081
```

`--engine` selects the container engine for harnesses that launch containers.
Each invocation creates a unique directory beneath `artifacts/qt-ws-functional/`
(or `--output`), containing per-suite logs/results and `summary.json`. The same
summary is printed to stdout; progress goes to stderr. A suite passes only when
its process exits successfully and its nonempty result report contains only
passing checks. The gate exits nonzero on failure and skips subsequent suites;
`--keep-going` retains the failing exit status while running the remaining suites.
Neither an automated pass nor a changed screenshot hash replaces visual review.

## Evidence and limits

Results, screenshots and failure state are retained under ignored `artifacts/`:
`workspace-check/`, `login-state/`, `mdi-check/`, `resize-{mode}-check/`,
`presentation-check/`, `linux-ownership/`, `uncertain-write-check/`,
`mdi-uncertain-write-check/` and `startup-check/`. Early resize failures may be
under `resize-check/`. Renderer runs default to `render-<timestamp>/` and capture
Playwright traces and container/version manifests. Runtime state and per-user
logs are under `artifacts/stack/workspaces/users/{user}/{generation}/`.

Keep observation files and screenshots private: they can contain form contents.
Full artifacts and generated credentials are not committed. Recorded runtime
results and environment qualifications are in [Findings and limits](../docs/findings.md)
and the [rendering decision](../docs/decisions/rendering.md). The
[vncws qualification record](../docs/integration/vncws.md) describes the separate
synthetic network/soak runs, ultrawide acceptance and Japanese glyph verification.
Ordinary functional checks alone do not establish those properties. Actual client
Qt/private-library compatibility, Windows/RDP keyboard layouts and IMEs, broader
font coverage and production capacity remain separate qualification work.
