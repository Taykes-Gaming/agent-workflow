"""Pruning of the working folders `wf` keeps under the git common dir. Each writer prunes its own folder as it
writes; pruning prints nothing and never raises."""

import os
import shutil
import time
from pathlib import Path


def _age(entry):
    """An entry's newest mtime: for a folder, the newest of any file inside it, recursively."""
    newest = entry.lstat().st_mtime
    if entry.is_dir() and not entry.is_symlink():
        for root, dirs, files in os.walk(entry):
            for name in [*dirs, *files]:
                try:
                    newest = max(newest, os.lstat(os.path.join(root, name)).st_mtime)
                except OSError:
                    continue
    return newest


def prune(folder, *, keep=None, older_than_days=None, spare=(), now=None):
    """Removes the entries directly inside `folder` that are beyond the `keep` newest, or untouched for more
    than `older_than_days` days before `now`, except those named in `spare`. Returns the removed names, sorted;
    an OSError on an entry leaves that entry alone."""
    now = time.time() if now is None else now
    try:
        entries = list(Path(folder).iterdir())
    except OSError:
        return []
    aged = []
    for entry in entries:
        try:
            aged.append((_age(entry), entry))
        except OSError:
            continue
    aged.sort(key=lambda pair: (pair[0], pair[1].name), reverse=True)
    doomed = set()
    if keep is not None:
        doomed |= {e.name for _, e in aged[max(keep, 0):]}
    if older_than_days is not None:
        doomed |= {e.name for age, e in aged if now - age > older_than_days * 86400}
    removed = []
    for _, entry in aged:
        if entry.name not in doomed or entry.name in spare:
            continue
        try:
            if entry.is_dir() and not entry.is_symlink():
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError:
            continue
        removed.append(entry.name)
    return sorted(removed)
