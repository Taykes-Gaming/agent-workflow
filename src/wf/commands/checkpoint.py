"""wf checkpoint: a one-line-per-step note an implementer keeps, so a resume costs one read.

`wf checkpoint <text>` appends `<UTC time>  <text>` to the note for the current
branch; with no text it prints the last 20 lines of the note, after a first line `… N earlier lines (--all)` (`--json`: a `more` record) when it cut some (`--all`: all of it); `--clear` empties it. The note lives
in the shared git directory, `<git-common-dir>/wf-checkpoint/<branch>.md`
(`/` in the branch becoming `-`), so every worktree of the branch sees it and
it is never committed. `wf dispatch --resume` hands its last lines to the
resumed session.
"""

import time
from pathlib import Path

from ..config import UsageError
from ..gh import branch_file, common_dir, run_git

NEEDS_CONFIG = False
TAIL = 20  # how many of the newest lines a bare `wf checkpoint` prints
RESUME_LINES = 10  # how many of the newest lines `wf dispatch --resume` passes on


class Runner:
    def git(self, *args):
        return run_git(*args)


RUNNER = Runner()


def add_parser(sub, parents):
    p = sub.add_parser("checkpoint", parents=parents, help="append to, print or clear this branch's progress note")
    p.add_argument("text", nargs="*", help="one line: what is done and what is next")
    p.add_argument("--all", action="store_true", help=f"with no text: print the whole note, not its last {TAIL} lines")
    p.add_argument("--clear", action="store_true", help="empty the note")


def note_path(common, branch):
    return Path(common) / "wf-checkpoint" / (branch_file(branch) + ".md")


def tail(common, branch, n=RESUME_LINES):
    try:
        lines = note_path(common, branch).read_text().splitlines()
    except OSError:
        return []
    return lines[-n:] if n else lines


def run(args, cfg, board):
    if args.clear and args.text:
        raise UsageError("pass text or --clear, not both")
    common = common_dir(git=RUNNER.git)
    branch = RUNNER.git("rev-parse", "--abbrev-ref", "HEAD")
    path = note_path(common, branch)
    if args.clear:
        path.unlink(missing_ok=True)
        yield f"{branch}: checkpoint cleared", {"branch": branch, "cleared": True}
        return
    if args.text:
        line = f"{time.strftime('%Y-%m-%dT%H:%MZ', time.gmtime())}  {' '.join(' '.join(args.text).split())}"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a") as f:
            f.write(line + "\n")
        yield line, {"branch": branch, "line": line, "path": str(path)}
        return
    lines = tail(common, branch, n=None)
    if not args.all and len(lines) > TAIL:
        cut = len(lines) - TAIL
        yield f"… {cut} earlier lines (--all)", {"branch": branch, "more": cut}
        lines = lines[-TAIL:]
    if not lines:
        yield f"{branch}: no checkpoint", {"branch": branch, "lines": []}
    for line in lines:
        yield line, {"branch": branch, "line": line}
