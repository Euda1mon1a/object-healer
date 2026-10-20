"""object-healer: with no command, open the window; `object-healer heal` repairs from the shell.

Exit status: 0 when every file healed, 1 when any failed, 2 for a usage or setup error.
"""
import argparse
import sys

from . import __version__, floaters
from .blender import (DEFAULT_TIMEOUT, SUPPORTED, BlenderNotFound, HealResult, find_blender,
                      heal_file)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="object-healer",
        description="Repair meshes for 3D printing in headless Blender. "
                    "With no command, opens the drag-and-drop window.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")
    sub.add_parser("gui", help="open the drag-and-drop window (the default)")
    heal = sub.add_parser(
        "heal", help="heal files from the command line",
        description="Merge duplicate vertices, make normals consistent and fill holes. "
                    f"Formats: {' '.join(SUPPORTED)}. Each IN is written to <name>_healed.<ext> "
                    "next to it unless -o names the output.",
    )
    heal.add_argument("inputs", nargs="+", metavar="IN", help="mesh file(s) to heal")
    heal.add_argument("-o", "--output", metavar="OUT",
                      help="output path for a single IN; its extension picks the format")
    heal.add_argument("--strip-floaters", action="store_true",
                      help="first remove pieces smaller than 5%% of the biggest piece "
                           "(scan debris); needs object-healer[scan]")
    heal.add_argument("--blender", metavar="PATH",
                      help="Blender executable (default: $BLENDER, then Blender.app, then PATH)")
    heal.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, metavar="SECONDS",
                      help=f"limit per file (default {DEFAULT_TIMEOUT})")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command in (None, "gui"):
        from .gui import run_gui
        return run_gui()

    if args.output and len(args.inputs) > 1:
        parser.error("-o/--output takes a single IN")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.strip_floaters and not floaters.available():
        print(f"object-healer: error: {floaters.MISSING_EXTRA}", file=sys.stderr)
        return 2
    try:
        blender = find_blender({"BLENDER": args.blender} if args.blender else None)
    except BlenderNotFound as e:
        print(f"object-healer: error: {e}", file=sys.stderr)
        return 2

    failed = 0
    for src in args.inputs:
        try:
            r = heal_file(src, args.output, blender=blender, strip_floaters=args.strip_floaters,
                          timeout=args.timeout)
        except Exception as e:  # one bad file must not stop the rest of the batch
            r = HealResult(False, src, reason=f"unexpected error: {type(e).__name__}: {e}")
        if r.ok:
            extra = f"; removed {r.floaters_removed} floating piece(s)" if r.floaters_removed else ""
            print(f"healed {src} -> {r.output} ({r.vertices:,} vertices, {r.faces:,} faces{extra})")
        else:
            failed += 1
            print(f"failed {src}: {r.reason}", file=sys.stderr)
    return 1 if failed else 0
