"""
Tests for the resilient db_worker wrapper and the task_worker command.

The contract worth locking: a dropped connection is retried with a fresh one, a requested shutdown is
never retried over, and installing twice does not stack wrappers.
"""

from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.db.utils import InterfaceError, OperationalError
from django_tasks_db.management.commands.db_worker import Worker

from django_tasks_concurrent.db_worker import install_resilient_run

COMMAND_MODULE = "django_tasks_concurrent.management.commands.task_worker"
DB_WORKER_MODULE = "django_tasks_concurrent.db_worker"


@pytest.fixture(autouse=True)
def restore_worker_run():
    """install_resilient_run patches the CLASS, so every test has to put it back."""
    original_run = Worker.run
    yield
    Worker.run = original_run


def install_over(fake_run):
    """Install the wrapper over ``fake_run`` and hand back the wrapped ``Worker.run``."""
    Worker.run = fake_run
    install_resilient_run()
    return Worker.run


class TestInstallResilientRun:
    def test_it_retries_after_a_dropped_connection(self):
        calls = []

        def run(self):
            calls.append("run")
            if len(calls) == 1:
                raise OperationalError("server closed the connection unexpectedly")

        wrapped = install_over(run)

        with patch(f"{DB_WORKER_MODULE}.time.sleep") as sleep:
            with patch(f"{DB_WORKER_MODULE}.close_old_connections") as reconnect:
                wrapped(SimpleNamespace(running=True))

        assert calls == ["run", "run"]
        # The reconnect is the point — retrying on the same dead connection would just fail again.
        reconnect.assert_called_once()
        sleep.assert_called_once_with(1.0)

    def test_it_backs_off_exponentially_up_to_the_ceiling(self):
        attempts = []

        def run(self):
            attempts.append(1)
            if len(attempts) < 8:
                raise InterfaceError("connection already closed")

        wrapped = install_over(run)

        with patch(f"{DB_WORKER_MODULE}.time.sleep") as sleep:
            with patch(f"{DB_WORKER_MODULE}.close_old_connections"):
                wrapped(SimpleNamespace(running=True))

        waits = [call.args[0] for call in sleep.call_args_list]
        assert waits == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0]

    def test_a_shutdown_racing_the_error_is_not_retried_over(self):
        """`running` going false means somebody asked it to stop; reconnecting would ignore them."""
        worker = SimpleNamespace(running=True)

        def run(self):
            self.running = False
            raise OperationalError("connection lost during shutdown")

        wrapped = install_over(run)

        with pytest.raises(OperationalError):
            wrapped(worker)

    def test_a_clean_return_is_not_retried(self):
        calls = []

        wrapped = install_over(lambda self: calls.append("run"))
        wrapped(SimpleNamespace(running=True))

        assert calls == ["run"]

    def test_installing_twice_does_not_stack_wrappers(self):
        """Both the command and an inline worker may install it in one process."""
        wrapped = install_over(lambda self: None)
        install_resilient_run()

        assert Worker.run is wrapped


class TestTaskWorkerCommand:
    @pytest.fixture(autouse=True)
    def delegated_db_worker(self):
        with patch(f"{COMMAND_MODULE}.call_command") as call_db_worker:
            with patch(f"{COMMAND_MODULE}.install_resilient_run") as make_resilient:
                with patch(f"{COMMAND_MODULE}.run_scheduler_thread") as scheduler:
                    yield SimpleNamespace(
                        call_db_worker=call_db_worker, make_resilient=make_resilient, scheduler=scheduler
                    )

    def test_it_delegates_to_db_worker_with_the_settings_queue(self, delegated_db_worker):
        call_command("task_worker", stdout=StringIO())

        delegated_db_worker.call_db_worker.assert_called_once_with("db_worker", queue_name="default")
        delegated_db_worker.make_resilient.assert_called_once()
        delegated_db_worker.scheduler.assert_called_once_with(15.0)

    def test_reload_is_passed_through_only_when_asked(self, delegated_db_worker):
        """Unset means db_worker keeps applying its own settings.DEBUG default."""
        call_command("task_worker", "--no-reload", stdout=StringIO())

        assert delegated_db_worker.call_db_worker.call_args.kwargs["reload"] is False

    def test_scheduling_can_be_left_to_another_process(self, delegated_db_worker):
        """Several workers, one schedule — otherwise every worker sweeps the same slots."""
        call_command("task_worker", "--no-scheduler", stdout=StringIO())

        delegated_db_worker.scheduler.assert_not_called()
        delegated_db_worker.call_db_worker.assert_called_once()

    def test_the_worker_starts_even_if_the_scheduler_cannot(self, delegated_db_worker):
        """It is a passenger on upstream code that knows nothing about it."""
        delegated_db_worker.scheduler.side_effect = RuntimeError("no schedule for you")

        call_command("task_worker", stdout=StringIO())

        delegated_db_worker.call_db_worker.assert_called_once()
