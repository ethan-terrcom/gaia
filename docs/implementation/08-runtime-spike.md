# 08 — Runtime Spike (Decision Gate)

## Goal
Decide, with evidence, which `AgentRuntime` implementations Gaia ships first. The owner
is open to either **driving existing harnesses** (Claude Code via the Claude Agent SDK,
and Cline) or **native API agents** that Gaia builds itself. Background is in
`ARCHITECTURE.md §3`.

This is a **time-boxed spike: 2–3 days.** The output is a decision record plus throwaway
prototype code under `backend/spikes/`. That code is not merged into `gaia/`.

## Candidates
1. **Lead: Claude Agent SDK** (`pip install claude-agent-sdk`; docs at code.claude.com/docs/en/agent-sdk).
   Run it with `cwd=<worktree>`, restricted `allowed_tools`, and a permission callback that sends
   Bash requests to Gaia's command policy.
2. **Junior: Cline CLI** with DeepSeek as the provider. Find out:
   - Does it have a non-interactive/headless mode that takes a task prompt and exits?
   - Can it emit structured output (JSON events or a final summary) that Gaia can parse?
   - Can tool and command approvals be auto-denied or routed back to Gaia?
   - How is the API key supplied? Prefer a per-process env var, never a global config file.
3. **Junior: native DeepSeek loop.** A ~150-line prototype using `deepseek_client` (07) plus
   `read_file`, `write_file`, `list_dir` and `run_command` tools confined to the worktree.
4. Optional: **Aider** or **OpenHands** headless, as more harness candidates for the junior role.

## Method
- Pick one small real ticket in a sample repo (for example, "add a CLI flag and a test").
- The lead (Anthropic SDK structured call) writes a 3–5 step plan once. Every junior candidate
  gets the same plan.
- For each candidate, record:

| Metric | How |
|---|---|
| Task success | tests pass and the diff is reasonable (manual review) |
| Cost | tokens × price |
| Wall time | |
| Observability | can Gaia stream per-tool-call events? |
| Control | can Gaia confine paths, deny commands, cancel mid-run, set a timeout? |
| Secret isolation | can the runtime run with an env holding only its own key? |
| Integration effort | estimated days to production quality |

## Deliverable
`docs/decisions/ADR-001-agent-runtimes.md` containing the context, the table of results, the
decision, and its consequences. Expected outcome (confirm or overturn): the **Claude Agent SDK
for lead implementation work**, **Cline CLI or the native DeepSeek loop for juniors** (whichever
scores better on control and observability), and **native Anthropic SDK structured calls for
planning and review**.

## Acceptance criteria
- [ ] The ADR is merged, and the owner signs off before doc 10 starts.
