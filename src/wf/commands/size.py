"""wf size <ref> <XS|S|M|L|XL>: sets Size and the effort:* label."""

from . import SIZES


def add_parser(sub, parents):
    p = sub.add_parser("size", parents=parents, help="set Size and its label")
    p.add_argument("ref")
    p.add_argument("size", choices=SIZES)


def run(args, cfg, board):
    ref = cfg.parse_ref(args.ref)
    option = board.option_id("size", args.size)
    item = board.item(ref)
    board.set_field(item, "size", option)
    prefix = cfg.labels["size_prefix"]
    board.swap_label(item, prefix, prefix + args.size)
    shown = cfg.show_ref(ref.repo, ref.number)
    yield f"{shown} → Size {args.size}", {"ref": shown, "size": args.size}
