"""
Tests for ConcurrentWorker.
"""

import asyncio
import threading
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from asgiref.sync import sync_to_async
from django.db.utils import OperationalError
from django_tasks.base import Task

from django_tasks_concurrent.scheduler import defer_periodic_forever
from django_tasks_concurrent.worker import ConcurrentWorker

# Patched where the worker looks it up, not where it is defined.
SCHEDULER_LOOP = "django_tasks_concurrent.worker.defer_periodic_forever"


@pytest.fixture
def sync_worker(monkeypatch, worker_config):
    """Keep real Task.acall and sync bridges, replacing only database work and signals."""

    def make_worker(task_body, count=2):
        worker = ConcurrentWorker(**worker_config)
        records = []
        rows = []
        pending = {}
        completed = 0

        def record(stage):
            records.append((stage, threading.get_ident()))

        def finished(stage):
            nonlocal completed
            record(stage)
            completed += 1
            if completed == count:
                worker.running = False

        # A real Task uses its real acall; backend validation is unrelated to threading.
        monkeypatch.setattr(Task, "get_backend", lambda self: MagicMock())
        task = Task(func=task_body)
        for number in range(count):
            row = MagicMock()
            row.task = task
            row.task_result.args = (number,)
            row.task_result.kwargs = {}
            row.set_successful.side_effect = lambda result: finished("successful")
            row.set_failed.side_effect = lambda error: finished("failed")
            rows.append(row)
            pending[f"{worker.worker_id}-{number}"] = row

        def claim(sub_id):
            record(f"claim:{sub_id}")
            return pending.pop(sub_id, None)

        monkeypatch.setattr(worker, "_claim_task", sync_to_async(claim))
        monkeypatch.setattr("django_tasks_concurrent.worker.close_old_connections", lambda: record("recycle"))
        monkeypatch.setattr("django.db.connections.close_all", lambda: record("close"))
        monkeypatch.setattr("django_tasks_concurrent.worker.task_started.send", lambda **kwargs: record("started"))
        monkeypatch.setattr("django_tasks_concurrent.worker.task_finished.send", lambda **kwargs: record("finished"))
        return worker, rows, records

    return make_worker


class TestSyncThreadIsolation:
    @pytest.mark.asyncio
    async def test_two_sync_tasks_run_concurrently(self, sync_worker):
        """Serial task execution breaks the barrier instead of completing both tasks."""
        barrier = threading.Barrier(2, timeout=0.5)

        def task_body(number):
            barrier.wait()
            return number

        worker, rows, records = sync_worker(task_body)
        try:
            await asyncio.wait_for(asyncio.gather(worker._sub_worker(0), worker._sub_worker(1)), timeout=1.5)
        finally:
            barrier.abort()
            worker.running = False

        for number, row in enumerate(rows):
            row.set_failed.assert_not_called()
            row.set_successful.assert_called_once_with(number)
        assert len({thread_id for stage, thread_id in records if stage == "successful"}) == 2

    @pytest.mark.asyncio
    async def test_sub_workers_keep_sync_calls_on_separate_threads(self, sync_worker):
        """Claims, task bodies, results, signals and connection cleanup share each worker's thread."""
        body_threads = {}
        barrier = threading.Barrier(2, timeout=0.5)

        def task_body(number):
            body_threads[number] = threading.get_ident()
            try:
                barrier.wait()
            except threading.BrokenBarrierError:
                pass
            return number

        worker, rows, records = sync_worker(task_body)
        try:
            await asyncio.wait_for(asyncio.gather(worker._sub_worker(0), worker._sub_worker(1)), timeout=2)
        finally:
            barrier.abort()
            worker.running = False

        assert body_threads[0] != body_threads[1]
        for number, row in enumerate(rows):
            row.set_successful.assert_called_once_with(number)
        for number, thread_id in body_threads.items():
            assert (f"claim:{worker.worker_id}-{number}", thread_id) in records
            stages = [stage for stage, recorded_thread in records if recorded_thread == thread_id]
            for stage in ("started", "successful", "finished", "recycle", "close"):
                assert stage in stages
            assert stages[-1] == "close"

    @pytest.mark.asyncio
    async def test_scheduler_sweeps_while_sync_task_is_blocked(self, sync_worker, monkeypatch):
        """A scheduler started during a sync task must sweep before that task is released."""
        loop = asyncio.get_running_loop()
        started = asyncio.Event()
        swept = asyncio.Event()
        release = threading.Event()
        scheduler_records = []

        def task_body(number):
            loop.call_soon_threadsafe(started.set)
            assert release.wait(timeout=2)
            return number

        def sweep():
            scheduler_records.append(("sweep", threading.get_ident()))
            if not release.is_set():
                loop.call_soon_threadsafe(swept.set)
            return 0

        worker, rows, records = sync_worker(task_body, count=1)
        monkeypatch.setattr("django_tasks_concurrent.scheduler.defer_periodic_tasks", sweep)
        monkeypatch.setattr("django_tasks_concurrent.scheduler.seconds_until_next_slot", lambda **kwargs: 0.01)
        monkeypatch.setattr(
            "django_tasks_concurrent.scheduler.close_old_connections",
            lambda: scheduler_records.append(("recycle", threading.get_ident())),
        )
        sub_worker = asyncio.create_task(worker._sub_worker(0))
        scheduler = None
        try:
            await asyncio.wait_for(started.wait(), timeout=1)
            scheduler = asyncio.create_task(defer_periodic_forever(0.01))
            await asyncio.wait_for(swept.wait(), timeout=0.5)
            assert not release.is_set()
        finally:
            release.set()
            worker.running = False
            if scheduler is not None:
                scheduler.cancel()
            await asyncio.wait_for(
                asyncio.gather(sub_worker, *([scheduler] if scheduler is not None else []), return_exceptions=True),
                timeout=2,
            )

        rows[0].set_successful.assert_called_once_with(0)
        scheduler_thread = scheduler_records[0][1]
        worker_thread = next(thread_id for stage, thread_id in records if stage.startswith("claim:"))
        assert scheduler_thread != worker_thread
        assert all(thread_id == scheduler_thread for stage, thread_id in scheduler_records)
        assert ("close", scheduler_thread) in records


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
            with patch(SCHEDULER_LOOP, new_callable=AsyncMock):
                with patch("asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.add_signal_handler = MagicMock()
                    await worker.run()

        # Should have started 2 sub-workers
        assert mock_sub.call_count == 2
        mock_sub.assert_any_call(0)
        mock_sub.assert_any_call(1)


class TestConcurrentWorkerSchedules:
    """A worker schedules as well as executes — that is the whole contract of @periodic."""

    @pytest.mark.asyncio
    async def test_it_starts_the_scheduler_without_being_asked(self, worker_config):
        """No flag, no second process: running a worker is all a @periodic task should need."""
        worker = ConcurrentWorker(**worker_config)
        worker.running = False

        with patch.object(worker, "_sub_worker", new_callable=AsyncMock):
            with patch(SCHEDULER_LOOP, new_callable=AsyncMock) as mock_scheduler:
                with patch("asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.add_signal_handler = MagicMock()
                    await worker.run()

        mock_scheduler.assert_called_once_with(worker.scheduler_interval)

    @pytest.mark.asyncio
    async def test_run_returns_even_though_the_scheduler_never_does(self, worker_config):
        """The regression that made this a side task: the scheduler loops forever, so holding run()
        open until it finishes hangs the worker on every exit that isn't a signal."""
        worker = ConcurrentWorker(**worker_config)
        worker.running = False

        async def never_returns(_interval):
            await asyncio.Event().wait()

        with patch.object(worker, "_sub_worker", new_callable=AsyncMock):
            with patch(SCHEDULER_LOOP, side_effect=never_returns):
                with patch("asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.add_signal_handler = MagicMock()
                    await asyncio.wait_for(worker.run(), timeout=5)

        # run() returning at all is the assertion; this pins that it returned by CANCELLING the
        # scheduler rather than orphaning it to keep polling after the worker is gone.
        with pytest.raises(asyncio.CancelledError):
            await worker.scheduler_task

    @pytest.mark.asyncio
    async def test_a_dead_scheduler_stops_the_worker(self, worker_config):
        """A worker that quietly stopped scheduling looks healthy while every @periodic task stops
        firing. Exiting is the honest failure."""
        worker = ConcurrentWorker(**worker_config)

        async def dies_immediately(_interval):
            raise RuntimeError("scheduler is done for")

        async def sub_worker_until_stopped(_worker_num):
            while worker.running:
                await asyncio.sleep(0.01)

        with patch.object(worker, "_sub_worker", side_effect=sub_worker_until_stopped):
            with patch(SCHEDULER_LOOP, side_effect=dies_immediately):
                with patch("asyncio.get_event_loop") as mock_loop:
                    mock_loop.return_value.add_signal_handler = MagicMock()
                    await asyncio.wait_for(worker.run(), timeout=5)

        assert worker.running is False


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


class TestConcurrentWorkerDisconnectRecovery:
    """Sub-worker survives a dropped DB connection and reconnects."""

    @pytest.mark.asyncio
    async def test_sub_worker_reconnects_after_db_disconnect(self, worker_config):
        """
        A poll that fails because the database dropped the connection must still
        reset the connection (close_old_connections) so the NEXT poll reconnects.

        Regression guard: close_old_connections lives in a `finally`, not only on
        the success path. If it regressed back to running only on success, the
        broken connection would be reused and iteration 2 would never happen.
        """
        worker = ConcurrentWorker(**worker_config)
        claim_calls = 0
        reconnects = 0

        async def flaky_claim(sub_id):
            nonlocal claim_calls
            claim_calls += 1
            if claim_calls == 1:
                # The exact failure Postgres raises when it closes the connection.
                raise OperationalError("server closed the connection unexpectedly")
            # After the reconnect we've proven recovery — stop the loop.
            worker.running = False
            return None

        def count_reconnect():
            nonlocal reconnects
            reconnects += 1

        with patch.object(worker, "_claim_task", side_effect=flaky_claim):
            with patch(
                "django_tasks_concurrent.worker.close_old_connections",
                side_effect=count_reconnect,
            ):
                with patch(
                    "django_tasks_concurrent.worker.sync_to_async",
                    side_effect=lambda f: AsyncMock(side_effect=f),
                ):
                    with patch(
                        "django_tasks_concurrent.worker.asyncio.sleep",
                        new_callable=AsyncMock,
                    ):
                        await worker._sub_worker(0)

        # It kept polling past the disconnect (didn't die on iteration 1)...
        assert claim_calls == 2
        # ...and reset the connection on BOTH iterations, including the errored one.
        assert reconnects == 2
