"""The one door to GitHub: `gh api` over REST.

`gh` owns authentication. Every call runs `gh api <path> -i --method M
[--input -]`; `-i` puts the status line and headers on stdout so a non-2xx
response becomes a `GhError` carrying GitHub's own message, and so the `Link`
header can drive pagination. REST by default: the GraphQL budget is shared
and was exhausted on 2026-10-06, and the REST budget is separate. `graphql`
exists for the one lookup REST cannot do (`Board.add_item`).
"""

import json
import re
import subprocess

API = "https://api.github.com"
_NEXT = re.compile(r'<([^>]+)>;\s*rel="next"')


def run_git(*args, cwd=None, strip=True):
    """`git <args>` in `cwd`; a failure is a WfError carrying git's message. Every wf Runner's `git` is this."""
    proc = subprocess.run(["git", *args], capture_output=True, text=True, cwd=cwd)
    if proc.returncode != 0:
        raise WfError(f"git {args[0]}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip() if strip else proc.stdout


def common_dir(cwd=None, git=run_git):
    """The absolute git common directory of the checkout at `cwd`, shared by every worktree of it. `git` is the
    caller's runner, so a test can stand in for it."""
    args = ("rev-parse", "--path-format=absolute", "--git-common-dir")
    return (git(*args, cwd=cwd) if cwd is not None else git(*args)).strip()


def branch_file(branch):
    """The file stem a branch's notes and ledger are kept under: `/` becomes `-`."""
    return branch.replace("/", "-")


class WfError(Exception):
    """An error reported to the user as `wf: <message>`, exit 1."""


class GhError(WfError):
    """A failed GitHub call; the message is GitHub's."""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class Gh:
    def get(self, path):
        return self.call("GET", path)[0]

    def post(self, path, body):
        return self.call("POST", path, body)[0]

    def patch(self, path, body):
        return self.call("PATCH", path, body)[0]

    def delete(self, path):
        return self.call("DELETE", path)[0]

    def graphql(self, query, **variables):
        """One GraphQL request. Errors raise GhError unless data came back with
        them: GitHub returns partial data with per-node errors (a project the
        token cannot see), and the caller can use the nodes that resolved."""
        body = self.call("POST", "graphql", {"query": query, "variables": variables})[0]
        if not isinstance(body, dict):
            raise GhError(f"unexpected GraphQL response: {str(body)[:200]}")
        if body.get("errors") and not body.get("data"):
            raise GhError("; ".join(e.get("message", "GraphQL error") for e in body["errors"]))
        return body.get("data") or {}

    def get_all(self, path):
        """Every element of a paginated list endpoint, following `Link: rel="next"`."""
        out = []
        while path:
            data, headers = self.call("GET", path)
            out.extend(data or [])
            m = _NEXT.search(headers.get("link", ""))
            path = m.group(1).removeprefix(API) if m else None
        return out

    def call(self, method, path, body=None):
        status, headers, data = self._run(method, path, body)
        if not 200 <= status < 300:
            raise GhError(_message(status, data), status)
        return data, headers

    def _run(self, method, path, body):
        """Runs one request; returns (status, lower-cased headers, parsed body or None)."""
        argv = ["gh", "api", path, "-i", "--method", method]
        if body is not None:
            argv += ["--input", "-"]
        try:
            proc = subprocess.run(
                argv,
                input=json.dumps(body) if body is not None else None,
                capture_output=True,
                text=True,
            )
        except FileNotFoundError:
            raise GhError("gh is not installed or not on PATH") from None
        return _parse(proc.stdout, proc.stderr)


def _parse(stdout, stderr):
    if not stdout.startswith("HTTP/"):
        raise GhError(stderr.strip() or "gh api failed with no response")
    head, _, text = stdout.replace("\r\n", "\n").partition("\n\n")
    lines = head.split("\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        k, _, v = line.partition(":")
        headers[k.strip().lower()] = v.strip()
    text = text.strip()
    try:
        data = json.loads(text) if text else None
    except json.JSONDecodeError:
        data = text
    return status, headers, data


def _message(status, data):
    if isinstance(data, dict) and data.get("message"):
        msg = data["message"]
        details = [e.get("message") for e in data.get("errors") or [] if isinstance(e, dict) and e.get("message")]
        return msg + (": " + "; ".join(details) if details else "")
    if isinstance(data, str) and data:
        return data
    return f"HTTP {status}"
