# Gaia — Project Brief

> Audience: the senior/staff engineer who owns Gaia's architecture. This is the
> cleaned-up product request. `ARCHITECTURE.md` holds the technical decisions, and
> `implementation/` holds the ticket-sized work packages for implementing agents.

## 1. Problem

The owner ("the Architect") runs several AI coding agents at once, across repos,
worktrees, branches and tickets. Today that happens in separate tools (Cline in VS Code,
terminals, GitHub tabs), and nothing gives one view of who is doing what, what is
blocked on a human, or what each agent costs.

## 2. Product goal

Gaia is a **local-first control plane for a hierarchical team of AI coding agents.**
The Architect can:

1. **See everything.** Every agent, the ticket it works on, its worktree and branch, its
   current step, its live log, its cost so far, and any PR it opened.
2. **Move between work quickly.** Jump from a ticket to its run, its diff, its worktree
   (open in the editor or terminal), and its PR in one or two clicks.
3. **Direct a hierarchy:**
   - **Lead Engineer agent (Claude, `claude-opus-5-5`).** Turns a ticket into a plan,
     splits the plan into small steps, reviews junior output, and decides when work is
     ready for a PR.
   - **Junior Engineer agents (DeepSeek).** Implement one plan step at a time inside an
     isolated git worktree, and run the tests.
4. **Gate the important moments.** Plan approval, risky commands, opening a PR and merging
   all wait in an **approval inbox**. Agents never merge to a protected branch on their own.
5. **Monitor the team.** Run timelines, PR and CI status, token spend and budgets.

## 3. Scope

### v1 (local, single user)
- Runs on the Architect's Mac. The backend binds to `127.0.0.1` only.
- Manages one or more local git repos that have GitHub remotes.
- Local ticket store, with optional import from GitHub Issues.
- One orchestrated workflow: **ticket → plan → approve → implement steps → test → review → PR → approve merge.**
- A web UI for the dashboard, approval inbox, run detail and PR view.

### Designed for, not built in v1
- **Multi-user collaboration and cloud hosting.** Every row is scoped by `workspace_id` and
  `created_by` from day one. Auth sits behind an interface, the database is Postgres-ready,
  and no code assumes "localhost == trusted".
- Agents beyond coding (research, ops). The agent-role and runtime abstractions must not be
  coding-specific at the core.

### Out of scope
- Building a new IDE, or replacing GitHub as the code-review surface.
- Agents running fully autonomously with merge rights.

## 4. Open decision: how agents execute (owner is flexible)

The owner first asked for "Claude API for the lead, DeepSeek API for juniors (currently
run through Cline), probably tied together with LangGraph." They have since said they are
**open to running existing agent harnesses (Claude Code / Claude Agent SDK, and Cline)
instead of building agents on the raw APIs**, if that makes more sense.

`ARCHITECTURE.md §3` gives the recommendation: put an `AgentRuntime` interface between
the orchestrator and whatever executes agent turns, and evaluate both paths with a spike
(`implementation/08-runtime-spike.md`) before committing:

| Option | Lead | Junior | Pros | Cons |
|---|---|---|---|---|
| **A. Harness runtimes** | Claude Agent SDK (Claude Code as a library) | Cline CLI configured with DeepSeek | Mature file/edit/bash tools, context management and permissions with no extra work; matches how the owner works today | Less control over prompts and tools; some subprocess wrangling; Cline's headless mode needs checking |
| **B. Native API runtimes** | Anthropic Python SDK + Gaia tools | DeepSeek API (OpenAI-compatible) + Gaia tools | Full control and observability; one tool-security model | Gaia has to build and secure the file/edit/shell tools and the agent loop |

LangGraph stays as the **orchestration layer** in both options. It owns the workflow
state machine, checkpoints and human-in-the-loop interrupts, not the agent turns.

## 5. Non-negotiables

- **Security** (detail in `ARCHITECTURE.md §6`): secrets in the OS keychain, never in
  repo or plaintext dotfiles; agents confined to their worktree; command allowlists; no
  secrets in agent environments or prompts; repo content treated as untrusted (prompt
  injection); an audit log of every agent action and human approval; least-privilege
  GitHub tokens.
- **Human authority:** merges and other irreversible actions always need an explicit approval.
- **Cost control:** per-run and per-day token budgets with a hard stop.
- **Recoverability:** a crash or restart never loses workflow state, because LangGraph
  checkpoints are persisted.

## 6. Success criteria for v1
1. From the UI, the Architect creates a ticket on a registered repo and starts a run.
2. The lead produces a plan. The Architect approves or edits it in the inbox.
3. Junior agents implement the steps in a dedicated worktree on branch `gaia/<ticket>-<slug>`,
   with tests run after each step.
4. The lead reviews and asks for fixes or approves. Gaia opens a GitHub PR after the
   Architect approves.
5. The dashboard shows every active run, its state, its cost and its PR/CI status, live.
6. Killing the backend mid-run and restarting it resumes from the last checkpoint.
