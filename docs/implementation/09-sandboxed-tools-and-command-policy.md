# 09 — Sandboxed Tools & Command Policy

## Goal
The security core: path confinement, a command policy, and the tool set that native
runtimes use. Harness runtimes reuse the same policy through their permission hooks.

## Out of scope
Container isolation (18).

## Files
- `gaia/security/paths.py`
- `gaia/security/commands.py`
- `gaia/security/audit.py`
- `gaia/tools/fs.py`, `gaia/tools/shell.py`, `gaia/tools/git_tools.py`, `gaia/tools/registry.py`
- `backend/gaia/security/command_policy.default.toml`

## Tasks
1. `confine(root: Path, user_path: str) -> Path`
   - Reject absolute paths, `~`, and NUL bytes. Join to root, then `resolve(strict=False)`. The result must
     satisfy `is_relative_to(root.resolve())`.
   - Deny the `.git` directory and anything under it.
   - Check symlinks: if the target exists and is a symlink that resolves outside root, deny.
2. Command policy (TOML), evaluated on the argv list:
   ```toml
   [[allow]]  # runs without approval
   cmd = "pytest"
   [[allow]]
   cmd = "git"; subcommands = ["status","diff","add","commit","log","show"]
   [[deny]]   # never runs
   cmd = "git"; subcommands = ["push","reset","clean","rebase","remote","config"]
   [[deny]]
   cmd = ["sudo","curl","wget","ssh","scp","nc","rm"]  # rm via the delete_file tool only
   # Anything else → requires_approval
   ```
   Repos may add allow rules through `.gaia/policy.toml`, but **only allow rules for test or lint
   commands, and only after the owner approves the policy file once**. Repo content is untrusted.
   `evaluate(argv) -> Decision(allow|deny|ask, reason)`.
3. Tools (each is a Pydantic input model plus an async handler, registered with name, description and JSON schema):
   - `read_file(path, start_line?, end_line?)`, capped at 200 KB
   - `list_dir(path, depth≤3)`, ignoring `node_modules`, `.venv` and similar
   - `search(pattern, path?)`, implemented with `rg` through `run_proc` with a fixed argv
   - `write_file(path, content)`, `edit_file(path, old, new)` (exact single match, else error), `delete_file(path)`
   - `run_command(argv)` → policy → `run_proc` (timeout 300s, output cap 64 KB, scrubbed env, cwd = worktree)
   - `git_commit(message)`, which stages everything in the worktree and commits with author `Gaia <agent-name>`
   - When policy says `ask`, the tool returns a `NeedsApproval` result. Doc 12 turns it into an interrupt.
4. Every tool call writes an audit row (tool, args with large content hashed, decision, duration).
5. Writes to `.github/workflows/**`, `**/*.lock`, `package-lock.json`, `.env*` and the `.gaia/` dir return `ask`.

## Tests (aim for full coverage of this module)
- Path escapes: `../x`, `/etc/passwd`, a symlink to outside, `a/../../x`, `.git/config`, `foo\0bar`.
- Policy: `git push` is denied, `pytest -k x` is allowed, `python script.py` asks, `bash -c "..."` asks.
- `edit_file` with zero or multiple matches errors.
- `run_command` env does not contain any secret value.

## Acceptance criteria
- [ ] Reviewed by the architect, since this is security-critical code.
