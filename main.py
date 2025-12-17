"""
Lumen Log Monitor - Real-time log monitoring via Redis pub/sub.

Usage:
    cd logmon && python main.py      # Run directly
    python -m logmon                 # Run as module

Options:
    python main.py --backend-only
    python main.py --batch-only
    python main.py --local
"""
import argparse
import os
import sys
import subprocess
import atexit
import time

# Support both direct execution and module execution
try:
    from .config import MonitorConfig, LogSource
    from .monitor import LogMonitor
except ImportError:
    from config import MonitorConfig, LogSource
    from monitor import LogMonitor

# Global to track port-forward process
_port_forward_proc = None


def _cleanup_port_forward():
    """Cleanup port-forward process on exit."""
    global _port_forward_proc
    if _port_forward_proc:
        try:
            _port_forward_proc.terminate()
            _port_forward_proc.wait(timeout=2)
        except Exception:
            try:
                _port_forward_proc.kill()
            except Exception:
                pass


def start_port_forward(namespace: str = "workers", service: str = "redis", port: int = 6379) -> bool:
    """Start kubectl port-forward in background. Returns True if successful."""
    global _port_forward_proc

    print(f"  [..] Starting kubectl port-forward (svc/{service} in {namespace})...")

    try:
        # Check if kubectl is available
        result = subprocess.run(
            ["kubectl", "version", "--client"],
            capture_output=True,
            timeout=5
        )
        if result.returncode != 0:
            print("  [ERROR] kubectl not found or not configured")
            return False

        # Start port-forward
        _port_forward_proc = subprocess.Popen(
            ["kubectl", "port-forward", f"svc/{service}", f"{port}:{port}", "-n", namespace],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
        )

        # Register cleanup
        atexit.register(_cleanup_port_forward)

        # Wait a moment for connection
        time.sleep(2)

        # Check if still running
        if _port_forward_proc.poll() is not None:
            stderr = _port_forward_proc.stderr.read().decode() if _port_forward_proc.stderr else ""
            print(f"  [ERROR] Port-forward failed: {stderr}")
            return False

        print(f"  [OK] Port-forward started (localhost:{port})")
        return True

    except FileNotFoundError:
        print("  [ERROR] kubectl not found in PATH")
        return False
    except subprocess.TimeoutExpired:
        print("  [ERROR] kubectl timed out")
        return False
    except Exception as e:
        print(f"  [ERROR] Failed to start port-forward: {e}")
        return False


def main():
    parser = argparse.ArgumentParser(
        description="Lumen Log Monitor - Real-time log monitoring via Redis pub/sub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m tools.logmon
    python -m tools.logmon --redis-url redis://localhost:6379/0
    python -m tools.logmon --backend-only
    python -m tools.logmon --batch-only

Keyboard shortcuts:
    P       Pause/Resume log streaming
    C       Clear all logs from display
    1-5     Filter by level (1=DEBUG, 2=INFO, 3=WARN, 4=ERROR, 5=CRIT)
    0       Show all levels (remove level filter)
    B       Show backend logs only
    W       Show batch worker logs only
    A       Show all sources (reset all filters)
    Q       Quit the monitor

Requirements:
    - Redis server running
    - Services started with LOG_TO_REDIS=1
"""
    )

    parser.add_argument(
        "--redis-url", "-r",
        default=os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        help="Redis URL (default: REDIS_URL env or redis://localhost:6379/0)"
    )
    parser.add_argument(
        "--backend-only", "-b",
        action="store_true",
        help="Monitor backend logs only"
    )
    parser.add_argument(
        "--batch-only", "-w",
        action="store_true",
        help="Monitor batch worker logs only"
    )
    parser.add_argument(
        "--refresh-rate",
        type=float,
        default=0.2,
        help="Refresh rate in seconds (default: 0.2)"
    )
    parser.add_argument(
        "--max-lines", "-m",
        type=int,
        default=1000,
        help="Maximum lines to keep in buffer (default: 1000)"
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        help="Wait for keypress before starting (default: start immediately)"
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="Use local Redis instead of AKS (default: AKS via port-forward)"
    )
    parser.add_argument(
        "--namespace", "-n",
        default="workers",
        help="Kubernetes namespace for port-forward (default: workers)"
    )

    args = parser.parse_args()

    # Build sources list
    sources = []
    if not args.batch_only:
        sources.append(LogSource(name="backend", color="cyan"))
    if not args.backend_only:
        sources.append(LogSource(name="batch", color="yellow"))

    if not sources:
        print("Error: At least one log source must be enabled")
        sys.exit(1)

    # Verify Redis connection
    print("=" * 50)
    print("  Lumen Log Monitor (Redis pub/sub)")
    print("=" * 50)
    print(f"  Redis: {args.redis_url}")
    print(f"  Channels: {', '.join(f'logs:{s.name}' for s in sources)}")
    print(f"  Refresh: {args.refresh_rate}s")
    print()

    # Check Redis connection
    import redis
    from urllib.parse import urlparse

    def try_redis_connection(url: str) -> bool:
        """Try to connect to Redis. Returns True if successful."""
        try:
            parsed = urlparse(url)
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

    redis_ok = False

    if args.local:
        # Local mode: connect directly to local Redis
        print("  [..] Connecting to local Redis...")
        if try_redis_connection(args.redis_url):
            print("  [OK] Local Redis connection successful")
            redis_ok = True
        else:
            print("  [ERROR] Cannot connect to local Redis")
            print("  Make sure Redis is running locally")
    else:
        # AKS mode (default): try existing connection first, then port-forward
        print("  [..] Connecting to AKS Redis...")

        # First, check if there's already a working connection (existing port-forward)
        if try_redis_connection(args.redis_url):
            print("  [OK] Redis connection successful (existing port-forward)")
            redis_ok = True
        else:
            # No existing connection, try to start port-forward
            print("  [..] No existing connection, starting port-forward...")
            if start_port_forward(namespace=args.namespace):
                time.sleep(1)
                if try_redis_connection(args.redis_url):
                    print("  [OK] AKS Redis connection successful")
                    redis_ok = True
                else:
                    print("  [ERROR] Port-forward started but cannot connect to Redis")
            else:
                print("  [ERROR] Failed to start port-forward to AKS")
                print("  Make sure kubectl is configured and you have access to the cluster")

    if not redis_ok:
        sys.exit(1)

    print()
    print("  Make sure services are running with LOG_TO_REDIS=1")
    print()

    if args.wait:
        print("  Press any key to start, Q to quit...")
        print("=" * 50)
        # Wait for keypress
        try:
            import msvcrt
            msvcrt.getch()
        except ImportError:
            input()
    else:
        print("=" * 50)

    # Create config
    config = MonitorConfig(
        redis_url=args.redis_url,
        sources=sources,
        refresh_rate=args.refresh_rate,
        max_lines=args.max_lines
    )

    # Run monitor
    monitor = LogMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
