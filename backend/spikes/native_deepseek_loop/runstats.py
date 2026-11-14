"""Summarise junior-harness transcripts: wall time, turns, tokens, cache hit rate, tool mix, cost.

usage: python runstats.py runs/07a.jsonl [runs/06.jsonl ...]

Prices are deepseek-v4-pro's published rates (USD per 1M tokens, read 2026-11-14 from
https://api-docs.deepseek.com/quick_start/pricing). Off-peak is half price, so the two columns
bracket the real cost. Update PEAK if the pricing page changes.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

PEAK = {"hit": 0.044, "miss": 1.32, "out": 3.96}


def summarise(path: Path) -> str:
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    if not rows:
        return f"{path}: empty"
    turns = [r for r in rows if r["kind"] == "assistant"]
    tools = collections.Counter(r["data"]["name"] for r in rows if r["kind"] == "tool")
    prompt = hit = out = 0
    for r in turns:
        usage = r["data"].get("usage") or {}
        prompt += usage.get("prompt_tokens", 0)
        hit += usage.get("prompt_cache_hit_tokens", 0)
        out += usage.get("completion_tokens", 0)
    errors = sum(
        1 for r in rows if r["kind"] == "tool" and str(r["data"]["result"]).startswith("ERROR")
    )
    peak = (hit * PEAK["hit"] + (prompt - hit) * PEAK["miss"] + out * PEAK["out"]) / 1e6
    finished = any(r["kind"] == "finish" for r in rows)
    minutes = (rows[-1]["t"] - rows[0]["t"]) / 60
    return (
        f"{path.name}: wall={minutes:.1f}min turns={len(turns)} prompt={prompt / 1e6:.2f}M "
        f"cache_hit={hit / max(prompt, 1):.1%} out={out / 1e3:.0f}K tool_errors={errors} "
        f"cost=${peak / 2:.2f}-${peak:.2f} finished={finished}\n   tools: {dict(tools)}"
    )


if __name__ == "__main__":
    for arg in sys.argv[1:]:
        print(summarise(Path(arg)))
