"""
Django Tasks Concurrent - Async concurrent worker for Django Tasks.

Provides a management command that runs multiple async tasks concurrently
using asyncio TaskGroup. Optimized for I/O-bound tasks like API calls.

Usage:
    python manage.py concurrent_worker --concurrency=3
"""

__version__ = "0.2.0"


def __getattr__(name: str):
    """Lazy import to avoid loading Django models at import time."""
    if name == "ConcurrentWorker":
        from django_tasks_concurrent.worker import ConcurrentWorker  # noqa: PLC0415

        return ConcurrentWorker
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ["ConcurrentWorker", "__version__"]
