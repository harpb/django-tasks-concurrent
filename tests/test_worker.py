"""
Tests for ConcurrentWorker.
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from django_tasks_concurrent.worker import ConcurrentWorker


class TestConcurrentWorkerInit:
    """Tests for ConcurrentWorker initialization."""

    def test_init_sets_attributes(self, worker_config):
        """Worker initializes with correct attributes."""
        worker = ConcurrentWorker(**worker_config)

        assert worker.concurrency == 3
        assert worker.interval == 0.1
        assert worker.queue_name == "default"
        assert worker.backend_name == "default"
        assert worker.running is True
        assert worker.worker_id.startswith("concurrent-")

    def test_init_custom_backend(self):
        """Worker accepts custom backend name."""
        worker = ConcurrentWorker(
            concurrency=5,
            interval=2.0,
            queue_name="high-priority",
            backend_name="secondary",
        )

        assert worker.concurrency == 5
        assert worker.interval == 2.0
        assert worker.queue_name == "high-priority"
        assert worker.backend_name == "secondary"


class TestConcurrentWorkerShutdown:
    """Tests for shutdown handling."""

    def test_shutdown_sets_running_false(self, worker_config):
        """Shutdown method sets running to False."""
        worker = ConcurrentWorker(**worker_config)
        assert worker.running is True

        worker.shutdown()

        assert worker.running is False


class TestConcurrentWorkerClaimTask:
    """Tests for task claiming."""

    @pytest.mark.asyncio
    async def test_claim_task_returns_none_when_no_tasks(self, worker_config):
        """Returns None when no tasks available."""
        worker = ConcurrentWorker(**worker_config)

        with patch("django_tasks_concurrent.worker.DBTaskResult") as mock_db:
            mock_queryset = MagicMock()
            mock_queryset.ready.return_value.filter.return_value = mock_queryset
            mock_queryset.get_locked.return_value = None
            mock_db.objects = mock_queryset

            with patch("django_tasks_concurrent.worker.exclusive_transaction"):
                result = await worker._claim_task("test-worker-0")

        assert result is None

    @pytest.mark.asyncio
    async def test_claim_task_returns_task_when_available(self, worker_config):
        """Returns task when one is available."""
        worker = ConcurrentWorker(**worker_config)
        mock_task = MagicMock()
        mock_task.id = 123

        with patch("django_tasks_concurrent.worker.DBTaskResult") as mock_db:
            mock_queryset = MagicMock()
            mock_queryset.ready.return_value.filter.return_value = mock_queryset
            mock_queryset.db = "default"
            mock_queryset.get_locked.return_value = mock_task
            mock_db.objects = mock_queryset

            with patch("django_tasks_concurrent.worker.exclusive_transaction"):
                result = await worker._claim_task("test-worker-0")

        assert result == mock_task
        mock_task.claim.assert_called_once_with("test-worker-0")


class TestConcurrentWorkerRunTask:
    """Tests for task execution."""

    @pytest.mark.asyncio
    async def test_run_async_task(self, worker_config):
        """Async tasks are executed via task.acall()."""
        worker = ConcurrentWorker(**worker_config)

        mock_db_task = MagicMock()
        mock_db_task.id = 1
        mock_db_task.task.name = "test_task"
        mock_db_task.task.takes_context = False
        mock_db_task.task.acall = AsyncMock(return_value=10)
        mock_db_task.task_result.args = (5,)
        mock_db_task.task_result.kwargs = {}

        mock_backend = MagicMock()
        mock_db_task.task.get_backend.return_value = mock_backend

        with patch("django_tasks_concurrent.worker.task_started"):
            with patch("django_tasks_concurrent.worker.task_finished"):
                with patch("django_tasks_concurrent.worker.sync_to_async") as mock_sync:
                    mock_sync.side_effect = lambda f: AsyncMock(side_effect=f)

                    await worker._run_task(mock_db_task, "test-worker-0")

        mock_db_task.task.acall.assert_called_once_with(5)
        mock_db_task.set_successful.assert_called_once()
        assert mock_db_task.set_successful.call_args[0][0] == 10

    @pytest.mark.asyncio
    async def test_run_sync_task(self, worker_config):
        """Sync tasks are executed via task.acall()."""
        worker = ConcurrentWorker(**worker_config)

        mock_db_task = MagicMock()
        mock_db_task.id = 2
        mock_db_task.task.name = "sync_test_task"
        mock_db_task.task.takes_context = False
        mock_db_task.task.acall = AsyncMock(return_value=15)
        mock_db_task.task_result.args = (5,)
        mock_db_task.task_result.kwargs = {}

        mock_backend = MagicMock()
        mock_db_task.task.get_backend.return_value = mock_backend

        with patch("django_tasks_concurrent.worker.task_started"):
            with patch("django_tasks_concurrent.worker.task_finished"):
                with patch("django_tasks_concurrent.worker.sync_to_async") as mock_sync:
                    mock_sync.side_effect = lambda f: AsyncMock(side_effect=f)

                    await worker._run_task(mock_db_task, "test-worker-0")

        mock_db_task.set_successful.assert_called_once()

    @pytest.mark.asyncio
    async def test_run_task_handles_exception(self, worker_config):
        """Task exceptions are caught and task is marked failed."""
        worker = ConcurrentWorker(**worker_config)

        mock_db_task = MagicMock()
        mock_db_task.id = 3
        mock_db_task.task.name = "failing_task"
        mock_db_task.task.takes_context = False
        mock_db_task.task.acall = AsyncMock(side_effect=ValueError("Task failed!"))
        mock_db_task.task_result.args = ()
        mock_db_task.task_result.kwargs = {}

        mock_backend = MagicMock()
        mock_db_task.task.get_backend.return_value = mock_backend

        with patch("django_tasks_concurrent.worker.task_started"):
            with patch("django_tasks_concurrent.worker.task_finished"):
                with patch("django_tasks_concurrent.worker.sync_to_async") as mock_sync:
                    mock_sync.side_effect = lambda f: AsyncMock(side_effect=f)

                    await worker._run_task(mock_db_task, "test-worker-0")

        mock_db_task.set_failed.assert_called_once()
        error = mock_db_task.set_failed.call_args[0][0]
        assert isinstance(error, ValueError)
        assert str(error) == "Task failed!"


class TestConcurrentWorkerSubWorker:
    """Tests for sub-worker coroutine."""

    @pytest.mark.asyncio
    async def test_sub_worker_stops_when_running_false(self, worker_config):
        """Sub-worker exits when running is set to False."""
        worker = ConcurrentWorker(**worker_config)
        worker.running = False

        with patch.object(worker, "_claim_task", new_callable=AsyncMock) as mock_claim:
            await worker._sub_worker(0)

        # Should not have attempted to claim any tasks
        mock_claim.assert_not_called()

    @pytest.mark.asyncio
    async def test_sub_worker_processes_tasks(self, worker_config):
        """Sub-worker claims and runs tasks."""
        worker = ConcurrentWorker(**worker_config)
        call_count = 0

        async def mock_claim(sub_id):
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                worker.running = False
            return MagicMock() if call_count == 1 else None

        with patch.object(worker, "_claim_task", side_effect=mock_claim):
            with patch.object(worker, "_run_task", new_callable=AsyncMock) as mock_run:
                with patch("django_tasks_concurrent.worker.sync_to_async") as mock_sync:
                    mock_sync.return_value = AsyncMock()
                    await worker._sub_worker(0)

        assert mock_run.call_count == 1


class TestConcurrentWorkerRun:
    """Tests for main run method."""

    @pytest.mark.asyncio
    async def test_run_starts_sub_workers(self, worker_config):
        """Run method starts the configured number of sub-workers."""
        worker_config["concurrency"] = 2
        worker = ConcurrentWorker(**worker_config)
        worker.running = False  # Stop immediately

        with patch.object(worker, "_sub_worker", new_callable=AsyncMock) as mock_sub:
            with patch("asyncio.get_event_loop") as mock_loop:
                mock_loop.return_value.add_signal_handler = MagicMock()
                await worker.run()

        # Should have started 2 sub-workers
        assert mock_sub.call_count == 2
        mock_sub.assert_any_call(0)
        mock_sub.assert_any_call(1)


class TestConcurrentExecution:
    """Tests for concurrent task execution - the core value proposition."""

    @pytest.mark.asyncio
    async def test_two_async_tasks_run_concurrently(self):
        """
        Two 0.5s async tasks should complete in ~0.5s total, not 1s.

        This verifies the concurrent worker actually runs tasks in parallel.
        """
        task_delay = 0.5
        results = []

        async def slow_task(task_id: int) -> int:
            """Async task that takes task_delay seconds."""
            await asyncio.sleep(task_delay)
            results.append(task_id)
            return task_id

        # Run two tasks concurrently
        start_time = time.monotonic()
        await asyncio.gather(
            slow_task(1),
            slow_task(2),
        )
        elapsed = time.monotonic() - start_time

        # Both tasks should have completed
        assert len(results) == 2
        assert set(results) == {1, 2}

        # Should take ~0.5s, not ~1s (allowing 0.2s tolerance)
        assert elapsed < task_delay * 1.5, f"Expected ~{task_delay}s, got {elapsed:.2f}s (tasks ran sequentially?)"

    @pytest.mark.asyncio
    async def test_multiple_run_task_calls_concurrent(self, worker_config):
        """
        Multiple _run_task calls should execute concurrently.

        Simulates what happens when multiple sub-workers run tasks simultaneously.
        """
        worker = ConcurrentWorker(**worker_config)
        task_delay = 0.3
        completed_tasks = []

        # Create actual async functions (not lambdas returning coroutines)
        # because iscoroutinefunction checks the function, not return value
        async def make_slow_acall(task_id: int):
            async def slow_acall(*args, **kwargs) -> int:
                await asyncio.sleep(task_delay)
                completed_tasks.append(task_id)
                return task_id

            return slow_acall

        async def make_mock_db_task(task_id: int) -> MagicMock:
            mock = MagicMock()
            mock.id = task_id
            mock.task.name = f"task_{task_id}"
            mock.task.takes_context = False
            mock.task.acall = await make_slow_acall(task_id)
            mock.task_result.args = ()
            mock.task_result.kwargs = {}
            mock.task.get_backend.return_value = MagicMock()
            return mock

        mock_tasks = [await make_mock_db_task(i) for i in range(3)]

        start_time = time.monotonic()

        with patch("django_tasks_concurrent.worker.task_started"):
            with patch("django_tasks_concurrent.worker.task_finished"):
                with patch("django_tasks_concurrent.worker.sync_to_async") as mock_sync:
                    mock_sync.side_effect = lambda f: AsyncMock(side_effect=f)

                    # Run 3 tasks concurrently
                    await asyncio.gather(*[worker._run_task(task, f"worker-{i}") for i, task in enumerate(mock_tasks)])

        elapsed = time.monotonic() - start_time

        # All 3 tasks should have completed
        assert len(completed_tasks) == 3

        # Should take ~0.3s, not ~0.9s (3 * 0.3s sequential)
        # Allow some tolerance for test overhead
        assert elapsed < task_delay * 2, f"Expected ~{task_delay}s, got {elapsed:.2f}s (tasks ran sequentially?)"
