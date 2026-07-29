"""
Management command to run the concurrent async worker.

Usage:
    python manage.py concurrent_worker --concurrency=3
    python manage.py concurrent_worker --concurrency=5 --interval=0.5
"""

import asyncio
import logging
from argparse import ArgumentParser, BooleanOptionalAction

from django.conf import settings
from django.core.management.base import BaseCommand

from django_tasks_concurrent.scheduler import Scheduler
from django_tasks_concurrent.worker import ConcurrentWorker

logger = logging.getLogger("django_tasks_concurrent")


class Command(BaseCommand):
    help = "Run concurrent async task worker for Django Tasks"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--concurrency",
            type=int,
            default=3,
            help="Number of concurrent workers (default: 3)",
        )
        parser.add_argument(
            "--interval",
            type=float,
            default=1.0,
            help="Polling interval in seconds when no tasks (default: 1.0)",
        )
        parser.add_argument(
            "--queue-name",
            type=str,
            default="",
            help="Queue name (default: settings.TASK_QUEUE_NAME or 'default')",
        )
        parser.add_argument(
            "--backend",
            type=str,
            default="default",
            dest="backend_name",
            help="The backend to operate on (default: 'default')",
        )
        parser.add_argument(
            "--scheduler",
            action=BooleanOptionalAction,
            default=True,
            dest="with_scheduler",
            help="Run the @periodic scheduler in this process (default: on; --no-scheduler disables)",
        )
        parser.add_argument(
            "--scheduler-interval",
            type=float,
            default=15.0,
            help="Seconds between schedule polls when --with-scheduler is set (default: 15.0)",
        )

    def handle(
        self,
        *,
        concurrency: int,
        interval: float,
        queue_name: str,
        backend_name: str,
        with_scheduler: bool,
        scheduler_interval: float,
        verbosity: int,
        **options,
    ):
        # Configure logging based on verbosity
        if verbosity == 0:
            logger.setLevel(logging.CRITICAL)
        elif verbosity == 1:
            logger.setLevel(logging.INFO)
        else:
            logger.setLevel(logging.DEBUG)

        if not logger.hasHandlers():
            logger.addHandler(logging.StreamHandler(self.stdout))

        queue_name = queue_name or getattr(settings, "TASK_QUEUE_NAME", "default")
        self.stdout.write(f"Starting concurrent worker (concurrency={concurrency}, queue={queue_name})")

        scheduler = None
        if with_scheduler:
            scheduler = Scheduler(interval=scheduler_interval)
            self.stdout.write(f"Periodic scheduler enabled (poll every {scheduler_interval}s)")

        worker = ConcurrentWorker(
            concurrency=concurrency,
            interval=interval,
            queue_name=queue_name,
            backend_name=backend_name,
            scheduler=scheduler,
        )
        asyncio.run(worker.run())
