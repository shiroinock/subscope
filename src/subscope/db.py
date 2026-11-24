"""SQLite に使用量を貯める。置き場所は $XDG_DATA_HOME/subscope/subscope.db。

同じものを何度取り込んでも増えないよう、どの表にも主キーを置き、INSERT OR IGNORE で入れる。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
-- 手元の Claude Code が送った 1 リクエスト(~/.claude/projects/**/*.jsonl の assistant の行)
CREATE TABLE IF NOT EXISTS claude_requests (
  request_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  session_id TEXT,
  cwd TEXT,
  git_branch TEXT,
  model TEXT,
  is_sidechain INTEGER,
  input INTEGER NOT NULL,
  output INTEGER NOT NULL,
  cache_read INTEGER NOT NULL,
  cache_create_5m INTEGER NOT NULL,
  cache_create_1h INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS claude_requests_ts ON claude_requests (ts);

-- 手元の Claude Code が制限で断られたとき
CREATE TABLE IF NOT EXISTS claude_rejects (
  ts TEXT NOT NULL,
  session_id TEXT NOT NULL,
  cwd TEXT,
  rate_limit_type TEXT,
  resets_at INTEGER,
  PRIMARY KEY (ts, session_id)
);

-- Codex の 1 リクエスト(~/.codex/sessions の token_count の last_token_usage)
CREATE TABLE IF NOT EXISTS codex_requests (
  session_id TEXT NOT NULL,
  ts TEXT NOT NULL,
  cwd TEXT,
  model TEXT,
  input INTEGER NOT NULL,
  cached_input INTEGER NOT NULL,
  output INTEGER NOT NULL,
  reasoning INTEGER NOT NULL,
  PRIMARY KEY (session_id, ts)
);
CREATE INDEX IF NOT EXISTS codex_requests_ts ON codex_requests (ts);

-- クオータの消費率の観測。window は five_hour・seven_day(Claude)、primary・secondary(Codex)
CREATE TABLE IF NOT EXISTS quota_samples (
  service TEXT NOT NULL,
  window TEXT NOT NULL,
  ts TEXT NOT NULL,
  window_minutes INTEGER,
  used_percent REAL NOT NULL,
  resets_at INTEGER,
  PRIMARY KEY (service, window, ts)
);

-- Claude Code Action が送った 1 リクエスト(artifact claude-usage-* の OTel の api_request)
CREATE TABLE IF NOT EXISTS action_requests (
  request_id TEXT PRIMARY KEY,
  ts TEXT NOT NULL,
  repository TEXT,
  workflow TEXT,
  job TEXT,
  run_id TEXT,
  number TEXT,
  model TEXT,
  query_source TEXT,
  input INTEGER NOT NULL,
  output INTEGER NOT NULL,
  cache_read INTEGER NOT NULL,
  cache_create INTEGER NOT NULL,
  cost_usd REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS action_requests_ts ON action_requests (ts);

-- GitHub の課金 API(/users/{user}/settings/billing/usage)の明細
CREATE TABLE IF NOT EXISTS github_usage (
  date TEXT NOT NULL,
  product TEXT NOT NULL,
  sku TEXT NOT NULL,
  repository TEXT NOT NULL,
  quantity REAL NOT NULL,
  unit TEXT,
  gross_amount REAL,
  discount_amount REAL,
  net_amount REAL,
  PRIMARY KEY (date, sku, repository)
);

-- 取り込んだファイル(大きさと更新時刻が同じなら読み直さない)と artifact
CREATE TABLE IF NOT EXISTS ingested_files (
  path TEXT PRIMARY KEY,
  size INTEGER NOT NULL,
  mtime_ns INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ingested_artifacts (
  repository TEXT NOT NULL,
  artifact_id INTEGER NOT NULL,
  PRIMARY KEY (repository, artifact_id)
);
"""


def data_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(Path.home(), ".local", "share")
    return Path(base) / "subscope"


def default_path() -> Path:
    return data_dir() / "subscope.db"


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or default_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def file_changed(conn: sqlite3.Connection, path: Path) -> bool:
    """前に取り込んだときから大きさか更新時刻が変わっていれば True。"""
    st = path.stat()
    row = conn.execute("SELECT size, mtime_ns FROM ingested_files WHERE path = ?", (str(path),)).fetchone()
    return row != (st.st_size, st.st_mtime_ns)


def mark_file(conn: sqlite3.Connection, path: Path) -> None:
    st = path.stat()
    conn.execute(
        "INSERT OR REPLACE INTO ingested_files (path, size, mtime_ns) VALUES (?, ?, ?)",
        (str(path), st.st_size, st.st_mtime_ns),
    )
