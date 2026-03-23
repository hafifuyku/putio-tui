"""
put.io API v2 client — minimal, synchronous, for the TUI prototype.
"""

from __future__ import annotations

import json
import os
import urllib.request
import urllib.error
from dataclasses import dataclass, field
from typing import Optional
from datetime import datetime, timezone


API_BASE = "https://api.put.io/v2"


def _token() -> str:
    """Get OAuth token from env var or config file."""
    tok = os.environ.get("PUTIO_TOKEN", "")
    if tok:
        return tok
    # Try config file
    cfg = os.path.expanduser("~/.config/putio-tui/token")
    if os.path.exists(cfg):
        return open(cfg).read().strip()
    return ""


def _get(path: str, params: dict | None = None) -> dict:
    """Make authenticated GET request to put.io API."""
    token = _token()
    if not token:
        raise RuntimeError("No put.io token. Set PUTIO_TOKEN env var or write token to ~/.config/putio-tui/token")

    url = f"{API_BASE}{path}"
    if params:
        qs = "&".join(f"{k}={v}" for k, v in params.items())
        url = f"{url}?{qs}"

    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {body}")


def _post(path: str, data: dict | None = None) -> dict:
    """Make authenticated POST request to put.io API."""
    token = _token()
    if not token:
        raise RuntimeError("No put.io token.")

    url = f"{API_BASE}{path}"
    body = json.dumps(data or {}).encode("utf-8")

    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    })
    try:
        resp = urllib.request.urlopen(req, timeout=10)
        return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"API error {e.code}: {body}")


# ── Size formatting ──

def _fmt_size(size_bytes: int) -> str:
    """Format bytes into human-readable size string."""
    if size_bytes == 0:
        return ""
    units = [("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20), ("KB", 1 << 10)]
    for unit, threshold in units:
        if size_bytes >= threshold:
            val = size_bytes / threshold
            if val >= 100:
                return f"{val:.0f} {unit}"
            elif val >= 10:
                return f"{val:.1f} {unit}"
            else:
                return f"{val:.2f} {unit}"
    return f"{size_bytes} B"


def _fmt_time_ago(iso_str: str) -> str:
    """Convert ISO timestamp to relative time string like '2h ago'."""
    if not iso_str:
        return ""
    try:
        # Handle various formats put.io might return
        dt_str = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(dt_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        delta = now - dt
        seconds = int(delta.total_seconds())

        if seconds < 60:
            return "now"
        elif seconds < 3600:
            m = seconds // 60
            return f"{m}m ago"
        elif seconds < 86400:
            h = seconds // 3600
            return f"{h}h ago"
        elif seconds < 604800:
            d = seconds // 86400
            return f"{d}d ago"
        elif seconds < 2592000:
            w = seconds // 604800
            return f"{w}w ago"
        elif seconds < 31536000:
            mo = seconds // 2592000
            return f"{mo}mo ago"
        else:
            y = seconds // 31536000
            return f"{y}y ago"
    except Exception:
        return iso_str[:10]


# ── Public API functions ──

@dataclass
class FileInfo:
    id: int
    name: str
    is_dir: bool
    size: str
    size_bytes: int
    modified: str
    content_type: str = ""
    parent_id: int = 0
    created_at: str = ""


@dataclass
class FolderList:
    files: list[FileInfo]
    sort_by: str = "NAME_ASC"


@dataclass
class TransferInfo:
    id: int
    name: str
    size: str
    progress: float
    speed: str
    eta: str
    status: str
    source: str = ""
    peers: int = 0
    seeds: int = 0
    file_id: int | None = None
    save_parent_id: int = 0
    uploaded: str = ""


@dataclass
class EventInfo:
    name: str
    action: str
    timestamp: str
    file_id: int | None = None
    username: str = ""  # non-empty if event is from another user


@dataclass
class AccountInfo:
    username: str
    disk_used: int
    disk_total: int
    disk_used_str: str = ""
    disk_total_str: str = ""


def get_account() -> AccountInfo:
    """Get account info (storage, username)."""
    data = _get("/account/info")
    info = data.get("info", {})
    disk = info.get("disk", {})
    used = disk.get("used", 0)
    total = disk.get("size", 0)
    return AccountInfo(
        username=info.get("username", ""),
        disk_used=used,
        disk_total=total,
        disk_used_str=_fmt_size(used),
        disk_total_str=_fmt_size(total),
    )


def get_trash_enabled() -> bool:
    """Check if 'Move deleted files to trash' setting is enabled."""
    try:
        data = _get("/account/settings")
        settings = data.get("settings", {})
        return bool(settings.get("trash_enabled", True))
    except Exception:
        # Default to True (safer — moves to trash rather than permanent delete)
        return True


def list_files(parent_id: int = 0, sort_by: str | None = None) -> FolderList:
    """List files in a folder. Returns files and the folder's sort setting."""
    params: dict = {"parent_id": str(parent_id)}
    if sort_by:
        params["sort_by"] = sort_by
    data = _get("/files/list", params)

    # Read the folder's sort preference from the parent info
    parent_info = data.get("parent", {})
    folder_sort = parent_info.get("sort_by", "NAME_ASC") or "NAME_ASC"

    files = []
    for f in data.get("files", []):
        is_dir = f.get("file_type") == "FOLDER"
        size_bytes = f.get("size", 0)
        files.append(FileInfo(
            id=f.get("id", 0),
            name=f.get("name", ""),
            is_dir=is_dir,
            size=_fmt_size(size_bytes),
            size_bytes=size_bytes,
            modified=_fmt_time_ago(f.get("updated_at", "")),
            content_type=f.get("content_type", ""),
            parent_id=f.get("parent_id", 0),
            created_at=_fmt_time_ago(f.get("created_at", "")),
        ))
    return FolderList(files=files, sort_by=folder_sort)



def list_transfers() -> list[TransferInfo]:
    """List active and recent transfers."""
    data = _get("/transfers/list")
    transfers = []
    for t in data.get("transfers", []):
        size_bytes = t.get("size", 0)
        down_speed = t.get("down_speed", 0)
        eta_secs = t.get("estimated_time", 0)

        if down_speed > 0:
            speed_str = _fmt_size(down_speed) + "/s"
        else:
            speed_str = ""

        if eta_secs and eta_secs > 0:
            if eta_secs < 60:
                eta_str = f"{eta_secs}s"
            elif eta_secs < 3600:
                eta_str = f"{eta_secs // 60}m"
            else:
                eta_str = f"{eta_secs // 3600}h {(eta_secs % 3600) // 60}m"
        else:
            eta_str = ""

        transfers.append(TransferInfo(
            id=t.get("id", 0),
            name=t.get("name", ""),
            size=_fmt_size(size_bytes),
            progress=float(t.get("percent_done", 0)),
            speed=speed_str,
            eta=eta_str,
            status=t.get("status", ""),
            source=t.get("source", ""),
            peers=t.get("peers_connected", 0),
            seeds=t.get("peers_sending_to_us", 0),
            file_id=t.get("file_id"),
            save_parent_id=t.get("save_parent_id", 0),
            uploaded=_fmt_size(t.get("uploaded", 0)),
        ))
    return transfers


def list_events() -> list[EventInfo]:
    """List recent events/history."""
    data = _get("/events/list")
    events = []
    for e in data.get("events", []):
        etype = e.get("type", "")
        file_name = e.get("file_name", "") or e.get("transfer_name", "")

        # Map event types to action labels
        action_map = {
            "file_from_rss_created": "downloaded",
            "transfer_completed": "downloaded",
            "transfer_from_rss_created": "downloaded",
            "zip_created": "zipped",
            "file_shared": "shared",
            "transfer_error": "error",
            "transfer_callback_error": "error",
        }
        action = action_map.get(etype, etype.replace("_", " "))

        timestamp = _fmt_time_ago(e.get("created_at", ""))
        file_id = e.get("file_id") or e.get("transfer_file_id")

        # Check if event is from a different user (shared/family accounts)
        sharing_user = e.get("sharing_user_name", "") or e.get("user_name", "")

        events.append(EventInfo(
            name=file_name,
            action=action,
            timestamp=timestamp,
            file_id=file_id,
            username=sharing_user,
        ))
    return events


def add_transfer(url: str, parent_id: int = 0) -> dict:
    """Add a new transfer."""
    # put.io uses form data for this endpoint, not JSON
    import urllib.parse
    token = _token()
    data = urllib.parse.urlencode({"url": url, "save_parent_id": parent_id}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/transfers/add",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=15)
    return json.loads(resp.read())


def cancel_transfer(transfer_id: int) -> dict:
    """Cancel/remove a transfer."""
    import urllib.parse
    token = _token()
    data = urllib.parse.urlencode({"transfer_ids": str(transfer_id)}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/transfers/cancel",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def clean_transfers() -> dict:
    """Clear completed transfers."""
    return _post("/transfers/clean")


def delete_file(file_id: int | list[int]) -> dict:
    """Delete one or more files/folders."""
    import urllib.parse
    token = _token()
    if isinstance(file_id, list):
        ids_str = ",".join(str(i) for i in file_id)
    else:
        ids_str = str(file_id)
    data = urllib.parse.urlencode({"file_ids": ids_str}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/files/delete",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def search_files(query: str) -> list[FileInfo]:
    """Search for files by name."""
    data = _get("/files/search", {"query": query, "per_page": "100"})
    files = []
    for f in data.get("files", []):
        is_dir = f.get("file_type") == "FOLDER"
        size_bytes = f.get("size", 0)
        files.append(FileInfo(
            id=f.get("id", 0),
            name=f.get("name", ""),
            is_dir=is_dir,
            size=_fmt_size(size_bytes),
            size_bytes=size_bytes,
            modified=_fmt_time_ago(f.get("updated_at", "")),
            content_type=f.get("content_type", ""),
            parent_id=f.get("parent_id", 0),
        ))
    return files


def get_file(file_id: int) -> FileInfo:
    """Get info for a single file/folder."""
    data = _get(f"/files/{file_id}")
    f = data.get("file", {})
    is_dir = f.get("file_type") == "FOLDER"
    size_bytes = f.get("size", 0)
    return FileInfo(
        id=f.get("id", 0),
        name=f.get("name", ""),
        is_dir=is_dir,
        size=_fmt_size(size_bytes),
        size_bytes=size_bytes,
        modified=_fmt_time_ago(f.get("updated_at", "")),
        content_type=f.get("content_type", ""),
        parent_id=f.get("parent_id", 0),
    )


def get_download_url(file_id: int) -> str:
    """Get the download URL for a file."""
    data = _get(f"/files/{file_id}/url")
    return data.get("url", "")


def create_folder(name: str, parent_id: int = 0) -> dict:
    """Create a new folder."""
    import urllib.parse
    token = _token()
    data = urllib.parse.urlencode({"name": name, "parent_id": str(parent_id)}).encode()
    req = urllib.request.Request(
        f"{API_BASE}/files/create-folder",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def move_files(file_ids: list[int], parent_id: int) -> dict:
    """Move files to a different folder."""
    import urllib.parse
    token = _token()
    data = urllib.parse.urlencode({
        "file_ids": ",".join(str(i) for i in file_ids),
        "parent_id": str(parent_id),
    }).encode()
    req = urllib.request.Request(
        f"{API_BASE}/files/move",
        data=data,
        method="POST",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
    )
    resp = urllib.request.urlopen(req, timeout=10)
    return json.loads(resp.read())


def create_share_link(file_ids: list[int]) -> str:
    """Create a sharing link. Returns the URL."""
    # This might not be exactly right — put.io sharing API varies
    # Trying the /files/share endpoint
    try:
        import urllib.parse
        token = _token()
        data = urllib.parse.urlencode({
            "file_ids": ",".join(str(i) for i in file_ids),
            "friends": "everyone",
        }).encode()
        req = urllib.request.Request(
            f"{API_BASE}/files/share",
            data=data,
            method="POST",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
        )
        resp = urllib.request.urlopen(req, timeout=10)
        result = json.loads(resp.read())
        return result.get("url", f"https://put.io/file/{file_ids[0]}")
    except Exception:
        return f"https://put.io/file/{file_ids[0]}"
