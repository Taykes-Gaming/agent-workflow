"""One Projects v2 board over REST: its fields, its items, and writes to them.

Field and option ids are read from `GET …/fields` once per run (per `Board`)
and never cached on disk. Items are listed with `GET …/items?per_page=100`
and matched on the issue's repository and number.
"""

from dataclasses import dataclass, field
from urllib.parse import quote

from .gh import API, GhError, WfError

PER_PAGE = 100
ALREADY_ON_BOARD = "Content already exists in this project"
# The issue's own project items: REST has no issue → item route.
_ITEMS_OF_ISSUE = """
query($owner: String!, $name: String!, $number: Int!) {
  repository(owner: $owner, name: $name) {
    issue(number: $number) {
      projectItems(first: 20, includeArchived: true) {
        nodes { fullDatabaseId project { number owner { ... on User { login } ... on Organization { login } } } }
      }
    }
  }
}"""


@dataclass
class Item:
    id: int
    repo: str  # owner/name
    number: int
    title: str
    state: str  # open | closed
    labels: list
    values: dict = field(default_factory=dict)  # config field key → option name | text | number
    created: str = None  # the issue's created_at, ISO 8601


class Board:
    def __init__(self, gh, cfg):
        self.gh = gh
        self.cfg = cfg
        self.path = cfg.project_path
        self._fields = None
        self._items = None

    # Fields -----------------------------------------------------------------

    def fields(self):
        if self._fields is None:
            self._fields = self.gh.get(f"{self.path}/fields")
        return self._fields

    def forget_fields(self):
        """Drops the cached field list, so the next lookup sees fields created since."""
        self._fields = None

    @staticmethod
    def text(value):
        """A field value or option name in display form: REST wraps names as {raw, html}."""
        return _text(value)

    def field(self, key):
        if key not in self.cfg.fields:
            raise WfError(f"no '{key}' field configured in {self.cfg.path}")
        name = self.cfg.fields[key]
        for f in self.fields():
            if f["name"] == name:
                return f
        raise WfError(f"board has no field named '{name}'; fields: {', '.join(f['name'] for f in self.fields())}")

    def option_id(self, key, name):
        f = self.field(key)
        options = f.get("options") or []
        for o in options:
            if _text(o["name"]) == name:
                return o["id"]
        valid = ", ".join(_text(o["name"]) for o in options)
        raise WfError(f"no option named '{name}' on field '{f['name']}'; valid: {valid}")

    # Items ------------------------------------------------------------------

    def items(self):
        if self._items is None:
            # field() fails on a configured name the board lacks, so a typo
            # is an error rather than a column of blanks.
            by_id = {self.field(key)["id"]: key for key in self.cfg.fields}
            ids = ",".join(str(i) for i in by_id)
            query = f"per_page={PER_PAGE}" + (f"&fields={ids}" if ids else "")
            raw = self.gh.get_all(f"{self.path}/items?{query}")
            self._items = [it for it in (_item(r, by_id) for r in raw) if it]
        return self._items

    def find_item(self, ref):
        for it in self.items():
            if it.repo.lower() == ref.repo.lower() and it.number == ref.number:
                return it
        return None

    def add_item(self, ref):
        issue = self.gh.get(f"/repos/{ref.repo}/issues/{ref.number}")
        try:
            item_id = self.gh.post(f"{self.path}/items", {"type": "Issue", "id": issue["id"]})["id"]
        except GhError as e:
            if e.status != 422 or not str(e).startswith(ALREADY_ON_BOARD):
                raise
            item_id = self._find_added(ref)
        it = Item(
            id=item_id,
            repo=ref.repo,
            number=ref.number,
            title=issue.get("title", ""),
            state=issue.get("state", "open"),
            labels=[l["name"] for l in issue.get("labels") or []],
        )
        self.items().append(it)
        return it

    def _find_added(self, ref):
        """The item id of an issue already on the board but not yet in the REST
        listing, which lags an add by ~11 minutes (#1's live check). The only
        GraphQL call in wf: one query, on this path only."""
        owner, name = ref.repo.split("/", 1)
        data = self.gh.graphql(_ITEMS_OF_ISSUE, owner=owner, name=name, number=ref.number)
        nodes = (((data.get("repository") or {}).get("issue") or {}).get("projectItems") or {}).get("nodes") or []
        for node in nodes:
            if not node or not node.get("fullDatabaseId"):
                continue  # a node the token cannot see comes back null
            project = node.get("project") or {}
            login = (project.get("owner") or {}).get("login", "")
            if project.get("number") == self.cfg.number and login.lower() == self.cfg.owner.lower():
                return int(node["fullDatabaseId"])
        raise WfError(f"{self.cfg.show_ref(ref.repo, ref.number)} is on the board but could not be found")

    def item(self, ref):
        """The board item for `ref`, adding the issue to the board on a miss."""
        return self.find_item(ref) or self.add_item(ref)

    def set_field(self, item, key, value):
        """Writes one field: `value` is an option id, text or number."""
        self.set_fields(item, {key: value})

    def set_fields(self, item, values):
        """Writes every field of `values` (field key → option id, text or number) in one PATCH;
        nothing is sent when `values` is empty. `item.values` is not updated: it holds the listing's
        option names, and a write carries option ids."""
        if values:
            self.gh.patch(f"{self.path}/items/{item.id}",
                          {"fields": [{"id": self.field(k)["id"], "value": v} for k, v in values.items()]})

    # Labels (the board ↔ label mirror) --------------------------------------

    def set_label(self, item, name, present):
        """Adds or removes exactly the label `name` (the backlog mirror)."""
        base = f"/repos/{item.repo}/issues/{item.number}/labels"
        if present and name not in item.labels:
            self.gh.post(base, {"labels": [name]})
            item.labels.append(name)
        elif not present and name in item.labels:
            self._delete_label(base, name)
            item.labels.remove(name)

    def swap_label(self, item, prefix, want):
        """Removes every label on the issue starting with `prefix` other than
        `want`, then adds `want` (unless empty or already there)."""
        base = f"/repos/{item.repo}/issues/{item.number}/labels"
        for name in list(item.labels):
            if name.startswith(prefix) and name != want:
                self._delete_label(base, name)
                item.labels.remove(name)
        if want and want not in item.labels:
            self.gh.post(base, {"labels": [want]})
            item.labels.append(want)

    def _delete_label(self, base, name):
        """Removes one label. The labels come from the item listing, which lags
        (#1's live check), so GitHub's "Label does not exist" means it is already
        gone: not an error. Any other 404 (an issue or private repo the token
        cannot see) still fails."""
        try:
            self.gh.delete(f"{base}/{quote(name, safe='')}")
        except GhError as e:
            if e.status != 404 or str(e) != "Label does not exist":
                raise


def _text(v):
    """A field value or option name in display form: REST wraps names as {raw, html}."""
    if isinstance(v, dict):
        if "name" in v:
            return _text(v["name"])
        if "raw" in v:
            return v["raw"]
    return v


def _item(raw, by_id):
    content = raw.get("content") or {}
    if "number" not in content or "repository_url" not in content:
        return None  # a draft issue: nothing for a ref to name
    values = {}
    for f in raw.get("fields") or []:
        if f.get("id") in by_id and f.get("value") is not None:
            values[by_id[f["id"]]] = _text(f["value"])
    return Item(
        id=raw["id"],
        repo=content["repository_url"].removeprefix(f"{API}/repos/"),
        number=content["number"],
        title=content.get("title", ""),
        state=content.get("state", "open"),
        labels=[l["name"] for l in content.get("labels") or []],
        values=values,
        created=content.get("created_at"),
    )
