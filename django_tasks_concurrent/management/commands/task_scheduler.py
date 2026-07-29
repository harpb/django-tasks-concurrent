"""
Management command to run the periodic scheduler.

Standalone so the scheduler stays worker-agnostic: it only enqueues, so it pairs with whatever worker
you already run (`concurrent_worker`, Django's `db_worker`, or a wrapper around either). If you run
`concurrent_worker` and don't want a second process, use its `--with-scheduler` flag instead.

Usage:
    python manage.py task_scheduler
    python manage.py task_scheduler --interval=5
    python manage.py task_scheduler --once
    python manage.py task_scheduler --list
"""

import asyncio
import logging
from argparse import ArgumentParser

from django.core.management.base import BaseCommand

from django_tasks_concurrent.periodic_tasks import registered_periodic_tasks
from django_tasks_concurrent.scheduler import Scheduler, defer_periodic_tasks

logger = logging.getLogger("django_tasks_concurrent")


class Command(BaseCommand):
    help = "Run the periodic scheduler for Django Tasks — queues @periodic tasks when their slot arrives"

    def add_arguments(self, parser: ArgumentParser) -> None:
        parser.add_argument(
            "--interval",
            type=float,
            default=15.0,
            help="Seconds between polls (default: 15.0)",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="Sweep once and exit, instead of polling — for a smoke test or an external timer",
        )
        parser.add_argument(
            "--list",
            action="store_true",
            dest="list_schedules",
            help="Print the declared schedules and exit — the fastest way to check @periodic registered",
        )

    def handle(self, *, interval: float, once: bool, list_schedules: bool, verbosity: int, **options):
        if verbosity == 0:
            logger.setLevel(logging.CRITICAL)
        elif verbosity == 1:
            logger.setLevel(logging.INFO)
        else:
            logger.setLevel(logging.DEBUG)

        if not logger.hasHandlers():
            logger.addHandler(logging.StreamHandler(self.stdout))

        schedules = registered_periodic_tasks()

        if list_schedules:
            if not schedules:
                self.stdout.write("No @periodic tasks registered.")
                return
            for entry in schedules:
                self.stdout.write(entry.title)
            return

        if not schedules:
            # Worth saying out loud: the usual cause is a tasks module nobody imports, and a silent
            # scheduler polling an empty registry looks identical to one that is working.
            self.stdout.write(self.style.WARNING("No @periodic tasks registered — the scheduler has nothing to do."))

        if once:
            fired = defer_periodic_tasks()
            self.stdout.write(f"Queued {fired} periodic task(s)")
            return

        self.stdout.write(f"Starting scheduler (interval={interval}s, {len(schedules)} schedule(s))")
        asyncio.run(Scheduler(interval=interval).run())
