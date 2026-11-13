"""wf move <ref>… <status-verb>: sets Status and keeps the backlog label in step.

Moving to Ready or In progress refuses an issue with no milestone, so "which
sprint is this for" is asked when the work is picked up. The milestone is read
from the issue itself, not the board listing, which lags (#1's live check).
Every ref is checked before the first write.
"""

from . import UsageError, WfError, refs

NEEDS_MILESTONE = ("ready", "progress")


def add_parser(sub, parents):
    p = sub.add_parser("move", parents=parents, help="set Status on one or more issues")
    p.add_argument("args", nargs="+", metavar="REF… VERB", help="issue refs, then a status verb")


def run(args, cfg, board):
    *texts, verb = args.args
    if not texts:
        raise UsageError("move needs at least one ref before the status verb")
    if verb not in cfg.status:
        raise UsageError(f"unknown status '{verb}'; valid: {', '.join(cfg.status)}")
    targets = refs(cfg, texts)
    name = cfg.status[verb]
    option = board.option_id("status", name)
    if verb in NEEDS_MILESTONE:
        bare = [cfg.show_ref(r.repo, r.number) for r in targets
                if not board.gh.get(f"/repos/{r.repo}/issues/{r.number}").get("milestone")]
        if bare:
            raise WfError(f"no milestone on {', '.join(bare)}; set the sprint's milestone before {name}")
    label = cfg.labels["backlog"]
    for ref in targets:
        item = board.item(ref)
        board.set_field(item, "status", option)
        board.set_label(item, label, verb == "backlog")
        shown = cfg.show_ref(ref.repo, ref.number)
        yield f"{shown} → {name}", {"ref": shown, "status": name}
