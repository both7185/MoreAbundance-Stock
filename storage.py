"""
Where the website keeps its data files (products.csv, sup_codes.json, stock.json).

    [supabase] in secrets  -> Supabase table app_files (supabase_store.py) is the real copy.
                              While [github] is also set, every save is copied to the GitHub data branch
                              as a backup, and a file Supabase does not have yet is imported from the
                              data branch once (reading only — the data branch is never overwritten by it).
    only [github]          -> the GitHub data branch (datastore.py), as before.
    neither (run.bat)      -> the local files are the data; nothing here is used.

If [supabase] is set but not usable yet (supabase/schema.sql not run, or not the secret key), the app
keeps working on GitHub alone and says why, so nothing is written to Supabase until it is ready.
"""
from __future__ import annotations

import time
from pathlib import Path

import datastore
import supabase_store
from products import github_config

DATA_FILES = datastore.DATA_FILES
APP_DIR = Path(__file__).parent
_READY_TTL = (600, 60)                        # seconds to trust a check: (ready, not ready)
_ready: dict[str, tuple[str, float]] = {}     # supabase url -> (why not ready, '' = ready; when checked)


def supabase_config(secrets) -> dict | None:
    """Expects in the Cloud 'Secrets' box:
        [supabase]
        url = "https://xxxx.supabase.co"
        key = "sb_secret_..."          # secret key (or the legacy service_role key)
        github_backup = true           # optional: also copy every save to the GitHub data branch
    """
    try:
        sb = dict(secrets["supabase"])
    except Exception:
        return None
    if not str(sb.get("url", "")).strip() or not str(sb.get("key", "")).strip():
        return None
    return sb


def config(secrets) -> dict | None:
    """{'gh': ..., 'sb': ...} or None when neither is set (local run)."""
    gh, sb = github_config(secrets), supabase_config(secrets)
    if not gh and not sb:
        return None
    return {"gh": gh, "sb": sb}


def cache_key(cfg: dict | None) -> str:
    if not cfg:
        return ""
    gh, sb = cfg["gh"], cfg["sb"]
    return "|".join([sb["url"] if sb else "", gh["repo"] if gh else "", datastore.data_branch(gh) if gh else "",
                     gh["path"] if gh else ""])


def not_ready_reason(cfg: dict) -> str:
    """'' when Supabase can be used, else why not (also when Supabase is not in secrets)."""
    sb = cfg["sb"]
    if not sb:
        return "ไม่ได้ตั้งค่า Supabase"
    url = supabase_store.base_url(sb)
    reason, when = _ready.get(url, ("?", 0.0))
    if reason != "?" and time.time() - when < _READY_TTL[0 if reason == "" else 1]:
        return reason
    try:
        supabase_store.check(sb)
        reason = ""
    except supabase_store.NotReady as e:
        reason = str(e)
    # anything else (network down...) is raised: better to fail the save than write to GitHub only
    _ready[url] = (reason, time.time())
    return reason


def _active_sb(cfg: dict) -> dict | None:
    return cfg["sb"] if cfg["sb"] and not not_ready_reason(cfg) else None


def backup_gh(cfg: dict) -> dict | None:
    sb = cfg["sb"] or {}
    return cfg["gh"] if str(sb.get("github_backup", True)).lower() not in ("false", "0", "no") else None


def where(cfg: dict | None) -> str:
    """Name of the permanent store for messages: 'Supabase' / 'GitHub'."""
    if not cfg:
        return ""
    try:
        return "Supabase" if _active_sb(cfg) else "GitHub"
    except Exception:
        return "Supabase"


def _from_supabase(cfg: dict, sb: dict, name: str, notes: list[str]) -> bytes | None:
    content = supabase_store.fetch(sb, name)
    if content is None and cfg["gh"]:
        old = datastore.fetch(cfg["gh"], name)       # read only
        if old is not None:
            supabase_store.insert_if_missing(sb, name, old.decode("utf-8"),
                                             "นำเข้าจาก GitHub data branch")
            notes.append(f"นำเข้า {name} จาก GitHub มาไว้ใน Supabase แล้ว")
            content = supabase_store.fetch(sb, name) or old
    return content


def pull(cfg: dict) -> tuple[list[str], list[str]]:
    """Copy the data files into the app folder. Returns (files changed locally, notes for the user)."""
    sb = _active_sb(cfg)
    if not sb:
        notes = [f"ยังใช้ GitHub เก็บข้อมูล: {not_ready_reason(cfg)}"] if cfg["sb"] else []
        return (datastore.pull(cfg["gh"]) if cfg["gh"] else []), notes
    changed, notes = [], []
    for name in DATA_FILES:
        content = _from_supabase(cfg, sb, name, notes)
        if content is None:
            continue
        local = APP_DIR / name
        if not local.exists() or local.read_bytes() != content:
            local.write_bytes(content)
            changed.append(name)
    return changed, notes


def fetch(cfg: dict, name: str) -> bytes | None:
    """Latest version of one file, read just before changing it. None if no copy exists yet."""
    sb = _active_sb(cfg)
    if sb:
        return _from_supabase(cfg, sb, name, [])
    return datastore.fetch(cfg["gh"], name) if cfg["gh"] else None


def push(cfg: dict, name: str, content: str, message: str) -> str:
    """
    Save one file permanently. Raises if the main store fails.
    Returns '' or a warning when only the GitHub backup copy failed (the save itself is done).
    """
    sb = _active_sb(cfg)
    if not sb:
        datastore.push(cfg["gh"], name, content, message)
        return ""
    supabase_store.push(sb, name, content, message)
    gh = backup_gh(cfg)
    if gh:
        try:
            datastore.push(gh, name, content, message)
        except Exception as e:
            return f"บันทึกลง Supabase แล้ว แต่สำรองลง GitHub ไม่สำเร็จ ({str(e)[:150]})"
    return ""


def compare(cfg: dict) -> list[dict]:
    """Each file in Supabase vs the GitHub data branch, for checking the move."""
    rows = []
    for name in DATA_FILES:
        a = supabase_store.fetch(cfg["sb"], name)
        b = datastore.fetch(cfg["gh"], name)
        rows.append({"ไฟล์": name,
                     "Supabase": _describe(name, a),
                     "GitHub": _describe(name, b),
                     "ผล": "✅ ตรงกัน" if a == b else "❌ ไม่ตรงกัน"})
    return rows


def _describe(name: str, content: bytes | None) -> str:
    if content is None:
        return "— ไม่มี —"
    text = content.decode("utf-8", errors="ignore")
    try:
        if name == "stock.json":
            import stock
            d = stock.parse(text)
            return f"สินค้า {len(d['items'])} • ประวัติ {len(d['movements'])}"
        if name == "products.csv":
            import csv, io
            return f"{len(list(csv.DictReader(io.StringIO(text))))} แถว"
        if name == "sup_codes.json":
            import json
            return f"ร้าน {len(json.loads(text).get('shops', []))}"
    except Exception:
        pass
    return f"{len(content):,} bytes"
