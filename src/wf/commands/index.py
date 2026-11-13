"""wf index: run the project's `[map].index` commands (SCIP indexers) so `wf map` ranks over resolved references.

Each entry is a command, or a table `{ run = "…", out = "tools/index.scip" }`
naming the index it writes, so two indexers do not overwrite one `index.scip`.
They run in sequence from the repository's top level with `[map].env` added to
the environment. The indexes read back are `[map].scip` (a path or a list) and
every entry's `out`; until one exists, `wf map` uses ast-grep for its files.
Each command's output goes to `<git-dir>/wf-index/<step>.log`. Prints one line per command (`ok` or `FAIL`, the exit status, the log), one per index with the files and definitions it
covers, and one for how much of the tracked source the indexes cover together,
so a gap (a Unity folder no indexer reached) shows.
"""

from pathlib import Path

from .. import codemap
from ..gh import WfError
from . import gate
from . import map as map_cmd

NEEDS_CONFIG = False


RUNNER = gate.RUNNER  # git and the shell, as the gate drives them


def add_parser(sub, parents):
    sub.add_parser("index", parents=parents, help="run [map].index so wf map uses resolved references")


def run(args, cfg, board):
    runner = RUNNER
    cfg = cfg or map_cmd.optional_config()
    s = map_cmd.settings(cfg)
    if not s["index"]:
        raise WfError("wf index needs [map].index in .workflow.toml, e.g. index = [\"scip-python index .\"]")
    top = Path(runner.git("rev-parse", "--show-toplevel").strip())
    logs = Path(runner.git("rev-parse", "--absolute-git-dir").strip()) / "wf-index"
    logs.mkdir(parents=True, exist_ok=True)
    for k, entry in enumerate(s["index"], 1):
        command = entry["run"]
        log = logs / f"{k}-{gate.log_slug(command)}.log"
        code = runner.sh(command, str(top), log=log, env=s["env"] or None)
        why = f": {gate.first_failure(log.read_text(errors='replace'))}" if code else ""
        yield (f"index: {'ok' if code == 0 else 'FAIL'}  exit {code}  {command}  (log {log}){why}",
               {"command": command, "code": code, "log": str(log), "detail": why[2:] or None})
        if code != 0:
            args.exit_code = 1
            return
    paths = map_cmd.index_paths(s) or ["index.scip"]
    missing = [p for p in paths if not (top / p).is_file()]
    if missing:
        raise WfError(f"{', '.join(missing)} not written; [map].scip and each index entry's out name the indexes "
                      "the commands produce")
    tracked = [f for f in runner.git("-C", str(top), "ls-files", "-z").split("\0") if f.endswith(codemap.extensions(s["languages"]))]
    reader = codemap.Runner(top)
    merged = {}
    for p in paths:
        docs = codemap.read_scip(p, tracked, reader)
        defs = sum(len(d["defs"]) for d in docs.values())
        yield (f"index: {p}: {len(docs)} files, {defs} definitions",
               {"scip": p, "files": len(docs), "defs": defs})
        merged.update(docs)
    if len(paths) > 1 or len(merged) < len(tracked):
        yield (f"index: {len(merged)} of {len(tracked)} tracked source files covered; the rest stay on ast-grep",
               {"covered": len(merged), "tracked": len(tracked)})
