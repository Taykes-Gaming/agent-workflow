"""wf deploy <ref> <na|pending|staging|production> [--release TEXT]."""

from . import DEPLOY, need_field, set_deploy


def add_parser(sub, parents):
    p = sub.add_parser("deploy", parents=parents, help="set Deploy (and Release) and the deploy:* label")
    p.add_argument("ref")
    p.add_argument("where", choices=DEPLOY)
    p.add_argument("--release", help="Release text: deployed SHA or date, build number, tag")


def run(args, cfg, board):
    need_field(cfg, "deploy", "wf deploy")
    if args.release is not None:
        need_field(cfg, "release", "--release")
    ref = cfg.parse_ref(args.ref)
    name = DEPLOY[args.where]
    option = board.option_id("deploy", name)
    item = board.item(ref)
    set_deploy(cfg, board, item, args.where, option,
               {"release": args.release} if args.release is not None else None)
    shown = cfg.show_ref(ref.repo, ref.number)
    suffix = f" (release {args.release})" if args.release is not None else ""
    yield f"{shown} → Deploy {name}{suffix}", {"ref": shown, "deploy": name, "release": args.release}
