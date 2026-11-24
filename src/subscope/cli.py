"""subscope のコマンド。"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import __version__, db, gh, report, statusline
from .sources import actions, claude, codex, github

SOURCES = ("claude", "codex", "actions", "github")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="subscope",
        description="開発系サブスク(Claude・Codex・GitHub)のクオータをどれだけ使えているかを測る",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--db", type=Path, help="データベースの場所(既定は $XDG_DATA_HOME/subscope/subscope.db)")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("collect", help="ログと GitHub から使用量を集める")
    p.add_argument("--only", choices=SOURCES, action="append", help="この出どころだけ集める(何度でも)")
    p.add_argument("--github-months", type=int, default=2, help="GitHub の課金を何か月分読むか(既定 2)")

    p = sub.add_parser("report", help="集めた使用量を報告する")
    p.add_argument("--days", type=int, default=30, help="何日分を見るか(既定 30)")
    p.add_argument("--top", type=int, default=10, help="表に出す行の数(既定 10)")
    p.add_argument("--collect", action="store_true", help="報告の前に集める")

    sub.add_parser(
        "record-statusline",
        help="Claude Code のステータスラインの入力(標準入力の JSON)から、クオータの消費率を記録する",
    )

    args = parser.parse_args(argv)
    if args.command == "record-statusline":
        # Never break the status line: no output, and exit 0 whatever happens.
        try:
            with db.connect(args.db) as conn:
                statusline.record(conn, sys.stdin.read())
        except Exception:  # noqa: BLE001, S110
            pass
        return 0

    conn = db.connect(args.db)
    try:
        if args.command == "collect":
            return collect(conn, args.only or list(SOURCES), args.github_months)
        if args.command == "report":
            if args.collect:
                collect(conn, list(SOURCES), 2, quiet=True)
            sys.stdout.write(report.build(conn, days=args.days, top=args.top))
            return 0
    finally:
        conn.close()
    return 2


def collect(conn, sources: list[str], github_months: int, quiet: bool = False) -> int:
    status = 0

    def say(text: str) -> None:
        if not quiet:
            print(text)

    if "claude" in sources:
        s = claude.ingest(conn)
        say(f"claude: ファイル {s['files']}、リクエスト +{s['requests']}、制限で断られた記録 +{s['rejects']}")
    if "codex" in sources:
        s = codex.ingest(conn)
        say(f"codex: ファイル {s['files']}、リクエスト +{s['requests']}、消費率の観測 +{s['quota_samples']}")
    remote = [s for s in ("actions", "github") if s in sources]
    if remote and not gh.available():
        print("gh が見つからないので、actions と github は飛ばす", file=sys.stderr)
        return 1
    user = None
    if remote:
        try:
            user = gh.login()
        except gh.GhError as e:
            print(f"gh で GitHub を読めない:{e}", file=sys.stderr)
            return 1
    if "actions" in sources:
        try:
            s = actions.ingest(conn, user)
            say(f"actions: リポジトリ {s['repositories']}、artifact +{s['artifacts']}、リクエスト +{s['requests']}")
        except gh.GhError as e:
            print(f"actions を読めなかった:{e}", file=sys.stderr)
            status = 1
    if "github" in sources:
        try:
            months = github.recent_months(datetime.now(UTC).date(), github_months)
            s = github.ingest(conn, user, months)
            say(f"github: {s['months']} か月、明細 {s['items']}")
        except gh.GhError as e:
            hint = "(gh auth refresh -h github.com -s user で user スコープを足す)" if "user" in str(e) else ""
            print(f"github の課金を読めなかった:{e}{hint}", file=sys.stderr)
            status = 1
    return status
