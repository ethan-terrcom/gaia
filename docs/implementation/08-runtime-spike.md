# 08 — Runtime Spike (Decision Gate)

## Goal
Decide, with evidence, which `AgentRuntime` implementations Gaia ships first. The owner
is open to either **driving existing harnesses** (Claude Code via the Claude Agent SDK,
and Cline) or **native API agents** that Gaia builds itself. Background is in
`ARCHITECTURE.md §3`.

**Owner:** the lead engineer, not a junior. This is judgment-heavy evaluation.

This is a **time-boxed spike: 2–3 days.** The output is a decision record plus throwaway
prototype code under `backend/spikes/`. That code is not merged into `gaia/`.

## Candidates
1. **Lead: Claude Agent SDK** (`uv add claude-agent-sdk`; docs at code.claude.com/docs/en/agent-sdk).
   Use `ClaudeSDKClient`, which supports `interrupt()`; `query()` does not. Run it with
   `cwd=<worktree>` and `setting_sources=[]`. Remove unwanted tools (`WebFetch`, `WebSearch`, …) with
   `disallowed_tools`: `allowed_tools` only auto-approves, it does not restrict, and auto-approved
   calls skip `can_use_tool`. Use `permission_mode="dontAsk"` and a `PreToolUse` hook that sends every
   call to Gaia's command policy. Also evaluate the `sandbox`, `max_budget_usd`, `output_format` and
   `user` options. Note that `env` merges onto the inherited environment.
2. **Junior: Cline CLI** with DeepSeek as the provider. Known from the docs (2026-11): passing a prompt
   runs headless, but **auto-approves everything by default**; `--json` emits JSON lines (`agent_event`,
   `run_result`, `run_aborted`, …); `--timeout`, `-c/--cwd`, `--config`/`--data-dir` and `--hooks-dir`
   exist; there is no shell allow/deny list, only `PreToolUse` hooks; `-k` **persists the key** to
   `~/.cline/data/settings/providers.json`. Find out:
   - Does `--auto-approve false` plus a `PreToolUse` hook deny commands reliably in headless mode?
   - Can hooks from the workspace's `.cline/hooks` be disabled? If not, Cline is disqualified,
     because repo content would run code.
   - Can the provider point at the Gaia gateway (07b), so no key is supplied or persisted?
   - How do we cancel it (kill the process group?), and what do its exit codes mean?
3. **Junior: native DeepSeek loop.** A ~150-line prototype using `deepseek_client` (07) plus
   `read_file`, `write_file`, `list_dir` and `run_command` tools confined to the worktree.
4. **Junior: Claude Agent SDK driving DeepSeek** through DeepSeek's Anthropic-compatible endpoint
   (`ANTHROPIC_BASE_URL` → the gateway → `https://api.deepseek.com/anthropic`). If it holds up, one
   harness, one permission model and one event mapping serve both roles. Check tool-call fidelity,
   thinking blocks and cost. This is not an Anthropic-supported configuration, so weigh that in.
5. Optional: **Aider** or **OpenHands** headless, as more harness candidates for the junior role.

## Method
- Pick one small real ticket in a sample repo (for example, "add a CLI flag and a test").
- The lead (Anthropic SDK structured call) writes a 3–5 step plan once. Every junior candidate
  gets the same plan.
- Also compare **agentic planning** (the lead explores the repo read-only through the Agent SDK
  and returns a `Plan` through `output_format`) with doc 11's static repo context, on plan quality
  and cost.
- For each candidate, record:

| Metric | How |
|---|---|
| Task success | tests pass and the diff is reasonable (manual review) |
| Cost | tokens × price |
| Wall time | |
| Observability | can Gaia stream per-tool-call events? |
| Control | can Gaia confine paths, deny commands, cancel mid-run, set a timeout? |
| Secret isolation | can it run with **no** provider key in its env (through the gateway)? Which endpoints does it call? |
| Repo-config isolation | does it load any settings, hooks or instructions from the worktree? |
| Sandboxing | does it work under `srt` (sandbox-runtime) with the worktree-only profile? |
| Integration effort | estimated days to production quality |

## Deliverable
`docs/decisions/ADR-001-agent-runtimes.md` containing the context, the table of results, the
decision, and its consequences. Expected outcome (confirm or overturn): the **Claude Agent SDK
for lead implementation work**, **Cline CLI or the native DeepSeek loop for juniors** (whichever
scores better on control and observability), and **native Anthropic SDK structured calls for
planning and review**.

## Acceptance criteria
- [x] The ADR is merged: [`ADR-001`](../decisions/ADR-001-agent-runtimes.md), status *Accepted* on 2026-11-14, with the owner's portability condition (P1–P6).
- [ ] The owner signs off before 10b starts. (10a does not wait: its runtime-agnostic base was built in parallel.)

## As run (2026-11-14)
- **Candidates run:** the native DeepSeek loop; the Agent SDK with Claude; the Agent SDK driving
  DeepSeek through the Anthropic-compatible endpoint (under `srt`); and Cline 3.0.74 (live hook test
  only).
- **Not run:** Aider and OpenHands (optional).
- **Sample ticket:** a `--lines` flag for a small word-count CLI; the same ticket for every candidate.
- **Planning comparison:** the same ticket, static against agentic.
- **Prototypes:** `backend/spikes/runtime_spike/` and `backend/spikes/native_deepseek_loop/`.
- **Spend:** about $0.75 in total, gateway-metered, across every spike call.
- **Side fix:** `scripts/dev_run_token.py` reuses the scratch repository row for a path. Its
  timestamp-prefixed name collided for two runs within about a minute.
