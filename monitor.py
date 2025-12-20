"""Main monitor class that coordinates Redis subscription and display."""
import time
import sys
import subprocess
import redis
from urllib.parse import urlparse
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
        self._port_forward_proc = None
        self._render_needed = True  # Flag to trigger render after user input

    def _try_redis_connection(self) -> bool:
        """Try to connect to Redis. Returns True if successful."""
        try:
            parsed = urlparse(self.config.redis_url)
            host = parsed.hostname or 'localhost'
            if host == 'localhost':
                host = '127.0.0.1'
            client = redis.Redis(
                host=host,
                port=parsed.port or 6379,
                db=int(parsed.path.lstrip('/') or 0),
                decode_responses=True,
                protocol=2,
                socket_timeout=3
            )
            client.ping()
            return True
        except Exception:
            return False

    def _cleanup_port_forward(self):
        """Cleanup port-forward process."""
        if self._port_forward_proc:
            try:
                self._port_forward_proc.terminate()
                self._port_forward_proc.wait(timeout=2)
            except Exception:
                try:
                    self._port_forward_proc.kill()
                except Exception:
                    pass
            self._port_forward_proc = None

    def _start_port_forward(self) -> bool:
        """Start kubectl port-forward in background. Returns True if successful."""
        if not self.config.port_forward or not self.config.port_forward.enabled:
            return False

        pf = self.config.port_forward
        try:
            # Start port-forward
            self._port_forward_proc = subprocess.Popen(
                ["kubectl", "port-forward", f"svc/{pf.service}", f"{pf.port}:{pf.port}", "-n", pf.namespace],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            )

            # Wait a moment for connection
            time.sleep(2)

            # Check if still running
            if self._port_forward_proc.poll() is not None:
                return False

            return True

        except Exception:
            return False

    def _setup_subscriber(self):
        """Create Redis subscriber for enabled sources."""
        channels = [f"logs:{s.name}" for s in self.config.sources if s.enabled]
        self.subscriber = RedisLogSubscriber(self.config.redis_url, channels)

    def _reconnect(self):
        """Attempt to reconnect to Redis, including port-forward if needed."""
        try:
            # Stop existing subscriber
            if self.subscriber:
                self.subscriber.stop()
                time.sleep(0.5)

            # Check if Redis is reachable
            if not self._try_redis_connection():
                # Redis not reachable, try to restart port-forward
                if self.config.port_forward and self.config.port_forward.enabled:
                    self._cleanup_port_forward()
                    if self._start_port_forward():
                        time.sleep(1)
                        if not self._try_redis_connection():
                            return  # Still can't connect
                    else:
                        return  # Failed to start port-forward
                else:
                    return  # No port-forward config and Redis not reachable

            # Redis is reachable, setup subscriber
            self._setup_subscriber()
            self.subscriber.start()
            time.sleep(0.5)
        except Exception:
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
            self._render_needed = True
        elif key == b'P':  # Down arrow
            self.display.scroll_down(scroll_lines)
            self._render_needed = True
        elif key == b'O':  # End key - jump to latest
            self.display.scroll_to_bottom()
            self._render_needed = True
        elif key == b'G':  # Home key - jump to oldest
            self.display.scroll_up(9999)
            self._render_needed = True
        return True

    def _handle_key(self, key: str) -> bool:
        """Handle a keyboard key. Returns False to quit."""
        key_lower = key.lower()

        if key_lower == 'q':
            return False
        elif key_lower == 'p':
            self.display.toggle_pause()
            self._render_needed = True
        elif key_lower == 'c':
            self.display.clear()
            self._render_needed = True
        elif key_lower == '1':
            self.display.set_level_filter('DEBUG')
            self._render_needed = True
        elif key_lower == '2':
            self.display.set_level_filter('INFO')
            self._render_needed = True
        elif key_lower == '3':
            self.display.set_level_filter('WARNING')
            self._render_needed = True
        elif key_lower == '4':
            self.display.set_level_filter('ERROR')
            self._render_needed = True
        elif key_lower == '5':
            self.display.set_level_filter('CRITICAL')
            self._render_needed = True
        elif key_lower == '0':
            self.display.set_level_filter(None)
            self._render_needed = True
        elif key_lower == 'b':
            self.display.set_source_filter('backend')
            self._render_needed = True
        elif key_lower == 'w':
            self.display.set_source_filter('batch')
            self._render_needed = True
        elif key_lower == 'r':
            self.display.set_source_filter('ray')
            self._render_needed = True
        elif key_lower == 'a':
            self.display.set_source_filter(None)
            self.display.set_level_filter(None)
            self._render_needed = True
        elif key_lower == 'l':
            self._copy_logs_to_clipboard()
        elif key_lower == 'x':
            self._reconnect()
            self._render_needed = True

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

        # Ensure Redis connection (start port-forward if needed)
        if not self._try_redis_connection():
            if self.config.port_forward and self.config.port_forward.enabled:
                if not self._start_port_forward():
                    self.console.print("[red]Failed to start port-forward[/]")
                    return
                if not self._try_redis_connection():
                    self.console.print("[red]Cannot connect to Redis after port-forward[/]")
                    return
            else:
                self.console.print("[red]Cannot connect to Redis[/]")
                return

        self._setup_subscriber()

        # Start Redis subscription
        self.subscriber.start()

        # Get terminal dimensions for display
        height = self.console.height or 30
        width = self.console.width or 120

        try:
            # Use auto_refresh=False and manually control refresh
            # This allows freezing the screen when scrolled for text selection
            with Live(
                self.display.render(height=height, width=width),
                console=self.console,
                auto_refresh=False,  # Manual refresh control
                screen=True
            ) as live:
                while self.running:
                    # Check if subscriber is still running
                    if not self.subscriber.is_running:
                        self.display.connection_error = True
                    else:
                        self.display.connection_error = False

                    # Poll for new logs (always, even when scrolled - buffer keeps growing)
                    self._poll_logs()

                    # Check keyboard
                    if not self._check_keyboard():
                        self.running = False
                        break

                    # Update display:
                    # - Always render in live mode (scroll_offset == 0)
                    # - When scrolled, only render on user input (_render_needed)
                    # This freezes the screen when scrolled, allowing text selection
                    is_scrolled = self.display.scroll_offset > 0
                    should_render = (not is_scrolled) or self._render_needed

                    if should_render:
                        height = self.console.height or 30
                        width = self.console.width or 120
                        # Calculate available width for visual line calculations
                        available_width = max(40, width - 4)
                        # content_height = height - 8 (header+footer) - 2 (panel borders) = height - 10
                        content_height = max(1, height - 10)
                        # Clamp scroll before render to avoid stale offsets (now in visual lines)
                        self.display.clamp_scroll(visible_lines=content_height, available_width=available_width)
                        live.update(self.display.render(height=height, width=width))
                        live.refresh()  # Manual refresh
                        self._render_needed = False

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
            # Cleanup port-forward
            self._cleanup_port_forward()
            self.console.print("[dim]Monitor stopped.[/]")
