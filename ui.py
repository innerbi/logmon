"""Rich TUI components for log monitor."""
from rich.console import Group
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.style import Style
from rich.cells import cell_len  # For accurate width calculation with emojis/unicode
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

    def _calc_visual_lines(self, line: LogLine, available_width: int, max_lines: int = 5) -> int:
        """Calculate how many visual lines a log entry will occupy.

        Accounts for both text wrapping AND newlines within the message.
        Uses cell_len for accurate width with emojis/unicode (they take 2 columns).
        Caps at max_lines to prevent huge logs from dominating the display.
        Must match the truncation logic in render_logs().
        """
        # Prefix: [SRC] (6) + timestamp (13) + [L] (4) + logger (up to 17)
        prefix_len = 6 + 13 + 4  # = 23 base
        if line.logger_name:
            prefix_len += min(len(line.logger_name), 15) + 2  # ": " after logger

        # Apply same truncation as render_logs() BEFORE calculating
        msg = line.message.rstrip('\n')
        max_visual_width = available_width * max_lines
        # First, rough truncation by characters
        if len(msg) > max_visual_width:
            msg = msg[:max_visual_width]
        # Then check actual visual width (cell_len accounts for emojis taking 2 columns)
        while cell_len(msg) > max_visual_width and len(msg) > 0:
            msg = msg[:-1]
        msg_lines = msg.split('\n')
        if len(msg_lines) > max_lines:
            msg_lines = msg_lines[:max_lines]

        total_visual = 0
        for i, msg_part in enumerate(msg_lines):
            if i == 0:
                # First line includes the prefix
                # Use cell_len for accurate width with emojis/unicode
                line_len = prefix_len + cell_len(msg_part)
            else:
                # Subsequent lines are just the message content (no prefix)
                line_len = cell_len(msg_part)

            # Calculate wrapped lines for this part
            if line_len == 0:
                total_visual += 1  # Empty line still takes 1 visual line
            else:
                total_visual += max(1, (line_len + available_width - 1) // available_width)

            # Cap at max_lines
            if total_visual >= max_lines:
                return max_lines

        return max(1, min(total_visual, max_lines))

    def get_filtered_lines_by_visual(self, visible_height: int, available_width: int) -> List[LogLine]:
        """Get filtered lines for display based on VISUAL lines, not log entries.

        Counts from the LAST log entry upward, including only entries that
        fit COMPLETELY. This ensures newest content is always fully visible.
        """
        # Check cache validity and rebuild if needed
        with self._lock:
            cache_valid = self._cache_valid

        if not cache_valid:
            self._rebuild_cache()

        # Take a snapshot of the filtered cache
        with self._lock:
            filtered_snapshot = list(self._cached_filtered)

        if not filtered_snapshot:
            return []

        # Pre-calculate visual lines for each entry
        visual_counts = []
        for line in filtered_snapshot:
            visual_counts.append(self._calc_visual_lines(line, available_width))

        total_entries = len(filtered_snapshot)

        # Calculate which entry is at the "bottom" of our view
        # scroll_offset is in visual lines from the very end
        # Find which entry index corresponds to where we want to END
        if self.scroll_offset == 0:
            # Live mode: start from the last entry
            end_entry_idx = total_entries - 1
        else:
            # Scrolled mode: find the entry at scroll_offset visual lines from end
            visual_from_end = 0
            end_entry_idx = total_entries - 1
            for i in range(total_entries - 1, -1, -1):
                visual_from_end += visual_counts[i]
                if visual_from_end >= self.scroll_offset:
                    end_entry_idx = i
                    break

        # Now count backwards from end_entry_idx, including ONLY complete entries
        # Do NOT include partial entries - this prevents overflow into header/footer
        result = []
        accumulated_visual = 0

        for i in range(end_entry_idx, -1, -1):
            v_count = visual_counts[i]

            # Check if this entry fits completely
            if accumulated_visual + v_count <= visible_height:
                result.insert(0, filtered_snapshot[i])  # Insert at start to maintain order
                accumulated_visual += v_count
            else:
                # Do NOT include partial entries - causes overflow
                break

        return result

    def get_total_visual_lines(self, available_width: int) -> int:
        """Get total visual lines across all filtered entries."""
        with self._lock:
            cache_valid = self._cache_valid

        if not cache_valid:
            self._rebuild_cache()

        with self._lock:
            filtered_snapshot = list(self._cached_filtered)

        total = 0
        for line in filtered_snapshot:
            total += self._calc_visual_lines(line, available_width)
        return total

    def clamp_scroll(self, visible_lines: int = 50, available_width: int = 120):
        """Clamp scroll offset to valid range based on visual lines. Call this before render."""
        total_visual = self.get_total_visual_lines(available_width)
        max_offset = max(0, total_visual - visible_lines)
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
            status = "[red]DISCONNECTED[/] [dim][[/][yellow]X[/][dim]] Reconnect[/]"
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

        Args:
            height: Total height available for the panel (including borders)
            width: Total width available for the panel
        """
        # Calculate available width (panel width - borders - padding)
        available_width = max(40, width - 4)
        # Actual content height inside panel (subtract 2 for panel borders + 1 safety margin)
        # Be conservative to avoid any overflow
        content_height = max(1, height - 3)

        # Use visual-line-aware selection for both modes
        # scroll_offset is now in visual lines, not log entries
        selected_lines = self.get_filtered_lines_by_visual(
            visible_height=content_height,
            available_width=available_width
        )

        if not selected_lines:
            content = Text("No logs to display. Waiting for new logs...", style="dim")
            return Panel(content, title="[bold]Logs[/]", border_style="green", height=height)

        # Render selected lines
        content = Text()
        for idx, line in enumerate(selected_lines):
            # Add newline between entries (not after the last one)
            if idx > 0:
                content.append("\n")

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

            # Message (truncated if too long, strip trailing newlines)
            msg = line.message.rstrip('\n')
            # Limit to ~5 lines worth of content (matching max_lines=5 in _calc_visual_lines)
            # Use cell_len for accurate width with emojis/unicode
            max_visual_width = available_width * 5
            # First, rough truncation by characters
            if len(msg) > max_visual_width:
                msg = msg[:max_visual_width]
            # Then check actual visual width and truncate further if needed
            while cell_len(msg) > max_visual_width and len(msg) > 0:
                msg = msg[:-1]
            if len(msg) < len(line.message.rstrip('\n')):
                msg = msg + "..."
            # Also limit number of newlines
            msg_lines = msg.split('\n')
            if len(msg_lines) > 5:
                msg = '\n'.join(msg_lines[:5]) + "..."
            content.append(msg, style=style)

        # Use explicit height to prevent overflow into header/footer
        return Panel(content, title="[bold]Logs[/]", border_style="green", height=height)

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

        # Body height: total - header(5) - footer(3)
        body_height = max(5, height - 8)

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
