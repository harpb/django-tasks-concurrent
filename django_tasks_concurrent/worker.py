"""
Concurrent async worker for Django Tasks.

Runs multiple async tasks concurrently using asyncio TaskGroup.
While one task awaits I/O, others can execute.

``run_worker`` / ``run_worker_async`` are the entry points; the management command is a thin shell
over the first. Both take the same options, and both schedule ``@periodic`` tasks — see
``ConcurrentWorker``.
"""

import asyncio
import logging
import signal
from typing import TypedDict, Unpack

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import close_old_connections
from django.db.utils import OperationalError
from django_tasks.base import TaskContext
from django_tasks.signals import task_finished, task_started
from django_tasks.utils import get_random_id
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction

from django_tasks_concurrent.scheduler import defer_periodic_forever

logger = logging.getLogger("django_tasks_concurrent")


class ConcurrentWorker:
    """
    Async worker that runs multiple tasks concurrently.

    Uses asyncio TaskGroup to manage N sub-worker coroutines.
    Each sub-worker claims and runs tasks independently.

    Runs the @periodic scheduler too, as a side task beside the sub-workers. Not optional and not a
    flag: the scheduler only enqueues, so it costs one indexed query per wake-up, and a worker is
    exactly the thing a schedule needs in order to mean anything. With no schedules declared it does
    nothing at all, so there is no case where making people opt in would have saved them something.

    Args:
        concurrency: Number of concurrent sub-workers
        interval: Polling interval in seconds when no tasks available
        queue_name: Name of the task queue to process
        backend_name: Django Tasks backend name (default: "default")
        scheduler_interval: Ceiling on one scheduler sleep (default: 15.0). Not a poll period — the
            scheduler wakes for the next real slot, so this only bounds how long it may sleep.

    Example:
        worker = ConcurrentWorker(concurrency=3, interval=1.0, queue_name="default")
        asyncio.run(worker.run())
    """

    def __init__(
        self,
        concurrency: int,
        interval: float,
        queue_name: str,
        backend_name: str = "default",
        scheduler_interval: float = 15.0,
    ):
        self.concurrency = concurrency
        self.interval = interval
        self.queue_name = queue_name
        self.backend_name = backend_name
        self.scheduler_interval = scheduler_interval
        self.scheduler_task = None
        self.running = True
        self.worker_id = f"concurrent-{get_random_id()}"

    async def run(self) -> None:
        """Main entry point - start all sub-workers."""
        logger.info(
            f"Starting concurrent worker worker_id={self.worker_id} "
            f"concurrency={self.concurrency} queue={self.queue_name}"
        )

        # Handle shutdown signals
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.shutdown)

        # Deliberately OUTSIDE the sub-workers' task group. The scheduler never returns on its own,
        # and a task group waits for every member — so putting it inside means any end to the
        # sub-workers other than a signal hangs the worker forever on a loop with nothing left to
        # feed. It is a side task: it runs beside the real work and is cancelled when that work ends.
        self.scheduler_task = asyncio.create_task(
            defer_periodic_forever(self.scheduler_interval), name="periodic-scheduler"
        )
        self.scheduler_task.add_done_callback(self.on_scheduler_finished)

        try:
            async with asyncio.TaskGroup() as tg:
                for i in range(self.concurrency):
                    tg.create_task(self._sub_worker(i))
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error(f"Sub-worker error: {exc}")
        finally:
            self.scheduler_task.cancel()

        logger.info("Concurrent worker stopped")

    def on_scheduler_finished(self, scheduler_task) -> None:
        """Stop the worker if the scheduler died on its own.

        The loop swallows per-poll errors, so reaching here uncancelled means something unrecoverable
        — and a worker that has quietly stopped scheduling looks perfectly healthy from the outside
        while every ``@periodic`` task silently stops firing. Exiting is the honest failure.
        """
        if scheduler_task.cancelled():
            return
        scheduler_error = scheduler_task.exception()
        if scheduler_error is not None:
            logger.error(f"Periodic scheduler failed, stopping worker: {scheduler_error}")
            self.shutdown()

    def shutdown(self) -> None:
        """Handle shutdown signal."""
        logger.info("Shutting down concurrent worker...")
        self.running = False
        if self.scheduler_task is not None:
            # Sub-workers notice self.running and return; the scheduler is parked in a sleep and
            # would hold the task group open until its next wake-up, so it gets cancelled instead.
            self.scheduler_task.cancel()

    async def _sub_worker(self, worker_num: int) -> None:
        """
        Individual sub-worker coroutine.

        Polls for tasks, claims them, and runs them.
        Yields control on await points to allow other sub-workers to run.
        """
        sub_id = f"{self.worker_id}-{worker_num}"
        logger.debug(f"Sub-worker {sub_id} started")

        while self.running:
            try:
                task_result = await self._claim_task(sub_id)

                if task_result:
                    await self._run_task(task_result, sub_id)
                else:
                    # No tasks available - wait before polling again
                    await asyncio.sleep(self.interval)

            except Exception as e:
                logger.exception(f"Sub-worker {sub_id} error: {e}")
                await asyncio.sleep(self.interval)
            finally:
                # Reset stale/broken DB connections on EVERY iteration, including the
                # error path. If a poll failed because the database dropped the
                # connection (server restart, network blip), close_old_connections()
                # discards the dead connection so the next poll opens a fresh one.
                # Placed in `finally` deliberately: if this only ran on success, a
                # dropped connection would be reused every iteration and the sub-worker
                # would keep erroring without ever recovering.
                await sync_to_async(close_old_connections)()

        logger.debug(f"Sub-worker {sub_id} stopped")

    @sync_to_async
    def _claim_task(self, sub_id: str) -> DBTaskResult | None:
        """
        Claim a ready task from the queue.

        Runs in thread pool to avoid blocking event loop.
        Uses SELECT FOR UPDATE with skip_locked for safe concurrent access.
        """
        tasks = DBTaskResult.objects.ready().filter(
            backend_name=self.backend_name,
            queue_name=self.queue_name,
        )

        try:
            with exclusive_transaction(tasks.db):
                task_result = tasks.get_locked()
                if task_result:
                    task_result.claim(sub_id)
                    logger.info(f"Sub-worker {sub_id} claimed task {task_result.id}")
                    return task_result
        except OperationalError as e:
            # Ignore locked databases and keep trying
            if "is locked" not in str(e):
                raise
        return None

    async def _run_task(self, db_task_result: DBTaskResult, sub_id: str) -> None:
        """
        Execute a task - async tasks run natively, sync via thread pool.
        Uses task.acall() which handles both sync and async functions.
        """
        task = db_task_result.task
        task_result = db_task_result.task_result

        logger.info(f"Sub-worker {sub_id} running {task.name} (id={db_task_result.id})")

        # Send task_started signal
        backend_type = task.get_backend()
        await sync_to_async(task_started.send)(sender=backend_type, task_result=task_result)

        try:
            if task.takes_context:
                result = await task.acall(
                    TaskContext(task_result=task_result),
                    *task_result.args,
                    **task_result.kwargs,
                )
            else:
                result = await task.acall(*task_result.args, **task_result.kwargs)

            # Mark successful
            await sync_to_async(db_task_result.set_successful)(result)
            logger.info(f"Sub-worker {sub_id} completed task {db_task_result.id}")

            # Send task_finished signal
            await sync_to_async(task_finished.send)(sender=backend_type, task_result=db_task_result.task_result)

        except Exception as e:
            logger.exception(f"Sub-worker {sub_id} task {db_task_result.id} failed: {e}")
            await sync_to_async(db_task_result.set_failed)(e)

            # Send task_finished signal even on failure
            try:
                sender = type(db_task_result.task.get_backend())
                await sync_to_async(task_finished.send)(sender=sender, task_result=db_task_result.task_result)
            except Exception:
                logger.exception("Failed to send task_finished signal")


class WorkerOptions(TypedDict, total=False):
    """Every option the worker takes.

    One TypedDict shared by both entry points, so the sync and async forms cannot drift into
    documenting different surfaces.
    """

    concurrency: int
    interval: float
    queue_name: str
    backend_name: str
    scheduler_interval: float


async def run_worker_async(**options: Unpack[WorkerOptions]) -> None:
    """Run the worker until it is shut down. The real entry point; ``run_worker`` wraps it.

    Use this from code that already owns an event loop. ``queue_name`` defaults to
    ``settings.TASK_QUEUE_NAME`` so a deployment names its queue once, in settings, instead of at
    every call site.
    """
    worker = ConcurrentWorker(
        concurrency=options.get("concurrency", 3),
        interval=options.get("interval", 1.0),
        queue_name=options.get("queue_name") or getattr(settings, "TASK_QUEUE_NAME", "default"),
        backend_name=options.get("backend_name", "default"),
        scheduler_interval=options.get("scheduler_interval", 15.0),
    )
    await worker.run()


def run_worker(**options: Unpack[WorkerOptions]) -> None:
    """Synchronous version of ``run_worker_async`` — creates the event loop and runs the worker in it.

    This is what a management command or an entry-point script wants. Same options, same behaviour.
    """
    asyncio.run(run_worker_async(**options))
