#!/usr/bin/env python3
"""review_prep.py <pr> [--repo owner/name] [--dir DIR] [--force] [--since SHA] [--previous FILE]

Everything a wf-review needs before any agent runs, gathered over `gh api`
REST (no GraphQL: its budget is shared). Writes DIR/prep.json and
DIR/diff.patch and prints the prep.json path. DIR defaults to
$TMPDIR/wf-review/<owner>-<name>-<pr>.

prep.json carries `stop`: a reason not to review (closed, draft, already
reviewed) or null; with --force the rest is gathered anyway. The lanes read the diff and issue from DIR rather than each
fetching them. When `wf` is installed, DIR/map.md is `wf map` around the
changed files (prep.json `map`), so a lane starts from the definitions the
change touches instead of searching for them.

--since SHA makes it a fix-round prep: diff.patch holds only the commits after
SHA (read from the local checkout, which must be at the PR head), pr.patch
holds the whole PR, and --previous FILE (the review those commits answer) is
copied in as previous-review.md. --since implies --force: the PR was reviewed.
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import quote

MARKER = "<!-- wf-review -->"
HISTORY_FILES = 15  # touched files probed for prior history
_LINK = re.compile(r"\b(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\s+#(\d+)\b", re.I)
_OMIT = re.compile(r"(^|/)(fixtures|__snapshots__|testdata)/|(^|/)(package-lock\.json|Cargo\.lock|Package\.resolved|yarn\.lock|pnpm-lock\.yaml|uv\.lock)$")
_REPLACES = re.compile(r"\breplac(?:e|es|ed|ing)\b|\bport(?:ed|s)?\s+from\b|\bsupersed", re.I)


def linked_issue(body):
    m = _LINK.search(body or "")
    return int(m.group(1)) if m else None


def claude_md_paths(tree_paths, changed_files):
    """The root CLAUDE.md and any CLAUDE.md in a directory above a changed file."""
    present = {p for p in tree_paths if p == "CLAUDE.md" or p.endswith("/CLAUDE.md")}
    wanted = {"CLAUDE.md"}
    for f in changed_files:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            wanted.add("/".join(parts[:i]) + "/CLAUDE.md")
    return sorted(present & wanted)


def _claude_md_tree(repo, head):
    """(CLAUDE.md paths at the PR head, whether the list may be incomplete): from `git ls-files` when the checkout is
    at the head, else from the API's recursive tree, which GitHub truncates on a very large repo."""
    local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if local == head:
        proc = subprocess.run(["git", "ls-files", "--full-name", "--", ":(top)CLAUDE.md", ":(top)*/CLAUDE.md"],
                              capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.splitlines(), False
    tree = _gh(f"/repos/{repo}/git/trees/{head}?recursive=1")
    return [t["path"] for t in tree.get("tree", [])], bool(tree.get("truncated"))


def split_diff(diff):
    """(kept diff, omitted paths): fixture, snapshot and lock-file hunks are
    left out so every lane does not re-read bulk data. A rename is omitted
    only when both its old and new paths are bulk data."""
    kept, omitted = [], []
    for block in re.split(r"(?m)^(?=diff --git )", diff):
        m = re.match(r"diff --git a/(\S+) b/(\S+)", block)
        if m and _OMIT.search(m.group(1)) and _OMIT.search(m.group(2)):
            omitted.append(m.group(2))
        else:
            kept.append(block)
    return "".join(kept), omitted


def history_path(f):
    """The path a changed file had at the PR's base: its old name if renamed."""
    return f.get("previous_filename") or f["filename"]


def stop_reason(pr, comments):
    if pr.get("state") != "open":
        return "merged" if pr.get("merged") else "closed"
    if pr.get("draft"):
        return "draft"
    if any(MARKER in (c.get("body") or "") for c in comments):
        return "already reviewed"
    return None


def history_lane(files_with_history, issue_body):
    """Run the domain-history lane when there is history to read or replaced code to compare against."""
    return bool(files_with_history) or bool(_REPLACES.search(issue_body or ""))


def _gh(path, accept=None, paginate=False, optional=False):
    argv = ["gh", "api", path]
    if accept:
        argv += ["-H", f"Accept: {accept}"]
    if paginate:
        argv += ["--paginate", "--slurp"]
    proc = subprocess.run(argv, capture_output=True, text=True)
    if proc.returncode != 0:
        if optional:
            return None
        sys.exit(f"review_prep: gh api {path}: {proc.stderr.strip() or proc.stdout.strip()}")
    if accept:
        return proc.stdout
    data = json.loads(proc.stdout)
    return [x for page in data for x in page] if paginate else data


def _repo_from_git():
    url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True, text=True).stdout.strip()
    m = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not m:
        sys.exit("review_prep: cannot read owner/name from `git remote get-url origin`; pass --repo")
    return m.group(1)


_CODE = (".py", ".cs", ".ts", ".tsx", ".js", ".mjs", ".cjs", ".go")  # what `wf map` extracts


def _map(out, changed, head, budget=1500):
    """`wf map` around the changed code files, written to DIR/map.md; None (with the reason in prep.json
    `map_skipped`) when wf is not installed, nothing changed is code, the checkout is not at the PR head
    (the map's lines would belong to another commit), or the map fails or takes over two minutes."""
    files = [f for f in changed if f.endswith(_CODE)]
    if not shutil.which("wf"):
        return None, "wf is not installed"
    if not files:
        return None, "no changed code files"
    local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if local != head:
        return None, f"checkout is at {local[:7] or 'unknown'}, PR head is {head[:7]}"
    try:
        proc = subprocess.run(["wf", "map", "--budget", str(budget), *files], capture_output=True, text=True,
                              timeout=120)
    except subprocess.TimeoutExpired:
        return None, "wf map took over 120 s"
    if proc.returncode != 0 or not proc.stdout.strip():
        return None, f"wf map failed: {proc.stderr.strip()[:200]}"
    (out / "map.md").write_text(proc.stdout)
    return str(out / "map.md"), None


def _lint(changed_py, head):
    """ruff on the changed Python files, only when the checkout is at the PR head."""
    if not changed_py or not shutil.which("ruff"):
        return None
    local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if local != head:
        return f"not run: checkout is at {local[:7] or 'unknown'}, PR head is {head[:7]}"
    proc = subprocess.run(["ruff", "check", "--output-format", "concise", *changed_py], capture_output=True, text=True)
    if proc.returncode not in (0, 1):
        return f"ruff failed: {proc.stderr.strip()[:300]}"
    return proc.stdout.strip()


def review_comments(reviews, inline, comments):
    """Every review, inline comment and wf-review comment on the PR so far: what a fix round answers."""
    out = []
    for r in reviews:
        if (r.get("body") or "").strip():
            out.append(f"## Review by {(r.get('user') or {}).get('login', '?')} ({r.get('state', '')})\n\n{r['body'].strip()}\n")
    for c in inline:
        out.append(f"## {c.get('path')}:{c.get('line') or c.get('original_line') or '?'}\n\n{(c.get('body') or '').strip()}\n")
    for c in comments:
        if MARKER in (c.get("body") or "") or "code review" in (c.get("body") or "").lower()[:200]:
            out.append(f"## Comment\n\n{c['body'].strip()}\n")
    return "\n".join(out) or "none\n"


def _since_diff(since, head):
    """The local diff from SHA to the PR head; exits if SHA is not in the head's history."""
    local = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    if local != head:
        sys.exit(f"review_prep: --since needs the checkout at the PR head {head[:7]}, not {local[:7] or 'unknown'}")
    if subprocess.run(["git", "merge-base", "--is-ancestor", since, head], capture_output=True).returncode != 0:
        sys.exit(f"review_prep: {since[:12]} is not in the PR head's history (force-pushed?); run a full review")
    diff = subprocess.run(["git", "diff", since, head], capture_output=True, text=True, check=True).stdout
    files = subprocess.run(["git", "diff", "--name-only", since, head], capture_output=True, text=True,
                           check=True).stdout.split()
    return diff, files


def main(argv):
    p = argparse.ArgumentParser(prog="review_prep.py")
    p.add_argument("pr", type=int)
    p.add_argument("--repo")
    p.add_argument("--dir")
    p.add_argument("--force", action="store_true")
    p.add_argument("--since")
    p.add_argument("--previous")
    a = p.parse_args(argv)
    n, force, out_dir = a.pr, a.force or bool(a.since), a.dir
    repo = a.repo or _repo_from_git()
    out = Path(out_dir or Path(tempfile.gettempdir()) / "wf-review" / f"{repo.replace('/', '-')}-{n}")
    out.mkdir(parents=True, exist_ok=True)

    pr = _gh(f"/repos/{repo}/pulls/{n}")
    comments = _gh(f"/repos/{repo}/issues/{n}/comments?per_page=100", paginate=True)
    prep = {"repo": repo, "pr": n, "title": pr["title"], "stop": stop_reason(pr, comments), "dir": str(out)}
    if prep["stop"] and not force:
        (out / "prep.json").write_text(json.dumps(prep, indent=2))
        print(out / "prep.json")
        return

    files = _gh(f"/repos/{repo}/pulls/{n}/files?per_page=100", paginate=True)
    changed = [f["filename"] for f in files]
    diff, omitted = split_diff(_gh(f"/repos/{repo}/pulls/{n}", accept="application/vnd.github.diff"))
    since = None
    if a.since:
        (out / "pr.patch").write_text(diff)
        delta, delta_files = _since_diff(a.since, pr["head"]["sha"])
        diff, omitted = split_diff(delta)
        since = {"sha": a.since, "files": delta_files, "pr_diff": str(out / "pr.patch")}
        if a.previous:
            shutil.copyfile(a.previous, out / "previous-review.md")
            since["previous_review"] = str(out / "previous-review.md")
        (out / "review-comments.md").write_text(review_comments(
            _gh(f"/repos/{repo}/pulls/{n}/reviews?per_page=100", paginate=True),
            _gh(f"/repos/{repo}/pulls/{n}/comments?per_page=100", paginate=True), comments))
        since["review_comments"] = str(out / "review-comments.md")
    (out / "diff.patch").write_text(diff)

    issue_n = linked_issue(pr.get("body"))
    issue = _gh(f"/repos/{repo}/issues/{issue_n}", optional=True) if issue_n else None
    if issue:
        (out / "issue.md").write_text(f"# #{issue_n} {issue['title']}\n\n{issue.get('body') or ''}\n")

    tree_paths, tree_truncated = _claude_md_tree(repo, pr["head"]["sha"])
    base = pr["base"]["sha"]
    with_history = []
    for f in [f for f in files if f["status"] != "added"][:HISTORY_FILES]:
        if _gh(f"/repos/{repo}/commits?path={quote(history_path(f))}&sha={base}&per_page=1"):
            with_history.append(f["filename"])

    map_path, map_skipped = _map(out, changed, pr["head"]["sha"])
    prep.update({
        "base": base,
        "head": pr["head"]["sha"],
        "additions": pr["additions"],
        "deletions": pr["deletions"],
        "changed_files": changed,
        "diff": str(out / "diff.patch"),
        "omitted_from_diff": omitted,
        "issue": {"number": issue_n, "title": issue["title"], "path": str(out / "issue.md")} if issue else None,
        "claude_md": claude_md_paths(tree_paths, changed),
        "claude_md_incomplete": tree_truncated,
        "files_with_history": with_history,
        "history_lane": not since and history_lane(with_history, issue.get("body") if issue else ""),
        "since": since,
        "tools": {"ast_grep": bool(shutil.which("ast-grep"))},
        "map": map_path,
        "map_skipped": map_skipped,
        "lint": _lint([f for f in changed if f.endswith(".py") and os.path.exists(f)], pr["head"]["sha"]),
    })
    (out / "prep.json").write_text(json.dumps(prep, indent=2))
    print(out / "prep.json")


if __name__ == "__main__":
    main(sys.argv[1:])
