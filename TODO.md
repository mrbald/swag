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
- [ ] Measure realistic network/performance profiles through the deployed browser/proxy/session path and record remaining limits.
