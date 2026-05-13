"""Daily commercial-asset news collection DAG (stub).

Schedule: @daily. Calls airflow/scripts/run_news_pipeline.py with
--asset commercial --date {{ ds }}.
"""

from __future__ import annotations

# DAG definition intentionally omitted in skeleton — Airflow is not a hard
# dependency of this repo yet. When wiring up Airflow:
#   from airflow import DAG
#   from airflow.operators.bash import BashOperator
#   ...
