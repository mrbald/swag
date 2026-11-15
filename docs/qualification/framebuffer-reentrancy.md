# Framebuffer paint reentrancy qualification

Date: 2026-11-15. Target: RHEL8 Linux / Qt 5.15. Local evidence uses
AlmaLinux 8.10 **ARM64**, GCC Toolset 15.2.1, Qt 5.15.3-11.el8_10 and glibc
2.28-251.el8_10.40. The local Podman runs used the separate rootless connection
`CONTAINER_CONNECTION=podman-machine-default`. This does not qualify the client's exact x86-64 Qt build.

## Decision

Use a small `QVncBackingStore` subclass. Do not hold the image mutex across
application painting, which can enter a nested event loop. Keep Qt's existing
compositor, encoder, stacking, cursor and alpha-composition semantics. Only the
outer paint clears its dirty alpha region. Defer a resize until the outer paint
ends, preserving old pixels and clearing the new area. Debug assertions enforce
GUI-thread ownership. Ordinary painting has no new image copy or second buffer.
Qt may copy an image while its painter is interrupted; that cost is exceptional.

Opus-5.5/xhigh reviewed the design and recommended this over a private compositor
fork. Sol (`gpt-6.1-sol`, xhigh) implemented it. Opus-5.5/high reviewed the code and
returned no blocking findings. The app and fixtures use C++23; the plugin retains
its C++17-compatible source-transplant contract because this fix needs no newer
language feature. Copied Qt alpha-clear code retains its upstream license notice.

ASan also exposed an existing screen dirty-map leak at shutdown. Deleting that
screen-owned map fixes it; the compositor body remains unchanged. Ownership was
checked: the pointer starts null, resize deletes/replaces the old map, the cursor
destructor does not access it, and integration deletes the server before screen
removal. The sanitizer rerun passed without suppressions.

## Evidence

Evidence is retained locally under `artifacts/framebuffer-reentrancy/` (ignored
by Git), including exact source packets and the two completed review results.

| Check | Result |
| --- | --- |
| Unchanged baseline, real noVNC | Initial pixels pass; modal from table `data()` never renders; heartbeat freezes; GUI thread waits in `futex_wait_queue`. |
| Independent baseline Raw decoder | Expected hang after normal pixels/input pass; nested paint and frozen heartbeat verified. |
| Final candidate, real noVNC | Seven checks pass: two modal/dismiss/recovery cycles, continued input, exact table-frame SHA-256 parity. |
| Raw / persistent-zlib recovery | 20 Raw cycles and three zlib cycles pass, with independent decoded pixels. |
| Existing functional suites | 42 compression, resize and browser smoke checks pass. |
| Native ASan + LSan | Seven cases pass: nested painting, deferred resize, cancellation, empty sizes, preservation/alpha clearing, failed allocation, redirected painter. |
| Live ASan fixture | 26 checks pass with two errors in one paint plus real-input parent update/resize actions. |
| Deferred-resize exposure characterization | Eight ASan wire checks pass, including exact cell recovery after explicit follow-up. At 1000×700, exposed pixel (900,100) remains black after dismissal instead of the original background (239,239,239); the resize guard is observed in the Qt log. This confirms the documented invalidation limit. |
| Near-limit ASan fixture | 4096×1209 maximized backing store, two dialogs per paint, two cycles: decoded pixels, exact first-cell restoration and liveness pass. |

Both the instrumented plugin and fixtures were compiled with
`-fsanitize=address -fno-omit-frame-pointer -DQT_FORCE_ASSERTS`; the plugin's actual
flags are in `asan-plugin-build/CMakeCache.txt`. Release images were also built
and exercised. Leak checking remained enabled. The `INT_MAX` allocation test
exercises a null-image result via parameter overflow, not actual memory exhaustion.

## Normal-operation performance

Same benchmark executable, same 1280×800 screen, 100 warm-up updates, five runs of
2,000 updates per workload and variant, alternating order. Each update does a
synchronous widget repaint and dispatches the compositor update. Paint counts
and the final screen pixel are checked outside the timing interval. No VNC
client is attached: this measures painting/compositing, not encoding or WAN cost.

| Workload | Baseline median µs/update | Candidate median µs/update | Change |
| --- | ---: | ---: | ---: |
| 128×64 dirty region | 5.109 | 5.123 | +0.28% |
| 1280×800 full window | 285.390 | 284.801 | -0.21% |

These differences are within run-to-run variation: no measurable regression and
no claimed speedup. Raw samples, CPU times, ranges, image IDs and p95 **run means**
(not individual-frame latency percentiles) are in `final-comparison/results.json`.

## Reproduce

On the EL8 build host with GCC Toolset 15 and matching Qt private/static packages:

```sh
cmake -S tests/fixtures -B build/framebuffer-fixtures -DCMAKE_BUILD_TYPE=RelWithDebInfo
cmake --build build/framebuffer-fixtures --parallel 2
QT_PLUGIN_PATH=/opt/swag/plugins timeout 30s \
  build/framebuffer-fixtures/vncws_backingstore \
  -platform vncws:size=800x600:port=5901 --case all
```

For sanitizer qualification, install `gcc-toolset-15-libasan-devel`, build the
plugin with the flags above and `-DCMAKE_SHARED_LINKER_FLAGS=-fsanitize=address`,
then configure the fixtures with `-DSWAG_FIXTURE_ASAN=ON`. Point `QT_PLUGIN_PATH`
at that instrumented plugin, not the release installation.

Run the interactive fixture with a fresh observation file:

```sh
QT_PLUGIN_PATH=/opt/swag/plugins SWAG_STATE_FILE=/tmp/reentrant-state.json \
  build/framebuffer-fixtures/vncws_reentrant \
  -platform vncws:size=1280x800:addr=127.0.0.1:port=5901
.venv/bin/python tests/vncws_reentrant.py --url ws://127.0.0.1:5901/ \
  --state-file /tmp/reentrant-state.json --encoding zlib --cycles 3 \
  --output artifacts/reentrant-wire
```

The fixture stays running; invoke the harness from a second terminal. Restart it
between runs. Add `--expect-hang` only when checking the unmodified baseline.
Fixture options `--dialogs-per-paint 2`, `--allow-parent-resize`, `--maximized`
and `--alpha-dialog` select additional scenarios. Wire option
`--parent-action both` clicks the modal's update/resize controls; it requires
`--allow-parent-resize`. All actions are real input; observation JSON is read-only.

For browser verification, serve the repository's `probe.html` with noVNC as in
the existing probe launcher, then run `tests/browser/vncws_reentrant.py` with
`--url`, `--state-file`, and `--output`. The URL must select that fixture's
WebSocket port, e.g. `probe.html?wsPort=5901`.

Run `tests/vncws_paint_benchmark.py --baseline-image IMAGE --candidate-image IMAGE
--output artifacts/paint-comparison` against the EL8 probe images. It builds the
benchmark once, runs both images sequentially, and saves logs and JSON. Set
`CONTAINER_CONNECTION` if using a nondefault Podman connection.

## Accepted limits and review resolutions

- Deferred resize exposure or nested same-window painting can remain blank/stale
  until the application explicitly invalidates it. This change does not force a
  full QWidget repaint; the queued asynchronous dialog pattern in
  [gotchas.md](../../gotchas.md) remains the preferred application fix.
- Failed deferred allocation retains the old image and retries at each outer
  `endPaint`, until success or a superseding resize. Qt's own OOM logging may
  repeat; only the plugin's diagnostic is limited per backing store. This is an
  exceptional recovery tradeoff, not a tested memory-pressure guarantee.
- The redirected-image test covers the storage-lifetime invariant, not a real
  high-DPI application session. Arbitrary widget deletion during painting,
  recursive error-dialog chains, and general reentrant model mutation are not
  made safe by this change.
- The plugin accepts depth 32 only; 16/24-bit screen formats are outside its
  supported contract. Alpha blending is not changed: upstream framebuffer
  composition uses Source mode. Translucent-parent reentrancy is not qualified.
