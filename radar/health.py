"""Local health probe: the running bot renews a heartbeat while connected."""
import time
from pathlib import Path

HEARTBEAT = Path('/tmp/radar-heartbeat')


def healthy(path=HEARTBEAT, max_age=60):
    try:
        return 0 <= time.time() - path.stat().st_mtime < max_age
    except OSError:
        return False


def main():
    raise SystemExit(0 if healthy() else 1)


if __name__ == '__main__':
    main()
