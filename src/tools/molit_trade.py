"""국토부 실거래가. PublicDataReader 없으면 graceful skip."""

from __future__ import annotations

import contextlib
import io
import json
import urllib.parse
from datetime import date
from typing import Any

from .. import config


def _add_months(yyyymm: str, delta: int) -> str:
    year = int(yyyymm[:4])
    month = int(yyyymm[4:6])
    idx = year * 12 + month - 1 + delta
    return f"{idx // 12:04d}{idx % 12 + 1:02d}"


def default_period(months: int = 12) -> tuple[str, str]:
    end = _add_months(date.today().strftime("%Y%m"), -1)
    start = _add_months(end, -(months - 1))
    return start, end


def _service_key() -> str | None:
    if not config.MOLIT_SERVICE_KEY:
        return None
    return urllib.parse.unquote(config.MOLIT_SERVICE_KEY)


def fetch_transactions(
    sigungu_cd: str,
    property_type: str = "상업업무용",
    trade_type: str = "매매",
    months: int = 12,
    max_rows: int = 50,
) -> dict[str, Any]:
    key = _service_key()
    if not key:
        return {"skipped": "MOLIT_SERVICE_KEY missing"}

    try:
        from PublicDataReader import TransactionPrice
    except ImportError:
        return {"skipped": "PublicDataReader not installed"}

    start, end = default_period(months)
    api = TransactionPrice(key)
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured):
        df = api.get_data(
            property_type=property_type,
            trade_type=trade_type,
            sigungu_code=sigungu_cd,
            start_year_month=start,
            end_year_month=end,
        )

    if df is None or len(df) == 0:
        return {
            "row_count": 0,
            "rows": [],
            "period": [start, end],
            "warnings": [l for l in captured.getvalue().splitlines() if l.strip()],
        }

    rows = json.loads(
        df.head(max_rows).to_json(orient="records", force_ascii=False, date_format="iso")
    )

    # 가격 추세 요약
    summary: dict[str, Any] = {"row_count": int(len(df))}
    for amount_col in ("거래금액", "보증금액", "월세금액"):
        if amount_col in df.columns:
            try:
                series = (
                    df[amount_col]
                    .astype(str)
                    .str.replace(",", "")
                    .str.replace(" ", "")
                    .replace("", "0")
                    .astype(float)
                )
                summary[amount_col] = {
                    "mean": float(series.mean()),
                    "median": float(series.median()),
                    "min": float(series.min()),
                    "max": float(series.max()),
                }
            except Exception:
                pass

    return {
        "period": [start, end],
        "summary": summary,
        "rows": rows,
    }
