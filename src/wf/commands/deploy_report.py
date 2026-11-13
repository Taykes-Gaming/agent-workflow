"""wf deploy-report: Done items whose Deploy is not Production or n/a."""

from . import DEPLOY, MAX_LINES, need_field

LIVE = (DEPLOY["production"], DEPLOY["na"])


def add_parser(sub, parents):
    p = sub.add_parser("deploy-report", parents=parents, help="merged work that is not live yet")
    p.add_argument("--all", action="store_true", help=f"print every item, not the first {MAX_LINES}")


def run(args, cfg, board):
    need_field(cfg, "deploy", "wf deploy-report")
    done = cfg.status["done"]
    rows = [it for it in sorted(board.items(), key=lambda it: (it.repo.lower(), it.number))
            if it.values.get("status") == done and it.values.get("deploy") not in LIVE]
    capped = not (args.all or args.json)  # --json is one record per item, whatever the count
    for i, it in enumerate(rows):
        if capped and i == MAX_LINES:
            yield f"… and {len(rows) - MAX_LINES} more", {"more": len(rows) - MAX_LINES}
            return
        shown = cfg.show_ref(it.repo, it.number)
        deploy = it.values.get("deploy")
        release = it.values.get("release")
        yield f"{shown}\t{deploy or 'unset'}\t{release or '-'}\t{it.title}", {
            "ref": shown, "deploy": deploy, "release": release, "title": it.title,
        }
