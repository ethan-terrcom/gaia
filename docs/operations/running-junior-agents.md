# Running DeepSeek junior agents

How the lead engineer (a Claude Code session) builds Gaia today: it writes a handoff for each work
package, runs a **DeepSeek junior** on it in a sandbox, reviews the result, and merges it. This is
the same lead/junior loop Gaia will run for its users, done by hand. Each package teaches us
something about running juniors, and we turn those lessons into skills that Gaia's own lead will
reuse.

**Read this if** you are a new lead session picking up implementation work, or you want to
understand how a package went from a doc to a merged PR.

| Need | Where |
|---|---|
| The harness (agent loop + sandbox) | [`backend/spikes/native_deepseek_loop/agent.py`](../../backend/spikes/native_deepseek_loop/agent.py) |
| Run statistics and cost | [`backend/spikes/native_deepseek_loop/runstats.py`](../../backend/spikes/native_deepseek_loop/runstats.py) |
| Short, task-shaped checklists | [`.claude/skills/`](../../.claude/skills/): `write-junior-handoff`, `review-junior-package`, `land-junior-package` |
| Handoffs written so far | [`docs/implementation/handoffs/`](../implementation/handoffs/) |
| Reviews of junior work | [`docs/reviews/`](../reviews/) |

---

## 1. The model in one picture

```
 owner ──approves scope, merge rights──▶ lead (Claude Code session)
                                           │ 1. write handoff (design decided)
                                           │ 2. create worktree, install deps
                                           ▼
                         ┌─────────── sandbox-exec ───────────┐
                         │ junior (deepseek-v4-pro tool loop) │
                         │  read/write files in ONE worktree  │
                         │  run: uv run pytest|ruff|mypy|...  │
                         │  no network · no git writes        │
                         └──────────────┬─────────────────────┘
                                        │ report via finish()
                                        ▼
                        lead: 3. review + fix small bugs + flag big ones
                              4. docs: review file, guides, README
                              5. commit (pinned GIT_DIR) → PR → CI → squash-merge to main
```

The roles are fixed. The **junior** writes code and tests. The **lead** decides the design, reviews,
does everything that needs the network or git write access, and merges. The **owner** (the human
chief architect) decides product, cost and security trade-offs, and grants merge rights for each
session.

## 2. Ground rules (from the owner)

- **Every package gets its own branch, cut from the current `main`, and merges directly to
  `main`.** Never stack PRs, and never commit on the main checkout. Merge rights are granted per
  session, by the owner, and only for branches that are pushed with a PR and green CI.
- **Fix slight bugs yourself; flag major functionality or security concerns** to the owner with a
  recommendation. The test: does it remove data, change a contract, trade off security, or cost
  money? If so, flag it.
- **Documentation is part of done.** Each package leaves:
  - a review in `docs/reviews/`;
  - a "Decisions and deviations (as built)" section in its implementation doc;
  - updated guides in `docs/backend/`, which carry explainers for intermediate Python developers;
  - an updated `backend/README.md`.
- **Secrets never appear anywhere:** not in code, logs, transcripts, PRs or terminal output.

## 3. Prerequisites (once per machine)

| What | Why | Check |
|---|---|---|
| `DEEPSEEK_KEY` exported in `~/.zshrc` | the junior's API key | `zsh -ic 'test -n "$DEEPSEEK_KEY" && echo set'` (never echo the value) |
| `CLAUDE_KEY` exported in `~/.zshrc` | the owner's Anthropic key, for live lead-side tests only | same check |
| `uv`, Node (`.nvmrc`), `gh` (authenticated) | sync, hooks, PRs | `gh auth status` |
| macOS `sandbox-exec` | the harness sandbox | `which sandbox-exec` |

The keys are read only through `zsh -ic`, because a non-interactive shell does not load
`~/.zshrc`. Gaia itself keeps keys in the macOS Keychain (`gaia secrets set`). Moving the owner's
keys there is still an open follow-up.

## 4. Step by step

### 4.1 Pick the next packages
Read the build-order table and the waves in
[`docs/implementation/00-README.md`](../implementation/00-README.md). A package may start only once
everything in its "Depends on" column is merged. Packages in the same wave can run in parallel,
each in its own worktree. Three at a time has worked well; your bottleneck is review, not DeepSeek.

Some packages are **not** for juniors:
- **08** is the spike and ADR;
- **12a** is lead-owned or pair-reviewed;
- the prompt files in **11** are written by the lead.

Security-critical packages (**09a/09b**, **18d**) also get an architect review.

### 4.2 Write the handoff
Follow the `write-junior-handoff` skill. The most important part is **"Lead notes"**: decide the
design, and give facts the junior cannot look up offline, such as library behaviour pinned to a
version or a tool's config schema. Read the real package or docs yourself first, for example
`npm pack <pkg>@<version>` into a scratch folder, or the installed source under
`backend/.venv/lib/python3.12/site-packages/`. The handoff file lives on the package branch.

### 4.3 Create the worktree and install everything up front
The junior has no network, so it cannot install anything.
```sh
SP=<your session scratchpad>
git worktree add -b impl/<NN>-<name> $SP/worktrees/<NN>-<name> main
(cd $SP/worktrees/<NN>-<name>/backend  && uv sync)
(cd $SP/worktrees/<NN>-<name>/frontend && npm ci)      # the pre-commit hook needs it at commit time
cp <handoff>.md $SP/worktrees/<NN>-<name>/docs/implementation/handoffs/
```
If the package needs a new dependency, add it yourself on the branch **before** you dispatch.
Pin it to a version that is at least two weeks old: check the release date on PyPI or npm.

### 4.4 Dispatch
```sh
mkdir -p $SP/runs
cd /Users/ef/terrapincs/gaia/backend/spikes/native_deepseek_loop   # trusted copy on main
zsh -ic "exec uv run --no-project --with httpx python agent.py \
  --worktree $SP/worktrees/<NN>-<name> \
  --task $SP/worktrees/<NN>-<name>/docs/implementation/handoffs/<NN>-<name>.md \
  --log $SP/runs/<NN>.jsonl --report $SP/runs/<NN>-report.md --max-turns 220" \
  > $SP/runs/<NN>.out 2>&1
```
In Claude Code, run this with `run_in_background: true`; you are notified when it exits, so do not
poll. Run the harness from the copy on `main`, never from a junior's worktree, because a junior
could have edited that copy.

What the harness does:
- **Tools:**
  - `read_file`, `list_dir`, `search`, `write_file` and `edit_file` reach only inside the worktree.
    Paths are resolved, and `.git` is refused.
  - `run` accepts an argv list with no shell, allowlisted to `uv run
    {pytest,ruff,mypy,alembic,python,gaia}` and `git {status,diff,log,show,ls-files}`.
  - `finish(report)` ends the run.
- **Sandbox:** every `run` command executes under `sandbox-exec` with a generated profile:
  - remote network is denied;
  - writes are allowed only to the worktree, a per-run tmp dir, the uv cache and `/dev`, and never
    to `<worktree>/.git`;
  - reads of shell profiles, `~/.ssh`, `~/.aws`, `~/.config`, `~/.gnupg`, `~/.gaia`, `~/.claude`
    and the keychains are denied.
- **Environment:** the child environment is built from scratch: `UV_OFFLINE=1`, `UV_NO_SYNC=1`, git
  config isolation, and `GAIA_DATA_DIR` pointing at a tmp dir. The DeepSeek key is popped from
  `os.environ` at startup, and every `*KEY*`, `*TOKEN*` and `*SECRET*` variable is removed. The key
  is used only in the HTTPS header to DeepSeek.
- **Model:** `deepseek-v4-pro`, thinking on by default. `reasoning_content` is passed back on each
  turn, which DeepSeek requires for tool loops. Up to 32K output tokens per turn. Transient API
  errors (429/5xx) retry with backoff.
- **Logs:**
  - `<NN>.jsonl` holds every model message, usage block and tool call (args truncated to 2,000
    characters, results to 4,000);
  - `<NN>.out` holds one progress line per turn;
  - `<NN>-report.md` holds the junior's final report, plus a usage comment.

### 4.5 While it runs
- Peek at progress only if asked: `tail -1 $SP/runs/<NN>.out`.
- Use the time on the next handoffs, on spike work, or on reviewing an earlier package.
- Typical run: **14–25 minutes, 120–130 turns, 16–19M prompt tokens at about 99% cache hits, 75–100K
  output tokens.** That costs **about $0.55–$1.40 per package**, at DeepSeek's off-peak and peak
  prices (`runstats.py` computes it).

### 4.6 Review
Follow the `review-junior-package` skill. In short:
1. Run `make check` in the worktree.
2. Check the diff's scope against the handoff's file list.
3. Read the report's deviations and open questions.
4. Hunt the bug classes juniors' tests hide (that skill has the table with real examples from
   Gaia).
5. Run the acceptance criterion live: temporary `GAIA_DATA_DIR`, port 8799, the token kept in a
   shell variable, and afterwards `grep -c` showing the token appears 0 times in the server log.
6. Fix small things on the branch, with a test each; flag the big ones.
7. Write `docs/reviews/<date>-review-<NN>.md` and update the guides and README.

### 4.7 Land
Follow the `land-junior-package` skill. The one non-obvious step is **committing without trusting
the worktree**. The junior could have planted a `.git` file that points git at a config which runs
code (finding F1). So, before any git command in the worktree:
```sh
WT=$SP/worktrees/<NN>-<name>; NAME=<NN>-<name>
test ! -L "$WT/.git" && [ "$(cat "$WT/.git")" = "gitdir: /Users/ef/terrapincs/gaia/.git/worktrees/$NAME" ]
export GIT_DIR=/Users/ef/terrapincs/gaia/.git/worktrees/$NAME GIT_WORK_TREE=$WT
```
Then:
1. `git add -A`, then `git status --short`. Read the list.
2. Commit, with the attribution line. Let pre-commit run, and never use `--no-verify`.
3. `git push -u origin impl/<NN>-<name>`.
4. `gh pr create --base main`.
5. `gh pr checks <n> --watch`.
6. `gh pr merge <n> --squash --delete-branch`.
7. On the main checkout: `git pull --ff-only`, then `git worktree prune`.

After a parallel batch, merge one PR at a time. Rebase each remaining branch on the new `main`,
then resolve the usual conflicts: `AppServices` in `api/deps.py`, router includes in `api/app.py`,
and the Alembic revision chain. Rerun `make check`, then push with `--force-with-lease`.

## 5. What we have learned

| Lesson | Consequence |
|---|---|
| Juniors' tests encode their own model of an API, so stubs hide wrong request shapes (07a sent `tool_choice` without tools). | Give real API facts in the handoff, and verify live at review. |
| Juniors take the most permissive reading of an unspecified security choice. | Pre-decide security choices in the lead notes. |
| A junior cannot prove "live" criteria (`curl`, a real sandbox, a real provider). | Ask for a test-level proof plus a dev hook (`GAIA_DEV_ROUTES`, `scripts/dev_*.py`) that the lead runs. |
| macOS refuses to nest `sandbox-exec`, so a junior cannot run `srt` tests. | Mark them `skipif` without `GAIA_SRT_PATH`; the lead runs them. |
| `httpx.ASGITransport` buffers whole responses. | Write SSE bodies as standalone async generators. |
| Coverage does not trace SQLAlchemy's greenlets. | Add a direct-call test when a line inside a session shows as missed. |
| A missing `frontend/node_modules` makes the pre-commit hook fail silently. | Run `npm ci` in every worktree. |
| Context caching makes long sessions cheap: about 99% of prompt tokens are cache hits. | Prefer one thorough session per package over many short ones. |
| Destructive defaults (cascading deletes) pass every test. | Ask "what disappears with this row?" in every review. |

## 6. When something goes wrong

| Symptom | Likely cause and fix |
|---|---|
| The report says `MAX TURNS REACHED` | The package is too big, or the junior is stuck. Read the last 20 transcript entries. Split the package, or re-dispatch with clarified lead notes. Edit the handoff on the branch. |
| `run` returns `exit=1` with uv trying to download | A dependency was missing at dispatch. Stop, `uv sync` on the branch, and re-dispatch. |
| `API 4xx` in `<NN>.out` | Usually a request-shape change at DeepSeek, or an exhausted balance. Check the error text in `.out`; it never contains the key. |
| The junior edited a file outside its list | Revert that file on review, and note it in the review doc. |
| The commit fails in pre-commit | Fix it and re-add. `node_modules` missing → `npm ci`. Formatting → let the hook fix it, then re-add. |

## 7. Open follow-ups for this process
- **ADR-001 (doc 08)** decides which runtimes Gaia ships. This harness is the "native DeepSeek loop"
  candidate, and the evidence from these runs feeds the ADR.
- **Gateway:** once 07b is merged, the harness should call DeepSeek through Gaia's gateway with a
  per-run token, so even the harness process holds no provider key.
- **srt:** once 09a is merged, move the harness from a hand-written `sandbox-exec` profile to the
  `srt` profile Gaia generates.
- **Frontend packages:** packages 16 and 17 need `npm run lint|test|build` added to the `run`
  allowlist.
