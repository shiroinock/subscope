"""gh を呼ぶ。トークンは gh が持っているものを使い、subscope は読まない。"""

from __future__ import annotations

import json
import shutil
import subprocess


class GhError(RuntimeError):
    pass


def available() -> bool:
    return shutil.which("gh") is not None


def api(path: str, *, paginate: bool = False) -> object:
    """gh api の JSON を返す。paginate のときは配列ごとに 1 行の JSON にして、つないで返す。"""
    args = ["gh", "api", path]
    if paginate:
        args += ["--paginate", "--slurp"]
    out = run(args)
    return json.loads(out) if out.strip() else None


def api_bytes(path: str) -> bytes:
    proc = subprocess.run(["gh", "api", path], capture_output=True, check=False)
    if proc.returncode != 0:
        raise GhError(proc.stderr.decode(errors="replace").strip() or f"gh api {path} が失敗した")
    return proc.stdout


def run(args: list[str]) -> str:
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise GhError(proc.stderr.strip() or f"{' '.join(args)} が失敗した")
    return proc.stdout


def login() -> str:
    data = api("user")
    if not isinstance(data, dict) or not data.get("login"):
        raise GhError("gh api user から login を読めなかった")
    return data["login"]
