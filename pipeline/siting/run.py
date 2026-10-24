"""CLI entry point.

Example: ``python -m siting.run --config ../config/device_config.yaml --out ../output``.

Stub. The run order from the design (config validate, grid build, fetch, score,
provenance validate, validation report, atomic write, gap summary) is wired in
by later tasks.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="siting.run", description="Run the MA tidal siting pipeline."
    )
    parser.add_argument(
        "--config",
        default="../config/device_config.yaml",
        help="Path to Device_Config YAML",
    )
    parser.add_argument("--out", default="../output", help="Static_Output directory")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    print(
        "siting.run: pipeline not implemented yet "
        f"(config={args.config}, out={args.out})",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
