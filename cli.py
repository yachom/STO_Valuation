"""Command-line entrypoints."""

from __future__ import annotations

import argparse


def cmd_analyze_address(args: argparse.Namespace) -> int:
    raise NotImplementedError


def cmd_collect_news(args: argparse.Namespace) -> int:
    raise NotImplementedError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="sto")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_analyze = sub.add_parser("analyze-address", help="On-demand address analysis")
    p_analyze.add_argument("--asset", required=True)
    p_analyze.add_argument("--address", required=True)
    p_analyze.set_defaults(func=cmd_analyze_address)

    p_news = sub.add_parser("collect-news", help="Run the batch news pipeline once")
    p_news.add_argument("--asset", required=True)
    p_news.add_argument("--date", required=True, help="YYYY-MM-DD")
    p_news.set_defaults(func=cmd_collect_news)

    args = parser.parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
