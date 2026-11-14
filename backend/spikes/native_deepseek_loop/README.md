# Native DeepSeek loop (junior harness)

This is the lead's harness for running a DeepSeek junior on one Gaia work package. It is also the
"native DeepSeek loop" candidate in the doc 08 runtime spike. It is throwaway prototype code: nothing
in `gaia/` imports it, and ruff applies only a narrow set of ignores to `spikes/**`
(`backend/pyproject.toml`).

- `agent.py` is the confined tool loop: file tools, an allowlisted `run` under `sandbox-exec`, and
  `finish`.
- `runstats.py` summarises transcripts: wall time, tokens, cache hit rate and cost.

How to use it, and why it is built this way:
[`docs/operations/running-junior-agents.md`](../../../docs/operations/running-junior-agents.md).
