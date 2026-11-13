"""wf map [SEED…] [--text FILE|-] [--changed [--base REF]] [--budget N] [--refresh]: a ranked map of the code around a seed.

Prints the definitions most related to the seed, grouped by file with their
signature line, cut to `--budget` tokens (default `[map].budget` or 2000). A
seed is a symbol name or a path; `--text` adds every identifier and path an
issue body or diff mentions; `--changed` adds the files changed against
`--base` (default `[map].base`, `[opencode].base`, then `dev`). With no seed at all
the map is the repository's most referenced definitions.

The first line is a comment with what was mapped, how it was seeded
(`identifier-seeded`, or `prose-seeded (low confidence)` when only plain
words of the text named a definition) and which seeds matched nothing. Any agent calls it over Bash (no tool schema to carry), and
`wf dispatch` and `wf review` call it for the implementer's pack and the
lanes' prep. See docs/map.md and `wf.codemap` for how the graph is built,
ranked and cached.
"""

import sys
from pathlib import Path

from .. import codemap, config
from ..gh import WfError, common_dir
from . import UsageError, base_branch
from . import changed as changed_files

NEEDS_CONFIG = False
DEFAULTS = {"budget": 2000, "languages": ["python", "csharp", "typescript", "tsx", "javascript", "go"],
            "scip": [], "index": [], "base": None, "env": {}, "common": codemap.COMMON_SHARE}


def add_parser(sub, parents):
    p = sub.add_parser("map", parents=parents, help="a ranked map of the definitions around a seed")
    p.add_argument("seeds", nargs="*", metavar="SEED", help="symbol name or path")
    p.add_argument("--text", metavar="FILE", help="seed with the identifiers and paths in FILE (- for stdin)")
    p.add_argument("--changed", action="store_true", help="seed with the files changed against --base")
    p.add_argument("--base", metavar="REF", help="what --changed measures against (default [map].base, [opencode].base, dev)")
    p.add_argument("--budget", type=int, help="token budget (default [map].budget or 2000)")
    p.add_argument("--refresh", action="store_true", help="rebuild the cached graph for this commit")
    p.add_argument("--json-graph", action="store_true", help="print the graph instead of a map (debugging)")


def settings(cfg):
    s = dict(DEFAULTS)
    if cfg and cfg.map:
        s.update(cfg.map)
    s["base"] = base_branch(cfg, s)
    return s


def optional_config():
    """The .workflow.toml when there is one (a bad one is still an error); None outside a configured repo."""
    try:
        path = config.find()
    except WfError:
        return None
    return config.load(path) if path.is_file() else None


def index_paths(s):
    """The SCIP indexes the map reads: `[map].scip`, then each `[map].index` entry's `out` it does not name."""
    paths = list(s["scip"] or [])
    paths += [e["out"] for e in s["index"] if e.get("out") and e["out"] not in paths]
    return paths


def graph(cfg, refresh=False, runner=None):
    runner = runner or codemap.RUNNER
    s = settings(cfg)
    common = common_dir(git=runner.git)
    sha = runner.git("rev-parse", "HEAD").strip()
    # an index not written yet leaves its files to ast-grep until `wf index` runs
    scip = [p for p in index_paths(s) if (Path(runner.cwd or ".") / p).is_file()]
    return codemap.cached_graph(common, sha, s["languages"], scip=scip, refresh=refresh, runner=runner), s


def text_map(cfg, cwd=None, terms=(), text="", budget=None):
    """The rendered map for a checkout, header line included, for the dispatch pack and the review prep.
    Returns "" when the map cannot be built (no ast-grep, not a git checkout): the map is an aid, never a
    blocker."""
    try:
        g, s = graph(cfg, runner=codemap.Runner(cwd))
        body, info = codemap.code_map(g, terms, text, budget or s["budget"], common=s["common"])
    except (WfError, OSError, ValueError):  # no git, no ast-grep, an unwritable cache, unreadable output
        return ""
    return header(info) + ("\n" + body if body else "")


def header(info):
    counts = info.get("counts") or {}
    source = (f"scip ({counts['scip']} files) + ast-grep ({counts['ast-grep']} files)"
              if counts.get("scip") and counts.get("ast-grep") else info["source"])
    n = info.get("seeds", 0)
    if info["seeded"]:
        seed = f" from {n} seed{'s' if n != 1 else ''}"
        mode = f"{info['mode']}-seeded" + (" (low confidence)" if info["confidence"] == "low" else "")
    else:
        seed, mode = "", "unseeded (most referenced)"
    head = f"# wf map: {info['count']} of {info['defs']} definitions{seed}, ~{info['tokens']} tokens, {mode}, source {source}"
    if info["unmatched"]:
        head += "; no match for " + ", ".join(info["unmatched"])
    dropped = (info.get("tiers") or {}).get("dropped")
    if dropped:
        head += "; too common to seed: " + ", ".join(dropped)
    return head


def run(args, cfg, board):
    if args.budget is not None and args.budget < 50:
        raise UsageError("--budget must be at least 50 tokens")
    runner = codemap.RUNNER
    cfg = cfg or optional_config()
    g, s = graph(cfg, refresh=args.refresh, runner=runner)
    if args.json_graph:
        import json
        yield json.dumps(g.to_json()), g.to_json()
        return
    terms = list(args.seeds)
    if args.changed:
        files = changed_files(runner, args.base or s["base"], fallback=True)
        if files is None:
            raise WfError(f"--changed: git cannot diff against {args.base or s['base']}")
        terms += sorted(f for f in files if f.endswith(codemap.extensions(s["languages"])))  # docs are not seeds
    text = ""
    if args.text:
        try:
            text = sys.stdin.read() if args.text == "-" else Path(args.text).read_text()
        except OSError as e:
            raise WfError(f"cannot read {args.text}: {e.strerror}") from None
    body, info = codemap.code_map(g, terms, text, args.budget or s["budget"], common=s["common"])
    yield header(info) + ("\n" + body if body else ""), {"map": body, **info}
