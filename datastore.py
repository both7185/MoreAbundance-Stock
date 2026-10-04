"""
Keep the editable data (products.csv, sup_codes.json) on a separate GitHub branch ("data").

Why: on Streamlit Community Cloud the disk is reset on every deploy, and the code lives on `main`.
If the data lived on `main` too, pushing code (with an old products.csv from your PC) would
overwrite what was added on the website. With a separate branch:

    main  -> code (+ the first version of the data files, used only to start the data branch)
    data  -> products.csv, sup_codes.json  (written only by the app)

* pull(): download the data files from the `data` branch into the app folder, so the rest of the
  app keeps reading plain local files. Creates the branch from `main` on the very first run.
* push(): commit one data file to the `data` branch (the branch is created from `main` if missing).
  Commits to `data` do not redeploy the app.
* Without GitHub secrets (local run.bat) nothing here is used: the local files are the data.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.request
from pathlib import Path

DATA_FILES = ["products.csv", "sup_codes.json"]
APP_DIR = Path(__file__).parent
PROTECT_SECONDS = 300
_recent_push: dict[str, tuple[bytes, float]] = {}   # file -> (content we committed, when)


def _headers(gh: dict) -> dict:
    return {
        "Authorization": f"Bearer {gh['token']}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "po-summary-app",
    }


def _api(gh: dict, method: str, path: str, body: dict | None = None) -> dict | None:
    """Call the GitHub API. Returns parsed JSON, or None on 404."""
    base = gh.get("api_url", "https://api.github.com").rstrip("/")   # api_url: only for tests / GitHub Enterprise
    url = f"{base}/repos/{gh['repo']}/{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=_headers(gh), method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise RuntimeError(f"GitHub {method} {path} → {e.code}: {e.read().decode(errors='ignore')[:200]}") from e


def data_branch(gh: dict) -> str:
    return gh.get("data_branch") or "data"


def remote_path(gh: dict, filename: str) -> str:
    """Same folder as secrets 'path' (products.csv), e.g. 'app/products.csv' -> 'app/sup_codes.json'."""
    folder = gh["path"].rsplit("/", 1)[0] if "/" in gh.get("path", "") else ""
    return f"{folder}/{filename}" if folder else filename


def ensure_branch(gh: dict) -> None:
    """Create the data branch from the code branch if it does not exist yet."""
    branch = data_branch(gh)
    if _api(gh, "GET", f"git/ref/heads/{branch}") is not None:
        return
    base = _api(gh, "GET", f"git/ref/heads/{gh.get('branch', 'main')}")
    if base is None:
        raise RuntimeError(f"ไม่พบ branch '{gh.get('branch', 'main')}' ใน {gh['repo']}")
    _api(gh, "POST", "git/refs", {"ref": f"refs/heads/{branch}", "sha": base["object"]["sha"]})


def pull(gh: dict) -> list[str]:
    """
    Copy the data files from the data branch into the app folder.
    Returns the names of files that changed locally. Files missing on the branch are left as they are.
    """
    changed = []
    branch = data_branch(gh)
    ensure_branch(gh)   # first run after deploy: start the data branch from main's current files
    for name in DATA_FILES:
        info = _api(gh, "GET", f"contents/{remote_path(gh, name)}?ref={branch}")
        if not info or info.get("type") != "file":
            continue
        if info.get("content"):
            content = base64.b64decode(info["content"])
        else:  # large files: the API gives a download URL instead of inline content
            req = urllib.request.Request(info["download_url"], headers=_headers(gh))
            with urllib.request.urlopen(req, timeout=30) as resp:
                content = resp.read()
        local = APP_DIR / name
        recent = _recent_push.get(name)
        if recent and content != recent[0] and time.time() - recent[1] < PROTECT_SECONDS \
                and local.exists() and local.read_bytes() == recent[0]:
            continue  # GitHub may briefly return the previous version right after our commit
        if not local.exists() or local.read_bytes() != content:
            local.write_bytes(content)
            changed.append(name)
    return changed


def push(gh: dict, filename: str, content: str, message: str) -> None:
    """Commit one data file to the data branch."""
    ensure_branch(gh)
    branch = data_branch(gh)
    path = remote_path(gh, filename)
    current = _api(gh, "GET", f"contents/{path}?ref={branch}")
    body = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode(),
        "branch": branch,
    }
    if current and current.get("sha"):
        body["sha"] = current["sha"]
    _api(gh, "PUT", f"contents/{path}", body)
    _recent_push[filename] = (content.encode("utf-8"), time.time())
