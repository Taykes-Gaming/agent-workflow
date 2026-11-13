"""wf rebuild [--dry-run]: Status, Size and Deploy from issue state and labels.

The rules of the shell `rebuild-from-labels.sh`:
  Status: closed → done; open with the backlog label → backlog; other open → ready.
  Size:   an effort:<size> label → that option; no label → untouched.
  Deploy: a deploy:<verb> label → that option; no label → untouched.
Only fields that differ from the target are written, one PATCH per item.
Rewriting a single-select field's options clears every item's value, and the
labels mirror the board so it can be rebuilt. Model, the counts and Release
are not touched; In progress / Blocked / In review are lost and re-set by hand.
"""

from . import DEPLOY, SIZES


def add_parser(sub, parents):
    p = sub.add_parser("rebuild", parents=parents, help="rewrite Status/Size/Deploy from issue state and labels")
    p.add_argument("--dry-run", action="store_true", help="print the edits, make none")


def targets(cfg, item):
    """[(field key, option name)] the rules give for one item."""
    out = []
    if item.state == "closed":
        out.append(("status", cfg.status["done"]))
    elif cfg.labels["backlog"] in item.labels:
        out.append(("status", cfg.status["backlog"]))
    else:
        out.append(("status", cfg.status["ready"]))
    sizes = [s for s in SIZES if cfg.labels["size_prefix"] + s in item.labels]
    if sizes:
        out.append(("size", sizes[-1]))
    if "deploy" in cfg.fields:
        verbs = [d for d in DEPLOY if cfg.labels["deploy_prefix"] + d in item.labels]
        if verbs:
            out.append(("deploy", DEPLOY[verbs[-1]]))
    return out


def run(args, cfg, board):
    for it in board.items():
        shown = cfg.show_ref(it.repo, it.number)
        changes = [(key, name) for key, name in targets(cfg, it) if it.values.get(key) != name]
        options = {key: board.option_id(key, name) for key, name in changes}  # a missing option fails a dry run too
        if not args.dry_run:
            board.set_fields(it, options)
        for key, name in changes:
            field = cfg.fields[key]
            if args.dry_run:
                yield f"would set {shown} {field} → {name}", {"ref": shown, "field": field, "value": name, "applied": False}
            else:
                yield f"{shown} {field} → {name}", {"ref": shown, "field": field, "value": name, "applied": True}
