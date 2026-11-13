"""wf fields [--create]: the board's fields against `[fields]` in the config; `--create` adds the missing ones.

A field is created only when the board has none of that name, by its one
REST call (`POST …/projectsV2/<n>/fields`). An existing field is never
touched: rewriting a single-select's options over the API regenerates every
option id and blanks the column on every item (Status, Size and Deploy come
back with `wf rebuild`; Model would not). Status is built into every board
and cannot be created, so a Status whose options do not match `[status]` is
reported for the setup skill's one GraphQL call.

Option sets for a new single select come from the config (`[status]`),
`SIZES`, `DEPLOY`, and for Model the known families; add families later in
the board UI, which keeps values.
"""

from ..config import OPTIONAL_FIELDS, REQUIRED_FIELDS, STATUS_VERBS
from . import DEPLOY, SIZES

MODELS = ("opus", "sonnet", "haiku", "fable")
NUMBER = ("findings", "rounds", "ktokens", "turns", "cache_ktokens", "review_ktokens")


def add_parser(sub, parents):
    p = sub.add_parser("fields", parents=parents, help="configured fields the board lacks; --create adds them")
    p.add_argument("--create", action="store_true", help="create every missing field (never alters an existing one)")


def _options(names):
    return [{"name": n, "color": "GRAY", "description": ""} for n in names]


def spec(cfg, key):
    """The create body for a configured field."""
    name = cfg.fields[key]
    if key in NUMBER:
        return {"name": name, "data_type": "number"}
    if key == "release":
        return {"name": name, "data_type": "text"}
    options = {"size": SIZES, "deploy": tuple(DEPLOY.values()), "model": MODELS,
               "status": tuple(cfg.status[v] for v in STATUS_VERBS)}
    return {"name": name, "data_type": "single_select", "single_select_options": _options(options[key])}


def run(args, cfg, board):
    have = {f["name"]: f for f in board.fields()}
    keys = [k for k in (*REQUIRED_FIELDS, *OPTIONAL_FIELDS) if k in cfg.fields]
    missing = [k for k in keys if cfg.fields[k] not in have]
    status = have.get(cfg.fields["status"])
    if status is not None:
        want = [cfg.status[v] for v in STATUS_VERBS]
        got = [board.text(o["name"]) for o in status.get("options") or []]
        lacking = [n for n in want if n not in got]
        if lacking:
            yield (f"{status['name']}: options {', '.join(lacking)} missing; set them in the board UI or with the "
                   "setup skill's GraphQL step (wf never rewrites options)"), {"field": status["name"], "lacking": lacking}
    if not missing:
        yield f"fields: all {len(keys)} configured fields exist", {"missing": []}
        return
    if not args.create:
        yield (f"fields: {len(missing)} missing: {', '.join(cfg.fields[k] for k in missing)}; wf fields --create adds them",
               {"missing": missing})
        return
    for key in missing:
        body = spec(cfg, key)
        if key == "status":
            yield (f"{body['name']}: Status is built in and cannot be created; the board lacks a field by that name, "
                   "check [fields].status"), {"field": body["name"], "created": False}
            continue
        created = board.gh.post(f"{cfg.project_path}/fields", body)
        board.forget_fields()  # the next lookup sees the new field
        opts = f" ({', '.join(o['name'] for o in body['single_select_options'])})" if "single_select_options" in body else ""
        yield (f"{body['name']}: created, {body['data_type']}{opts}",
               {"field": body["name"], "key": key, "created": True, "id": created.get("id"), "data_type": body["data_type"]})
