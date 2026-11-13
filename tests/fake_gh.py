"""An in-memory `Gh`: serves the recorded REST fixtures and records every call.

It replaces only `Gh._run`, so the real status-code check, error message and
`Link` pagination in `Gh` are what the tests exercise.
"""

import contextlib
import copy
import io
import json
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

from wf import cli
from wf.gh import API, Gh

FIXTURES = Path(__file__).resolve().parent / "fixtures"

CONFIG = """\
[board]
owner = "acme"
owner_type = "{owner_type}"
number = 2
[repos]
web = "acme/web"
mobile = "acme/mobile"
core = "acme/core"
[fields]
status = "Status"
size = "Size"
model = "Model"
findings = "Review Findings"
rounds = "Fix Rounds"
ktokens = "Agent kTokens"
turns = "Turns"
{deploy}
[status]
backlog = "Backlog"
ready = "Ready"
progress = "In progress"
blocked = "Blocked"
review = "In review"
done = "Done"
[labels]
backlog = "backlog"
size_prefix = "effort:"
deploy_prefix = "deploy:"
{review}
"""


# The optional spend fields, as `configured(fields=SPEND_FIELDS)` names them and `spend_fields` adds them to a board.
SPEND_FIELDS = 'cache_ktokens = "Cache kTokens"\nreview_ktokens = "Review kTokens"\n'


def spend_fields(gh):
    for i, name in ((1030, "Cache kTokens"), (1031, "Review kTokens")):
        gh.fields.append({"id": i, "name": name, "data_type": "number"})
    return gh


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


class FakeGh(Gh):
    def __init__(self, owner_type="user", page_size=None):
        kind = "orgs" if owner_type == "org" else "users"
        self.project = f"/{kind}/acme/projectsV2/2"
        self.fields = fixture("fields.json")
        self.items = fixture("items.json")
        self.page_size = page_size
        self.calls = []
        self.failures = {}
        self.next_item = 9900
        self.graphql_data = None  # served for POST graphql
        self.extra = {}  # (method, bare path) → (status, data) for endpoints the fake does not model
        self.milestone = {"number": 3, "title": "Sprint 3"}  # on every issue a GET serves, unless its content sets one
        self.timeline = [{"event": "cross-referenced", "source": {"issue": {
            "number": 40, "repository": {"full_name": "acme/web"},
            "pull_request": {"merged_at": "2026-10-28T10:00:00Z"}}}}]
        self.head = "claude/157-fake-head"  # the head branch of every PR a GET serves

    def fail(self, method, path, status, body):
        self.failures[(method, path)] = (status, body)

    def writes(self):
        return [c for c in self.calls if c[0] != "GET"]

    def _run(self, method, path, body):
        self.calls.append((method, path, copy.deepcopy(body)))
        bare, _, query = path.partition("?")
        if (method, bare) in self.failures:
            status, data = self.failures[(method, bare)]
            return status, {}, data
        if (method, bare) in self.extra:
            status, data = self.extra[(method, bare)]
            return status, {}, copy.deepcopy(data)
        p = self.project
        if (method, bare) == ("POST", "graphql") and self.graphql_data is not None:
            return 200, {}, copy.deepcopy(self.graphql_data)
        if (method, bare) == ("GET", f"{p}/fields"):
            return 200, {}, copy.deepcopy(self.fields)
        if (method, bare) == ("GET", f"{p}/items"):
            return self._page(bare, query)
        if (method, bare) == ("POST", f"{p}/fields"):
            self.next_item += 1
            self.fields.append({"id": self.next_item, **copy.deepcopy(body)})
            return 201, {}, {"id": self.next_item, "name": body["name"], "data_type": body["data_type"]}
        if (method, bare) == ("POST", f"{p}/items"):
            self.next_item += 1
            return 201, {}, {"id": self.next_item, "content_type": body["type"]}
        if method == "PATCH" and re.fullmatch(rf"{re.escape(p)}/items/\d+", bare):
            return 200, {}, {"id": int(bare.rsplit("/", 1)[1])}
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)/issues/(\d+)", bare)
        if method == "GET" and m:
            for it in self.items:
                c = it["content"]
                if c["repository_url"] == f"{API}/repos/{m[1]}" and c["number"] == int(m[2]):
                    return 200, {}, {"milestone": copy.deepcopy(self.milestone), **copy.deepcopy(c)}
            return 200, {}, {"id": 8000 + int(m[2]), "number": int(m[2]), "title": "new", "state": "open", "labels": [],
                             "milestone": copy.deepcopy(self.milestone)}
        if method == "PATCH" and m:
            return 200, {}, {"number": int(m[2]), **body}
        m = re.fullmatch(r"/repos/[^/]+/[^/]+/pulls/(\d+)", bare)
        if method == "GET" and m:
            return 200, {}, {"number": int(m[1]), "merged_at": "2026-10-28T10:00:00Z", "head": {"ref": self.head}}
        if method == "GET" and re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/timeline", bare):
            return 200, {}, copy.deepcopy(self.timeline)
        if re.fullmatch(r"/repos/[^/]+/[^/]+/issues/\d+/labels(/.+)?", bare) and method in ("POST", "DELETE"):
            return 200, {}, []
        return 404, {}, {"message": "Not Found"}

    def _page(self, bare, query):
        items = copy.deepcopy(self.items)
        if not self.page_size:
            return 200, {}, items
        params = dict(kv.split("=", 1) for kv in query.split("&") if kv)
        start = int(params.get("after", 0))
        end = start + self.page_size
        headers = {}
        if end < len(items):
            rest = "&".join(f"{k}={v}" for k, v in params.items() if k != "after")
            headers["link"] = f'<{API}{bare}?{rest}&after={end}>; rel="next"'
        return 200, headers, items[start:end]


@contextlib.contextmanager
def configured(owner_type="user", deploy=True, review=None, opencode=None, gate=None, fields=""):
    extra = ('deploy = "Deploy"\nrelease = "Release"\n' if deploy else "") + fields
    review = f"[review]\n{review}" if review else ""
    if opencode:
        review += f"\n[opencode]\n{opencode}"
    if gate:
        review += f"\n{gate}"
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / ".workflow.toml"
        path.write_text(CONFIG.format(owner_type=owner_type, deploy=extra, review=review))
        with mock.patch.dict(os.environ, {"WF_CONFIG": str(path)}):
            yield path


def run(argv, gh, **config):
    """Runs `wf argv` against `gh`; returns (exit code, stdout, stderr)."""
    out, err = io.StringIO(), io.StringIO()
    with configured(**config), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(argv, gh=gh)
    return code, out.getvalue(), err.getvalue()


def field_id(gh, name):
    return next(f["id"] for f in gh.fields if f["name"] == name)


def option(gh, field, name):
    f = next(f for f in gh.fields if f["name"] == field)
    return next(o["id"] for o in f["options"] if o["name"]["raw"] == name)
