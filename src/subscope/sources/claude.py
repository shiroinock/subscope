"""手元の Claude Code のログ(~/.claude/projects/**/*.jsonl)を取り込む。

- assistant の行の message.usage を、requestId ごとに 1 リクエストとして入れる。1 つの応答が複数の
  行(テキスト・ツール呼び出し)に分かれても、同じ requestId の usage は同じなので 1 回だけ数える。
- 制限で断られたとき(rate_limit_info の status が rejected)を claude_rejects に入れる。
- クオータの消費率はログに無い。ステータスラインの入力から statusline.record で取る。
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .. import db


def default_root() -> Path:
    config = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(Path.home(), ".claude")
    return Path(config) / "projects"


def ingest(conn: sqlite3.Connection, root: Path | None = None) -> dict[str, int]:
    root = root or default_root()
    stats = {"files": 0, "requests": 0, "rejects": 0}
    if not root.is_dir():
        return stats
    for path in sorted(root.rglob("*.jsonl")):
        if not db.file_changed(conn, path):
            continue
        requests, rejects = ingest_file(conn, path)
        db.mark_file(conn, path)
        stats["files"] += 1
        stats["requests"] += requests
        stats["rejects"] += rejects
    conn.commit()
    return stats


def ingest_file(conn: sqlite3.Connection, path: Path) -> tuple[int, int]:
    requests = rejects = 0
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            # Most lines are neither; skip them before parsing.
            if '"usage"' not in line and "rate_limit_info" not in line:
                continue
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if not isinstance(entry, dict):
                continue
            row = request_row(entry)
            if row is not None:
                cur = conn.execute("INSERT OR IGNORE INTO claude_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", row)
                requests += cur.rowcount
            reject = reject_row(entry)
            if reject is not None:
                cur = conn.execute("INSERT OR IGNORE INTO claude_rejects VALUES (?,?,?,?,?)", reject)
                rejects += cur.rowcount
    return requests, rejects


def request_row(entry: dict) -> tuple | None:
    message = entry.get("message")
    if entry.get("type") != "assistant" or not isinstance(message, dict):
        return None
    usage = message.get("usage")
    request_id = entry.get("requestId")
    model = message.get("model")
    # "<synthetic>" is a message Claude Code made itself (an error, a cancel); it used no tokens.
    if not isinstance(usage, dict) or not request_id or model == "<synthetic>":
        return None
    creation = usage.get("cache_creation") or {}
    created = int(usage.get("cache_creation_input_tokens") or 0)
    created_1h = int(creation.get("ephemeral_1h_input_tokens") or 0)
    created_5m = int(creation.get("ephemeral_5m_input_tokens") or (created - created_1h))
    return (
        request_id,
        entry.get("timestamp") or "",
        entry.get("sessionId"),
        entry.get("cwd"),
        entry.get("gitBranch"),
        model,
        1 if entry.get("isSidechain") else 0,
        int(usage.get("input_tokens") or 0),
        int(usage.get("output_tokens") or 0),
        int(usage.get("cache_read_input_tokens") or 0),
        max(created_5m, 0),
        created_1h,
    )


def reject_row(entry: dict) -> tuple | None:
    info = find_rate_limit_info(entry)
    if not info or info.get("status") != "rejected":
        return None
    return (
        entry.get("timestamp") or "",
        entry.get("sessionId") or "",
        entry.get("cwd"),
        info.get("rateLimitType"),
        info.get("resetsAt"),
    )


def find_rate_limit_info(value: object, depth: int = 0) -> dict | None:
    """rate_limit_info は行の種類によって入れ子の深さが違うので、浅く探す。"""
    if depth > 4:
        return None
    if isinstance(value, dict):
        info = value.get("rate_limit_info")
        if isinstance(info, dict):
            return info
        for child in value.values():
            if isinstance(child, (dict, list)):
                found = find_rate_limit_info(child, depth + 1)
                if found:
                    return found
    elif isinstance(value, list):
        for child in value:
            found = find_rate_limit_info(child, depth + 1)
            if found:
                return found
    return None
