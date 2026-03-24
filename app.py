"""
putio — A persistent TUI client for put.io
MC-style borders, full terminal coverage, gold selection highlight.
Connects to the real put.io API.
"""

from __future__ import annotations

import os
import sys
import threading
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Static
from textual.screen import ModalScreen
from textual.reactive import reactive
from textual.css.query import NoMatches
from textual.widgets import Button, Input, Label
from textual import events
from rich.text import Text
from rich.style import Style

import api as putio_api


# ═══════════════════════════════════════════════
# Data types (kept for internal rendering)
# ═══════════════════════════════════════════════

class FileType(Enum):
    DIR = "d"
    VIDEO = "v"
    AUDIO = "a"
    FILE = "f"
    IMAGE = "i"


@dataclass
class PutFile:
    name: str
    file_type: FileType
    size: str
    modified: str
    tags: list[str] = field(default_factory=list)
    transfer_pct: Optional[int] = None
    transfer_speed: Optional[str] = None
    file_id: int = 0  # put.io file ID for API calls
    size_bytes: int = 0


@dataclass
class Transfer:
    name: str
    size: str
    progress: float
    speed: str
    eta: str
    source: str
    peers: Optional[int] = None
    seeds: Optional[int] = None
    tags: list[str] = field(default_factory=list)
    transfer_id: int = 0  # put.io transfer ID
    status: str = ""
    uploaded: str = ""


@dataclass
class HistoryEntry:
    name: str
    action: str
    timestamp: str
    file_id: int | None = None
    username: str = ""


# ── Helpers to convert API responses to internal types ──

CONTENT_TYPE_MAP = {
    "video/": FileType.VIDEO,
    "audio/": FileType.AUDIO,
    "image/": FileType.IMAGE,
}


def _api_file_to_putfile(f: putio_api.FileInfo) -> PutFile:
    """Convert API FileInfo to internal PutFile."""
    if f.is_dir:
        ft = FileType.DIR
    else:
        ft = FileType.FILE
        for prefix, file_type in CONTENT_TYPE_MAP.items():
            if f.content_type.startswith(prefix):
                ft = file_type
                break
    return PutFile(
        name=f.name,
        file_type=ft,
        size=f.size,
        modified=f.modified,
        file_id=f.id,
        size_bytes=f.size_bytes,
    )


def _api_transfer_to_transfer(t: putio_api.TransferInfo) -> Transfer:
    """Convert API TransferInfo to internal Transfer."""
    return Transfer(
        name=t.name,
        size=t.size,
        progress=t.progress,
        speed=t.speed,
        eta=t.eta,
        source=t.source,
        peers=t.peers,
        seeds=t.seeds,
        transfer_id=t.id,
        status=t.status,
        uploaded=t.uploaded,
    )


def _api_event_to_history(e: putio_api.EventInfo) -> HistoryEntry:
    """Convert API EventInfo to internal HistoryEntry."""
    return HistoryEntry(
        name=e.name,
        action=e.action,
        timestamp=e.timestamp,
        file_id=e.file_id,
        username=e.username,
    )


# ── Global state (populated from API) ──

_transfers: list[Transfer] = []
_history: list[HistoryEntry] = []
_account: Optional[putio_api.AccountInfo] = None
_loading: bool = True
_error: str = ""

# ═══════════════════════════════════════════════
# Colors
# ═══════════════════════════════════════════════

GOLD = "#FDCE45"
GOLD_DIM = "#9e7c0a"
GOLD_BG = "#3d3209"
TEXT = "#e4e4e4"
TEXT_SEC = "#999999"
TEXT_DIM = "#666666"
TEXT_GHOST = "#444444"
GREEN = "#4ade80"
RED = "#f87171"
PURPLE = "#a78bfa"
PINK = "#f472b6"
BLUE = "#60a5fa"
BG = "#0a0a0a"
BG_RAISED = "#111111"
BORDER_COLOR = "#333333"

TYPE_COLORS = {
    FileType.DIR: GOLD,
    FileType.VIDEO: PURPLE,
    FileType.AUDIO: PINK,
    FileType.FILE: TEXT_DIM,
    FileType.IMAGE: GREEN,
}

ACTION_COLORS = {
    "downloaded": GREEN,
    "deleted": RED,
    "shared": BLUE,
    "renamed": GOLD,
    "zipped": TEXT_SEC,
    "error": RED,
    "transfer completed": GREEN,
    "file from rss created": GREEN,
}

ACTION_SYMBOLS = {
    "downloaded": "↓",
    "deleted": "×",
    "shared": "→",
    "renamed": "~",
    "zipped": "□",
    "error": "!",
    "transfer completed": "↓",
    "file from rss created": "↓",
}


# ═══════════════════════════════════════════════
# Box drawing helpers
# ═══════════════════════════════════════════════

BOX = {
    "tl": "┌", "tr": "┐", "bl": "└", "br": "┘",
    "h": "─", "v": "│", "t_down": "┬", "t_up": "┴",
    "t_right": "├", "t_left": "┤", "cross": "┼",
}


def hline(width: int, left: str = "", right: str = "") -> Text:
    """Draw a horizontal border line."""
    inner = width - len(left) - len(right)
    t = Text()
    t.append(left + BOX["h"] * inner + right, style=Style(color=BORDER_COLOR))
    return t


# ═══════════════════════════════════════════════
# Main panel — renders everything with box borders
# ═══════════════════════════════════════════════

class MainView(Static):
    """Full-screen MC-style bordered view."""

    active_view: reactive[str] = reactive("files")
    cursor: reactive[int] = reactive(0)
    current_path: reactive[str] = reactive("files/~")
    sidebar_focused: reactive[bool] = reactive(False)
    sidebar_cursor: reactive[int] = reactive(0)  # 0=files, 1=transfers, 2=history

    SIDEBAR_VIEWS = ["files", "transfers", "history", "search"]

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._files: list[PutFile] = []
        self._transfer_cursor = 0
        self._history_cursor = 0
        self._files_scroll = 0
        self._transfers_scroll = 0
        self._history_scroll = 0
        self._folder_stack: list[tuple[int, str]] = []  # (parent_id, display_name) for navigation
        self._current_folder_id = 0  # root
        self._marked: set[int] = set()  # indices of marked files
        self._sort_key = "name_asc"
        self._search_results: list[PutFile] = []
        self._search_cursor = 0
        self._search_scroll = 0
        self._search_query = ""

    def render(self) -> Text:
        # Get terminal size from widget
        w = self.size.width
        h = self.size.height
        if w < 20 or h < 8:
            return Text("too small")

        sidebar_w = 18
        main_w = w - sidebar_w - 3  # 3 for border chars (left, mid, right)
        content_h = h - 5  # top border, header, bottom border, action bar, view tabs

        lines: list[Text] = []

        # ── Row 0: Top border with title ──
        top = Text()
        top.append(BOX["tl"], style=Style(color=BORDER_COLOR))
        title = " put.io "
        top.append(BOX["h"], style=Style(color=BORDER_COLOR))
        top.append(title, style=Style(color=GOLD, bold=True))
        remaining_top = w - 2 - 1 - len(title)
        # Storage + transfers in top bar
        acct = _account
        if acct:
            storage_str = f"{acct.disk_used_str}/{acct.disk_total_str}"
        else:
            storage_str = "..."
        active_transfers = len([t for t in _transfers if t.progress < 100])
        right_info = f" {storage_str}  ↓ {active_transfers} active "
        padding = remaining_top - len(right_info) - 1
        top.append(BOX["h"] * max(0, padding), style=Style(color=BORDER_COLOR))
        top.append(f" {storage_str}", style=Style(color=TEXT_DIM))
        top.append("  ↓ ", style=Style(color=TEXT_GHOST))
        top.append(str(active_transfers), style=Style(color=GOLD))
        top.append(" active ", style=Style(color=TEXT_GHOST))
        top.append(BOX["tr"], style=Style(color=BORDER_COLOR))
        lines.append(top)

        # ── Row 1: Header row (sidebar label + path/view title) ──
        header = Text()
        header.append(BOX["v"], style=Style(color=BORDER_COLOR))

        # Sidebar header area
        sh = "  navigate"
        sh_padded = sh + " " * max(0, sidebar_w - len(sh))
        header.append(sh_padded, style=Style(color=TEXT_GHOST))

        header.append(BOX["v"], style=Style(color=BORDER_COLOR))

        # Main panel header
        if self.active_view == "files":
            path_str = f" {self.current_path}"
            item_count = f"{len(self._files)} items"
        elif self.active_view == "transfers":
            path_str = " transfers"
            total_label = f"{len(_transfers)} transfers"
            item_count = total_label
        elif self.active_view == "history":
            path_str = " history"
            item_count = f"{len(_history)} entries"
        else:
            path_str = f' search: "{self._search_query}"' if self._search_query else " search"
            item_count = f"{len(self._search_results)} results"

        main_header = path_str
        right_label = item_count
        gap = max(1, main_w - len(main_header) - len(right_label))
        header.append(main_header, style=Style(color=TEXT_SEC))
        header.append(" " * gap)
        header.append(right_label, style=Style(color=TEXT_GHOST))

        header.append(BOX["v"], style=Style(color=BORDER_COLOR))
        lines.append(header)

        # ── Row 2: Separator ──
        sep = Text()
        sep.append(BOX["t_right"], style=Style(color=BORDER_COLOR))
        sep.append(BOX["h"] * sidebar_w, style=Style(color=BORDER_COLOR))
        sep.append(BOX["cross"], style=Style(color=BORDER_COLOR))
        sep.append(BOX["h"] * main_w, style=Style(color=BORDER_COLOR))
        sep.append(BOX["t_left"], style=Style(color=BORDER_COLOR))
        lines.append(sep)

        # ── Content rows ──
        sidebar_lines = self._render_sidebar(sidebar_w, content_h)
        main_lines = self._render_main_panel(main_w, content_h)

        for i in range(content_h):
            row = Text()
            row.append(BOX["v"], style=Style(color=BORDER_COLOR))

            if i < len(sidebar_lines):
                row.append_text(sidebar_lines[i])
            else:
                row.append(" " * sidebar_w)

            row.append(BOX["v"], style=Style(color=BORDER_COLOR))

            if i < len(main_lines):
                row.append_text(main_lines[i])
            else:
                row.append(" " * main_w)

            row.append(BOX["v"], style=Style(color=BORDER_COLOR))
            lines.append(row)

        # ── Bottom border ──
        bottom = Text()
        bottom.append(BOX["bl"], style=Style(color=BORDER_COLOR))
        bottom.append(BOX["h"] * sidebar_w, style=Style(color=BORDER_COLOR))
        bottom.append(BOX["t_up"], style=Style(color=BORDER_COLOR))
        bottom.append(BOX["h"] * main_w, style=Style(color=BORDER_COLOR))
        bottom.append(BOX["br"], style=Style(color=BORDER_COLOR))
        lines.append(bottom)

        # ── Action bar + view tabs ──
        lines.append(self._render_action_bar(w))
        lines.append(self._render_status(w))

        return Text("\n").join(lines)

    def _render_sidebar(self, w: int, h: int) -> list[Text]:
        """Render sidebar navigation lines."""
        lines: list[Text] = []

        nav_items = [
            ("files", "~", "files"),
            ("transfers", "↓", "transfers"),
            ("history", "◷", "history"),
            ("search", "/", "search"),
        ]

        for nav_idx, (view_id, icon, label) in enumerate(nav_items):
            line = Text()
            is_active = self.active_view == view_id
            is_sidebar_highlighted = self.sidebar_focused and nav_idx == self.sidebar_cursor

            if is_sidebar_highlighted:
                # Gold bar highlight in sidebar
                sel = Style(color="#111111", bgcolor=GOLD)
                sel_dim = Style(color="#5c4a00", bgcolor=GOLD)
                line.append(" ● " if is_active else "   ", style=sel_dim)
                line.append(f"{icon} ", style=sel_dim)
                line.append(label, style=sel)
                if view_id == "transfers" and len(_transfers) > 0:
                    badge = str(len(_transfers))
                    pad = w - line.cell_len - len(badge) - 1
                    line.append(" " * max(1, pad), style=sel)
                    line.append(badge, style=sel_dim)
                    line.append(" ", style=sel)
                else:
                    pad = w - line.cell_len
                    line.append(" " * max(0, pad), style=sel)
            else:
                if is_active:
                    line.append(" ● ", style=Style(color=GOLD))
                else:
                    line.append("   ")

                icon_color = GOLD_DIM if is_active else TEXT_GHOST
                line.append(f"{icon} ", style=Style(color=icon_color))

                label_color = GOLD if is_active else TEXT_SEC
                line.append(label, style=Style(color=label_color))

                if view_id == "transfers" and len(_transfers) > 0:
                    badge_color = GOLD if is_active else TEXT_GHOST
                    badge = str(len(_transfers))
                    pad = w - line.cell_len - len(badge) - 1
                    line.append(" " * max(1, pad))
                    line.append(badge, style=Style(color=badge_color))
                    line.append(" ")
                else:
                    pad = w - line.cell_len
                    line.append(" " * max(0, pad))

            lines.append(line)

        # Separator
        sep = Text()
        sep.append("  " + "─" * (w - 3) + " ", style=Style(color=TEXT_GHOST))
        lines.append(sep)

        # Storage
        lines.append(Text(" " * w))

        acct = _account
        if acct and acct.disk_total > 0:
            pct = acct.disk_used / acct.disk_total
            pct_str = f"{pct * 100:.0f}%"
            size_label = f"{acct.disk_used_str}/{acct.disk_total_str}"
        else:
            pct = 0
            pct_str = "..."
            size_label = "..."

        stor = Text()
        stor.append("  storage ", style=Style(color=TEXT_GHOST))
        stor.append(pct_str, style=Style(color=GOLD))
        pad = w - stor.cell_len
        stor.append(" " * max(0, pad))
        lines.append(stor)

        bar = Text()
        bar_w = w - 4
        filled = int(bar_w * pct)
        empty = bar_w - filled
        bar.append("  ", style=Style(color=TEXT_GHOST))
        bar.append("█" * filled, style=Style(color=GOLD))
        bar.append("░" * empty, style=Style(color=TEXT_GHOST))
        pad = w - bar.cell_len
        bar.append(" " * max(0, pad))
        lines.append(bar)

        tb = Text()
        tb.append(f"  {size_label}", style=Style(color=TEXT_GHOST))
        pad = w - tb.cell_len
        tb.append(" " * max(0, pad))
        lines.append(tb)

        # Fill remaining
        while len(lines) < h:
            lines.append(Text(" " * w))

        return lines[:h]

    @staticmethod
    def _ensure_visible(cursor: int, scroll: int, visible: int) -> int:
        """Adjust scroll offset so cursor is within visible window."""
        if cursor < scroll:
            return cursor
        if cursor >= scroll + visible:
            return cursor - visible + 1
        return scroll

    def _render_main_panel(self, w: int, h: int) -> list[Text]:
        """Render the active view's content."""
        if self.active_view == "files":
            return self._render_files(w, h)
        elif self.active_view == "transfers":
            return self._render_transfers(w, h)
        elif self.active_view == "history":
            return self._render_history(w, h)
        else:
            return self._render_search(w, h)

    def _render_files(self, w: int, h: int) -> list[Text]:
        lines: list[Text] = []

        # Column layout: 1 margin + name_w + 2 gap + 9 size + 2 gap + 8 mod + 2 gap + status
        # Total fixed after name: 2+9+2+8+2+5 = 28
        name_w = w - 28
        if name_w < 10:
            name_w = 10

        # Column header — built with same structure as data rows
        max_name = name_w - 1  # same as data rows
        hdr = Text()
        hdr.append(" ", style=Style(color=TEXT_GHOST))
        hdr_name = "name" + " " * max(0, max_name - 4)
        hdr.append(hdr_name, style=Style(color=TEXT_GHOST))
        hdr.append("  ", style=Style(color=TEXT_GHOST))
        hdr.append(f"{'size':>9}", style=Style(color=TEXT_GHOST))
        hdr.append("  ", style=Style(color=TEXT_GHOST))
        hdr.append(f"{'modified':>8}", style=Style(color=TEXT_GHOST))
        trail = w - hdr.cell_len
        hdr.append(" " * max(0, trail))
        lines.append(hdr)

        # Scrolling: header takes 1 line, remaining lines for file rows
        visible_rows = h - 1
        self._files_scroll = self._ensure_visible(self.cursor, self._files_scroll, visible_rows)

        # File rows (only visible window)
        start = self._files_scroll
        end = start + visible_rows
        for idx in range(start, min(end, len(self._files))):
            f = self._files[idx]
            is_selected = idx == self.cursor and not self.sidebar_focused
            is_marked = idx in self._marked

            # Name
            name = f.name
            max_name = name_w - 1  # -1 for leading space
            if f.file_type == FileType.DIR:
                name_display = "/" + name
            else:
                name_display = name
            if len(name_display) > max_name:
                name_display = name_display[:max_name - 2] + ".."

            # Tags
            tag_str = ""
            if f.tags and not is_selected:
                tag_str = " ".join(f.tags)

            # Pad name to fixed width
            name_content = name_display
            if tag_str:
                available = max_name - len(name_display) - 1 - len(tag_str)
                name_content = name_display + " " * max(1, available + 1)
            else:
                name_content = name_display + " " * max(0, max_name - len(name_display))

            # Size / Modified / Status
            size_str = f"{f.size:>9}" if f.size else "         "
            mod_str = f"{f.modified:>8}" if f.modified else "        "
            status_part = ""
            if f.transfer_pct is not None:
                status_part = f"↓{f.transfer_pct}%"
            elif f.file_type != FileType.DIR and f.size:
                status_part = "●"

            content = Text()

            if is_selected and is_marked:
                sel_style = Style(color="#cc99ff", bgcolor="#2a1a3d", bold=True)
                sel_dim = Style(color="#9966cc", bgcolor="#2a1a3d")

                content.append(" ")
                content.append(name_content, style=sel_style)
                if tag_str:
                    content.append(tag_str, style=sel_dim)
                    pad_after = max_name - len(name_content) - len(tag_str)
                    if pad_after > 0:
                        content.append(" " * pad_after, style=sel_style)
                content.append("  ", style=sel_style)
                content.append(size_str, style=sel_dim)
                content.append("  ", style=sel_style)
                content.append(mod_str, style=sel_dim)
                content.append("  ", style=sel_style)
                if status_part.startswith("↓"):
                    content.append(status_part, style=sel_dim)
                elif status_part == "●":
                    content.append(status_part, style=Style(color=GREEN, bgcolor="#2a1a3d"))
                else:
                    content.append(" ", style=sel_style)
            elif is_selected:
                sel_style = Style(color="#111111", bgcolor=GOLD)
                sel_dim = Style(color="#5c4a00", bgcolor=GOLD)

                content.append(" ")
                content.append(name_content, style=sel_style)
                if tag_str:
                    content.append(tag_str, style=sel_dim)
                    pad_after = max_name - len(name_content) - len(tag_str)
                    if pad_after > 0:
                        content.append(" " * pad_after, style=sel_style)
                content.append("  ", style=sel_style)
                content.append(size_str, style=sel_dim)
                content.append("  ", style=sel_style)
                content.append(mod_str, style=sel_dim)
                content.append("  ", style=sel_style)
                if status_part.startswith("↓"):
                    content.append(status_part, style=sel_dim)
                elif status_part == "●":
                    content.append(status_part, style=Style(color="#2d6a1e", bgcolor=GOLD))
                else:
                    content.append(" ", style=sel_style)
            elif is_marked:
                mark_style = Style(color="#cc99ff", bold=True)
                mark_dim = Style(color="#9966cc")
                content.append(" ")
                content.append(name_content, style=mark_style)
                if tag_str:
                    content.append(tag_str, style=mark_dim)
                    pad_after = max_name - len(name_content) - len(tag_str)
                    if pad_after > 0:
                        content.append(" " * pad_after)
                content.append("  ")
                content.append(size_str, style=mark_dim)
                content.append("  ")
                content.append(mod_str, style=mark_dim)
                content.append("  ")
                if status_part.startswith("↓"):
                    content.append(status_part, style=Style(color=GOLD))
                elif status_part == "●":
                    content.append(status_part, style=Style(color=GREEN))
                else:
                    content.append(" ")
            else:
                name_color = GOLD if f.file_type == FileType.DIR else TEXT_SEC
                content.append(" ")
                content.append(name_content, style=Style(color=name_color))
                if tag_str:
                    content.append(tag_str, style=Style(color=TEXT_GHOST))
                    pad_after = max_name - len(name_content) - len(tag_str)
                    if pad_after > 0:
                        content.append(" " * pad_after)
                content.append("  ")
                content.append(size_str, style=Style(color=TEXT_DIM))
                content.append("  ")
                content.append(mod_str, style=Style(color=TEXT_DIM))
                content.append("  ")
                if status_part.startswith("↓"):
                    content.append(status_part, style=Style(color=GOLD))
                elif status_part == "●":
                    content.append(status_part, style=Style(color=GREEN))
                else:
                    content.append(" ")

            # Pad to full width
            pad = w - content.cell_len
            if is_selected and is_marked and pad > 0:
                content.append(" " * pad, style=Style(bgcolor="#2a1a3d"))
            elif is_selected and pad > 0:
                content.append(" " * pad, style=Style(bgcolor=GOLD))
            elif pad > 0:
                content.append(" " * pad)

            lines.append(content)

        # Fill
        while len(lines) < h:
            lines.append(Text(" " * w))

        return lines[:h]

    def _render_transfers(self, w: int, h: int) -> list[Text]:
        lines: list[Text] = []

        # Each transfer takes 4 lines (name, bar, detail, blank)
        lines_per_item = 4
        visible_items = max(1, h // lines_per_item)
        self._transfers_scroll = self._ensure_visible(self._transfer_cursor, self._transfers_scroll, visible_items)

        start = self._transfers_scroll
        end = min(start + visible_items + 1, len(_transfers))  # +1 for partial visibility

        for idx in range(start, end):
            t = _transfers[idx]
            is_selected = idx == self._transfer_cursor and not self.sidebar_focused

            # Name line
            name_line = Text()
            if is_selected:
                sel = Style(color="#111111", bgcolor=GOLD)
                name = t.name
                if len(name) > w - 3:
                    name = name[:w - 5] + ".."
                tag_str = " " + " ".join(t.tags) if t.tags else ""
                row_text = f" {name}{tag_str}"
                pad = w - len(row_text)
                name_line.append(row_text, style=sel)
                if pad > 0:
                    name_line.append(" " * pad, style=sel)
            else:
                name_line.append(f" {t.name}", style=Style(color=TEXT_SEC))
                if t.tags:
                    name_line.append(f" {' '.join(t.tags)}", style=Style(color=TEXT_GHOST))
                pad = w - name_line.cell_len
                if pad > 0:
                    name_line.append(" " * pad)
            lines.append(name_line)

            # Progress bar line
            bar_line = Text()
            bar_w = min(40, w - 30)
            filled = int(bar_w * (t.progress / 100))
            empty = bar_w - filled
            is_done = t.progress >= 100
            if is_selected:
                bar_color = "#5c4a00" if not is_done else "#2e7d32"
                bar_line.append(" ", style=sel)
                bar_line.append("█" * filled, style=Style(color=bar_color, bgcolor=GOLD))
                bar_line.append("░" * empty, style=Style(color="#5c4a00", bgcolor=GOLD))
                bar_line.append(f"  {t.progress:5.1f}%", style=Style(color=bar_color, bgcolor=GOLD))
                if t.speed:
                    bar_line.append(f"  {t.speed}", style=Style(color="#5c4a00", bgcolor=GOLD))
                if t.eta and not is_done:
                    bar_line.append(f"  eta {t.eta}", style=Style(color="#5c4a00", bgcolor=GOLD))
                pad = w - bar_line.cell_len
                if pad > 0:
                    bar_line.append(" " * pad, style=sel)
            else:
                if is_done and t.status in ("SEEDING", "seeding"):
                    bar_color = "#4CAF50"
                elif is_done:
                    bar_color = TEXT_SEC
                else:
                    bar_color = GOLD
                bar_line.append(" ")
                bar_line.append("█" * filled, style=Style(color=bar_color))
                bar_line.append("░" * empty, style=Style(color=TEXT_GHOST))
                bar_line.append(f"  {t.progress:5.1f}%", style=Style(color=bar_color))
                if t.speed:
                    bar_line.append(f"  {t.speed}", style=Style(color=TEXT_DIM))
                if t.eta and not is_done:
                    bar_line.append(f"  eta {t.eta}", style=Style(color=TEXT_GHOST))
                pad = w - bar_line.cell_len
                if pad > 0:
                    bar_line.append(" " * pad)
            lines.append(bar_line)

            # Detail line
            det_line = Text()
            detail = f" {t.size}"
            if t.uploaded:
                detail += f" · ↑ {t.uploaded}"
            if t.peers:
                detail += f" · peers: {t.peers}"
            if t.seeds:
                detail += f" · seeds: {t.seeds}"
            if t.status and t.progress >= 100:
                detail += f" · {t.status}"
            if is_selected:
                det_line.append(detail, style=Style(color="#5c4a00", bgcolor=GOLD))
                pad = w - det_line.cell_len
                if pad > 0:
                    det_line.append(" " * pad, style=sel)
            else:
                det_line.append(detail, style=Style(color=TEXT_GHOST))
                pad = w - det_line.cell_len
                if pad > 0:
                    det_line.append(" " * pad)
            lines.append(det_line)

            # Blank separator
            lines.append(Text(" " * w))

        while len(lines) < h:
            lines.append(Text(" " * w))
        return lines[:h]

    def _render_history(self, w: int, h: int) -> list[Text]:
        lines: list[Text] = []

        visible_items = max(1, h)
        self._history_scroll = self._ensure_visible(self._history_cursor, self._history_scroll, visible_items)

        start = self._history_scroll
        for idx in range(start, len(_history)):
            if len(lines) >= h:
                break
            e = _history[idx]
            is_selected = idx == self._history_cursor and not self.sidebar_focused

            line = Text()
            symbol = ACTION_SYMBOLS.get(e.action, "·")
            color = ACTION_COLORS.get(e.action, TEXT_DIM)

            # Build display name with optional [username] prefix
            display_name = e.name
            user_prefix = f"[{e.username}] " if e.username else ""

            # prefix: " ◷  " (4 chars), suffix: "  {timestamp}" (10 chars)
            prefix = f" {symbol}  "
            ts_suffix = f"  {e.timestamp:>8}"
            max_name = w - len(prefix) - len(ts_suffix)
            name_str = f"{user_prefix}{display_name}"
            if len(name_str) > max_name:
                name_str = name_str[:max(0, max_name - 2)] + ".."

            if is_selected:
                sel = Style(color="#111111", bgcolor=GOLD)
                sel_dim = Style(color="#5c4a00", bgcolor=GOLD)
                line.append(prefix + name_str, style=sel)
                pad_name = max_name - len(name_str)
                line.append(" " * max(0, pad_name), style=sel)
                line.append(ts_suffix, style=sel_dim)
                pad = w - line.cell_len
                if pad > 0:
                    line.append(" " * pad, style=sel)
            else:
                line.append(f" {symbol} ", style=Style(color=color, bold=True))
                if user_prefix:
                    uname_part = f" [{e.username}]"
                    remaining = max_name - len(user_prefix)
                    if len(display_name) > remaining:
                        dname_part = f" {display_name[:max(0, remaining - 2)]}.."
                    else:
                        dname_part = f" {display_name}"
                    line.append(uname_part, style=Style(color=TEXT_GHOST))
                    line.append(dname_part, style=Style(color=TEXT_SEC))
                else:
                    if len(display_name) > max_name:
                        line.append(f" {display_name[:max(0, max_name - 2)]}..", style=Style(color=TEXT_SEC))
                    else:
                        line.append(f" {display_name}", style=Style(color=TEXT_SEC))
                pad_name = max_name - len(name_str)
                line.append(" " * max(0, pad_name))
                line.append(ts_suffix, style=Style(color=TEXT_DIM))
                pad = w - line.cell_len
                if pad > 0:
                    line.append(" " * pad)
            lines.append(line)

        while len(lines) < h:
            lines.append(Text(" " * w))
        return lines[:h]

    def _render_search(self, w: int, h: int) -> list[Text]:
        lines: list[Text] = []

        if not self._search_results:
            hint = Text()
            if self._search_query:
                hint.append("  no results", style=Style(color=TEXT_DIM))
            else:
                hint.append('  press / to search', style=Style(color=TEXT_DIM))
            pad = w - hint.cell_len
            if pad > 0:
                hint.append(" " * pad)
            lines.append(hint)
            while len(lines) < h:
                lines.append(Text(" " * w))
            return lines[:h]

        name_w = w - 22  # space for size + modified
        if name_w < 10:
            name_w = 10

        visible_rows = h
        self._search_scroll = self._ensure_visible(self._search_cursor, self._search_scroll, visible_rows)

        start = self._search_scroll
        end = start + visible_rows
        for idx in range(start, min(end, len(self._search_results))):
            f = self._search_results[idx]
            is_selected = idx == self._search_cursor and not self.sidebar_focused

            if f.file_type == FileType.DIR:
                name_display = "/" + f.name
            else:
                name_display = f.name
            max_name = name_w - 1
            if len(name_display) > max_name:
                name_display = name_display[:max_name - 2] + ".."
            name_content = name_display + " " * max(0, max_name - len(name_display))

            size_str = f"{f.size:>9}" if f.size else "         "
            mod_str = f"{f.modified:>8}" if f.modified else "        "

            content = Text()
            if is_selected:
                sel_style = Style(color="#111111", bgcolor=GOLD)
                sel_dim = Style(color="#5c4a00", bgcolor=GOLD)
                content.append(" ")
                content.append(name_content, style=sel_style)
                content.append("  ", style=sel_style)
                content.append(size_str, style=sel_dim)
                content.append("  ", style=sel_style)
                content.append(mod_str, style=sel_dim)
            else:
                name_color = GOLD if f.file_type == FileType.DIR else TEXT_SEC
                content.append(" ")
                content.append(name_content, style=Style(color=name_color))
                content.append("  ")
                content.append(size_str, style=Style(color=TEXT_DIM))
                content.append("  ")
                content.append(mod_str, style=Style(color=TEXT_DIM))

            pad = w - content.cell_len
            if is_selected and pad > 0:
                content.append(" " * pad, style=Style(bgcolor=GOLD))
            elif pad > 0:
                content.append(" " * pad)

            lines.append(content)

        while len(lines) < h:
            lines.append(Text(" " * w))
        return lines[:h]

    def _render_action_bar(self, w: int) -> Text:
        """Render shortcuts bar at the bottom."""
        if self.active_view == "files":
            actions = [
                ("Tab", "Switch panes"), ("/", "Search"), ("+", "Select"),
                ("*", "Invert selection"), ("F7", "New folder"), ("m", "Move"), ("s", "Sort"),
                ("Del", "Delete"), ("q", "Quit"),
            ]
        elif self.active_view == "transfers":
            actions = [
                ("Tab", "Switch panes"), ("/", "Search"),
                ("a", "Add"), ("c", "Cancel"), ("o", "Clear done"), ("q", "Quit"),
            ]
        elif self.active_view == "search":
            actions = [
                ("Tab", "Switch panes"), ("/", "Search"), ("q", "Quit"),
            ]
        else:
            actions = [
                ("Tab", "Switch panes"), ("/", "Search"), ("q", "Quit"),
            ]

        bar = Text()
        for i, (key, label) in enumerate(actions):
            if i > 0:
                bar.append("   ")
            bar.append(key, style=Style(color=GOLD, bold=True))
            bar.append(label, style=Style(color=TEXT_DIM))

        pad = w - bar.cell_len
        if pad > 0:
            bar.append(" " * pad)
        return bar

    def _render_status(self, w: int) -> Text:
        """Render view tabs and status info."""
        line = Text()

        if self._marked and self.active_view == "files":
            line.append(f" {len(self._marked)} marked ", style=Style(color=GOLD, bold=True))

        views = [("1", "files"), ("2", "transfers"), ("3", "history"), ("/", "search")]
        for num, name in views:
            if name == self.active_view:
                line.append(f" {num}:{name}", style=Style(color=GOLD))
            else:
                line.append(f" {num}:{name}", style=Style(color=TEXT_GHOST))

        pad = max(0, w - line.cell_len)
        line.append(" " * pad)
        return line

    # ── Navigation methods ──

    def cursor_down(self) -> None:
        if self.sidebar_focused:
            if self.sidebar_cursor < len(self.SIDEBAR_VIEWS) - 1:
                self.sidebar_cursor += 1
                # Live-update the right pane as you navigate sidebar
                self.active_view = self.SIDEBAR_VIEWS[self.sidebar_cursor]
            self.refresh()
            return
        if self.active_view == "files":
            if self.cursor < len(self._files) - 1:
                self.cursor += 1
        elif self.active_view == "transfers":
            if self._transfer_cursor < len(_transfers) - 1:
                self._transfer_cursor += 1
        elif self.active_view == "history":
            if self._history_cursor < len(_history) - 1:
                self._history_cursor += 1
        elif self.active_view == "search":
            if self._search_cursor < len(self._search_results) - 1:
                self._search_cursor += 1
        self.refresh()

    def cursor_up(self) -> None:
        if self.sidebar_focused:
            if self.sidebar_cursor > 0:
                self.sidebar_cursor -= 1
                # Live-update the right pane as you navigate sidebar
                self.active_view = self.SIDEBAR_VIEWS[self.sidebar_cursor]
            self.refresh()
            return
        if self.active_view == "files":
            if self.cursor > 0:
                self.cursor -= 1
        elif self.active_view == "transfers":
            if self._transfer_cursor > 0:
                self._transfer_cursor -= 1
        elif self.active_view == "history":
            if self._history_cursor > 0:
                self._history_cursor -= 1
        elif self.active_view == "search":
            if self._search_cursor > 0:
                self._search_cursor -= 1
        self.refresh()

    def open_file(self, f: PutFile) -> str | None:
        """Open a non-folder file. Returns error string or None on success."""
        import subprocess, shutil
        try:
            url = putio_api.get_download_url(f.file_id)
        except Exception as e:
            return f"Error getting URL: {e}"
        if not url:
            return "No download URL available"

        # Try VLC (macOS app bundle, then PATH)
        vlc = "/Applications/VLC.app/Contents/MacOS/VLC"
        if not os.path.exists(vlc):
            vlc = shutil.which("vlc")
        if vlc:
            subprocess.Popen(
                [vlc, url],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            return None

        # Fallback: macOS open command
        subprocess.Popen(
            ["open", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return None

    def enter_folder(self) -> None:
        """Right arrow / Enter: navigate into folder or open file."""
        if self.sidebar_focused:
            view = self.SIDEBAR_VIEWS[self.sidebar_cursor]
            self.active_view = view
            self.sidebar_focused = False
            self.refresh()
            if view == "search":
                self.app.action_search()
            return
        if self.active_view == "search":
            selected = self.get_search_selected()
            if not selected:
                return
            if selected.file_type != FileType.DIR:
                err = self.open_file(selected)
                if err:
                    self.app.notify(err, severity="error")
                else:
                    self.app.notify(f"Opening: {selected.name}")
            else:
                # Navigate into the folder in the files view
                self._folder_stack.clear()
                self._folder_stack.append((0, "files/~"))
                self._current_folder_id = selected.file_id
                self.current_path = f"files/~/{selected.name}"
                self.cursor = 0
                self._files_scroll = 0
                self._load_files(selected.file_id, add_parent=True)
                self.active_view = "files"
            return
        if self.active_view == "history":
            if not _history:
                return
            entry = _history[self._history_cursor]
            if not entry.file_id:
                return
            try:
                finfo = putio_api.get_file(entry.file_id)
                parent_id = finfo.parent_id
                self._folder_stack.clear()
                if parent_id != 0:
                    self._folder_stack.append((0, "files/~"))
                self._current_folder_id = parent_id
                self.current_path = "files/~"
                self.cursor = 0
                self._files_scroll = 0
                self._load_files(parent_id, add_parent=parent_id != 0)
                for i, f in enumerate(self._files):
                    if f.file_id == entry.file_id:
                        self.cursor = i
                        break
                self.active_view = "files"
            except Exception as e:
                self.app.notify(f"File not found: {e}", severity="error")
            return
        if self.active_view != "files" or not self._files:
            return
        selected = self._files[self.cursor]
        if selected.file_type != FileType.DIR:
            err = self.open_file(selected)
            if err:
                self.app.notify(err, severity="error")
            else:
                self.app.notify(f"Opening: {selected.name}")
            return

        if selected.name == "..":
            self.go_back()
            return

        # Push current location onto stack
        self._folder_stack.append((self._current_folder_id, self.current_path))
        self._current_folder_id = selected.file_id

        new_path = f"{self.current_path}/{selected.name}"
        self.current_path = new_path
        self.cursor = 0
        self._files_scroll = 0

        # Load files from API in background
        self._load_files(selected.file_id, add_parent=True)

    def go_back(self) -> None:
        """Left arrow / Backspace: go to parent folder."""
        if self.active_view != "files":
            return
        if not self._folder_stack:
            return

        parent_id, parent_path = self._folder_stack.pop()
        self._current_folder_id = parent_id
        self.current_path = parent_path
        self.cursor = 0
        self._files_scroll = 0

        self._load_files(parent_id, add_parent=len(self._folder_stack) > 0)

    def _load_files(self, folder_id: int, add_parent: bool = False, sort_by: str | None = None) -> None:
        """Load files from API. Reads the folder's sort preference."""
        self._marked.clear()
        try:
            result = putio_api.list_files(folder_id, sort_by=sort_by)
            self._sort_key = result.sort_by
            self._files = []
            if add_parent:
                self._files.append(PutFile("..", FileType.DIR, "", ""))
            self._files.extend(_api_file_to_putfile(f) for f in result.files)
        except Exception as e:
            self._files = []
            if add_parent:
                self._files.append(PutFile("..", FileType.DIR, "", ""))
            self._files.append(PutFile(f"error: {e}", FileType.FILE, "", ""))
        self.refresh()

    def jump_top(self) -> None:
        if self.active_view == "files":
            self.cursor = 0
        elif self.active_view == "transfers":
            self._transfer_cursor = 0
        elif self.active_view == "history":
            self._history_cursor = 0
        elif self.active_view == "search":
            self._search_cursor = 0
        self.refresh()

    def page_down(self) -> None:
        """Move cursor down by a page."""
        if self.sidebar_focused:
            return
        page = max(1, self.size.height - 6)
        if self.active_view == "files":
            self.cursor = min(len(self._files) - 1, self.cursor + page)
        elif self.active_view == "transfers":
            self._transfer_cursor = min(len(_transfers) - 1, self._transfer_cursor + page)
        elif self.active_view == "history":
            self._history_cursor = min(len(_history) - 1, self._history_cursor + page)
        elif self.active_view == "search":
            self._search_cursor = min(len(self._search_results) - 1, self._search_cursor + page)
        self.refresh()

    def page_up(self) -> None:
        """Move cursor up by a page."""
        if self.sidebar_focused:
            return
        page = max(1, self.size.height - 6)
        if self.active_view == "files":
            self.cursor = max(0, self.cursor - page)
        elif self.active_view == "transfers":
            self._transfer_cursor = max(0, self._transfer_cursor - page)
        elif self.active_view == "history":
            self._history_cursor = max(0, self._history_cursor - page)
        elif self.active_view == "search":
            self._search_cursor = max(0, self._search_cursor - page)
        self.refresh()

    def jump_bottom(self) -> None:
        if self.active_view == "files":
            self.cursor = max(0, len(self._files) - 1)
        elif self.active_view == "transfers":
            self._transfer_cursor = max(0, len(_transfers) - 1)
        elif self.active_view == "history":
            self._history_cursor = max(0, len(_history) - 1)
        elif self.active_view == "search":
            self._search_cursor = max(0, len(self._search_results) - 1)
        self.refresh()

    def toggle_mark(self) -> None:
        if self.active_view != "files" or not self._files:
            return
        f = self._files[self.cursor]
        if f.name == "..":
            # Don't mark the parent entry, just move down
            if self.cursor < len(self._files) - 1:
                self.cursor += 1
            self.refresh()
            return
        if self.cursor in self._marked:
            self._marked.discard(self.cursor)
        else:
            self._marked.add(self.cursor)
        # Move cursor down after toggling (MC behavior)
        if self.cursor < len(self._files) - 1:
            self.cursor += 1
        self.refresh()

    def invert_marks(self) -> None:
        if self.active_view != "files" or not self._files:
            return
        all_indices = {i for i, f in enumerate(self._files) if f.name != ".."}
        self._marked = all_indices - self._marked
        self.refresh()

    def get_marked_files(self) -> list[PutFile]:
        return [self._files[i] for i in sorted(self._marked) if i < len(self._files)]

    def apply_sort(self, sort_key: str) -> None:
        """Re-fetch the current folder with a new sort order."""
        self._sort_key = sort_key
        self._marked.clear()
        self.cursor = 0
        self._files_scroll = 0
        self._load_files(
            self._current_folder_id,
            add_parent=len(self._folder_stack) > 0,
            sort_by=sort_key,
        )

    def do_search(self, query: str) -> None:
        if not query:
            return
        self._search_query = query
        self._search_cursor = 0
        self._search_scroll = 0
        try:
            results = putio_api.search_files(query)
            self._search_results = [_api_file_to_putfile(f) for f in results]
        except Exception as e:
            self._search_results = []
            self.app.notify(f"Search error: {e}", severity="error")
        self.active_view = "search"
        self.sidebar_focused = False
        self.refresh()

    def get_search_selected(self) -> Optional[PutFile]:
        if self._search_results and 0 <= self._search_cursor < len(self._search_results):
            return self._search_results[self._search_cursor]
        return None

    def switch_view(self, view: str) -> None:
        self.active_view = view
        self.sidebar_focused = False
        self.refresh()

    def toggle_sidebar(self) -> None:
        """Tab toggles focus between sidebar and main panel."""
        if self.sidebar_focused:
            # Leaving sidebar: focus goes to main, first item selected
            self.active_view = self.SIDEBAR_VIEWS[self.sidebar_cursor]
            self.sidebar_focused = False
            # Reset main cursor and scroll to first item
            if self.active_view == "files":
                self.cursor = 0
                self._files_scroll = 0
            elif self.active_view == "transfers":
                self._transfer_cursor = 0
                self._transfers_scroll = 0
            elif self.active_view == "history":
                self._history_cursor = 0
                self._history_scroll = 0
            elif self.active_view == "search":
                self._search_cursor = 0
                self._search_scroll = 0
        else:
            # Entering sidebar: set sidebar cursor to current view
            self.sidebar_cursor = self.SIDEBAR_VIEWS.index(self.active_view)
            self.sidebar_focused = True
        self.refresh()

    def get_selected_file(self) -> Optional[PutFile]:
        if self.active_view == "files" and self._files:
            return self._files[self.cursor]
        return None

    def delete_selected(self) -> Optional[PutFile]:
        if self._files and 0 <= self.cursor < len(self._files):
            f = self._files[self.cursor]
            if f.name == "..":
                return None
            removed = self._files.pop(self.cursor)
            if self.cursor >= len(self._files) and self.cursor > 0:
                self.cursor -= 1
            self._marked.discard(len(self._files))  # clean up stale index
            self.refresh()
            return removed
        return None

    def delete_marked(self) -> list[PutFile]:
        marked_files = self.get_marked_files()
        if not marked_files:
            return []
        marked_indices = sorted(self._marked, reverse=True)
        for idx in marked_indices:
            if 0 <= idx < len(self._files):
                self._files.pop(idx)
        self._marked.clear()
        if self.cursor >= len(self._files) and self.cursor > 0:
            self.cursor = len(self._files) - 1
        self.refresh()
        return marked_files

    def reload_data(self) -> None:
        """Refresh all data from API."""
        global _transfers, _history, _account, _loading, _error
        try:
            _account = putio_api.get_account()
            _transfers = [_api_transfer_to_transfer(t) for t in putio_api.list_transfers()]
            try:
                _history = [_api_event_to_history(e) for e in putio_api.list_events()]
            except Exception:
                pass  # events endpoint might not exist or be empty
            if not self._files:
                self._load_files(self._current_folder_id, add_parent=False)
            _loading = False
        except Exception as e:
            _error = str(e)
            _loading = False
        self.refresh()

    def tick_transfers(self) -> None:
        """Periodically refresh transfers from API."""
        global _transfers
        try:
            _transfers = [_api_transfer_to_transfer(t) for t in putio_api.list_transfers()]
        except Exception:
            pass
        self.refresh()


# ═══════════════════════════════════════════════
# Modals
# ═══════════════════════════════════════════════

class AddTransferScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    AddTransferScreen { align: center middle; }
    #add-dialog {
        width: 60; height: 11;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #add-dialog Label { margin-bottom: 1; color: #999; }
    #add-dialog .hint { color: #444; text-align: center; }
    #add-dialog Input { border: none; outline: none; background: #222; padding: 0 1; height: 1; }
    #add-dialog Input:focus { border: none; outline: none; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="add-dialog"):
            yield Label("paste a magnet link, URL, or path:")
            yield Input(placeholder="> ", id="transfer-input")
            yield Label("save to: files/~                  [Tab to change]", classes="hint")
            yield Label("[Enter] add    [Esc] cancel", classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss("")


SORT_OPTIONS = [
    ("NAME_ASC", "Name A→Z"),
    ("NAME_DESC", "Name Z→A"),
    ("SIZE_ASC", "Size smallest first"),
    ("SIZE_DESC", "Size largest first"),
    ("DATE_ASC", "Date added oldest"),
    ("DATE_DESC", "Date added newest"),
    ("MODIFIED_ASC", "Date modified oldest"),
    ("MODIFIED_DESC", "Date modified newest"),
]


class SortScreen(ModalScreen[Optional[str]]):
    BINDINGS = [
        Binding("enter", "confirm", "Select", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("j", "cursor_down", "Down", priority=True),
        Binding("k", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("up", "cursor_up", "Up", priority=True),
    ]
    CSS = """
    SortScreen { align: center middle; }
    #sort-dialog {
        width: 40; height: 18;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #sort-dialog Label { margin-bottom: 1; color: #999; }
    .sort-hint { color: #444; text-align: center; }
    #sort-list { height: 1fr; }
    """

    def __init__(self, current_sort: str, **kwargs):
        super().__init__(**kwargs)
        self._cursor = 0
        for i, (key, _) in enumerate(SORT_OPTIONS):
            if key == current_sort:
                self._cursor = i
                break

    def compose(self) -> ComposeResult:
        with Vertical(id="sort-dialog"):
            yield Label("sort by:")
            s = Static("", id="sort-list")
            s.can_focus = True
            yield s
            yield Label("j/k move  Enter select  Esc cancel", classes="sort-hint")

    def on_mount(self) -> None:
        self.query_one("#sort-list", Static).focus()
        self._render_list()

    def _render_list(self) -> None:
        t = Text()
        for i, (key, label) in enumerate(SORT_OPTIONS):
            if i == self._cursor:
                t.append(f"  {label}", style=Style(color="#111111", bgcolor=GOLD, bold=True))
                pad = max(0, 34 - len(label) - 2)
                t.append(" " * pad, style=Style(bgcolor=GOLD))
            else:
                t.append(f"  {label}", style=Style(color=TEXT))
            if i < len(SORT_OPTIONS) - 1:
                t.append("\n")
        self.query_one("#sort-list", Static).update(t)

    def action_cursor_down(self) -> None:
        if self._cursor < len(SORT_OPTIONS) - 1:
            self._cursor += 1
            self._render_list()

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._render_list()

    def action_confirm(self) -> None:
        self.dismiss(SORT_OPTIONS[self._cursor][0])

    def action_cancel(self) -> None:
        self.dismiss(None)


class SearchScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    SearchScreen { align: center middle; }
    #search-dialog {
        width: 60; height: 11;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #search-dialog Label { margin-bottom: 1; color: #999; }
    #search-dialog .hint { color: #444; text-align: center; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="search-dialog"):
            yield Label("search files:")
            yield Input(placeholder="query", id="search-input")
            yield Label("[Enter] search    [Esc] cancel", classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss("")


class MkdirScreen(ModalScreen[str]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    CSS = """
    MkdirScreen { align: center middle; }
    #mkdir-dialog {
        width: 60; height: 11;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #mkdir-dialog Label { margin-bottom: 1; color: #999; }
    #mkdir-dialog .hint { color: #444; text-align: center; }
    #mkdir-dialog Input { border: none; outline: none; background: #222; padding: 0 1; height: 1; }
    #mkdir-dialog Input:focus { border: none; outline: none; }
    """

    def compose(self) -> ComposeResult:
        with Vertical(id="mkdir-dialog"):
            yield Label("create new folder:")
            yield Label("")
            yield Input(placeholder="folder name", id="mkdir-input")
            yield Label("[Enter] create    [Esc] cancel", classes="hint")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss("")


class DeleteConfirmScreen(ModalScreen[bool]):
    BINDINGS = [
        Binding("enter", "submit", "Submit", priority=True),
        Binding("left", "toggle", "Toggle", show=False, priority=True),
        Binding("right", "toggle", "Toggle", show=False, priority=True),
        Binding("tab", "toggle", "Toggle", show=False, priority=True),
        Binding("escape", "cancel_dialog", "Cancel", priority=True),
    ]
    CSS = """
    DeleteConfirmScreen { align: center middle; }
    #delete-dialog {
        width: 60; height: 11;
        border: round #f87171;
        background: #161616;
        padding: 1 2;
    }
    #delete-dialog Label { margin-bottom: 1; }
    .warn { color: #f87171; }
    .dim { color: #666; }
    #delete-buttons { text-align: center; margin-top: 1; }
    """

    def __init__(self, file_name: str, file_size: str, count: int = 1, **kwargs):
        super().__init__(**kwargs)
        self._file_name = file_name
        self._file_size = file_size
        self._count = count
        self._selected = 0  # 0 = confirm, 1 = cancel

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-dialog"):
            if self._count > 1:
                yield Label(f"permanently delete {self._count} items:", classes="warn")
                yield Label(f'"{self._file_name}" and {self._count - 1} more', classes="warn")
            else:
                yield Label("permanently delete:", classes="warn")
                yield Label(f'"{self._file_name}"', classes="warn")
            yield Label(f"{self._file_size}", classes="dim")
            s = Static("", id="delete-buttons")
            s.can_focus = True
            yield s

    def on_mount(self) -> None:
        self.query_one("#delete-buttons", Static).focus()
        self._update_buttons()

    def _update_buttons(self) -> None:
        lbl = self.query_one("#delete-buttons", Static)
        t = Text()
        if self._selected == 0:
            t.append("  yes, I'm sure  ", Style(bgcolor="rgb(248,113,113)", color="black", bold=True))
            t.append("    ")
            t.append("  cancel  ", Style(bgcolor="rgb(51,51,51)", color="rgb(153,153,153)"))
        else:
            t.append("  yes, I'm sure  ", Style(bgcolor="rgb(51,51,51)", color="rgb(153,153,153)"))
            t.append("    ")
            t.append("  cancel  ", Style(bgcolor="rgb(248,113,113)", color="black", bold=True))
        lbl.update(t)

    def action_toggle(self) -> None:
        self._selected = 1 - self._selected
        self._update_buttons()

    def action_submit(self) -> None:
        self.dismiss(self._selected == 0)

    def action_cancel_dialog(self) -> None:
        self.dismiss(False)


class ShareResultScreen(ModalScreen[None]):
    BINDINGS = [
        Binding("enter", "close", "OK"),
        Binding("escape", "close", "OK"),
    ]
    CSS = """
    ShareResultScreen { align: center middle; }
    #share-dialog {
        width: 48; height: 8;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #share-dialog Label { margin-bottom: 1; }
    .link { color: #FDCE45; }
    .ok { color: #4ade80; }
    .hint-center { color: #444; text-align: center; }
    """

    def __init__(self, file_name: str, file_id: int = 0, **kwargs):
        super().__init__(**kwargs)
        self._file_name = file_name
        try:
            self._link = putio_api.create_share_link([file_id])
        except Exception:
            self._link = f"https://put.io/file/{file_id}"

    def compose(self) -> ComposeResult:
        with Vertical(id="share-dialog"):
            yield Label(self._link, classes="link")
            yield Label("copied to clipboard ✓", classes="ok")
            yield Label("")
            yield Label("Enter dismiss", classes="hint-center")

    def action_close(self) -> None:
        self.dismiss(None)


class MoveDestinationScreen(ModalScreen[Optional[int]]):
    """Folder tree browser for picking a move destination."""

    BINDINGS = [
        Binding("enter", "confirm", "Move here", priority=True),
        Binding("m", "move_here", "Move here", priority=True),
        Binding("escape", "cancel", "Cancel", priority=True),
        Binding("j", "cursor_down", "Down", priority=True),
        Binding("k", "cursor_up", "Up", priority=True),
        Binding("down", "cursor_down", "Down", priority=True),
        Binding("up", "cursor_up", "Up", priority=True),
        Binding("right", "expand", "Expand", priority=True),
        Binding("l", "expand", "Expand", priority=True),
        Binding("left", "collapse", "Collapse", priority=True),
        Binding("h", "collapse", "Collapse", priority=True),
        Binding("pagedown", "page_down", "PgDn", priority=True),
        Binding("pageup", "page_up", "PgUp", priority=True),
        Binding("ctrl+d", "page_down", "PgDn", priority=True),
        Binding("ctrl+u", "page_up", "PgUp", priority=True),
    ]
    CSS = """
    MoveDestinationScreen { align: center middle; }
    #move-dialog {
        width: 60; height: 20;
        border: round #FDCE45;
        background: #161616;
        padding: 1 2;
    }
    #move-dialog Label { margin-bottom: 0; }
    #move-tree { height: 1fr; }
    .move-title { color: #FDCE45; }
    .move-hint { color: #444; text-align: center; }
    .move-count { color: #cc99ff; }
    """

    @dataclass
    class _TreeNode:
        folder_id: int
        name: str
        depth: int
        expanded: bool = False
        loaded: bool = False
        children: list[MoveDestinationScreen._TreeNode] = field(default_factory=list)

    def __init__(self, file_count: int, current_folder_id: int, **kwargs):
        super().__init__(**kwargs)
        self._file_count = file_count
        self._current_folder_id = current_folder_id
        self._cursor = 0
        self._scroll = 0
        # Build initial tree with root expanded
        self._root = self._TreeNode(folder_id=0, name="Your Files", depth=0, expanded=True)
        self._load_children(self._root)
        self._flat: list[MoveDestinationScreen._TreeNode] = []
        self._rebuild_flat()
        # Position cursor on current folder if visible
        for i, node in enumerate(self._flat):
            if node.folder_id == current_folder_id:
                self._cursor = i
                break

    def _load_children(self, node: _TreeNode) -> None:
        if node.loaded:
            return
        node.loaded = True
        try:
            result = putio_api.list_files(node.folder_id)
            node.children = [
                self._TreeNode(folder_id=f.id, name=f.name, depth=node.depth + 1)
                for f in result.files if f.is_dir
            ]
        except Exception:
            node.children = []

    def _rebuild_flat(self) -> None:
        flat: list[MoveDestinationScreen._TreeNode] = []
        def walk(node: _TreeNode) -> None:
            flat.append(node)
            if node.expanded:
                for child in node.children:
                    walk(child)
        walk(self._root)
        self._flat = flat

    def compose(self) -> ComposeResult:
        with Vertical(id="move-dialog"):
            count_text = f"{self._file_count} item{'s' if self._file_count > 1 else ''}"
            yield Label(f"move {count_text} to:", classes="move-title")
            s = Static("", id="move-tree")
            s.can_focus = True
            yield s
            yield Label("j/k move  Enter expand  m move here  Esc cancel", classes="move-hint")

    def on_mount(self) -> None:
        self.query_one("#move-tree", Static).focus()
        self._render_tree()

    def _render_tree(self) -> None:
        widget = self.query_one("#move-tree", Static)
        tree_h = max(1, widget.size.height or 14)
        tree_w = max(1, widget.size.width or 40)

        # Scrolling
        if self._cursor < self._scroll:
            self._scroll = self._cursor
        if self._cursor >= self._scroll + tree_h:
            self._scroll = self._cursor - tree_h + 1

        t = Text()
        start = self._scroll
        end = start + tree_h
        for i in range(start, min(end, len(self._flat))):
            node = self._flat[i]
            is_cur = i == self._cursor

            indent = "  " * node.depth
            if node.children or not node.loaded:
                arrow = "▼ " if node.expanded else "▶ "
            else:
                arrow = "  "

            line_text = f"{indent}{arrow}/{node.name}"

            if is_cur and node.folder_id == self._current_folder_id:
                t.append(line_text, style=Style(color="#111111", bgcolor=GOLD, bold=True))
                tag = " (current)"
                t.append(tag, style=Style(color="#5c4a00", bgcolor=GOLD))
                pad = max(0, tree_w - len(line_text) - len(tag))
                t.append(" " * pad, style=Style(bgcolor=GOLD))
            elif is_cur:
                t.append(line_text, style=Style(color="#111111", bgcolor=GOLD, bold=True))
                pad = max(0, tree_w - len(line_text))
                t.append(" " * pad, style=Style(bgcolor=GOLD))
            elif node.folder_id == self._current_folder_id:
                t.append(line_text, style=Style(color=GOLD))
                t.append(" (current)", style=Style(color=TEXT_GHOST))
            else:
                t.append(line_text, style=Style(color=TEXT))

            if i < min(end, len(self._flat)) - 1:
                t.append("\n")

        widget.update(t)

    def action_cursor_down(self) -> None:
        if self._cursor < len(self._flat) - 1:
            self._cursor += 1
            self._render_tree()

    def action_cursor_up(self) -> None:
        if self._cursor > 0:
            self._cursor -= 1
            self._render_tree()

    def action_page_down(self) -> None:
        widget = self.query_one("#move-tree", Static)
        page = max(1, (widget.size.height or 14) - 2)
        self._cursor = min(len(self._flat) - 1, self._cursor + page)
        self._render_tree()

    def action_page_up(self) -> None:
        widget = self.query_one("#move-tree", Static)
        page = max(1, (widget.size.height or 14) - 2)
        self._cursor = max(0, self._cursor - page)
        self._render_tree()

    def action_expand(self) -> None:
        node = self._flat[self._cursor]
        if not node.expanded:
            self._load_children(node)
            node.expanded = True
            self._rebuild_flat()
            self._render_tree()

    def action_collapse(self) -> None:
        node = self._flat[self._cursor]
        if node.expanded:
            node.expanded = False
            self._rebuild_flat()
            # Keep cursor in bounds
            if self._cursor >= len(self._flat):
                self._cursor = len(self._flat) - 1
            self._render_tree()
        elif node.depth > 0:
            # Jump to parent
            for i, n in enumerate(self._flat):
                if n.depth == node.depth - 1 and i < self._cursor:
                    parent_i = i
            self._cursor = parent_i
            self._render_tree()

    def action_confirm(self) -> None:
        node = self._flat[self._cursor]
        # If folder has (or might have) children and isn't expanded, expand it
        if not node.expanded and (node.children or not node.loaded):
            self.action_expand()
            return
        self.dismiss(node.folder_id)

    def action_move_here(self) -> None:
        node = self._flat[self._cursor]
        self.dismiss(node.folder_id)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ═══════════════════════════════════════════════
# App
# ═══════════════════════════════════════════════

class PutioTUI(App):
    TITLE = "put.io"

    CSS = """
    Screen { background: #0a0a0a; }
    #main-view { width: 1fr; height: 1fr; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("tab", "toggle_sidebar", "Tab", priority=True),
        Binding("1", "view_files", "Files"),
        Binding("2", "view_transfers", "Transfers"),
        Binding("3", "view_history", "History"),
        Binding("j", "down", "Down"),
        Binding("k", "up", "Up"),
        Binding("down", "down", "Down"),
        Binding("up", "up", "Up"),
        Binding("enter", "enter", "Open"),
        Binding("right", "enter", "Open"),
        Binding("l", "enter", "Open"),
        Binding("left", "back", "Back"),
        Binding("h", "back", "Back"),
        Binding("backspace", "back", "Back"),
        Binding("a", "add_transfer", "Add"),
        Binding("d", "download", "Download"),
        Binding("D", "delete_item", "Delete"),
        Binding("delete", "delete_item", "Delete"),
        Binding("s", "sort_files", "Sort"),
        Binding("g", "jump_top", "Top"),
        Binding("G", "jump_bottom", "Bottom"),
        Binding("pagedown", "page_down", "PgDn"),
        Binding("pageup", "page_up", "PgUp"),
        Binding("ctrl+d", "page_down", "PgDn"),
        Binding("ctrl+u", "page_up", "PgUp"),
        Binding("plus", "toggle_mark", "Mark"),
        Binding("space", "toggle_mark", "Mark"),
        Binding("asterisk", "invert_marks", "Invert"),
        Binding("m", "move_item", "Move"),
        Binding("f6", "move_item", "Move"),
        Binding("f7", "mkdir", "Mkdir"),
        Binding("f8", "delete_item", "Delete"),
        Binding("f10", "quit", "Quit"),
        Binding("slash", "search", "Search"),
        Binding("c", "cancel_transfer", "Cancel Transfer"),
        Binding("o", "clean_transfers", "Clear Completed"),
    ]

    def compose(self) -> ComposeResult:
        yield MainView(id="main-view")

    def on_mount(self) -> None:
        # Load initial data from API
        mv = self._mv()
        self.call_later(mv.reload_data)
        # Refresh transfers every 5 seconds
        self.set_interval(5.0, self._tick)

    def _tick(self) -> None:
        try:
            self.query_one("#main-view", MainView).tick_transfers()
        except NoMatches:
            pass

    def _mv(self) -> MainView:
        return self.query_one("#main-view", MainView)

    def action_toggle_sidebar(self) -> None:
        self._mv().toggle_sidebar()

    def action_view_files(self) -> None:
        self._mv().switch_view("files")

    def action_view_transfers(self) -> None:
        self._mv().switch_view("transfers")

    def action_view_history(self) -> None:
        self._mv().switch_view("history")

    def action_down(self) -> None:
        self._mv().cursor_down()

    def action_up(self) -> None:
        self._mv().cursor_up()

    def action_enter(self) -> None:
        self._mv().enter_folder()

    def action_back(self) -> None:
        self._mv().go_back()

    def action_jump_top(self) -> None:
        self._mv().jump_top()

    def action_jump_bottom(self) -> None:
        self._mv().jump_bottom()

    def action_page_down(self) -> None:
        self._mv().page_down()

    def action_page_up(self) -> None:
        self._mv().page_up()

    def action_cancel_transfer(self) -> None:
        mv = self._mv()
        if mv.active_view != "transfers" or not _transfers:
            return
        t = _transfers[mv._transfer_cursor]
        def on_confirm(confirmed: bool) -> None:
            if confirmed:
                try:
                    putio_api.cancel_transfer(t.transfer_id)
                    mv.tick_transfers()
                except Exception as e:
                    self.notify(f"Error: {e}", severity="error")
        self.push_screen(
            DeleteConfirmScreen(t.name, t.size),
            callback=on_confirm,
        )

    def action_clean_transfers(self) -> None:
        mv = self._mv()
        if mv.active_view != "transfers":
            return
        try:
            putio_api.clean_transfers()
            mv.tick_transfers()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_add_transfer(self) -> None:
        def on_result(val: str) -> None:
            if val:
                try:
                    putio_api.add_transfer(val)
                    # Refresh transfers immediately
                    self._mv().tick_transfers()
                except Exception as e:
                    self.notify(f"Error: {e}", severity="error")
        self.push_screen(AddTransferScreen(), callback=on_result)

    def action_delete_item(self) -> None:
        mv = self._mv()
        marked = mv.get_marked_files()

        if marked:
            self._delete_files(mv, marked)
        else:
            f = mv.get_selected_file()
            if f and f.name != "..":
                self._delete_files(mv, [f])

    def _delete_files(self, mv: MainView, files: list) -> None:
        file_ids = [f.file_id for f in files]
        count = len(files)
        first = files[0]

        trash_on = True
        try:
            trash_on = putio_api.get_trash_enabled()
        except Exception:
            trash_on = True

        if trash_on:
            try:
                putio_api.delete_file(file_ids if count > 1 else first.file_id)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
                return
            if count > 1:
                mv.delete_marked()
                self.notify(f"Moved {count} items to trash")
            else:
                mv.delete_selected()
                self.notify(f"Moved to trash: {first.name}")
        else:
            def on_confirm(yes: bool) -> None:
                if yes:
                    try:
                        putio_api.delete_file(file_ids if count > 1 else first.file_id)
                    except Exception as e:
                        self.notify(f"Error: {e}", severity="error")
                        return
                    if count > 1:
                        mv.delete_marked()
                        self.notify(f"Deleted {count} items")
                    else:
                        mv.delete_selected()
                        self.notify(f"Deleted: {first.name}")
            size_str = f"{count} items" if count > 1 else first.size
            self.push_screen(
                DeleteConfirmScreen(first.name, size_str, count=count),
                callback=on_confirm,
            )

    def action_share_item(self) -> None:
        mv = self._mv()
        f = mv.get_selected_file()
        if f and f.file_id:
            self.push_screen(ShareResultScreen(f.name, f.file_id))

    def action_download(self) -> None:
        pass  # visual feedback only in prototype

    def action_toggle_mark(self) -> None:
        self._mv().toggle_mark()

    def action_invert_marks(self) -> None:
        self._mv().invert_marks()

    def action_sort_files(self) -> None:
        mv = self._mv()
        if mv.active_view != "files":
            return

        def on_result(sort_key: Optional[str]) -> None:
            if sort_key:
                mv.apply_sort(sort_key)

        self.push_screen(SortScreen(mv._sort_key), callback=on_result)

    def action_search(self) -> None:
        mv = self._mv()

        def on_result(query: str) -> None:
            if query:
                mv.do_search(query)

        self.push_screen(SearchScreen(), callback=on_result)

    def action_mkdir(self) -> None:
        mv = self._mv()
        if mv.active_view != "files":
            return

        def on_result(name: str) -> None:
            if not name:
                return
            try:
                putio_api.create_folder(name, mv._current_folder_id)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
                return
            self.notify(f"Created: /{name}")
            mv._load_files(mv._current_folder_id, add_parent=len(mv._folder_stack) > 0)

        self.push_screen(MkdirScreen(), callback=on_result)

    def action_move_item(self) -> None:
        mv = self._mv()
        if mv.active_view != "files":
            return
        marked = mv.get_marked_files()
        if marked:
            files_to_move = marked
        else:
            f = mv.get_selected_file()
            if not f or f.name == "..":
                return
            files_to_move = [f]

        file_ids = [f.file_id for f in files_to_move]
        count = len(files_to_move)

        def on_dest(folder_id: Optional[int]) -> None:
            if folder_id is None:
                return
            try:
                putio_api.move_files(file_ids, folder_id)
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
                return
            if count > 1:
                mv.delete_marked()
                self.notify(f"Moved {count} items")
            else:
                mv.delete_selected()
                self.notify(f"Moved: {files_to_move[0].name}")

        self.push_screen(
            MoveDestinationScreen(count, mv._current_folder_id),
            callback=on_dest,
        )


PUTIO_CLIENT_ID = "9034"
TOKEN_PATH = os.path.expanduser("~/.config/putio-tui/token")


def _read_saved_token() -> str:
    """Read token from env var or config file."""
    tok = os.environ.get("PUTIO_TOKEN", "")
    if tok:
        return tok
    if os.path.exists(TOKEN_PATH):
        return open(TOKEN_PATH).read().strip()
    return ""


def _save_token(token: str) -> None:
    """Save token to config file."""
    os.makedirs(os.path.dirname(TOKEN_PATH), exist_ok=True)
    with open(TOKEN_PATH, "w") as f:
        f.write(token)
    os.chmod(TOKEN_PATH, 0o600)


def _oauth_login() -> str:
    """Device linking flow — user enters a code at put.io/link."""
    import urllib.request
    import json
    import time

    url = f"https://api.put.io/v2/oauth2/oob/code?app_id={PUTIO_CLIENT_ID}"
    resp = urllib.request.urlopen(url, timeout=10)
    data = json.loads(resp.read())
    code = data.get("code", "")

    if not code:
        print("Failed to get device code from put.io.")
        return ""

    print()
    print("  ┌( ಠ‿ಠ)┘welcome!")
    print()
    print(f"  Go to https://put.io/link")
    print(f"  Enter code: {code}")
    print()
    print("Waiting for approval...", end="", flush=True)

    check_url = f"https://api.put.io/v2/oauth2/oob/code/{code}"
    while True:
        time.sleep(3)
        try:
            resp = urllib.request.urlopen(check_url, timeout=10)
            result = json.loads(resp.read())
            token = result.get("oauth_token", "")
            if token:
                print(" done!")
                return token
        except urllib.error.HTTPError:
            pass
        print(".", end="", flush=True)


def main():
    # Accept token as argument or env var
    if len(sys.argv) > 1 and not sys.argv[1].startswith("-"):
        os.environ["PUTIO_TOKEN"] = sys.argv[1]
        _save_token(sys.argv[1])

    token = _read_saved_token()

    if not token:
        token = _oauth_login()
        if not token:
            print("Login failed. No token received.")
            sys.exit(1)
        _save_token(token)
        print("Logged in! Token saved to ~/.config/putio-tui/token")

    os.environ["PUTIO_TOKEN"] = token
    app = PutioTUI()
    app.run(mouse=False)


if __name__ == "__main__":
    main()
