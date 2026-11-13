"""wf board [--check]: drift and fix-stream lines, then open items grouped by Status, plus Done-but-not-live (Deploy neither Production nor n/a).

The first line is always the drift between the board and GitHub: items Done
but open, and items closed but not Done. `drift: none` is the normal. With
[labels].fix_prefix set, a fix stream (issues sharing one `<prefix>N` label)
that still has an open issue warns past FIX_CAP issues or FIX_DAYS days since
its first issue. Without the cache_ktokens and review_ktokens fields, a line
says what `wf merged` cannot record and how to add them; it is
a warning only. `--check` exits 1 on drift or a stream past its cap.
"""

from datetime import datetime, timezone

from ..config import STATUS_VERBS
from . import MAX_LINES, truncate
from .deploy_report import LIVE

TITLE = 60
DRIFT_REFS = 10
FIX_CAP = 10
FIX_DAYS = 7  # five working days
SPEND_FIELDS = {"cache_ktokens": "Cache kTokens", "review_ktokens": "Review kTokens"}


def add_parser(sub, parents):
    p = sub.add_parser("board", parents=parents, help="list open items by Status")
    p.add_argument("--check", action="store_true",
                   help="exit 1 on drift or a fix stream past its cap")


def _refs(cfg, items):
    shown = [cfg.show_ref(it.repo, it.number) for it in items[:DRIFT_REFS]]
    more = f" … and {len(items) - DRIFT_REFS} more" if len(items) > DRIFT_REFS else ""
    return ", ".join(shown) + more


def drift(cfg, items):
    """(Done but open, closed but not Done)."""
    done = cfg.status["done"]
    return ([it for it in items if it.state == "open" and it.values.get("status") == done],
            [it for it in items if it.state == "closed" and it.values.get("status") != done])


def fix_streams(cfg, items, now=None):
    """[(label, issues, days since its first issue)] for every fix stream with an open issue past the cap."""
    prefix = cfg.labels.get("fix_prefix")
    if not prefix:
        return []
    now = now or datetime.now(timezone.utc)
    streams = {}
    for it in items:
        for name in it.labels:
            if name.startswith(prefix):
                streams.setdefault(name, []).append(it)
    out = []
    for name, its in sorted(streams.items()):
        if not any(it.state == "open" for it in its):
            continue
        dates = [datetime.fromisoformat(it.created.replace("Z", "+00:00")) for it in its if it.created]
        days = (now - min(dates)).days if dates else 0
        if len(its) > FIX_CAP or days > FIX_DAYS:
            out.append((name, len(its), days))
    return out


def run(args, cfg, board):
    order = [cfg.status[v] for v in STATUS_VERBS]
    items = board.items()

    open_done, closed_not_done = drift(cfg, items)
    if open_done or closed_not_done:
        parts = []
        if open_done:
            parts.append(f"{len(open_done)} Done but open: {_refs(cfg, open_done)}")
        if closed_not_done:
            parts.append(f"{len(closed_not_done)} closed but not Done: {_refs(cfg, closed_not_done)}")
        line = "drift: " + "; ".join(parts)
    else:
        line = "drift: none"
    refs = lambda its: [cfg.show_ref(it.repo, it.number) for it in its[:DRIFT_REFS]]
    yield line, {"drift": {"done_open": refs(open_done), "closed_not_done": refs(closed_not_done),
                           "total": {"done_open": len(open_done), "closed_not_done": len(closed_not_done)}}}
    over = fix_streams(cfg, items)
    for name, count, days in over:
        yield (f"fix stream {name}: {count} issues over {days} days, past the cap of {FIX_CAP} issues or "
               f"{FIX_DAYS} days; plan a sprint or stop"), {"fix_stream": name, "issues": count, "days": days}
    missing = [k for k in SPEND_FIELDS if k not in cfg.fields]
    if missing:
        example = ", ".join(f'{k} = "{SPEND_FIELDS[k]}"' for k in missing)
        yield (f"fields: {', '.join(missing)} not configured; wf merged records no "
               f"{' or '.join(SPEND_FIELDS[k] for k in missing)}. Name them under [fields] in {cfg.path.name} "
               f"(e.g. {example}), then wf fields --create"), {"missing": missing}
    if args.check and (open_done or closed_not_done or over):
        args.exit_code = 1

    def key(it):
        status = it.values.get("status")
        rank = order.index(status) if status in order else len(order)
        return rank, it.repo.lower(), it.number

    rows = sorted((it for it in items if it.state == "open"), key=key)
    if "deploy" in cfg.fields:
        done = cfg.status["done"]
        rows += sorted(
            (it for it in items
             if it.state != "open" and it.values.get("status") == done and it.values.get("deploy") not in LIVE),
            key=key,
        )
    for i, it in enumerate(rows):
        if i == MAX_LINES:
            yield f"… and {len(rows) - MAX_LINES} more", {"more": len(rows) - MAX_LINES}
            return
        shown = cfg.show_ref(it.repo, it.number)
        size = it.values.get("size") or "-"
        status = it.values.get("status") or "-"
        yield f"{shown} {size} {status} {truncate(it.title, TITLE)}", {
            "ref": shown, "size": it.values.get("size"), "status": it.values.get("status"),
            "deploy": it.values.get("deploy"), "title": it.title,
        }
