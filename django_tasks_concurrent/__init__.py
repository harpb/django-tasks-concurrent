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
    python manage.py task_worker              # sequential db_worker, scheduling + reconnect

    from django_tasks_concurrent import run_worker, run_worker_async

    run_worker(concurrency=3)          # creates the event loop for you
    await run_worker_async(concurrency=3)

    from django_tasks import task
    from django_tasks_concurrent import periodic

    @periodic(cron="*/5 * * * *")
    @task()
    def cleanup_foobar(timestamp: int):
        ...

Running a worker is all it takes — it schedules as well as executes. Only a worker this package does
NOT own (Django's own ``db_worker``) needs the schedule started by hand, with
``scheduler.run_scheduler_thread()``.
"""

__version__ = "0.6.0"


def __getattr__(name: str):
    """Lazy import to avoid loading Django models at import time."""
    if name in {"ConcurrentWorker", "WorkerOptions", "run_worker", "run_worker_async"}:
        from django_tasks_concurrent import worker  # noqa: PLC0415

        return getattr(worker, name)
    if name == "install_resilient_run":
        from django_tasks_concurrent.db_worker import install_resilient_run  # noqa: PLC0415

        return install_resilient_run
    if name in {"periodic", "PeriodicTask", "registered_periodic_tasks"}:
        # The module is periodic_tasks, NOT periodic: a submodule named `periodic` would be set as an
        # attribute of this package on first import and shadow the decorator, so
        # `from django_tasks_concurrent import periodic` would hand back a module you can't call.
        from django_tasks_concurrent import periodic_tasks  # noqa: PLC0415

        return getattr(periodic_tasks, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "ConcurrentWorker",
    "PeriodicTask",
    "WorkerOptions",
    "__version__",
    "install_resilient_run",
    "periodic",
    "registered_periodic_tasks",
    "run_worker",
    "run_worker_async",
]
