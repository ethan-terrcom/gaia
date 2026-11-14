# ADR-001: Agent runtimes for v1

- **Status:** Proposed. **Awaiting the owner's signature** (doc 08 is a decision gate; 10b+ waits for it).
- **Date:** 2026-11-14
- **Author:** lead engineer (Claude). The spike evidence is in `backend/spikes/`.
- **Decides:** which `AgentRuntime` implementations Gaia ships first (ARCHITECTURE §3), and how the
  lead plans.

## Decision (proposed)
| Role | Runtime | Status |
|---|---|---|
| **Junior implementation** | **Native DeepSeek loop** (`NativeDeepSeekRuntime`): Gaia's own tool loop over 09b's tools, with `deepseek-v4-pro` through the gateway | **default** → package **10b** |
| **Lead implementation and escalations** | **Claude Agent SDK** (`ClaudeAgentSdkRuntime`): `ClaudeSDKClient`, Claude through the gateway, under `srt` | → package **10c** |
| **Lead planning and review** | **Native structured calls** (doc 11: one streamed `messages.parse`/`stream` call with `output_format`) | already in progress (11) |
| Agentic planning (Agent SDK, read-only) | optional fallback for large repos or vague tickets | revisit with real tickets |
| Agent SDK driving DeepSeek | **not adopted**; kept as a documented fallback | — |
| Cline CLI | **disqualified** | — |

This **overturns** ARCHITECTURE §3's working hypothesis ("harness runtimes for v1") **for juniors**,
and **confirms** it for lead implementation work.

## Context
Gaia needs a junior role (cheap, many runs) and a lead role (strong, fewer runs). Whatever the
runtime, Gaia enforces confinement, secret isolation and budgets outside it:
- the `srt` sandbox (09a, D1);
- the LLM gateway, so no provider key ever enters an agent's environment (07b, D2);
- the command policy (09a);
- in-turn approvals (doc 10, requirement 8).

The question is which runtime gives the best control, observability and cost **inside** those
boundaries.

## Method
Each candidate ran in its own git worktree of a sample repository (`wordcount`), on one real
ticket: "add a `--lines` flag with per-file and total line counts, plus tests; don't change the
output without the flag".
- All calls went through the 07b gateway, with a per-run token as the only credential. Cost is
  **gateway-metered**, at the verified DeepSeek and Anthropic prices.
- The native loop also has evidence from **six real Gaia packages** (07a, 05b, 06, 07b, 09a, 13).
  Three more ran during this spike.

## Results

### Implementation candidates on the same ticket
| Metric | Native DeepSeek loop | Agent SDK + Claude Opus 5.5 | Agent SDK + DeepSeek (Anthropic endpoint) | Cline CLI 3.0.74 |
|---|---|---|---|---|
| **Task success** | ✅ 6 tests pass, minimal diff | ✅ 6 tests pass, minimal diff | ✅ 5 tests pass, minimal diff | ran (trivial prompt only) |
| **Cost (gateway)** | **$0.007** (about 20K prompt tokens, 90% cache hits) | $0.107 (4 calls) | $0.028 (14 calls, off-peak) | — |
| **Wall time** | 31 s | **22 s** | 118 s | 2 s (trivial) |
| **Observability** | full: every request, response and tool call is ours (JSONL today, `EventSink` in 10b) | good: typed message stream, `ResultMessage` usage, hook events | same as the Claude column, but the **SDK misprices it** ("unrecognized model"); use the gateway's figures | good: NDJSON `agent_event`, `hook_event`, `run_result` |
| **Path confinement** | ours (`confine`, 09a) | `cwd`, plus PreToolUse on file tools if needed; the sandbox is the boundary | same | sandbox only |
| **Command control** | **argv-native**: `CommandPolicy.evaluate` directly | PreToolUse hook works headless and denies reliably. **The Bash tool takes a shell string**, so Gaia must `shlex`-split it and deny metacharacters | same; 3 of 9 commands denied as shell strings (`-c` quoting, pipes) | `--auto-approve false` denies **every** tool headless; a file-IPC "desktop" approval mode exists |
| **Cancel and timeout** | ours (asyncio plus process group) | `ClaudeSDKClient.interrupt()`; the process can be killed | same | `--timeout`, kill the group |
| **Secret isolation** | ✅ the harness holds only a run token (gateway) | ✅ verified: env = `ANTHROPIC_BASE_URL` plus a `gaia_rt_` token; **only `v1/messages`** reached the gateway | ✅ same, with `…/llm/deepseek/anthropic` | ⚠️ `cline auth -k` **persists the credential** to `settings/providers.json` (acceptable only because it is a run token) |
| **Repo-config isolation** | ✅ loads nothing from the worktree | ✅ `setting_sources=[]`; no `.claude/` or `CLAUDE.md` loaded | ✅ | ❌ **executes repo-supplied hooks**: a `.clinerules/hooks/TaskStart.sh` in the worktree **ran**, confirmed live, with `--auto-approve false` and an isolated `--data-dir`. Workspace rules and hook paths are hard-coded in `@cline/shared` (`resolveHooksConfigSearchPaths`), with no opt-out flag |
| **Works under `srt`** | ✅ (its commands run sandboxed today via `sandbox-exec`; `srt` in 10b) | ✅ the whole process under `srt` with network only to the gateway, **after removing `NO_PROXY` inside the sandbox** (see Consequences) | ✅ verified the same way | not attempted (disqualified) |
| **Integration effort** | about 4 days (10b): the loop exists; port it onto 09b tools, `EventSink`, approvals and the gateway | about 4 days (10c): message→event mapping, the shell-string policy adapter, approvals in the hook, orphan handling for the bundled CLI process | +1 day over 10c | — |
| **Other** | proven on **6 real packages**: 14–31 min, 10–33M prompt tokens at 99% cache, **$0.47–$2.15 per package** | bundled CLI 2.1.314 (273 MB native binary in the wheel). Pin the SDK (0.2.175) | "not an Anthropic-supported configuration" (doc 08) | runs `ioreg` (macOS hardware query) at startup; auto-approves by default |

### Planning: static versus agentic (Claude Opus 5.5, same ticket, `lead_plan.md` system prompt)
| | Static: one structured call (doc 11) | Agentic: Agent SDK, `Read`/`Glob`/`Grep`, `output_format` |
|---|---|---|
| Result | valid `Plan`, 2 steps | valid `Plan`, 1 step with more precise instructions and sharper risks (line-count semantics, a single-file total row) |
| Time and cost | about 15 s, **about $0.04** | 25 s, **$0.13** (11 turns) |
| Control | no tools; context is chosen by Gaia (`repo_context`, `confine`) | read-only tools, `dontAsk`, `setting_sources=[]` |

## Rationale
1. **Juniors: native loop.**
   - It is the only candidate where Gaia's argv-based policy, in-turn approvals and event
     emission are *native* rather than adapted.
   - It is cheapest by an order of magnitude ($0.007 against $0.028–0.107 here), and it has run
     every junior package in this project so far.
   - The harness runtimes' main advantages (mature edit tools, context management) matter less for
     small, well-specified plan steps. This spike's three diffs were practically identical.
2. **Lead implementation: Agent SDK + Claude.**
   - It is the fastest and highest-quality runtime, its isolation story holds (`setting_sources=[]`,
     gateway-only egress, PreToolUse denial), and the lead role runs rarely, so cost matters less.
   - Its shell-string Bash tool is a known, solvable adapter problem.
3. **Planning stays static by default.**
   - It is 3× cheaper with comparable quality on a small repo.
   - Agentic planning becomes an option, chosen per repo or ticket once 12b has real data. It
     already needs nothing new: the Agent SDK runtime with read-only tools.
4. **Cline is disqualified** by doc 08's own rule. Repository content runs code (hooks) on start,
   and there is no flag to stop it. Its key persistence and default auto-approve are further
   reasons.

## Consequences
1. **09b / 10b / 10c sandbox launch:** `srt` sets `NO_PROXY=localhost,127.0.0.1,…`, so clients
   connect to the loopback gateway **directly**, which Seatbelt blocks (`EPERM`). Two options:
   - launch the agent as `srt … -- env -u NO_PROXY -u no_proxy <cmd>`, so gateway traffic goes
     through srt's proxy and its `127.0.0.1:<port>` allowlist entry (**chosen**: verified in the
     spike);
   - `allowLocalBinding: true` (**rejected**: it opens every local port to the agent).

   Add this to `gaia.security.sandbox.wrap_argv` (a 09b task), with a live test.
2. **Inside the sandbox, `TMPDIR` must point at the task's tmp dir.** pytest's `tmp_path` failed
   until it did. 10a's `build_agent_env` already sets it.
3. **10c needs a Bash adapter:** `shlex.split`, deny shell metacharacters with a message the model
   acts on ("run one plain command"), then `CommandPolicy.evaluate`. `ask` goes to the in-turn
   approval broker (10a).
4. **Long lead calls should stream.** A single non-streamed Opus planning call through the gateway
   returned 502 after 97 s. It did not reproduce (a clean rerun took 15.7 s), but streaming removes
   the idle-connection risk. Package 11's `parse` should stream for large `max_tokens`.
5. **Cost truth is the gateway.** The Agent SDK's `total_cost_usd` is wrong for non-Claude models;
   never use it for budgets.
6. **The gateway allowlist is sufficient as built:** `v1/messages` (Claude Code and the SDK, with
   non-essential traffic disabled by `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`) and
   `anthropic/v1/messages` / `chat/completions` (DeepSeek).
7. **When signed:** update ARCHITECTURE §3's runtime table and hypothesis, add 10b (native DeepSeek)
   and 10c (Agent SDK) rows to `00-README.md`, and mark Cline as out of scope for v1.

## Owner sign-off
- [ ] Approved as proposed. Owner: ______ Date: ______
- Changes requested: ______
