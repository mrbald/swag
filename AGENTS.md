# Working rules

- Keep planning lightweight: `SPEC.md` is the agreed scope; `TODO.md` is a short checklist.
- Root owns integration, top-level build files, browser checks, and task status.
- Delegate bounded tasks with disjoint file ownership. Workers return changed files,
  checks, evidence paths, and blockers; do not concurrently edit the checklist.
- Preserve unrelated user changes. Use only `swag-*` names for test containers.
- Compare the same Qt executable and browser client. Browser tests must use real
  input through noVNC; application instrumentation may observe, not perform actions.
- Distinguish source observations, runtime measurements, and untested behavior.
- Keep output concise; retain full logs under ignored `artifacts/`.
- Flag a good `/clear` point after a completed self-contained task whose history is
  no longer needed, or `/compact` when obsolete tool output dominates. Never suggest
  either during active work, running agents, or verification of uncommitted changes.
