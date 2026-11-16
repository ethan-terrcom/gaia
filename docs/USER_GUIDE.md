# Gaia user guide: from install to your first merged change

> **Audience:** someone running Gaia for the first time. You know git and a terminal; you don't
> need to know anything about Gaia's internals.
>
> **Time:**
> - about 15 minutes to install and watch a free demo run;
> - about 15 more to connect your own API keys and repository.

## 1. What Gaia does
You write a **ticket**: "add a farewell() function with a test". Gaia then works through it:

```
ticket ─► lead plans (Claude) ─► you approve the plan
       ─► junior implements (DeepSeek), in its own git worktree, inside a sandbox
       ─► Gaia runs your tests ─► lead reviews ─► repeat per step
       ─► you approve opening a PR ─► CI runs ─► you type "merge 42" ─► merged
```

Everything happens **on your machine**. The agents work in a separate checkout of your repository
(a *git worktree*) on a `gaia/…` branch, so your own working copy is never touched. Gaia stops and
asks you before anything irreversible:
- before acting on a plan;
- before running an unusual command;
- before spending more than the budget;
- before opening a PR;
- before merging.

## 2. Before you start
You need a Mac (Gaia's sandbox uses macOS Seatbelt) with:

| Tool | Install |
|---|---|
| [uv](https://docs.astral.sh/uv/) 0.13+ | `brew install uv` (it fetches Python 3.12 itself) |
| Node.js 24 or 26 | `brew install node` |
| git 2.40+ | Xcode command line tools: `xcode-select --install` |
| [gh](https://cli.github.com/) and [gitleaks](https://github.com/gitleaks/gitleaks) (for contributors) | `brew install gh gitleaks` |

For real runs you will also need, later in this guide:
- an **Anthropic API key** for the lead;
- a **DeepSeek API key** for the juniors;
- optionally, a **GitHub fine-grained token**, to open and merge PRs.

The demo in §4 needs none of them.

## 3. Install
```sh
git clone <this repository> gaia && cd gaia
make setup              # Python and Node dependencies
make sandbox-install    # the pinned sandbox (srt) into ~/.gaia/tools
make migrate            # creates the database at ~/.gaia/gaia.sqlite
```
Everything Gaia keeps lives in `~/.gaia/`: the database, logs, worktrees and the sandbox tool.
Change the location with `GAIA_DATA_DIR=/some/path` on every command.

## 4. Watch a run for free (demo mode)
Demo mode runs **all of Gaia for real**, including the sandbox, the tests, git and the approvals,
except that the AI models are replaced by a scripted stand-in. It costs nothing and needs no keys.

```sh
make demo
```
This does three things:
1. creates a small demo repository (`greet.py` with a unit test) under `~/.gaia/demo/greet`;
2. registers it with Gaia and adds a ticket, "Add a farewell() function";
3. builds the UI and starts Gaia with `GAIA_FAKE_LLM=1`, printing a login link like
   `http://127.0.0.1:8765/#token=…`.

**What you will see.** `make demo` prints a lot (database migration, the UI build), then a line
`demo initialized` with a "Demo ready" note. That note is only information: **you do not need to
run another command.** Gaia then starts and the terminal stays busy, with no prompt, because the
server is running. That is normal. Leave that terminal alone, look for the line starting
`http://127.0.0.1:8765/#token=`, and open it in your browser. If you need a terminal for other
commands, open a second one. Press `Ctrl-C` in the first to stop Gaia.

The first time, macOS may ask whether Python can use the Keychain: Gaia stores its own login
token there. Allow it.

**Open that link.** The token moves into the browser tab's session storage and leaves the address
bar. Keep the link private: it is your login. A **Demo mode** badge in the top bar reminds you
that no real model is being called. Then:

1. **Tickets** (`g t`): find "Add a farewell() function" and press **Start run**. Leave the
   runtime and budget at their defaults. You land on the run's page.
2. **The run page** shows the status, the branch Gaia created, and a live **Timeline**. In a few
   seconds the lead "plans", and the run waits for you.
3. **Approvals** (`g a`, or the badge in the sidebar): the plan shows its one step, "Add a demo
   note", and its test command. Press **Approve** (or `a`).
4. Back on the run page, watch the junior:
   - write a file;
   - run the tests in the sandbox;
   - finish.

   Gaia runs the tests again itself, the lead reviews, and Gaia commits. Open the **Diff** tab to
   see the change.
5. A **"Open PR?"** approval appears, showing the lead's verdict and any issues it raised. The
   demo repository has no GitHub remote, so this is where the demo ends. **Reject** it to close
   the run; the demo commit stays on its `gaia/…` branch in `~/.gaia/demo/greet`. With a real
   repository (§6), approving opens a draft PR, Gaia watches CI, and a final **merge** approval
   asks you to type `merge <number>`.

Press `Ctrl-C` in the terminal to stop Gaia.

## 5. Connect your keys
Keys go into the **macOS Keychain**, never into a file or your shell profile. Each command prompts
for the value without echoing it:

```sh
cd backend
uv run gaia secrets set ANTHROPIC_API_KEY    # the lead: planning and review
uv run gaia secrets set DEEPSEEK_API_KEY     # the juniors: implementation
uv run gaia secrets set GITHUB_TOKEN         # optional: PRs, CI status, merges
uv run gaia secrets list                     # shows set / not set, never a value
```

If you already `export` a key in `~/.zshrc`, store it this way, delete the `export` line, and
**rotate the key** in the provider's console: it has been on disk in plain text.

**The GitHub token.** Create a *fine-grained* personal access token
(GitHub → Settings → Developer settings), limited to the repositories Gaia will work on, with:

| Permission | Access |
|---|---|
| Contents | Read and write |
| Pull requests | Read and write |
| Checks / Commit statuses | Read |
| Issues | Read |

Check it with `uv run gaia github check --repo <name>` once the repository is registered (§6). Also
**protect your default branch** on GitHub (require a PR), as a second safety net.

## 6. Your first real run
Start Gaia without demo mode:
```sh
make run          # builds the UI, prints your login link, and serves Gaia on 127.0.0.1:8765
make ui-url       # any time later, in another terminal: prints the login link again
```

### 6.1 Register a repository
**Worktrees** (`g w`) → **Register repository**:

| Field | What to enter |
|---|---|
| Name | A short name, for example `my-service` |
| Local path | The absolute path of your clone, for example `/Users/you/code/my-service` |
| Test command | How to run the tests, as you would type it: `uv run pytest -q`, `npm test`. There is no shell, so no pipes or `&&`. |
| Setup command | What a fresh checkout needs before the tests can run, for example `uv sync` or `npm ci`. Setup is the only step allowed to reach package registries. It runs in the sandbox, so it cannot install git hooks: for Gaia itself use `make setup-worktree`, not `make setup`. |

To change a command later, press **Edit commands** on the repository's card. The next run uses
the new values. (The path, name and remote cannot be edited.)

Gaia never changes your clone. It creates worktrees from it on `gaia/…` branches. For PRs, the
clone's `origin` must be a GitHub HTTPS remote.

### 6.2 Write a good ticket
**Tickets** → **New ticket**. The lead plans from the ticket, so write it like an issue for a new
colleague:
- **What** should change and **why**;
- **Where**, if you know: files, modules or endpoints;
- **Done means**: the tests or behaviour you expect.

Small, well-scoped tickets work best. A plan has at most 10 steps.

### 6.3 Run it and make the decisions
Press **Start run**. Optionally pick the junior runtime, and set a **budget** for this run (the
default is $5). Then work from the **Approvals** inbox:

| Approval | What you see | What your answer does |
|---|---|---|
| **Plan** | Summary, risks and steps, each with its files and test command | **Approve** starts the work. **Edit steps** lets you change titles, instructions and test commands, then approve your version. **Reject** ends the run. |
| **Command** | The exact command, one argument per box, its directory, and the agent's reason | The agent asked to run something outside the allowlist. **Approve** runs it in the sandbox once; **Reject** tells the agent no. |
| **Escalation** | Why the step keeps failing, and the reviewer's feedback | **Approve** retries the step; **Reject** stops the run. |
| **Budget** | Spent so far, and the cap | **Approve** grants another run budget, or the amount you type. A *daily* cap stays a hard limit: approve again once you have raised `GAIA_DAILY_BUDGET_USD`, or after midnight UTC. |
| **Open PR** | The lead's verdict, feedback and issues, plus a link to the full diff | **Approve** pushes the `gaia/…` branch and opens a draft PR. |
| **Merge** | The PR and its CI state | Type `merge <number>` to confirm. Nothing else merges. |

Keyboard: `j`/`k` move, `a` approves, `r` rejects. `a` never merges.

While it runs, the **run page** shows every agent message and tool call live, and the **Diff** tab
shows what changed. **Cancel run** stops it at any time; the worktree is kept, so no work is lost.

## 7. Day to day
- **Dashboard** (`g d`): active runs, what needs you, open PRs, today's spend, and the last 14
  days.
- **Runs** (`g r`): every run, newest first, with its status, current step and cost. Filter by
  Active, Failed, Completed or Cancelled; click a run to open its page.
- **Spend:** the top bar shows today's spend against the daily cap (default $20). A run that hits
  its cap pauses and asks you. At the daily cap, new runs are refused until tomorrow (UTC) or until
  you raise the cap.
- **The emergency stop:** **Pause all** in the top bar stops every running agent at once and holds
  every run, until you press **Resume all**. Nothing is lost: runs continue from their last
  completed step.
- **PRs** (`g p`): every PR Gaia opened, with CI and review state.
- **Worktrees** (`g w`): every checkout Gaia made, with flags for uncommitted changes and for
  anything unexpected. Reclaim disk from finished runs with
  `uv run gaia worktrees cleanup` (a dry run: it shows what it would remove), then `--apply`. Gaia
  never removes a worktree with uncommitted or unmerged work.
- **Logs:** `~/.gaia/logs/gaia.jsonl`, one JSON object per line, with every secret redacted.

## 8. Settings you might change
Set any of these as environment variables when starting Gaia:

| Variable | Default | Meaning |
|---|---|---|
| `GAIA_RUN_BUDGET_USD` | `5` | Default budget per run |
| `GAIA_DAILY_BUDGET_USD` | `20` | Hard cap per UTC day, across all runs |
| `GAIA_MAX_CONCURRENT_RUNS` | `3` | Runs executing at once; the rest wait |
| `GAIA_JUNIOR_RUNTIME` / `GAIA_LEAD_RUNTIME` | `native_deepseek` / `claude_agent_sdk` | Which runtime each role uses |
| `GAIA_COMMAND_APPROVAL_TIMEOUT_S` | `600` | How long an agent waits for your command approval |
| `GAIA_DATA_DIR` | `~/.gaia` | Where everything is kept |
| `GAIA_PORT` | `8765` | The port (Gaia only ever listens on 127.0.0.1) |

## 9. When something goes wrong

| You see | It means | Do this |
|---|---|---|
| `sandbox-runtime (srt) not found` | The sandbox isn't installed | `make sandbox-install` |
| A 503 `provider_not_configured` in a run's errors | A key is missing | §5, then start the run again |
| "The orchestrator is not running" | Gaia started without its run engine | Check the terminal for the startup error; start it again |
| A run **failed** at "prepare worktree": "setup command exited with status …" | Your setup command failed in the sandbox. The end of its output is on the run's **Timeline**, on the `run.failed` line | Run the same command yourself in the repository to see it work; commands that install git hooks or reach hosts outside the package registries fail in the sandbox |
| A run **failed** at "open PR": "no GitHub remote" | The repository has no GitHub `origin` | The commits are on the run's `gaia/…` branch in the worktree: merge them yourself. To use PRs, add the remote in your repository (`git remote add origin https://github.com/<owner>/<repo>.git`, then push `main`), and start a **new run**: Gaia re-reads `origin` when it opens the PR |
| A run is **paused (budget)** | A budget cap was hit | Decide the **Budget** approval |
| "ticket already has an active run" | One run per ticket at a time | Open the existing run from **Runs** or the dashboard |
| The UI says you're logged out | The tab's session ended | `make ui-url` and open the new link |
| Anything else | | `~/.gaia/logs/gaia.jsonl`: every API error includes a `request_id` you can search for |

## 10. How Gaia keeps you safe
In short:
- the agents run in a **sandbox** that cannot read your home directory, keys or SSH files, and
  cannot reach the internet;
- they **never hold an API key**; they reach the models through Gaia, which enforces your budgets;
- **you approve** anything irreversible.

The details, and the risks we accept, are in [SECURITY.md](SECURITY.md).
