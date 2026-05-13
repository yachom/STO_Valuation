"""Airflow-invoked entrypoint for the daily news pipeline.

The DAG calls this via PythonOperator/BashOperator. All actual logic lives in
``common.services.news.pipeline`` + the asset's tagger/graph_signal.
"""

from __future__ import annotations

import argparse
from datetime import date


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--asset", required=True)
    parser.add_argument("--date", required=True, help="YYYY-MM-DD")
    args = parser.parse_args(argv)
    _target_date = date.fromisoformat(args.date)
    raise NotImplementedError


if __name__ == "__main__":
    raise SystemExit(main())
