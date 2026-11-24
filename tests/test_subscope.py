"""subscope のテスト。小さな作り物のログで、取り込みと報告を確かめる。"""

from __future__ import annotations

import io
import json
import sqlite3
import tempfile
import unittest
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

from subscope import db, report, statusline
from subscope.sources import actions, claude, codex, github


def memory_db() -> sqlite3.Connection:
    return db.connect(Path(":memory:"))


def write_jsonl(path: Path, entries: list[object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(e) + "\n" for e in entries), encoding="utf-8")


def claude_line(request_id: str, **usage) -> dict:
    return {
        "type": "assistant",
        "requestId": request_id,
        "timestamp": "2026-11-20T10:00:00Z",
        "sessionId": "s1",
        "cwd": "/Users/me/git/app/.claude/worktrees/feature",
        "gitBranch": "feature",
        "message": {
            "model": "claude-opus-5-5",
            "usage": {
                "input_tokens": usage.get("input", 10),
                "output_tokens": usage.get("output", 20),
                "cache_read_input_tokens": usage.get("cache_read", 1000),
                "cache_creation_input_tokens": 300,
                "cache_creation": {"ephemeral_5m_input_tokens": 100, "ephemeral_1h_input_tokens": 200},
            },
        },
    }


class ClaudeTest(unittest.TestCase):
    def test_counts_each_request_once(self):
        conn = memory_db()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(
                root / "p" / "a.jsonl",
                [
                    claude_line("r1"),
                    claude_line("r1"),  # the same response split over two lines
                    claude_line("r2", output=5),
                    {**claude_line("r3"), "message": {"model": "<synthetic>", "usage": {}}},
                    {"type": "user", "message": {"content": "hi"}},
                    {
                        "type": "system",
                        "timestamp": "2026-11-20T11:00:00Z",
                        "sessionId": "s1",
                        "error": {"rate_limit_info": {"status": "rejected", "rateLimitType": "five_hour"}},
                    },
                ],
            )
            stats = claude.ingest(conn, root)
            self.assertEqual(stats, {"files": 1, "requests": 2, "rejects": 1})
            # Unchanged files are skipped.
            self.assertEqual(claude.ingest(conn, root)["files"], 0)
        row = conn.execute(
            "SELECT cache_create_5m, cache_create_1h FROM claude_requests WHERE request_id = 'r1'"
        ).fetchone()
        self.assertEqual(tuple(row), (100, 200))

    def test_cache_creation_without_breakdown_counts_as_5m(self):
        entry = claude_line("r1")
        del entry["message"]["usage"]["cache_creation"]
        row = claude.request_row(entry)
        self.assertEqual(row[-2:], (300, 0))


class CodexTest(unittest.TestCase):
    def test_reads_requests_and_rate_limits(self):
        conn = memory_db()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            write_jsonl(
                root / "2026" / "rollout.jsonl",
                [
                    {"type": "session_meta", "payload": {"id": "c1", "cwd": "/Users/me/git/tool"}},
                    {"type": "turn_context", "payload": {"model": "gpt-5-codex"}},
                    {
                        "type": "event_msg",
                        "timestamp": "2026-11-20T10:00:00Z",
                        "payload": {
                            "type": "token_count",
                            "info": {
                                "last_token_usage": {
                                    "input_tokens": 1000,
                                    "cached_input_tokens": 800,
                                    "output_tokens": 50,
                                }
                            },
                            "rate_limits": {
                                "primary": {"used_percent": 12.0, "window_minutes": 300, "resets_at": 1764000000},
                                "secondary": {"used_percent": 3.0, "window_minutes": 10080, "resets_at": 1764500000},
                            },
                        },
                    },
                ],
            )
            stats = codex.ingest(conn, root)
        self.assertEqual(stats, {"files": 1, "requests": 1, "quota_samples": 2})
        row = conn.execute("SELECT session_id, cwd, model, input, cached_input FROM codex_requests").fetchone()
        self.assertEqual(tuple(row), ("c1", "/Users/me/git/tool", "gpt-5-codex", 1000, 800))


class StatuslineTest(unittest.TestCase):
    def test_records_once_per_minute(self):
        conn = memory_db()
        text = json.dumps(
            {
                "rate_limits": {
                    "five_hour": {"used_percentage": 40, "resets_at": 1764000000},
                    "seven_day": {"used_percentage": 10, "resets_at": 1764500000},
                }
            }
        )
        now = datetime(2026, 11, 20, 10, 0, 5, tzinfo=UTC)
        self.assertEqual(statusline.record(conn, text, now), 2)
        self.assertEqual(statusline.record(conn, text, now.replace(second=40)), 0)
        self.assertEqual(statusline.record(conn, text, now.replace(minute=1)), 2)

    def test_ignores_input_without_rate_limits(self):
        conn = memory_db()
        self.assertEqual(statusline.record(conn, "not json"), 0)
        self.assertEqual(statusline.record(conn, json.dumps({"model": {}})), 0)


def otlp_zip(meta: dict, records: list[dict]) -> bytes:
    def attr(key, value):
        kind = "intValue" if isinstance(value, int) else "doubleValue" if isinstance(value, float) else "stringValue"
        return {"key": key, "value": {kind: value}}

    batch = {
        "resourceLogs": [
            {"scopeLogs": [{"logRecords": [{"attributes": [attr(k, v) for k, v in r.items()]} for r in records]}]}
        ]
    }
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("meta.json", json.dumps(meta))
        z.writestr("otel.jsonl", json.dumps(batch) + "\n")
    return buf.getvalue()


class ActionsTest(unittest.TestCase):
    def test_reads_api_requests_from_artifact(self):
        conn = memory_db()
        data = otlp_zip(
            {"repository": "me/app", "workflow": "review", "job": "review", "run_id": 1, "number": 7},
            [
                {
                    "event.name": "api_request",
                    "request_id": "q1",
                    "model": "claude-opus-5-5",
                    "input_tokens": "10",
                    "output_tokens": 20,
                    "cache_read_tokens": 300,
                    "cache_creation_tokens": 40,
                    "cost_usd": 0.12,
                },
                {"event.name": "tool_result", "request_id": "q2"},
            ],
        )
        self.assertEqual(actions.ingest_zip(conn, "me/app", data), 1)
        self.assertEqual(actions.ingest_zip(conn, "me/app", data), 0)
        row = conn.execute("SELECT repository, number, input, output, cost_usd FROM action_requests").fetchone()
        self.assertEqual(tuple(row), ("me/app", "7", 10, 20, 0.12))


class GithubTest(unittest.TestCase):
    def test_recent_months_crosses_the_year(self):
        self.assertEqual(github.recent_months(date(2026, 2, 3), 3), [(2025, 12), (2026, 1), (2026, 2)])


class ReportTest(unittest.TestCase):
    def test_project_name(self):
        self.assertEqual(report.project_name("/Users/me/git/app/.claude/worktrees/x"), "app")
        self.assertEqual(report.project_name("/Users/me/git/app/.codex/worktrees/y/sub"), "app")
        self.assertEqual(report.project_name("/Users/me/git/tool"), "tool")
        self.assertEqual(report.project_name(None), "(不明)")
        self.assertEqual(report.project_name("/Users/me/Library/scratch-workspaces/a/b"), "(使い捨てのフォルダ)")

    def test_table_pads_by_display_width(self):
        lines = report.table(["名前", "数"], [["あ", "1"], ["abc", "22"]]).splitlines()
        widths = {report.display_width(line.rstrip()) for line in lines}
        self.assertEqual(len(widths), 1, lines)

    def test_build_with_data(self):
        conn = memory_db()
        conn.execute(
            "INSERT INTO claude_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", "2026-11-20T10:00:00Z", "s", "/x/app", "main", "claude-opus-5-5", 0, 10, 20, 1000, 0, 0),
        )
        conn.execute(
            "INSERT INTO quota_samples VALUES (?,?,?,?,?,?)",
            ("codex", "primary", "2026-11-20T10:00:00Z", 300, 30.0, 1763640000),
        )
        text = report.build(conn, days=30, now=datetime(2026, 11, 21, tzinfo=UTC))
        self.assertIn("app", text)

    def test_build_empty(self):
        text = report.build(memory_db(), now=datetime(2026, 11, 21, tzinfo=UTC))
        self.assertIn("record-statusline", text)


if __name__ == "__main__":
    unittest.main()
