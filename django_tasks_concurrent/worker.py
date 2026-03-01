"""
Concurrent async worker for Django Tasks.

Runs multiple async tasks concurrently using asyncio TaskGroup.
While one task awaits I/O, others can execute.
"""

import asyncio
import logging
import signal

from asgiref.sync import sync_to_async
from django.db import close_old_connections
from django.db.utils import OperationalError
from django_tasks.base import TaskContext
from django_tasks.signals import task_finished, task_started
from django_tasks.utils import get_random_id
from django_tasks_db.models import DBTaskResult
from django_tasks_db.utils import exclusive_transaction

logger = logging.getLogger("django_tasks_concurrent")


class ConcurrentWorker:
    """
    Async worker that runs multiple tasks concurrently.

    Uses asyncio TaskGroup to manage N sub-worker coroutines.
    Each sub-worker claims and runs tasks independently.

    Args:
        concurrency: Number of concurrent sub-workers
        interval: Polling interval in seconds when no tasks available
        queue_name: Name of the task queue to process
        backend_name: Django Tasks backend name (default: "default")

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
    ):
        self.concurrency = concurrency
        self.interval = interval
        self.queue_name = queue_name
        self.backend_name = backend_name
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

        try:
            async with asyncio.TaskGroup() as tg:
                for i in range(self.concurrency):
                    tg.create_task(self._sub_worker(i))
        except* Exception as eg:
            for exc in eg.exceptions:
                logger.error(f"Sub-worker error: {exc}")

        logger.info("Concurrent worker stopped")

    def shutdown(self) -> None:
        """Handle shutdown signal."""
        logger.info("Shutting down concurrent worker...")
        self.running = False

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

                # Clean up stale DB connections
                await sync_to_async(close_old_connections)()

            except Exception as e:
                logger.exception(f"Sub-worker {sub_id} error: {e}")
                await asyncio.sleep(self.interval)

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
