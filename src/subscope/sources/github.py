"""GitHub の課金 API の明細を取り込む(Actions の分・保存容量など)。

/users/{user}/settings/billing/usage は gh のトークンに user スコープが要る
(gh auth refresh -h github.com -s user)。
"""

from __future__ import annotations

import sqlite3
from datetime import date

from .. import gh


def ingest(conn: sqlite3.Connection, user: str, months: list[tuple[int, int]]) -> dict[str, int]:
    stats = {"months": 0, "items": 0}
    for year, month in months:
        data = gh.api(f"users/{user}/settings/billing/usage?year={year}&month={month}")
        items = (data or {}).get("usageItems") or [] if isinstance(data, dict) else []
        for item in items:
            row = (
                (item.get("date") or "")[:10],
                item.get("product") or "",
                item.get("sku") or "",
                item.get("repositoryName") or "",
                float(item.get("quantity") or 0),
                item.get("unitType"),
                item.get("grossAmount"),
                item.get("discountAmount"),
                item.get("netAmount"),
            )
            # The current month's numbers grow during the month: replace, not ignore.
            conn.execute("INSERT OR REPLACE INTO github_usage VALUES (?,?,?,?,?,?,?,?,?)", row)
            stats["items"] += 1
        stats["months"] += 1
    conn.commit()
    return stats


def recent_months(today: date, count: int) -> list[tuple[int, int]]:
    months = []
    year, month = today.year, today.month
    for _ in range(count):
        months.append((year, month))
        month -= 1
        if month == 0:
            year, month = year - 1, 12
    return list(reversed(months))
