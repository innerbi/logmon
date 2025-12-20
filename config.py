"""Configuration for log monitor."""
from dataclasses import dataclass, field
from typing import List, Optional
import os


@dataclass
class LogSource:
    """A log source to monitor."""
    name: str
    color: str  # Rich color name
    enabled: bool = True


@dataclass
class PortForwardConfig:
    """Configuration for kubectl port-forward."""
    enabled: bool = False
    namespace: str = "workers"
    service: str = "redis"
    port: int = 6379


@dataclass
class MonitorConfig:
    """Monitor configuration."""
    redis_url: str
    sources: List[LogSource]
    refresh_rate: float = 0.5  # seconds
    max_lines: int = 1000  # Max lines to keep in buffer
    port_forward: Optional[PortForwardConfig] = None

    @classmethod
    def default(cls) -> "MonitorConfig":
        redis_url = os.getenv("REDIS_URL", "redis://localhost:6379/0")
        return cls(
            redis_url=redis_url,
            sources=[
                LogSource("backend", "cyan"),
                LogSource("batch", "yellow"),
                LogSource("ray", "magenta"),
            ]
        )
