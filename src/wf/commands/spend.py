"""wf spend <record-ref> [--since TIME]: the orchestrator's spend onto the sprint record.

The orchestrator's work (dispatch, rulings, review of hand-backs, the owner's
questions) belongs to no one issue, so it is recorded once per sprint, on the
sprint-record issue. Its sessions are the `session` lines `wf hook` wrote to
the ledger of the base branch (`[gate].base`, `[opencode].base`, then `dev`,
as `wf gate` resolves it), each measured from its transcript between the
record's creation (or `--since`) and its close (or now). A session that
stopped on an issue branch is that issue's, and subagents are the
implementers' `wf merged`; neither is counted here.

Sets Agent kTokens, and Cache kTokens and Turns when those fields are
configured; never Model, so `wf report` keeps the record out of the model
rows and prints it as its own `orchestrator` row. Re-running recomputes from
the transcripts, so it never counts twice. Refuses an issue without the
record label (`[labels].record`, default `sprint-record`).
"""

from pathlib import Path

from .. import ledger
from . import WfError, usage
from . import base_branch


def add_parser(sub, parents):
    p = sub.add_parser("spend", parents=parents, help="record the orchestrator's spend on the sprint record")
    p.add_argument("ref", help="the sprint-record issue")
    p.add_argument("--since", type=usage.when, metavar="TIME", help="window start (default: the record's creation)")


def record_label(cfg):
    return cfg.labels.get("record", "sprint-record")


def run(args, cfg, board):
    ref = cfg.parse_ref(args.ref)
    label = record_label(cfg)
    base = base_branch(cfg, cfg.gate)
    common, _ = ledger.where()
    sessions = ledger.latest(ledger.read(common, base), "session")
    paths = [p for p in dict.fromkeys(e.get("transcript") for e in sessions) if p and Path(p).is_file()]
    if not paths:
        raise WfError(f"no session on {base} in the ledger ({ledger.path(common, base)}) with a transcript here: "
                      "the wf-setup plugin's hooks record them from install on")
    issue = board.gh.get(f"/repos/{ref.repo}/issues/{ref.number}")
    shown = cfg.show_ref(ref.repo, ref.number)
    if label not in [l["name"] for l in issue.get("labels") or []]:
        raise WfError(f"{shown} has no '{label}' label; wf spend records onto the sprint record only")
    since = args.since or usage.when(issue["created_at"])
    until = usage.when(issue["closed_at"]) if issue.get("closed_at") else None
    t = usage.measure(*paths, since=since, until=until)
    item = board.item(ref)
    values = {"ktokens": usage.kilo(t["tokens"])}
    if "cache_ktokens" in cfg.fields:
        values["cache_ktokens"] = usage.kilo(t["cache_read"])
    if "turns" in cfg.fields:
        values["turns"] = t["turns"]
    board.set_fields(item, values)
    window = f"{since:%Y-%m-%d %H:%M}–{f'{until:%Y-%m-%d %H:%M}' if until else 'now'} UTC"
    yield (f"{shown}: orchestrator {usage.describe(t)}, {len(paths)} session(s) on {base}, {window}",
           {"ref": shown, "branch": base, "sessions": len(paths), "since": since.isoformat(),
            "until": until.isoformat() if until else None, "ktokens": usage.kilo(t["tokens"]),
            "cache_ktokens": usage.kilo(t["cache_read"]), **{k: v for k, v in t.items() if k != "read_paths"}})
