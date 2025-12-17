"""Redis pub/sub log subscriber."""
import json
import threading
from typing import Callable, List, Optional
from dataclasses import dataclass
from queue import Queue, Empty


@dataclass
class LogLine:
    """A parsed log line."""
    source: str
    timestamp: str
    level: str
    logger_name: str
    message: str
    raw: str


class RedisLogSubscriber:
    """
    Subscribes to Redis pub/sub channels for log streaming.

    Runs subscription in a background thread and queues messages
    for the main thread to consume.
    """

    def __init__(self, redis_url: str, channels: List[str]):
        """
        Initialize subscriber.

        Args:
            redis_url: Redis connection URL
            channels: List of channels to subscribe (e.g., ['logs:backend', 'logs:batch'])
        """
        self.redis_url = redis_url
        self.channels = channels
        self.queue: Queue[LogLine] = Queue(maxsize=1000)
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._pubsub = None

    def start(self):
        """Start the subscriber in a background thread."""
        if self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._subscribe_loop, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the subscriber."""
        self._running = False
        if self._pubsub:
            try:
                self._pubsub.unsubscribe()
            except Exception:
                pass  # Connection may already be closed
            try:
                self._pubsub.close()
            except Exception:
                pass  # Ignore close errors
        self._pubsub = None

    def _subscribe_loop(self):
        """Background thread that subscribes to Redis channels."""
        import redis
        from urllib.parse import urlparse

        try:
            parsed = urlparse(self.redis_url)
            # Force IPv4 - 'localhost' may resolve to IPv6 which port-forward doesn't support
            host = parsed.hostname or 'localhost'
            if host == 'localhost':
                host = '127.0.0.1'
            client = redis.Redis(
                host=host,
                port=parsed.port or 6379,
                db=int(parsed.path.lstrip('/') or 0),
                decode_responses=True,
                protocol=2
            )
            self._pubsub = client.pubsub()
            self._pubsub.subscribe(*self.channels)

            for message in self._pubsub.listen():
                if not self._running:
                    break

                if message['type'] == 'message':
                    try:
                        data = json.loads(message['data'])
                        log_line = LogLine(
                            source=data.get('component', 'unknown'),
                            timestamp=data.get('timestamp', ''),
                            level=data.get('level', 'INFO'),
                            logger_name=data.get('logger', ''),
                            message=data.get('message', ''),
                            raw=message['data']
                        )
                        # Don't block if queue is full, just drop old messages
                        if self.queue.full():
                            try:
                                self.queue.get_nowait()
                            except Empty:
                                pass
                        self.queue.put_nowait(log_line)
                    except (json.JSONDecodeError, KeyError):
                        pass

        except Exception as e:
            # Connection error - will be handled by monitor
            pass
        finally:
            self._running = False

    def get_new_lines(self) -> List[LogLine]:
        """Get all new log lines from the queue (non-blocking)."""
        lines = []
        while True:
            try:
                line = self.queue.get_nowait()
                lines.append(line)
            except Empty:
                break
        return lines

    @property
    def is_running(self) -> bool:
        return self._running
