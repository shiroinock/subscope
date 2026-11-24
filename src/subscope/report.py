"""貯めた使用量から報告を作る。

サブスクのクオータの消費は、トークンの種類ごとの重さが API の値段の比に近いと見て、
「入力換算トークン」(入力 1 トークン分に換算した量)で比べる。値段の比は次のとおり。

- Claude:入力 1、出力 5、キャッシュの読み 0.1、キャッシュの書き 1.25(5 分)・2(1 時間)
- Codex:キャッシュに無い入力 1、キャッシュの入力 0.1、出力 8

クオータの消費率そのものではないので、プロジェクトやタスクのあいだの比べ物として使う。
消費率は quota_samples(Claude はステータスライン、Codex はログ)から出す。
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import PurePosixPath

CLAUDE_WEIGHTED = "input + 5 * output + 0.1 * cache_read + 1.25 * cache_create_5m + 2 * cache_create_1h"
CODEX_WEIGHTED = "(input - cached_input) + 0.1 * cached_input + 8 * output"
ACTION_WEIGHTED = "input + 5 * output + 0.1 * cache_read + 1.25 * cache_create"


def project_name(cwd: str | None) -> str:
    """作業ディレクトリからプロジェクトの名前を出す。worktree は元のリポジトリにまとめる。"""
    if not cwd:
        return "(不明)"
    parts = PurePosixPath(cwd).parts
    if "scratch-workspaces" in parts:
        return "(使い捨てのフォルダ)"
    # <repo>/.claude/worktrees/<name>, <repo>/.codex/worktrees/<name>
    for i in range(1, len(parts) - 1):
        if parts[i] in (".claude", ".codex") and parts[i + 1] == "worktrees":
            return parts[i - 1]
    return parts[-1] if parts else cwd


@dataclass
class Window:
    service: str
    window: str
    resets_at: int
    window_minutes: int
    used: float
    last_seen: str


def windows(conn: sqlite3.Connection, service: str, window: str) -> list[Window]:
    rows = conn.execute(
        """
        SELECT resets_at, MAX(window_minutes), MAX(used_percent), MAX(ts)
        FROM quota_samples
        WHERE service = ? AND window = ? AND resets_at IS NOT NULL
        GROUP BY resets_at ORDER BY resets_at
        """,
        (service, window),
    ).fetchall()
    # resets_at can move by a second or two between samples of the same window: merge those.
    merged: list[Window] = []
    for resets_at, minutes, used, last in rows:
        if merged and resets_at - merged[-1].resets_at < 600:
            w = merged[-1]
            w.resets_at, w.used, w.last_seen = resets_at, max(w.used, used), max(w.last_seen, last)
        else:
            merged.append(Window(service, window, resets_at, minutes or 0, used, last))
    return merged


def fmt_tokens(n: float) -> str:
    for unit, size in (("B", 1e9), ("M", 1e6), ("k", 1e3)):
        if abs(n) >= size:
            return f"{n / size:.1f}{unit}"
    return f"{n:.0f}"


def table(headers: list[str], rows: list[list[str]]) -> str:
    if not rows:
        return "  (なし)"
    widths = [max(display_width(str(c)) for c in col) for col in zip(headers, *rows)]
    lines = []
    for row in [headers, *rows]:
        cells = [pad(str(c), w, right=i > 0) for i, (c, w) in enumerate(zip(row, widths))]
        lines.append("  " + "  ".join(cells).rstrip())
    return "\n".join(lines)


def display_width(s: str) -> int:
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in s)


def pad(s: str, width: int, right: bool) -> str:
    gap = " " * (width - display_width(s))
    return gap + s if right else s + gap


def quota_section(conn: sqlite3.Connection, now: datetime) -> str:
    out = ["## クオータの消費率"]
    now_ts = now.timestamp()
    any_data = False
    for service, window, label in (
        ("claude", "seven_day", "Claude 7 日枠"),
        ("claude", "five_hour", "Claude 5 時間枠"),
        ("codex", "primary", "Codex 主枠"),
        ("codex", "secondary", "Codex 副枠"),
    ):
        ws = windows(conn, service, window)
        if not ws:
            continue
        any_data = True
        current = [w for w in ws if w.resets_at > now_ts]
        past = [w for w in ws if w.resets_at <= now_ts]
        out.append(f"\n### {label}")
        if current:
            w = current[-1]
            length = w.window_minutes * 60 or 1
            elapsed = max(0.0, min(1.0, 1 - (w.resets_at - now_ts) / length))
            pace = w.used / elapsed if elapsed > 0.02 else None
            reset = datetime.fromtimestamp(w.resets_at, UTC).astimezone()
            line = f"  いま {w.used:.0f}% 使用、枠の {elapsed * 100:.0f}% が経過(リセット {reset:%m-%d %H:%M})"
            if pace is not None:
                line += f"。このペースだとリセット時に {pace:.0f}%"
                if pace < 80:
                    line += f"(約 {100 - pace:.0f}% 余る見込み)"
                elif pace > 100:
                    line += "(途中で使い切る見込み)"
            out.append(line)
        if window == "five_hour":
            full = sum(1 for w in past if w.used >= 99.5)
            out.append(f"  終わった枠 {len(past)} 個のうち、使い切った枠 {full} 個")
        elif past:
            rows = []
            for w in past[-6:]:
                reset = datetime.fromtimestamp(w.resets_at, UTC).astimezone()
                rows.append([f"{reset:%m-%d}", f"{w.used:.0f}%", f"{max(0.0, 100 - w.used):.0f}%"])
            out.append(table(["リセット", "最大の使用", "余らせた"], rows))
    if not windows(conn, "claude", "seven_day") and not windows(conn, "claude", "five_hour"):
        out.append(
            "\n  Claude の消費率はまだ記録が無い。ステータスラインのコマンドから `subscope record-statusline` を"
            "呼ぶと記録が始まる(README)。"
        )
    if not any_data and not windows(conn, "codex", "primary"):
        out.append("  Codex の消費率は `subscope collect` でログから読む。")
    return "\n".join(out)


def claude_section(conn: sqlite3.Connection, since: str, top: int) -> str:
    out = ["## Claude(手元の Claude Code)"]
    rows = conn.execute(
        f"""SELECT cwd, model, COUNT(*), SUM({CLAUDE_WEIGHTED}), SUM(cache_read),
                   SUM(input + cache_read + cache_create_5m + cache_create_1h), SUM(is_sidechain)
            FROM claude_requests WHERE ts >= ? GROUP BY cwd, model""",
        (since,),
    ).fetchall()
    by_project: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0, 0])
    by_model: dict[str, float] = defaultdict(float)
    for cwd, model, count, weighted, cache_read, all_input, side in rows:
        p = by_project[project_name(cwd)]
        p[0] += count
        p[1] += weighted or 0
        p[2] += cache_read or 0
        p[3] += all_input or 0
        p[4] += side or 0
        by_model[model or "(不明)"] += weighted or 0
    total = sum(p[1] for p in by_project.values())
    if not total:
        out.append("  (この期間の記録なし)")
        return "\n".join(out)
    out.append(f"  入力換算 {fmt_tokens(total)} トークン、{sum(int(p[0]) for p in by_project.values())} リクエスト")
    models = ", ".join(f"{m} {v / total:.0%}" for m, v in sorted(by_model.items(), key=lambda x: -x[1]))
    out.append(f"  モデル別:{models}")
    table_rows = []
    for name, (count, weighted, cache_read, all_input, side) in sorted(by_project.items(), key=lambda x: -x[1][1])[
        :top
    ]:
        hit = cache_read / all_input if all_input else 0
        table_rows.append(
            [
                name,
                f"{weighted / total:.1%}",
                fmt_tokens(weighted),
                str(int(count)),
                f"{hit:.0%}",
                f"{side / count:.0%}" if count else "-",
            ]
        )
    out.append(table(["プロジェクト", "割合", "入力換算", "リクエスト", "キャッシュ", "サブエージェント"], table_rows))
    rejects = conn.execute(
        "SELECT rate_limit_type, COUNT(*) FROM claude_rejects WHERE ts >= ? GROUP BY rate_limit_type", (since,)
    ).fetchall()
    if rejects:
        out.append("  制限で断られた回数:" + "、".join(f"{t or '不明'} {n} 回" for t, n in rejects))
    return "\n".join(out)


def action_section(conn: sqlite3.Connection, since: str, top: int) -> str:
    out = ["## Claude(Claude Code Action)"]
    rows = conn.execute(
        f"""SELECT repository, workflow, COUNT(*), COUNT(DISTINCT run_id), SUM({ACTION_WEIGHTED}), SUM(cost_usd)
            FROM action_requests WHERE ts >= ? GROUP BY repository, workflow
            ORDER BY SUM({ACTION_WEIGHTED}) DESC""",
        (since,),
    ).fetchall()
    if not rows:
        out.append("  (この期間の記録なし。artifact claude-usage-* を残すワークフローが動くと増える)")
        return "\n".join(out)
    action_total = sum(r[4] or 0 for r in rows)
    local_total = (
        conn.execute(f"SELECT SUM({CLAUDE_WEIGHTED}) FROM claude_requests WHERE ts >= ?", (since,)).fetchone()[0] or 0
    )
    share = action_total / (action_total + local_total) if action_total + local_total else 0
    out.append(
        f"  入力換算 {fmt_tokens(action_total)} トークン(手元と合わせた Claude 全体の {share:.1%})、"
        f"API 料金での換算 ${sum(r[5] or 0 for r in rows):.2f}"
    )
    out.append(
        table(
            ["リポジトリ", "ワークフロー", "実行", "リクエスト", "入力換算", "換算額"],
            [
                [r[0] or "-", r[1] or "-", str(r[3]), str(r[2]), fmt_tokens(r[4] or 0), f"${r[5] or 0:.2f}"]
                for r in rows[:top]
            ],
        )
    )
    return "\n".join(out)


def codex_section(conn: sqlite3.Connection, since: str, top: int) -> str:
    out = ["## Codex"]
    rows = conn.execute(
        f"""SELECT cwd, COUNT(*), SUM({CODEX_WEIGHTED}), SUM(cached_input), SUM(input)
            FROM codex_requests WHERE ts >= ? GROUP BY cwd""",
        (since,),
    ).fetchall()
    by_project: dict[str, list[float]] = defaultdict(lambda: [0, 0, 0, 0])
    for cwd, count, weighted, cached, total_input in rows:
        p = by_project[project_name(cwd)]
        p[0] += count
        p[1] += weighted or 0
        p[2] += cached or 0
        p[3] += total_input or 0
    total = sum(p[1] for p in by_project.values())
    if not total:
        out.append("  (この期間の記録なし)")
        return "\n".join(out)
    out.append(f"  入力換算 {fmt_tokens(total)} トークン、{sum(int(p[0]) for p in by_project.values())} リクエスト")
    out.append(
        table(
            ["プロジェクト", "割合", "入力換算", "リクエスト", "キャッシュ"],
            [
                [n, f"{p[1] / total:.1%}", fmt_tokens(p[1]), str(int(p[0])), f"{p[2] / p[3]:.0%}" if p[3] else "-"]
                for n, p in sorted(by_project.items(), key=lambda x: -x[1][1])[:top]
            ],
        )
    )
    return "\n".join(out)


def github_section(conn: sqlite3.Connection, now: datetime, top: int) -> str:
    month = now.strftime("%Y-%m")
    out = [f"## GitHub({month})"]
    rows = conn.execute(
        """SELECT product, sku, unit, SUM(quantity), SUM(gross_amount), SUM(discount_amount), SUM(net_amount)
           FROM github_usage WHERE substr(date, 1, 7) = ? GROUP BY product, sku ORDER BY SUM(gross_amount) DESC""",
        (month,),
    ).fetchall()
    if not rows:
        out.append("  (記録なし。`subscope collect` は gh のトークンに user スコープが要る)")
        return "\n".join(out)
    out.append(
        table(
            ["製品", "SKU", "使った量", "単位", "無料枠で引いた額", "請求額"],
            [[p, s, f"{q:.1f}", u or "", f"${d or 0:.2f}", f"${n or 0:.2f}"] for p, s, u, q, g, d, n in rows],
        )
    )
    repos = conn.execute(
        """SELECT repository, SUM(quantity) FROM github_usage
           WHERE substr(date, 1, 7) = ? AND lower(product) = 'actions' AND unit IN ('Minutes', 'minutes')
           GROUP BY repository ORDER BY SUM(quantity) DESC LIMIT ?""",
        (month, top),
    ).fetchall()
    if repos:
        out.append("  Actions の分が多いリポジトリ(公開リポジトリの分も含む):")
        out.append(table(["リポジトリ", "分"], [[r or "-", f"{q:.0f}"] for r, q in repos]))
    return "\n".join(out)


def build(conn: sqlite3.Connection, days: int = 30, top: int = 10, now: datetime | None = None) -> str:
    now = now or datetime.now(UTC)
    since = (now - timedelta(days=days)).isoformat()
    sections = [
        f"# subscope:直近 {days} 日",
        quota_section(conn, now),
        claude_section(conn, since, top),
        action_section(conn, since, top),
        codex_section(conn, since, top),
        github_section(conn, now, top),
    ]
    return "\n\n".join(sections) + "\n"
