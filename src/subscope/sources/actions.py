"""Claude Code Action の使用量を取り込む。

shiroinock/workflows の Claude のジョブは、Claude Code の OpenTelemetry の出力を artifact
claude-usage-<job>-<attempt> に残す(otel.jsonl と meta.json)。自分の持つリポジトリの artifact を
探して取ってきて、api_request のイベント(1 リクエストごとのモデル・トークン・費用)を入れる。
"""

from __future__ import annotations

import io
import json
import sqlite3
import zipfile
from datetime import UTC, datetime, timedelta

from .. import gh

PREFIX = "claude-usage-"


def ingest(conn: sqlite3.Connection, user: str, since_days: int = 100) -> dict[str, int]:
    stats = {"repositories": 0, "artifacts": 0, "requests": 0}
    since = datetime.now(UTC) - timedelta(days=since_days)
    for repo in owned_repositories(user):
        pushed = parse_time(repo.get("pushed_at"))
        if pushed is not None and pushed < since:
            continue
        full = repo["full_name"]
        stats["repositories"] += 1
        for artifact in usage_artifacts(full):
            seen = conn.execute(
                "SELECT 1 FROM ingested_artifacts WHERE repository = ? AND artifact_id = ?",
                (full, artifact["id"]),
            ).fetchone()
            if seen:
                continue
            data = gh.api_bytes(f"repos/{full}/actions/artifacts/{artifact['id']}/zip")
            stats["requests"] += ingest_zip(conn, full, data)
            conn.execute("INSERT INTO ingested_artifacts VALUES (?, ?)", (full, artifact["id"]))
            stats["artifacts"] += 1
        conn.commit()
    return stats


def owned_repositories(user: str) -> list[dict]:
    pages = gh.api("user/repos?affiliation=owner&per_page=100", paginate=True) or []
    repos = [r for page in pages for r in page]
    return [r for r in repos if r.get("owner", {}).get("login") == user]


def usage_artifacts(full_name: str) -> list[dict]:
    pages = gh.api(f"repos/{full_name}/actions/artifacts?per_page=100", paginate=True) or []
    artifacts = [a for page in pages for a in page.get("artifacts", [])]
    return [a for a in artifacts if a.get("name", "").startswith(PREFIX) and not a.get("expired")]


def ingest_zip(conn: sqlite3.Connection, repository: str, data: bytes) -> int:
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = z.namelist()
        meta = json.loads(z.read("meta.json")) if "meta.json" in names else {}
        lines = z.read("otel.jsonl").decode("utf-8", errors="replace").splitlines() if "otel.jsonl" in names else []
    added = 0
    for line in lines:
        try:
            batch = json.loads(line)
        except ValueError:
            continue
        for attrs in api_requests(batch):
            row = request_row(attrs, meta, repository)
            if row is None:
                continue
            cur = conn.execute("INSERT OR IGNORE INTO action_requests VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", row)
            added += cur.rowcount
    return added


def api_requests(batch: object):
    """OTLP(JSON)のログから、claude_code.api_request のイベントの属性を辞書で返す。"""
    if not isinstance(batch, dict):
        return
    for resource_logs in batch.get("resourceLogs") or []:
        for scope_logs in resource_logs.get("scopeLogs") or []:
            for record in scope_logs.get("logRecords") or []:
                attrs = attributes(record.get("attributes") or [])
                if attrs.get("event.name") == "api_request":
                    yield attrs


def attributes(items: list) -> dict:
    out = {}
    for item in items:
        value = item.get("value") or {}
        for kind in ("stringValue", "intValue", "doubleValue", "boolValue"):
            if kind in value:
                out[item.get("key")] = value[kind]
                break
    return out


def request_row(attrs: dict, meta: dict, repository: str) -> tuple | None:
    request_id = attrs.get("request_id") or attrs.get("client_request_id")
    if not request_id:
        return None
    return (
        request_id,
        attrs.get("event.timestamp") or "",
        meta.get("repository") or repository,
        meta.get("workflow"),
        meta.get("job"),
        meta.get("run_id"),
        meta.get("number") or None,
        attrs.get("model"),
        attrs.get("query_source"),
        to_int(attrs.get("input_tokens")),
        to_int(attrs.get("output_tokens")),
        to_int(attrs.get("cache_read_tokens")),
        to_int(attrs.get("cache_creation_tokens")),
        float(attrs.get("cost_usd") or 0),
    )


def to_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None
