"""
Tests for the concurrent_worker management command.

The command is a shell over ``run_worker`` — it parses flags and hands them over — so these lock the
translation from argv to options, not the worker's behaviour.
"""

from io import StringIO
from unittest.mock import patch

import pytest
from django.core.management import call_command

COMMAND_RUN_WORKER = "django_tasks_concurrent.management.commands.concurrent_worker.run_worker"


@pytest.fixture
def run_worker():
    with patch(COMMAND_RUN_WORKER) as mock_run_worker:
        yield mock_run_worker


class TestConcurrentWorkerCommand:
    """Tests for concurrent_worker management command."""

    def test_command_default_arguments(self, run_worker):
        """Command uses default arguments when none provided."""
        call_command("concurrent_worker", stdout=StringIO())

        options = run_worker.call_args.kwargs
        assert options["concurrency"] == 3
        assert options["interval"] == 1.0
        assert options["queue_name"] == "default"
        assert options["backend_name"] == "default"
        # Scheduling is not a flag — a worker schedules, full stop. Only its sleep ceiling is tunable.
        assert options["scheduler_interval"] == 15.0

    def test_command_custom_concurrency(self, run_worker):
        """Command accepts custom concurrency."""
        call_command("concurrent_worker", concurrency=5, stdout=StringIO())

        assert run_worker.call_args.kwargs["concurrency"] == 5

    def test_command_custom_interval(self, run_worker):
        """Command accepts custom polling interval."""
        call_command("concurrent_worker", interval=0.5, stdout=StringIO())

        assert run_worker.call_args.kwargs["interval"] == 0.5

    def test_command_custom_queue_name(self, run_worker):
        """Command accepts custom queue name."""
        call_command("concurrent_worker", queue_name="high-priority", stdout=StringIO())

        assert run_worker.call_args.kwargs["queue_name"] == "high-priority"

    def test_command_custom_backend(self, run_worker):
        """Command accepts custom backend name."""
        call_command("concurrent_worker", backend="secondary", stdout=StringIO())

        assert run_worker.call_args.kwargs["backend_name"] == "secondary"

    def test_command_custom_scheduler_interval(self, run_worker):
        """Command accepts a custom scheduler sleep ceiling."""
        call_command("concurrent_worker", scheduler_interval=60.0, stdout=StringIO())

        assert run_worker.call_args.kwargs["scheduler_interval"] == 60.0

    def test_command_output_message(self, run_worker):
        """Command outputs startup message."""
        out = StringIO()
        call_command("concurrent_worker", concurrency=3, stdout=out)

        output = out.getvalue()
        assert "Starting concurrent worker" in output
        assert "concurrency=3" in output

    def test_command_runs_worker(self, run_worker):
        """Command actually starts the worker."""
        call_command("concurrent_worker", stdout=StringIO())

        run_worker.assert_called_once()
