"""Claude Code のステータスラインの入力から、クオータの消費率を記録する。

Claude Code はステータスラインのコマンドに JSON を標準入力で渡し、Pro・Max のサブスクでは
rate_limits.five_hour・rate_limits.seven_day(used_percentage・resets_at)を含める。
ステータスラインの表示を邪魔しないよう、何も出力せず、失敗しても 0 で終わる。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime

WINDOWS = {"five_hour": 300, "seven_day": 10080}


def record(conn: sqlite3.Connection, text: str, now: datetime | None = None) -> int:
    try:
        data = json.loads(text)
    except ValueError:
        return 0
    limits = data.get("rate_limits") if isinstance(data, dict) else None
    if not isinstance(limits, dict):
        return 0
    ts = (now or datetime.now(UTC)).isoformat()
    added = 0
    for window, minutes in WINDOWS.items():
        w = limits.get(window)
        if not isinstance(w, dict) or w.get("used_percentage") is None:
            continue
        # One sample per window per minute is enough; the status line is redrawn far more often.
        minute = ts[:16]
        exists = conn.execute(
            "SELECT 1 FROM quota_samples WHERE service = 'claude' AND window = ? AND substr(ts, 1, 16) = ?",
            (window, minute),
        ).fetchone()
        if exists:
            continue
        conn.execute(
            "INSERT OR IGNORE INTO quota_samples VALUES ('claude', ?, ?, ?, ?, ?)",
            (window, ts, minutes, float(w["used_percentage"]), w.get("resets_at")),
        )
        added += 1
    conn.commit()
    return added
