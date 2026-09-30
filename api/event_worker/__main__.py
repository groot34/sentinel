"""Event delivery worker entry point.

Starts the background event delivery worker for processing outbox events.

Usage:
    python -m api.event_worker

Environment variables:
    SENTINEL_DATABASE_URL  PostgreSQL connection URL (required)
"""

from __future__ import annotations

import sys

from api.event_worker.worker import main

if __name__ == "__main__":
    main()
