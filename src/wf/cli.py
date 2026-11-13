"""wf: argument parsing, output and exit codes.

Exit 0 on success (for `wf dispatch`, opencode's own exit status); 1 with
`wf: <message>` on stderr when GitHub (a `GhError`, carrying GitHub's message)
or the config refuses; 2 on a usage error; 124 when `wf wait` times out.
"""

import argparse
import json
import sys

from . import config
from .board import Board
from .commands import (board, checkpoint, deploy, deploy_report, dispatch, fields, gate, hook, index, logread, map, merged,
                       move, rebuild, report, review, size, spend, usage, wait)
from .config import UsageError
from .gh import Gh, WfError

COMMANDS = (move, merged, deploy, size, board, fields, report, deploy_report, rebuild, review, dispatch, gate, checkpoint, usage,
            spend, hook, map, index, wait, logread)


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        raise UsageError(message)


def parser():
    common = _Parser(add_help=False)
    common.add_argument("--json", action="store_true", help="one JSON object per action on stdout")
    common.add_argument("--quiet", action="store_true", help="print errors only")
    p = _Parser(prog="wf", description="Projects v2 board commands over the GitHub REST API.")
    sub = p.add_subparsers(dest="command", required=True, metavar="COMMAND")
    for mod in COMMANDS:
        mod.add_parser(sub, [common])
        sub.choices[mod.__name__.rsplit(".", 1)[1].replace("_", "-")].set_defaults(
            run=mod.run, needs_config=getattr(mod, "NEEDS_CONFIG", True))
    return p


def main(argv=None, gh=None):
    try:
        args = parser().parse_args(argv)
        cfg = config.load() if args.needs_config else None
        board = Board(gh or Gh(), cfg) if cfg else None
        for line, record in args.run(args, cfg, board):
            if args.quiet:
                continue
            print(json.dumps(record) if args.json else line, flush=True)
    except UsageError as e:
        print(f"wf: {e}", file=sys.stderr)
        return 2
    except WfError as e:
        print(f"wf: {e}", file=sys.stderr)
        return 1
    return getattr(args, "exit_code", 0)
