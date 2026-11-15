# Handoff: 18d-tests part 2 — make the checklist tests pass

Same rules as `18d-security-checklist-tests.md` (read it first, §2–§4): **tests only, no
production code**; a real gap becomes `xfail(strict=True, reason="18d finding: …")` plus a line
in your report.

## Where things stand
Part 1 wrote `backend/tests/test_security_checklist.py` and `…_live.py` but could not run anything:
its `run` calls passed `argv` as a JSON **string**. **Pass `argv` as a JSON array**, e.g.
`{"argv": ["uv", "run", "pytest", "-q", "tests/test_security_checklist.py"]}`.

The lead fixed lint and mypy. Then 11 tests passed, 1 xfailed, the live ones skipped, and **6
failed**. The full output is in `docs/implementation/handoffs/18d-part2-failures.txt`:

| Test | Failure | Likely cause (verify) |
|---|---|---|
| `test_planted_secrets_never_reach_db_logs_or_events` | `httpx.StreamConsumed` | The gateway response is streamed; read it once (`aread()`), or use a non-streaming request, as `tests/test_gateway.py` does. |
| `test_every_route_requires_auth` | `assert {public allowlist} <= set()` | **The route enumeration finds nothing.** The app's routes are nested (routers included under `/api/v1`, plus the gateway and the stream's public router). Walk `app.routes` correctly: print them first. |
| `test_mutating_route_audit_evidence_table` | `assert {…} == set()` | The same enumeration bug. |
| `test_start_run_writes_audit_row` | 404 instead of 201 | The ticket isn't in the API's workspace, or the fake `RunManager` doesn't find it; build the rows like `tests/test_api_runs.py` does. |
| `test_cancel_run_writes_audit_row`, `test_approval_decide_writes_audit_row` | `FOREIGN KEY constraint failed` on `INSERT INTO run` | The run row's parents (ticket, repository, user) don't exist in that workspace. Use `tests/db_builders.make_chain`, or the helpers in `tests/test_api_runs.py` / `tests/test_api_decide.py`. |

## Do
1. Fix those six tests. **Run** the file after each fix.
2. Then run the whole suite and the checks (§5 of the part-1 handoff). The suite must pass at
   **100% coverage**.
3. If a fix shows a real product gap, use `xfail(strict=True)` and a report entry; don't change
   production code.
4. Delete `docs/implementation/handoffs/18d-part2-failures.txt` when you're done.

## Report (pass to `finish`)
Summary · What each failure was · Findings (xfails) · Final check output (**real output this
time**).
