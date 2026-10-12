"""
Keep the data files (products.csv, sup_codes.json, stock.json) in Supabase — table app_files,
one row per file with the same text the file holds (create it with supabase/schema.sql).

Talks to the Supabase Data API (PostgREST) with the project's secret key; no extra package needed.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request

TABLE = "app_files"
READY_ROW = "_ready"     # inserted by schema.sql


class NotReady(RuntimeError):
    """Supabase is set in secrets but cannot be used yet (schema.sql not run, or not the secret key)."""


def base_url(sb: dict) -> str:
    """'https://xxx.supabase.co', also when the secret was pasted with /rest/v1/ at the end."""
    url = sb["url"].strip().rstrip("/")
    for tail in ("/rest/v1", "/rest"):
        if url.endswith(tail):
            url = url[: -len(tail)]
    return url


def _headers(sb: dict) -> dict:
    key = sb["key"].strip()
    h = {"apikey": key, "Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "po-summary-app"}
    if not key.startswith("sb_"):            # legacy service_role key (a JWT) also goes as Bearer
        h["Authorization"] = f"Bearer {key}"
    return h


def _api(sb: dict, method: str, query: str, body=None, prefer: str = ""):
    url = f"{base_url(sb)}/rest/v1/{TABLE}?{query}"
    headers = _headers(sb)
    if prefer:
        headers["Prefer"] = prefer
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        text = e.read().decode(errors="ignore")
        if e.code == 404 or "PGRST205" in text or "42P01" in text:
            raise NotReady("ยังไม่มีตาราง app_files ใน Supabase — รันไฟล์ supabase/schema.sql ใน SQL Editor ก่อน") from e
        if e.code in (401, 403):
            raise NotReady("Supabase ไม่ยอมรับ key — ใน Secrets ต้องใส่ secret key (sb_secret_… หรือ service_role) "
                           f"ไม่ใช่ publishable/anon key ({e.code})") from e
        raise RuntimeError(f"Supabase {method} → {e.code}: {text[:200]}") from e


def check(sb: dict) -> None:
    """Raise NotReady if the table or the marker row cannot be read with this key."""
    rows = _api(sb, "GET", f"select=name&name=eq.{READY_ROW}")
    if not rows:
        # RLS hides every row from the publishable/anon key, so the marker looks missing
        raise NotReady("อ่านตาราง app_files ใน Supabase ไม่เห็นข้อมูล — ตรวจว่ารัน supabase/schema.sql แล้ว "
                       "และ key ใน Secrets เป็น secret key (ไม่ใช่ publishable/anon key)")


def fetch(sb: dict, name: str) -> bytes | None:
    rows = _api(sb, "GET", f"select=content&name=eq.{urllib.parse.quote(name)}")
    return rows[0]["content"].encode("utf-8") if rows else None


def push(sb: dict, name: str, content: str, message: str) -> None:
    """Insert or replace one file (the old version goes to app_file_history)."""
    _api(sb, "POST", "on_conflict=name", {"name": name, "content": content, "message": message},
         prefer="resolution=merge-duplicates,return=minimal")


def insert_if_missing(sb: dict, name: str, content: str, message: str) -> None:
    """Add a file only if Supabase does not have it yet (never replaces a newer row)."""
    _api(sb, "POST", "on_conflict=name", {"name": name, "content": content, "message": message},
         prefer="resolution=ignore-duplicates,return=minimal")
