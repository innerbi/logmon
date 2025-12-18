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

    def __post_init__(self):
        self.lines = deque(maxlen=self.max_lines)
        self._cache_valid = False
        self._cached_filtered = []

    def _invalidate_cache(self):
        """Invalidate filtered lines cache."""
        self._cache_valid = False

    def add_line(self, line: LogLine):
        """Add a new log line."""
        # No agregar si esta pausado o scrolleado (para que quede quieto)
        if not self.paused and self.scroll_offset == 0:
            self.lines.append(line)
            self._invalidate_cache()

            # Update stats
            if line.source not in self.stats:
                self.stats[line.source] = {}
            level = line.level.upper()
            self.stats[line.source][level] = self.stats[line.source].get(level, 0) + 1

    def _rebuild_cache(self):
        """Rebuild the filtered lines cache."""
        self._cached_filtered = []
        for line in self.lines:
            # Apply filters
            if self.filters.level and line.level.upper() != self.filters.level.upper():
                continue
            if self.filters.source and line.source != self.filters.source:
                continue
            if self.filters.search and self.filters.search.lower() not in line.raw.lower():
                continue
            self._cached_filtered.append(line)
        self._cache_valid = True

    def get_filtered_lines(self, limit: int = 50) -> List[LogLine]:
        """Get filtered lines for display (uses cache for performance)."""
        # Rebuild cache if invalid
        if not self._cache_valid:
            self._rebuild_cache()

        total = len(self._cached_filtered)
        if total == 0:
            return []

        # Calculate effective offset (clamped) WITHOUT modifying state
        max_offset = max(0, total - limit)
        effective_offset = min(self.scroll_offset, max_offset)

        # Get the window of lines
        end_idx = total - effective_offset
        start_idx = max(0, end_idx - limit)
        return self._cached_filtered[start_idx:end_idx]

    def clamp_scroll(self, visible_lines: int = 50):
        """Clamp scroll offset to valid range. Call this before render."""
        # Use cache for fast count
        if not self._cache_valid:
            self._rebuild_cache()
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
            status = "[red]DISCONNECTED (reconnecting...)[/]"
        elif self.paused:
            status = "[yellow]PAUSED[/]"
        elif self.scroll_offset > 0:
            status = f"[cyan]SCROLLED (+{self.scroll_offset})[/]"
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

    def render_logs(self, height: int = 30) -> Panel:
        """Render the main log panel."""
        lines = self.get_filtered_lines(limit=height)

        if not lines:
            content = Text("No logs to display. Waiting for new logs...", style="dim")
        else:
            content = Text()
            for line in lines:
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
            "[X]Reconnect",
            "[1-5]Level",
            "[B]ack/[W]ork/[R]ay",
            "[A]ll",
            "[Q]uit"
        ]
        text = Text(" | ".join(shortcuts), style="dim")
        return Panel(text, border_style="dim")

    def render(self, height: int = 30) -> Layout:
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
        layout["body"].update(self.render_logs(height=body_height))
        layout["footer"].update(self.render_footer())

        return layout

    def clear(self):
        """Clear all logs and stats."""
        self.lines.clear()
        self.stats.clear()
        self._invalidate_cache()

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
