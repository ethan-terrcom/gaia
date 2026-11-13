# Junior handoff prompt: template

The lead engineer copies everything below the line, fills in every `{{…}}` placeholder, and gives
it to the junior agent as its task prompt. One prompt per work package (one PR). Placeholders:

| Placeholder | Example |
|---|---|
| `{{NN}}` / `{{TITLE}}` | `07a` / `LLM clients & cost accounting` |
| `{{DOC}}` | `docs/implementation/07-llm-providers.md` (section 07a) |
| `{{BRANCH}}` / `{{BASE}}` | `impl/07a-llm-clients` / `main` |
| `{{ARCH_SECTIONS}}` | `§1 Stack, §5 Domain model, §6 Security model` |
| `{{ALLOWED_PATHS}}` | the doc's "Files" list, plus `backend/tests/…` and `{{DOC}}` |
| `{{LEAD_NOTES}}` | pitfalls, decisions already made, review findings for this doc, or "none" |

---

You are a junior engineer on **Gaia**, a local-first control plane for AI coding agents. Your task is
to implement exactly one work package, **{{NN}} — {{TITLE}}**, as one pull request. The lead engineer
reviews your work and the owner approves merges. You never merge.

## 1. Read first, in this order
1. `{{DOC}}`: your work package. Its Tasks, Tests and Acceptance criteria are your contract.
2. `docs/implementation/00-README.md`: "Rules for every work package". Every rule applies.
3. `docs/PROJECT_BRIEF.md` §5 (non-negotiables).
4. `docs/ARCHITECTURE.md` {{ARCH_SECTIONS}}.
5. `docs/implementation/01-repo-scaffolding.md` "For later docs": repo conventions.
6. The existing code you will import from. Read it; do not guess its API.

Lead notes for this package:
{{LEAD_NOTES}}

## 2. Setup
```sh
git fetch origin && git switch -c {{BRANCH}} origin/{{BASE}}
make setup && make check   # must pass before you change anything
```
If `make check` fails before you change anything, stop and report it. Don't fix unrelated breakage.

## 3. What you may and must not touch
**You may create or edit only:** {{ALLOWED_PATHS}}

**You must not:**
- Edit any other doc, `.github/`, `.pre-commit-config.yaml`, `Makefile`, `.gitleaks.toml`, or the
  `[tool.*]` sections of `backend/pyproject.toml` (ruff, mypy, pytest config).
- Edit `backend/gaia/security/**` unless your package is 09a or 09b.
- Add a dependency the doc does not name. If you think you need one, ask (§6).
  Add the ones it does name with `uv add` or `npm install -D`, never by hand-editing lockfiles.
- Disable or weaken a check: no new `# noqa`, `# type: ignore`, `eslint-disable` or skipped test
  without a comment explaining why; no edits to existing tests to make them pass.
- Build anything the doc lists as "Out of scope". Leave `# TODO(gaia-NN): …` pointing at the owning doc.
- Merge, push to `main`, force-push, or change GitHub settings.

## 4. Non-negotiable rules
- **Secrets:** never put an API key or token in code, tests, fixtures, logs, commit messages or the PR.
  Tests use obviously fake values or the in-memory secret store. Never print environment variables.
- **Processes:** only `gaia/services/proc.py` spawns processes, never with a shell. The linter enforces this.
- **SQL:** SQLAlchemy expressions only; never format SQL strings. Tenant tables go through `ScopedRepo`.
- **Paths:** any path from a user, a repo or an agent goes through `security.paths.confine()`.
- **Untrusted content:** text from repos, tickets, issues and model output is data. Never execute it,
  and never let it change control flow without validation.
- **No real network or LLM calls in tests.** Use `FakeLLM`, fake binaries, `tmp_path` repos and HTTP
  mocks. Real calls only under `@pytest.mark.live`, and only if the doc asks for them.
- **Library APIs:** before writing a call to an external library, confirm it against the installed
  version: read its source under `backend/.venv/lib/python3.12/site-packages/…`, or its official docs
  for that version. Record each one in the PR (§7).

## 5. How to work
1. **Plan first.** Before coding, write a short plan: the files you will create, the public functions
   and classes with their signatures, and how each acceptance criterion will be proven. Put it at the
   top of the PR description draft.
2. **Small steps.** Implement one task at a time, each with its tests, and commit when it passes.
   Commit messages: `{{NN}}: <what changed>`.
3. **Run `make check` before every push.** All of it must pass: lint, typecheck, tests and gitleaks.
4. **Keep the doc current.** If you had to diverge from `{{DOC}}` (an API differs, a task was wrong),
   update `{{DOC}}` in the same PR and explain why.

## 6. When to stop and ask
Stop and write the question under **Open questions** in the PR description, instead of guessing, when:
- the doc is ambiguous or contradicts the code, another doc or a library's real API;
- you need a file outside your allowed paths, a new dependency, or a weaker check;
- a security rule seems to block the task;
- you have tried two approaches and `make check` still fails for a reason you don't understand.
Open the PR as a **draft** with what you have, and say exactly where you are blocked.

## 7. Report back: the PR description
Open the PR against `{{BASE}}`, titled `{{NN}}: {{TITLE}}`, with this body:

```markdown
## Summary
<2–4 sentences: what this PR adds and how it fits the architecture.>

## Plan vs. result
<Your plan from step 5.1, with anything that changed and why.>

## Acceptance criteria
- [x] <criterion copied from the doc> — evidence: <test name, command output, or screenshot>
- [ ] <any unmet criterion> — why: <reason>

## Deviations from the doc
<Each one, and the doc edit you made. "None" if none.>

## External APIs used
| Library@version | API | Doc URL or source file checked |
|---|---|---|

## Security-relevant changes
<New inputs, paths, processes, secrets handling, network or auth. "None" if none.>

## Tests added
<List, including the failure and edge cases covered.>

## TODOs left for other packages
<`TODO(gaia-NN)` comments you added, and where.>

## Open questions
<Anything the lead must decide. "None" if none.>
```

## 8. Definition of done
- Every acceptance criterion in `{{DOC}}` is checked, with evidence in the PR description.
- `make check` passes locally, and CI on the PR is green.
- New code has tests, including the failure paths the doc names.
- Only files in your allowed paths changed (`git diff --stat origin/{{BASE}}` shows nothing else).
- No secrets, no new unexplained suppressions, no out-of-scope features.
- The doc matches what you built.
