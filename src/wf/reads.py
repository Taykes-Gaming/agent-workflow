"""Reads outside the edit set: the one number that says whether a map paid for itself.

An implementer's file reads (its Read tool, and `cat`, `head`, `tail`, `sed -n`
and `bat` from its shell) are counted against the files its run ended up
changing. A read of a file it then edited is the work; a read of any other
file is what a good map should have saved. `wf dispatch` prints the count per
run and `wf usage --edits REV` per Claude agent, so runs with and without the
map can be compared on the same issue.
"""

import os
import re
import shlex
from pathlib import Path

from .gh import WfError

OUTSIDE_FILES = 20  # paths of files outside the edit set a record lists
_READERS = ("cat", "head", "tail", "bat", "less", "more")
_SPLIT = re.compile(r"\|\||&&|[|;]")
_SED_SCRIPT = re.compile(r"^(?:[\d,$]+|/[^/]*/)(?:[pd=]|)$")


def bash_paths(command):
    """Paths a shell command reads: the file arguments of cat/head/tail/bat/less/more and `sed -n`, per
    pipeline stage. Searches (grep, rg, find) are not reads."""
    paths = []
    for stage in _SPLIT.split(command or ""):
        try:
            words = shlex.split(stage, posix=True)
        except ValueError:
            continue
        words = [w for w in words if "=" not in w or not re.match(r"^\w+=", w)]  # drop FOO=bar prefixes
        if not words:
            continue
        name = Path(words[0]).name
        if name == "sed" and "-n" not in words[1:]:
            continue
        if name not in _READERS and name != "sed":
            continue
        for w in words[1:]:
            if w.startswith("-") or w in ("<", ">", ">>", "2>&1"):
                continue
            if name == "sed" and _SED_SCRIPT.match(w):  # the script: 1,40p, /foo/p, $d
                continue
            if "/" in w or re.search(r"\.\w{1,6}$", w):
                paths.append(w)
    return paths


def relative(path, root):
    """`path` as the edit set names it: relative to `root` for an absolute path under it, normalised
    (`./src/a.py` → `src/a.py`) otherwise."""
    p = Path(path)
    if p.is_absolute():
        try:
            return str(p.resolve().relative_to(Path(root).resolve()))
        except ValueError:
            return str(p)
    return os.path.normpath(path)


def tally(reads, edited, root, cap=OUTSIDE_FILES):
    """Reads (paths in order) against the edit set (repo-relative paths): counts and the outside files, at most
    `cap` of them (all, with `cap=None`); `files_outside_edit_total` counts them all."""
    rel = [relative(p, root) for p in reads]
    outside = [p for p in rel if p not in edited]
    files = sorted(set(outside))
    t = {"reads": len(rel), "read_files": len(set(rel)), "reads_outside_edit": len(outside),
         "files_outside_edit": files, "files_outside_edit_total": len(files), "edited": len(edited)}
    return t if cap is None else capped(t, cap)


def capped(t, cap=OUTSIDE_FILES):
    """`t` with `files_outside_edit` cut to `cap` paths; the total stays."""
    return {**t, "files_outside_edit": t["files_outside_edit"][:cap]}


def describe(t):
    return (f"reads {t['reads']} over {t['read_files']} files; {t['reads_outside_edit']} reads of "
            f"{t.get('files_outside_edit_total', len(t['files_outside_edit']))} files outside the edit set ({t['edited']} files edited)")


def edited_files(git, rev, cwd=None):
    """Files changed since `rev` (any revision or range `git diff` takes), committed or not, as `git diff
    --name-only` names them: renames by their new name, untracked files one by one. None if git cannot say."""
    extra = {"cwd": cwd} if cwd else {}
    try:
        files = {f for f in git("diff", "--name-only", "-z", rev, **extra).split("\0") if f}
        for line in git("status", "--porcelain", "-uall", **extra).splitlines():
            if line.strip():  # "XY path", the leading status column possibly stripped already
                files.add(line.strip().split(None, 1)[-1].split(" -> ")[-1].strip('"'))
    except WfError:
        return None
    return files
