"""
Django Tasks Concurrent - Async concurrent worker and cron scheduler for Django Tasks.

Two pieces, usable together or apart:

  * a management command that runs multiple async tasks concurrently using asyncio TaskGroup,
    optimized for I/O-bound tasks like API calls;
  * a periodic scheduler declared in code, for the recurring work Django Tasks has no answer for
    (Django owns task definition, queuing, and results — cadence and execution are left to the
    worker layer).

Usage:
    python manage.py concurrent_worker --concurrency=3
    python manage.py concurrent_worker --concurrency=3 --with-scheduler
    python manage.py task_scheduler

    from django_tasks import task
    from django_tasks_concurrent import periodic

    @periodic(cron="*/5 * * * *")
    @task()
    def cleanup_foobar(timestamp: int):
        ...
"""

__version__ = "0.4.0"


def __getattr__(name: str):
    """Lazy import to avoid loading Django models at import time."""
    if name == "ConcurrentWorker":
        from django_tasks_concurrent.worker import ConcurrentWorker  # noqa: PLC0415

        return ConcurrentWorker
    if name in {"periodic", "PeriodicTask", "registered_periodic_tasks"}:
        # The module is periodic_tasks, NOT periodic: a submodule named `periodic` would be set as an
        # attribute of this package on first import and shadow the decorator, so
        # `from django_tasks_concurrent import periodic` would hand back a module you can't call.
        from django_tasks_concurrent import periodic_tasks  # noqa: PLC0415

        return getattr(periodic_tasks, name)
    if name == "Scheduler":
        from django_tasks_concurrent.scheduler import Scheduler  # noqa: PLC0415

        return Scheduler
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConcurrentWorker",
    "PeriodicTask",
    "Scheduler",
    "__version__",
    "periodic",
    "registered_periodic_tasks",
]
