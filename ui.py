"""Rich TUI components for log monitor."""
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from typing import List, Dict, Optional
from collections import deque
from dataclasses import dataclass, field
import threading

try:
    from .tail import LogLine
    from .config import LogSource
except ImportError:
    from tail import LogLine
    from config import LogSource


# Log level styles (ASCII-safe, no emojis)
LEVEL_STYLES = {
    'DEBUG': Style(color="bright_black"),
    'INFO': Style(color="white"),
    'WARNING': Style(color="yellow"),
    'ERROR': Style(color="red", bold=True),
    'CRITICAL': Style(color="magenta", bold=True),
}

LEVEL_MARKERS = {
    'DEBUG': '[D]',
    'INFO': '[I]',
    'WARNING': '[W]',
    'ERROR': '[E]',
    'CRITICAL': '[!]',
}


@dataclass
class FilterState:
    """Current filter state."""
    level: Optional[str] = None  # None = all levels
    source: Optional[str] = None  # None = all sources
    search: str = ""  # Text search filter


@dataclass
class LogDisplay:
    """Manages the log display buffer and rendering."""
    max_lines: int = 1000
    sources: Dict[str, LogSource] = field(default_factory=dict)
    lines: deque = field(default_factory=lambda: deque(maxlen=1000))
    filters: FilterState = field(default_factory=FilterState)
    paused: bool = False
    stats: Dict[str, Dict[str, int]] = field(default_factory=dict)
    connection_error: bool = False
    scroll_offset: int = 0  # 0 = latest, >0 = scrolled back
    _cache_valid: bool = False
    _cached_filtered: List = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _cache_version: int = 0  # Incremented on invalidation to detect stale rebuilds

    def __post_init__(self):
        self.lines = deque(maxlen=self.max_lines)
        self._cache_valid = False
        self._cached_filtered = []
        self._lock = threading.Lock()
        self._cache_version = 0

    def _invalidate_cache(self):
        """Invalidate filtered lines cache."""
        with self._lock:
            self._cache_valid = False
            self._cache_version += 1

    def add_line(self, line: LogLine):
        """Add a new log line."""
        if self.paused:
            return

        # Always add to buffer (never lose logs)
        self.lines.append(line)
        self._invalidate_cache()

        # Update stats
        if line.source not in self.stats:
            self.stats[line.source] = {}
        level = line.level.upper()
        self.stats[line.source][level] = self.stats[line.source].get(level, 0) + 1

        # If scrolled, DO NOT increment offset - freeze the view
        # New logs accumulate in buffer but are not shown until scroll_to_bottom()

    def _rebuild_cache(self):
        """Rebuild the filtered lines cache."""
        new_filtered = []
        # Take a snapshot of lines to avoid issues with concurrent modification
        lines_snapshot = list(self.lines)

        for line in lines_snapshot:
            # Apply filters
            if self.filters.level and line.level.upper() != self.filters.level.upper():
                continue
            if self.filters.source and line.source != self.filters.source:
                continue
            if self.filters.search and self.filters.search.lower() not in line.raw.lower():
                continue
            new_filtered.append(line)

        # Always update cache with latest snapshot
        # Even if invalidated during rebuild, this data is still fresher than before
        with self._lock:
            self._cached_filtered = new_filtered
            self._cache_valid = True

    def get_filtered_lines(self, limit: int = 50) -> List[LogLine]:
        """Get filtered lines for display (uses cache for performance)."""
        # Check cache validity and rebuild if needed
        with self._lock:
            cache_valid = self._cache_valid

        if not cache_valid:
            self._rebuild_cache()

        # Take a snapshot of the filtered cache
        with self._lock:
            filtered_snapshot = list(self._cached_filtered)

        total = len(filtered_snapshot)
        if total == 0:
            return []

        # Calculate effective offset (clamped) WITHOUT modifying state
        max_offset = max(0, total - limit)
        effective_offset = min(self.scroll_offset, max_offset)

        # Get the window of lines
        end_idx = total - effective_offset
        start_idx = max(0, end_idx - limit)
        return filtered_snapshot[start_idx:end_idx]

    def clamp_scroll(self, visible_lines: int = 50):
        """Clamp scroll offset to valid range. Call this before render."""
        # Check cache validity
        with self._lock:
            cache_valid = self._cache_valid

        if not cache_valid:
            self._rebuild_cache()

        with self._lock:
            total = len(self._cached_filtered)

        max_offset = max(0, total - visible_lines)
        self.scroll_offset = min(self.scroll_offset, max_offset)

    def scroll_up(self, lines: int = 2):
        """Scroll up (towards older logs)."""
        self.scroll_offset += lines

    def scroll_down(self, lines: int = 2):
        """Scroll down (towards newer logs)."""
        self.scroll_offset = max(0, self.scroll_offset - lines)

    def scroll_to_bottom(self):
        """Jump to latest logs."""
        self.scroll_offset = 0

    def render_header(self) -> Panel:
        """Render the header panel with status and stats."""
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Label", style="bold")
        table.add_column("Value")

        # Status
        if self.connection_error:
            status = "[red]DISCONNECTED[/]"
        elif self.paused:
            status = "[yellow]PAUSED[/]"
        elif self.scroll_offset > 0:
            status = f"[cyan]VIEW FROZEN (buffering) - Press End for latest[/]"
        else:
            status = "[green]LIVE[/]"
        table.add_row("Status:", status)

        # Active filters
        filters = []
        if self.filters.level:
            filters.append(f"Level: {self.filters.level}")
        if self.filters.source:
            filters.append(f"Source: {self.filters.source}")
        if self.filters.search:
            filters.append(f"Search: '{self.filters.search}'")
        filter_text = ", ".join(filters) if filters else "None"
        table.add_row("Filters:", filter_text)

        # Stats per source
        stats_parts = []
        for source, levels in self.stats.items():
            errors = levels.get('ERROR', 0) + levels.get('CRITICAL', 0)
            total = sum(levels.values())
            if errors > 0:
                stats_parts.append(f"{source}: {total} ([red]{errors} err[/])")
            else:
                stats_parts.append(f"{source}: {total}")
        stats_text = " | ".join(stats_parts) if stats_parts else "No logs yet"
        table.add_row("Stats:", stats_text)

        return Panel(table, title="[bold]Lumen Log Monitor[/]", border_style="blue")

    def render_logs(self, height: int = 30, width: int = 120) -> Panel:
        """Render the main log panel.

        When in live mode (scroll_offset=0): uses bottom-up rendering to ensure
        newest logs are always visible, cutting older logs if they don't fit.

        When scrolled (scroll_offset>0): shows logs from the scroll position,
        which may cut newer logs at the bottom if content overflows.
        """
        # Calculate available width (panel width - borders - padding)
        available_width = max(40, width - 4)

        if self.scroll_offset == 0:
            # LIVE MODE: bottom-up selection to show newest logs
            lines = self.get_filtered_lines(limit=height * 3)  # Get more for calculation

            if not lines:
                content = Text("No logs to display. Waiting for new logs...", style="dim")
                return Panel(content, title="[bold]Logs[/]", border_style="green")

            # Select lines from the end that fit in available height
            selected_lines = []
            total_visual_lines = 0

            for line in reversed(lines):
                # Estimate line length: [SRC] HH:MM:SS [L] logger: message
                prefix_len = 6 + 9 + 4  # [SRC] + timestamp + [L]
                if line.logger_name:
                    prefix_len += min(len(line.logger_name), 15) + 2

                msg_len = len(line.message)
                total_len = prefix_len + msg_len

                # Calculate how many visual lines this log entry needs
                visual_lines = max(1, (total_len + available_width - 1) // available_width)

                if total_visual_lines + visual_lines <= height:
                    selected_lines.insert(0, line)  # Insert at beginning to maintain order
                    total_visual_lines += visual_lines
                else:
                    break  # No more space
        else:
            # SCROLLED MODE: show from scroll position (old behavior)
            selected_lines = self.get_filtered_lines(limit=height)

            if not selected_lines:
                content = Text("No logs to display. Waiting for new logs...", style="dim")
                return Panel(content, title="[bold]Logs[/]", border_style="green")

        # Render selected lines
        content = Text()
        for line in selected_lines:
            # Source tag
            source_info = self.sources.get(line.source)
            source_color = source_info.color if source_info else "white"
            content.append(f"[{line.source[:3].upper()}] ", style=source_color)

            # Timestamp (just time part)
            if line.timestamp:
                time_part = line.timestamp.split(' ')[-1] if ' ' in line.timestamp else line.timestamp
                content.append(f"{time_part} ", style="dim")

            # Level marker
            level_upper = line.level.upper()
            marker = LEVEL_MARKERS.get(level_upper, '[?]')
            style = LEVEL_STYLES.get(level_upper, Style())
            content.append(f"{marker} ", style=style)

            # Logger name (truncated)
            if line.logger_name:
                logger_short = line.logger_name[-15:] if len(line.logger_name) > 15 else line.logger_name
                content.append(f"{logger_short}: ", style="cyan")

            # Message (full, no truncation)
            content.append(f"{line.message}\n", style=style)

        return Panel(content, title="[bold]Logs[/]", border_style="green")

    def render_footer(self) -> Panel:
        """Render the footer with keyboard shortcuts."""
        shortcuts = [
            "Arrows:Scroll",
            "End:Latest",
            "[P]ause",
            "[C]lear",
            "[L]ogs:Copy",
            "[1-5]Level",
            "[B]ack/[W]ork/[R]ay",
            "[A]ll",
            "[Q]uit"
        ]
        text = Text(" | ".join(shortcuts), style="dim")
        return Panel(text, border_style="dim")

    def render(self, height: int = 30, width: int = 120) -> Layout:
        """Render complete UI with fixed header/footer."""
        layout = Layout()

        # Split into header (fixed 5), body (flexible), footer (fixed 3)
        layout.split_column(
            Layout(name="header", size=5, minimum_size=5),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=3, minimum_size=3),
        )

        # Body height: total - header(5) - footer(3) - panel borders(2)
        body_height = max(5, height - 12)

        layout["header"].update(self.render_header())
        layout["body"].update(self.render_logs(height=body_height, width=width))
        layout["footer"].update(self.render_footer())

        return layout

    def clear(self):
        """Clear all logs and stats."""
        with self._lock:
            self.lines.clear()
            self.stats.clear()
            self._cache_valid = False
            self._cache_version += 1
            self._cached_filtered = []

    def set_level_filter(self, level: Optional[str]):
        """Set level filter."""
        self.filters.level = level
        self._invalidate_cache()

    def set_source_filter(self, source: Optional[str]):
        """Set source filter."""
        self.filters.source = source
        self._invalidate_cache()

    def toggle_pause(self):
        """Toggle pause state."""
        self.paused = not self.paused
