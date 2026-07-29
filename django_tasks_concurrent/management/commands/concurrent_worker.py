"""
Management command to run the concurrent async worker.

Usage:
    python manage.py concurrent_worker --concurrency=3
    python manage.py concurrent_worker --concurrency=5 --interval=0.5
"""

import asyncio
import logging
from argparse import ArgumentParser

from django.conf import settings
from django.core.management.base import BaseCommand

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

    def handle(
        self,
        *,
        concurrency: int,
        interval: float,
        queue_name: str,
        backend_name: str,
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

        worker = ConcurrentWorker(
            concurrency=concurrency,
            interval=interval,
            queue_name=queue_name,
            backend_name=backend_name,
        )
        asyncio.run(worker.run())
