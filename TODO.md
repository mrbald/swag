# Work

- [x] Capture the agreed scope and lightweight working rules.
- [x] Build one Qt interaction probe and both EL8 rendering launch paths.
- [x] Run browser input, visual, reconnect, and process-death checks; choose TigerVNC.
- [x] Verify the selected renderer over a private Unix socket with RFB TCP disabled.
- [x] Prove authenticated two-user session ownership (strictly zero or one Qt process per user).
- [x] Add the domain model, shared SQLite CRUD backend, and generated Qt grids/forms.
- [x] Verify browser CRUD/conflicts/reconnect, Linux ownership/death, and uncertain-write reconciliation.
- [x] Verify failed startup components are rejected and leave the authenticated demo running.
- [x] Fix cached sign-in pages and expired-tab recovery; cover logout/relogin and stale responses.
- [x] Add independent MDI child frames and qualify the mode without Openbox.
- [x] Resize the framebuffer and Qt root to the browser on connect, resize and takeover.
- [x] Reduce browser/Qt shell overhead and qualify user-controlled browser fullscreen.
- [x] Document how to adapt an existing multi-frame Qt application to this setup.

Root maintains this checklist. No separate task system.

- [x] Add opt-in in-process Qt WebSocket probe backend and reproducible build.
- [x] Verify basic controls, wheel and clipboard through noVNC; record evidence and limits.
- [x] Make the plugin directory build/install independently with the client's Qt 5.15.
- [x] Harden listener configuration, protocol parsing, framebuffer bounds and viewer cleanup.
- [x] Run standalone build, protocol fault and noVNC regression/lifecycle gates; commit results.
- [x] Integrate the optional Qt WebSocket backend with the original authenticated MDI portfolio app and verify its workflows; retain TigerVNC fallback.
- [x] Add bounded Qt WebSocket autoresize; verify wire replies and original MDI resize/reconnect/takeover workflows.
- [x] Add negotiated Unicode clipboard; verify multilingual text, empty clipboard and malformed/bounded payloads.
- [x] Measure realistic network/performance profiles through the deployed browser/proxy/session path and record remaining limits.
- [x] Add negotiated lossless framebuffer compression and bounded update pacing with Raw fallback.
- [x] Verify compressed pixels, negotiation/stream lifetime and slow-reader recovery; rerun browser regressions and compare the same WAN profiles.
- [x] Diagnose and reduce the local-latency regression without losing bounded pacing or WAN compression gains; rerun focused correctness and network gates.
- [x] Fix stale input modifiers and horizontal wheel direction; verify wire ordering and real noVNC input.
- [x] Keep each Qt listener reserved through manager-controlled generation teardown; verify endpoint-reuse races and failure cleanup.
- [x] Remove the private Qt heartbeat timeout; verify recovery after a GUI stall, run focused regression gates and restore the MDI demo.
- [x] Batch pending cursor and pixels into one response; independently verify framing, pixels and deferred updates.
- [x] Add serialization-aware browser clipboard preflight with accessible rejection and focused boundary tests.
- [x] Review and integrate both worktrees; pass standalone/combined builds and real noVNC functional gates.
- [x] Compare final network profiles and complete a two-user 30-minute soak with resource/cleanup evidence.
- [x] Commit verified checkpoints, record limits and restore the demo.
- [x] Complete bounded plugin defect hunt: fix reproduced Super-key and multi-button transition failures; record evidence and client-dependent findings.
- [ ] Parked by user: session failure-path review and retry blocked by automated model cybersecurity filter; no review result.
- [x] Add and verify a short top-level README guide for transplanting the plugin into the client application.
- [x] Preserve multiline workspace clipboard text and prove a two-row TSV roundtrip through the actual viewer.
- [x] Distinguish keypad Enter/Return and verify their Qt actions through noVNC.
- [x] Support 3440x1440 with coherent bounded geometry/transport limits; verify pixels, resize, cursor, rejection and recovery.
- [x] Qualify actual Qt item drag-and-drop through noVNC against the Tiger reference; fix only reproduced failures.
- [x] Fix opaque custom-cursor mask truncation exposed by the large-frame acceptance test; verify RGB and alpha cursors.
- [x] Integrate, pass three ultrawide network profiles and a five-minute two-user soak, restore the demo, and record the next everyday-behavior review candidates.
- [x] Reproduce and fix autorepeat semantics; pass 65 focused checks, commit and push the slice.
- [x] Fix the keyboard Context Menu mapping and Qt context event; pass 60 focused checks, commit and push the slice.
- [x] Supply Japanese font coverage; verify actual fallback glyphs and 38 browser checks, restore the demo, commit and push the slice.

- [x] Surface unavailable oversized outbound clipboard text; verify real noVNC/shell failure and recovery plus functional regressions, commit/push.
- [x] Add one functional regression entry point; pass 7 runner contract tests, 126 default checks and 26 selected app checks, commit/push.
- [x] Qualify the installed probe/plugin in a clean runtime-only EL8 image; verify packages/libraries/hashes and 4 real browser checks, document dependencies, commit/push.

- [x] Add bounded Unix RFB plugin transport, safe pathname/inherited-FD ownership and native acceptance; commit/push.
- [x] Integrate qt-unix manager/launcher and tools, verify lifetime/admission/cleanup and browser regressions; commit/push.
- [x] Qualify integrated Unix transport, record limits and restore the demo using the safer mode.

- [x] Reorganize integration recipes and qualification history; verify viewer/FD examples, peer-review and commit/push.
- [x] Track and conventionally apply the noVNC clipboard patch; verify unchanged output and failure guards, peer-review and commit/push.
