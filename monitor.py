"""Main monitor class that coordinates Redis subscription and display."""
import time
import sys
from rich.console import Console
from rich.live import Live

try:
    from .config import MonitorConfig
    from .tail import RedisLogSubscriber
    from .ui import LogDisplay
except ImportError:
    from config import MonitorConfig
    from tail import RedisLogSubscriber
    from ui import LogDisplay


class LogMonitor:
    """
    Main log monitor application.

    Subscribes to Redis pub/sub channels and displays logs in a TUI.
    """

    def __init__(self, config: MonitorConfig):
        self.config = config
        self.console = Console()
        self.display = LogDisplay(
            max_lines=config.max_lines,
            sources={s.name: s for s in config.sources}
        )
        self.subscriber: RedisLogSubscriber = None
        self.running = False

    def _setup_subscriber(self):
        """Create Redis subscriber for enabled sources."""
        channels = [f"logs:{s.name}" for s in self.config.sources if s.enabled]
        self.subscriber = RedisLogSubscriber(self.config.redis_url, channels)

    def _reconnect(self):
        """Attempt to reconnect to Redis."""
        try:
            if self.subscriber:
                self.subscriber.stop()
                # Give time for thread to terminate
                import time
                time.sleep(0.5)
            self._setup_subscriber()
            self.subscriber.start()
            # Wait a bit to check if connection succeeded
            time.sleep(0.5)
        except Exception as e:
            # Log error but continue
            pass

    def _poll_logs(self):
        """Poll Redis for new log messages."""
        if self.subscriber:
            for line in self.subscriber.get_new_lines():
                self.display.add_line(line)

    def _check_keyboard(self) -> bool:
        """
        Check for keyboard input (non-blocking on Windows).
        Returns False if should quit.
        """
        try:
            import msvcrt
            # Process all pending keys to avoid buffer buildup
            while msvcrt.kbhit():
                key = msvcrt.getch()
                # Handle special keys (arrows, etc)
                if key == b'\x00' or key == b'\xe0':
                    # Check if there's a second byte available
                    if msvcrt.kbhit():
                        special = msvcrt.getch()
                        result = self._handle_special_key(special)
                        if not result:
                            return False
                    # If no second byte, ignore the prefix
                else:
                    result = self._handle_key(key.decode('utf-8', errors='ignore'))
                    if not result:
                        return False
        except ImportError:
            # Unix - use select
            import select
            import tty
            import termios
            old_settings = termios.tcgetattr(sys.stdin)
            try:
                tty.setcbreak(sys.stdin.fileno())
                if select.select([sys.stdin], [], [], 0)[0]:
                    key = sys.stdin.read(1)
                    return self._handle_key(key)
            finally:
                termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_settings)
        except Exception:
            pass
        return True

    def _handle_special_key(self, key: bytes) -> bool:
        """Handle special keys (arrows). Returns False to quit."""
        # Scroll 6 lines at a time
        scroll_lines = 6

        if key == b'H':  # Up arrow
            self.display.scroll_up(scroll_lines)
        elif key == b'P':  # Down arrow
            self.display.scroll_down(scroll_lines)
        elif key == b'O':  # End key - jump to latest
            self.display.scroll_to_bottom()
        elif key == b'G':  # Home key - jump to oldest
            self.display.scroll_up(9999)
        return True

    def _handle_key(self, key: str) -> bool:
        """Handle a keyboard key. Returns False to quit."""
        key_lower = key.lower()

        if key_lower == 'q':
            return False
        elif key_lower == 'p':
            self.display.toggle_pause()
        elif key_lower == 'c':
            self.display.clear()
        elif key_lower == '1':
            self.display.set_level_filter('DEBUG')
        elif key_lower == '2':
            self.display.set_level_filter('INFO')
        elif key_lower == '3':
            self.display.set_level_filter('WARNING')
        elif key_lower == '4':
            self.display.set_level_filter('ERROR')
        elif key_lower == '5':
            self.display.set_level_filter('CRITICAL')
        elif key_lower == '0':
            self.display.set_level_filter(None)
        elif key_lower == 'b':
            self.display.set_source_filter('backend')
        elif key_lower == 'w':
            self.display.set_source_filter('batch')
        elif key_lower == 'r':
            self.display.set_source_filter('ray')
        elif key_lower == 'a':
            self.display.set_source_filter(None)
            self.display.set_level_filter(None)
        elif key_lower == 'l':
            self._copy_logs_to_clipboard()

        return True

    def _copy_logs_to_clipboard(self):
        """Copy all filtered logs to clipboard."""
        try:
            import subprocess
            lines = []
            for line in self.display.lines:
                # Apply same filters as display
                if self.display.filters.level and line.level.upper() != self.display.filters.level.upper():
                    continue
                if self.display.filters.source and line.source != self.display.filters.source:
                    continue
                if self.display.filters.search and self.display.filters.search.lower() not in line.raw.lower():
                    continue
                lines.append(line.raw)

            text = "\n".join(lines)
            # Use clip.exe on Windows
            process = subprocess.Popen(['clip'], stdin=subprocess.PIPE)
            process.communicate(text.encode('utf-8'))
        except Exception:
            pass  # Silently fail if clipboard not available

    def run(self):
        """Run the monitor (blocking)."""
        self.running = True
        self._setup_subscriber()

        # Start Redis subscription
        self.subscriber.start()

        # Get terminal dimensions for display
        height = self.console.height or 30
        width = self.console.width or 120

        try:
            with Live(
                self.display.render(height=height, width=width),
                console=self.console,
                refresh_per_second=int(1 / self.config.refresh_rate),
                screen=True
            ) as live:
                while self.running:
                    # Check if subscriber is still running
                    if not self.subscriber.is_running:
                        self.display.connection_error = True
                    else:
                        self.display.connection_error = False

                    # Poll for new logs
                    self._poll_logs()

                    # Check keyboard
                    if not self._check_keyboard():
                        self.running = False
                        break

                    # Update display
                    height = self.console.height or 30
                    width = self.console.width or 120
                    # Clamp scroll before render to avoid stale offsets
                    # body = height - 10
                    self.display.clamp_scroll(visible_lines=height - 10)
                    live.update(self.display.render(height=height, width=width))

                    # Small sleep
                    time.sleep(self.config.refresh_rate)

        except KeyboardInterrupt:
            pass
        finally:
            self.running = False
            try:
                if self.subscriber:
                    self.subscriber.stop()
            except Exception:
                pass  # Ignore cleanup errors
            self.console.print("[dim]Monitor stopped.[/]")
