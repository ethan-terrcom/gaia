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
- [ ] The ADR is merged, and the owner signs off before doc 10 starts.
