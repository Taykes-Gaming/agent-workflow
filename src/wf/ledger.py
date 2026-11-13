"""The spend ledger: one JSON line per agent run, main session or review, per branch.

`<git-common-dir>/wf-ledger/<branch>.jsonl` (`/` in the branch becoming `-`,
as `wf checkpoint` names its note), so every worktree of a checkout writes to
the same place and nothing is committed. Each line has `kind` and `at` (UTC):

- `agent`: an implementer that worked the branch, written by `wf hook` when a
  subagent stops and by `wf dispatch` after each OpenCode run. `id`,
  `agent_type`, `transcript`, `model` (the Model family), `tokens` (input +
  output + cache writes), `cache_read`, `turns`. A fix round stops the same
  agent again; its later line replaces the earlier one (last wins by id).
- `session`: a main session that stopped on the branch, written by `wf hook`
  on every Stop: `id`, `transcript`, `cwd`. Measured later, by `wf spend`.
- `review`: one `wf review` run of the branch's PR, failed runs too:
  `variant`, `tokens`, `cache_read`, `cost_usd`, `turns`.

The branch a line belongs to is decided once, when it is written; `wf merged`
reads the PR's head branch, `wf spend` the base branch.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from .gh import branch_file, common_dir, run_git

DIR = "wf-ledger"


def path(common, branch):
    return Path(common) / DIR / (branch_file(branch) + ".jsonl")


def where(cwd=None, git=run_git):
    """(git common dir, branch) of the checkout at `cwd`; a WfError outside git."""
    return common_dir(cwd, git), git("rev-parse", "--abbrev-ref", "HEAD", cwd=cwd)


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def append(common, branch, entry):
    p = path(common, branch)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(json.dumps({"kind": entry.pop("kind"), "at": now(), **entry}) + "\n")
    return p


def read(common, branch):
    """The branch's lines, in order; [] when it has none. A line that is not JSON is skipped."""
    try:
        lines = path(common, branch).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def latest(entries, kind):
    """The `kind` lines, one per id, the last written winning, in first-seen order."""
    by_id = {}
    for e in entries:
        if e.get("kind") == kind:
            by_id[e.get("id")] = e
    return list(by_id.values())


def of_kind(entries, kind):
    return [e for e in entries if e.get("kind") == kind]
