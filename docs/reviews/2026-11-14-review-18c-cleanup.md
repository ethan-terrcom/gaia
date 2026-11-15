# Review: 18c-cleanup — removing worktrees and scratch dirs of finished runs

- **Date:** 2026-11-14 · **Reviewer:** lead engineer (Claude) · **Implementer:** DeepSeek junior
- **Handoff:** [`handoffs/18c-worktree-cleanup.md`](../implementation/handoffs/18c-worktree-cleanup.md)
- **Verdict:** **merge as is.**

## Summary for the owner
Gaia can now reclaim disk from runs that finished a while ago. It removes their checkouts and its
own scratch files, and it **never** touches a checkout with uncommitted changes, unmerged commits
or signs of tampering. It is **manual and dry-run by default**:
- `gaia worktrees cleanup` shows what it would remove;
- `--apply` removes it;
- automatic daily cleanup is available, but **off** until you turn it on
  (`GAIA_CLEANUP_ENABLED=true`). That is your call; see the summary.

## Checked
- **Candidates:** every run on the worktree is terminal; the newest finish is older than N days;
  any null finish time → kept (stricter than the handoff).
- **Kept, with a reason:** dirty, tampered, unmerged commits (unless the PR is merged), or a
  symlinked row path.
- **`apply`:**
  - `remove(force=False)` only, so a worktree that became dirty since planning is refused and
    reported;
  - scratch dirs are re-checked for symlinks and `resolve_within(data_dir/runs)` right before
    `rmtree`;
  - only directories named after existing terminal runs.
- Every removal is logged and audited (`actor=system`).
- The CLI is a dry run unless `--apply`; the background task is opt-in, and a failing round is
  logged.

## Note
Worktree removal runs git inside a DB session. That is `WorktreeService.remove`'s existing design
(doc 05), not new code. Recorded only.

## Final checks
`ruff` ✓ · `mypy` ✓ · `pytest`: 1731 passed, **100%** coverage.
