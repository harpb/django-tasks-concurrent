"""
Django's ``db_worker``, with the two things a deployment always ends up adding.

Run this instead of ``db_worker`` when you want the plain sequential worker rather than
``concurrent_worker``, but still want it to schedule ``@periodic`` tasks and to survive a database
that blinks. It delegates to ``db_worker`` for the actual work — this is a wrapper, not a fork.

Usage:
    python manage.py task_worker
    python manage.py task_worker --no-reload
    python manage.py task_worker --no-scheduler --queue-name=high-priority
"""

import logging
from argparse import ArgumentParser, BooleanOptionalAction

from django.conf import settings
from django.core.management import call_command
from django.core.management.base import BaseCommand

from django_tasks_concurrent.db_worker import install_resilient_run
from django_tasks_concurrent.scheduler import run_scheduler_thread

logger = logging.getLogger("django_tasks_concurrent")


class Command(BaseCommand):
    help = "Run db_worker with the @periodic scheduler and reconnect-on-disconnect"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--queue-name",
            type=str,
            default="",
            help="Queue name (default: settings.TASK_QUEUE_NAME or 'default')",
        )
        parser.add_argument(
            "--reload",
            action=BooleanOptionalAction,
            default=None,
            help="Restart the worker on code changes (default: settings.DEBUG, same as db_worker).",
        )
        parser.add_argument(
            "--scheduler",
            action=BooleanOptionalAction,
            default=True,
            help="Also run the @periodic scheduler on a daemon thread (default: on).",
        )
        parser.add_argument(
            "--scheduler-interval",
            type=float,
            default=15.0,
            help="Ceiling on one @periodic scheduler sleep, in seconds (default: 15.0)",
        )

    def handle(self, *, queue_name: str, reload: bool | None, scheduler: bool, scheduler_interval: float, **options):
        """
        ``--scheduler`` is a flag here, unlike ``concurrent_worker`` where scheduling is unconditional.
        The difference is ownership: this command hands control to upstream ``db_worker`` and gets it
        back only at shutdown, so the schedule rides a daemon thread nobody supervises. Being able to
        say "not this process" is what lets you run several workers and schedule from only one.

        ``--no-reload`` mirrors runserver's ``--noreload``. Autoreload watches the whole project, so a
        worker restarts on edits to code it never runs — and a half-saved file can kill it outright.
        """
        install_resilient_run()

        queue_name = queue_name or getattr(settings, "TASK_QUEUE_NAME", "default")
        self.stdout.write(f"Starting task worker for queue: {queue_name}")

        if scheduler:
            self.start_scheduler(scheduler_interval)

        # Passed through only when set, so db_worker keeps applying its own settings.DEBUG default.
        reload_options = {} if reload is None else {"reload": reload}
        call_command("db_worker", queue_name=queue_name, **reload_options)

    def start_scheduler(self, interval: float) -> None:
        """Start the scheduler, and start the worker anyway if it can't.

        The opposite call from ``ConcurrentWorker``, which stops when its scheduler dies. There the
        scheduler is part of the worker; here it is a passenger on a worker that is upstream code and
        knows nothing about it, so it must not be able to prevent that worker from running.
        """
        try:
            run_scheduler_thread(interval)
            self.stdout.write(f"Periodic scheduler running (every {interval:.0f}s)")
        except Exception as scheduler_error:
            logger.warning(f"Periodic scheduler did not start ({scheduler_error=}).")
