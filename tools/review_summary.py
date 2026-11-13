#!/usr/bin/env python3
"""review_summary.py <run.json>… — one markdown row per `claude -p --output-format json` review run."""

import json
import sys


KEYS = ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheCreationInputTokens")
FAMILIES = ("opus", "sonnet", "haiku", "fable")


def _family(model):
    return next((f for f in FAMILIES if f in model), model)


def row(path):
    name = path.rsplit("/", 1)[-1].removesuffix(".json")
    try:
        with open(path) as f:
            d = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        return f"| {name} | - | - | - | - | - | - | unreadable: {e} |"
    models = d.get("modelUsage") or {}
    tokens = sum(m.get(k, 0) for m in models.values() for k in KEYS)
    per_model = ", ".join(f"{_family(n)} {sum(m.get(k, 0) for k in KEYS) // 1000}k" for n, m in models.items())
    sub = (d.get("subagent_stats") or {}).get("spawned", 0)
    status = "error: " + str(d.get("result", ""))[:60] if d.get("is_error") else "ok"
    return (f"| {name} | ${d.get('total_cost_usd', 0):.2f} | {tokens // 1000}k | "
            f"{per_model or '-'} | {sub} | {d.get('num_turns', 0)} | {d.get('duration_ms', 0) // 1000}s | {status} |")


if __name__ == "__main__":
    print("| Run | Cost | Tokens | By model | Subagents | Turns | Time | Status |")
    print("|---|---|---|---|---|---|---|---|")
    for p in sys.argv[1:]:
        print(row(p))
