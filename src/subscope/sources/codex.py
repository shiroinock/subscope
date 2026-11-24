"""Codex のログ(~/.codex/sessions/**/*.jsonl)を取り込む。

- session_meta の行からセッションの id と作業ディレクトリを、turn_context の行からモデルを読む。
- token_count の行の info.last_token_usage を 1 リクエストとして入れる。
- 同じ行の rate_limits(primary・secondary の used_percent・window_minutes・resets_at)を
  クオータの観測として入れる。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .. import db


def default_root() -> Path:
    home = os.environ.get("CODEX_HOME") or os.path.join(Path.home(), ".codex")
    return Path(home) / "sessions"


def ingest(conn: sqlite3.Connection, root: Path | None = None) -> dict[str, int]:
    root = root or default_root()
    stats = {"files": 0, "requests": 0, "quota_samples": 0}
    if not root.is_dir():
        return stats
    for path in sorted(root.rglob("*.jsonl")):
        if not db.file_changed(conn, path):
            continue
        requests, samples = ingest_file(conn, path)
        db.mark_file(conn, path)
        stats["files"] += 1
        stats["requests"] += requests
        stats["quota_samples"] += samples
    conn.commit()
    return stats


def ingest_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    session_id = path.stem
    cwd = model = None
    requests = samples = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            if not any(key in line for key in ('"session_meta"', '"turn_context"', '"token_count"')):
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            payload = entry.get("payload") or {}
            kind = entry.get("type")
            if kind == "session_meta":
                session_id = payload.get("id") or payload.get("session_id") or session_id
                cwd = payload.get("cwd") or cwd
            elif kind == "turn_context":
                cwd = payload.get("cwd") or cwd
                model = payload.get("model") or model
            elif payload.get("type") == "token_count":
                ts = entry.get("timestamp") or ""
                usage = (payload.get("info") or {}).get("last_token_usage")
                if isinstance(usage, dict):
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO codex_requests VALUES (?,?,?,?,?,?,?,?)",
                        (
                            session_id,
                            ts,
                            cwd,
                            model,
                            int(usage.get("input_tokens") or 0),
                            int(usage.get("cached_input_tokens") or 0),
                            int(usage.get("output_tokens") or 0),
                            int(usage.get("reasoning_output_tokens") or 0),
                        ),
                    )
                    requests += cur.rowcount
                samples += record_rate_limits(conn, ts, payload.get("rate_limits"))
    return requests, samples


def record_rate_limits(conn: sqlite3.Connection, ts: str, limits: object) -> int:
    if not isinstance(limits, dict) or not ts:
        return 0
    added = 0
    for window in ("primary", "secondary"):
        w = limits.get(window)
        if not isinstance(w, dict) or w.get("used_percent") is None:
            continue
        cur = conn.execute(
            "INSERT OR IGNORE INTO quota_samples VALUES (?,?,?,?,?,?)",
            ("codex", window, ts, w.get("window_minutes"), float(w["used_percent"]), w.get("resets_at")),
        )
        added += cur.rowcount
    return added
