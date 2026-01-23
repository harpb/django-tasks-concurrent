"""
Tests for the concurrent_worker management command.
"""

from io import StringIO
from unittest.mock import MagicMock, patch

from django.core.management import call_command


class TestConcurrentWorkerCommand:
    """Tests for concurrent_worker management command."""

    def test_command_default_arguments(self):
        """Command uses default arguments when none provided."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                mock_instance = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", stdout=out)

                mock_worker.assert_called_once_with(
                    concurrency=3,
                    interval=1.0,
                    queue_name="default",
                    backend_name="default",
                )

    def test_command_custom_concurrency(self):
        """Command accepts custom concurrency."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                mock_instance = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", concurrency=5, stdout=out)

                assert mock_worker.call_args[1]["concurrency"] == 5

    def test_command_custom_interval(self):
        """Command accepts custom polling interval."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                mock_instance = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", interval=0.5, stdout=out)

                assert mock_worker.call_args[1]["interval"] == 0.5

    def test_command_custom_queue_name(self):
        """Command accepts custom queue name."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                mock_instance = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", queue_name="high-priority", stdout=out)

                assert mock_worker.call_args[1]["queue_name"] == "high-priority"

    def test_command_custom_backend(self):
        """Command accepts custom backend name."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                mock_instance = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", backend="secondary", stdout=out)

                assert mock_worker.call_args[1]["backend_name"] == "secondary"

    def test_command_output_message(self):
        """Command outputs startup message."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker"):
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run"):
                out = StringIO()
                call_command("concurrent_worker", concurrency=3, stdout=out)

                output = out.getvalue()
                assert "Starting concurrent worker" in output
                assert "concurrency=3" in output

    def test_command_runs_worker(self):
        """Command runs the worker via asyncio.run."""
        with patch("django_tasks_concurrent.management.commands.concurrent_worker.ConcurrentWorker") as mock_worker:
            with patch("django_tasks_concurrent.management.commands.concurrent_worker.asyncio.run") as mock_run:
                mock_instance = MagicMock()
                mock_instance.run = MagicMock()
                mock_worker.return_value = mock_instance

                out = StringIO()
                call_command("concurrent_worker", stdout=out)

                mock_run.assert_called_once_with(mock_instance.run())
