"""wf report <label> [--model M]: the sprint metrics table, grouped by Model.

Columns are those of the shell `metrics-report.sh` it replaces, plus Cache
kTokens and Review kTokens when those fields are configured. The OpenCode
seat's rows are `opencode-<family>`, apart from the Claude seat's `<family>`.
The sprint record (`wf spend`) is its own last row, `orchestrator`, with no
slices.
"""

from . import board_model
from .spend import record_label

ORCHESTRATOR = "orchestrator"


def add_parser(sub, parents):
    p = sub.add_parser("report", parents=parents, help="sprint metrics table for a label")
    p.add_argument("label", help="e.g. sprint-3")
    p.add_argument("--model", type=board_model, help="only this Model's row, e.g. opus or opencode-opus")


def _n(v):
    return int(v or 0)


def run(args, cfg, board):
    extra = [(k, name) for k, name in (("cache_ktokens", "Cache kTokens"), ("review_ktokens", "Review kTokens"))
             if k in cfg.fields]
    groups, record = {}, None
    for it in board.items():
        if args.label not in it.labels:
            continue
        if record_label(cfg) in it.labels:
            if not args.model:
                record = record or {"k": 0, "t": 0, **{k: 0 for k, _ in extra}}
                record["k"] += _n(it.values.get("ktokens"))
                record["t"] += _n(it.values.get("turns"))
                for k, _ in extra:
                    record[k] += _n(it.values.get(k))
            continue
        if args.model and (it.values.get("model") or "-") != args.model:
            continue
        g = groups.setdefault(it.values.get("model") or "-", {"n": 0, "sizes": [], "f": 0, "r": 0, "k": 0, "t": 0,
                                                             **{k: 0 for k, _ in extra}})
        g["n"] += 1
        g["sizes"].append(it.values.get("size") or "-")
        g["f"] += _n(it.values.get("findings"))
        g["r"] += _n(it.values.get("rounds"))
        g["k"] += _n(it.values.get("ktokens"))
        g["t"] += _n(it.values.get("turns"))
        for k, _ in extra:
            g[k] += _n(it.values.get(k))
    heads = ["Model", "Slices", "Size mix", "Review findings", "Fix rounds", "Agent kTokens", "kTokens / slice",
             "Turns", *(name for _, name in extra)]
    yield "| " + " | ".join(heads) + " |", {"header": True}
    yield "|" + "---|" * len(heads), {"header": True}
    for model in sorted(groups):
        g = groups[model]
        per = round(g["k"] / g["n"])
        cells = [model, g["n"], ",".join(g["sizes"]), g["f"], g["r"], g["k"], per, g["t"], *(g[k] for k, _ in extra)]
        yield ("| " + " | ".join(map(str, cells)) + " |",
               {"model": model, "slices": g["n"], "sizes": g["sizes"], "findings": g["f"], "rounds": g["r"],
                "ktokens": g["k"], "ktokens_per_slice": per, "turns": g["t"], **{k: g[k] for k, _ in extra}})
    if record:
        cells = [ORCHESTRATOR, "-", "-", "-", "-", record["k"], "-", record["t"], *(record[k] for k, _ in extra)]
        yield ("| " + " | ".join(map(str, cells)) + " |",
               {"model": ORCHESTRATOR, "ktokens": record["k"], "turns": record["t"], **{k: record[k] for k, _ in extra}})
