# Runtime spike (doc 08) prototypes

This is throwaway evidence for [ADR-001](../../../docs/decisions/ADR-001-agent-runtimes.md). Nothing in
`gaia/` imports it.

| File | What it runs |
|---|---|
| `sdk_candidate.py` | The Claude Agent SDK (`ClaudeSDKClient`) on one ticket. Every Bash call goes through Gaia's `CommandPolicy` in a `PreToolUse` hook, with `setting_sources=[]`. Pass a model name: `claude-opus-5-5`, or `deepseek-v4-pro` via DeepSeek's Anthropic endpoint. |
| `planning_candidates.py` | `static` (one streamed structured call) against `agentic` (Agent SDK, read-only tools, `output_format`) planning. |
| `../native_deepseek_loop/agent.py` | The native DeepSeek loop. |

## Reproducing
1. Create a separate venv with `claude-agent-sdk==0.2.175` and `anthropic==2.8.0`, plus
   `uv pip install --no-deps -e backend` so the policy can be imported.
2. Serve the gateway on a scratch data dir, holding the provider keys in memory in the server
   process only.
3. Mint a token: `GAIA_DATA_DIR=… uv run python scripts/dev_run_token.py --provider anthropic|deepseek --repo-path <repo>`.
4. Run each candidate under `env -i`, with only `PATH`, `HOME` (a temp dir), `ANTHROPIC_BASE_URL`
   (`http://127.0.0.1:<port>/llm/anthropic` or `…/llm/deepseek/anthropic`), `ANTHROPIC_API_KEY=<run token>`
   and `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1`.
5. Optional: wrap it in `srt --settings <build_srt_settings(...)> -- env -u NO_PROXY -u no_proxy <cmd>`.
   Without the `env -u`, srt's `NO_PROXY` makes clients bypass its proxy for loopback, and the
   connection to the gateway fails with `EPERM`.

Costs come from the gateway's `llm_usage` rows, never from the SDK's `total_cost_usd`.
