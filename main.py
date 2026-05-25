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

# Support both direct execution and module execution
try:
    from .config import MonitorConfig, LogSource, PortForwardConfig
    from .monitor import LogMonitor
except ImportError:
    from config import MonitorConfig, LogSource, PortForwardConfig
    from monitor import LogMonitor


def main():
    parser = argparse.ArgumentParser(
        description="Lumen Log Monitor - Real-time log monitoring via Redis pub/sub",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python -m logmon
    python -m logmon --redis-url redis://localhost:6379/0
    python -m logmon --backend-only
    python -m logmon --batch-only

Keyboard shortcuts:
    P       Pause/Resume log streaming
    C       Clear all logs from display
    1-5     Filter by level (1=DEBUG, 2=INFO, 3=WARN, 4=ERROR, 5=CRIT)
    0       Show all levels (remove level filter)
    B       Show backend logs only
    W       Show batch worker logs only
    X       Reconnect to Redis (restarts port-forward if needed)
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
    # Always include ray logs for distributed task monitoring
    sources.append(LogSource(name="ray", color="magenta"))

    if not sources:
        print("Error: At least one log source must be enabled")
        sys.exit(1)

    # Show startup info
    print("=" * 50)
    print("  Lumen Log Monitor (Redis pub/sub)")
    print("=" * 50)
    print(f"  Redis: {args.redis_url}")
    print(f"  Channels: {', '.join(f'logs:{s.name}' for s in sources)}")
    print(f"  Refresh: {args.refresh_rate}s")
    print(f"  Mode: {'Local' if args.local else 'AKS (auto port-forward)'}")
    print()
    print("  Make sure services are running with LOG_TO_REDIS=1")
    print()

    if args.wait:
        print("  Press any key to start, Q to quit...")
        print("=" * 50)
        try:
            import msvcrt
            msvcrt.getch()
        except ImportError:
            input()
    else:
        print("=" * 50)

    # Configure port-forward (only if not local mode)
    port_forward = None
    if not args.local:
        port_forward = PortForwardConfig(
            enabled=True,
            namespace=args.namespace,
            service="redis",
            port=6379
        )

    # Create config
    config = MonitorConfig(
        redis_url=args.redis_url,
        sources=sources,
        refresh_rate=args.refresh_rate,
        max_lines=args.max_lines,
        port_forward=port_forward
    )

    # Run monitor (handles port-forward and reconnection internally)
    monitor = LogMonitor(config)
    monitor.run()


if __name__ == "__main__":
    main()
