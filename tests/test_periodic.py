"""
Tests for the periodic scheduler.

The contract worth locking is at-most-once-per-slot: repeated sweeps inside one slot must queue the
task exactly once, a new slot must queue it again, a slot older than the max delay must be skipped
entirely, and no single broken schedule may stop the others from firing.
"""

import threading
from datetime import UTC, datetime, timedelta

import pytest
from django.test import override_settings
from django.utils import timezone
from django_tasks import task
from django_tasks_db.models import DBTaskResult

from django_tasks_concurrent import periodic
from django_tasks_concurrent.models import PeriodicDefer
from django_tasks_concurrent.periodic_tasks import PERIODIC_TASKS, PeriodicTask, registered_periodic_tasks
from django_tasks_concurrent.scheduler import (
    defer_periodic_tasks,
    prune_periodic_defers,
    run_scheduler_thread,
    seconds_until_next_slot,
)


@task()
def every_minute(timestamp: int) -> int:
    return timestamp


@task()
def with_extra_kwargs(timestamp: int, value: int = 0) -> int:
    return value


TASK_PATH = "tests.test_periodic.every_minute"


@pytest.fixture(autouse=True)
def clean_registry():
    """Each test declares its own schedules — the registry is module-level, so isolate it."""
    PERIODIC_TASKS.clear()
    yield
    PERIODIC_TASKS.clear()


def register_every_minute(**overrides):
    options = {"cron": "* * * * *"}
    options.update(overrides)
    periodic(**options)(every_minute)
    return registered_periodic_tasks()[-1]


class TestPeriodicDecorator:
    def test_it_returns_the_task_unchanged_so_enqueue_still_works(self):
        """@periodic wraps a task; it must not replace it with something that can't be enqueued."""
        assert periodic(cron="* * * * *")(every_minute) is every_minute

    def test_it_registers_the_schedule(self):
        register_every_minute()

        assert [entry.task_name for entry in registered_periodic_tasks()] == [TASK_PATH]

    def test_task_name_matches_what_django_tasks_records(self):
        """The ledger key has to equal DBTaskResult.task_path or the two can never be joined."""
        assert register_every_minute().task_name == every_minute.module_path

    def test_an_invalid_cron_is_rejected_at_import_time(self):
        with pytest.raises(ValueError, match="not a valid cron expression"):
            periodic(cron="not a cron")(every_minute)

    def test_applying_it_to_a_plain_function_is_an_error(self):
        """The decorators are order-sensitive — @periodic goes ABOVE @task, not below."""

        def plain_function():
            pass

        with pytest.raises(TypeError, match="must wrap a django-tasks @task"):
            periodic(cron="* * * * *")(plain_function)

    def test_the_same_task_can_be_scheduled_twice_with_a_periodic_id(self):
        periodic(cron="* * * * *", periodic_id="fast")(every_minute)
        periodic(cron="0 * * * *", periodic_id="slow")(every_minute)

        assert len(registered_periodic_tasks()) == 2

    def test_rescheduling_the_same_key_differently_is_an_error(self):
        """Silently keeping one of two conflicting schedules is worse than refusing to start."""
        periodic(cron="* * * * *")(every_minute)

        with pytest.raises(ValueError, match="already registered"):
            periodic(cron="0 * * * *")(every_minute)

    def test_a_six_field_cron_adds_seconds(self):
        """Procrastinate's optional sixth column — 'once per second' has to be expressible."""
        entry = register_every_minute(cron="* * * * * */5")
        assert entry.cron == "* * * * * */5"


class TestCurrentSlot:
    def test_it_looks_backwards_not_forwards(self):
        """Backwards is what makes repeated sweeps agree on one slot and contend for one ledger row."""
        entry = PeriodicTask(task=every_minute, cron="* * * * *")
        now = datetime(2026, 7, 29, 12, 0, 30, tzinfo=UTC)

        assert entry.current_slot(now) == datetime(2026, 7, 29, 12, 0, 0, tzinfo=UTC)

    def test_it_is_stable_across_a_slot(self):
        entry = PeriodicTask(task=every_minute, cron="*/5 * * * *")
        early = datetime(2026, 7, 29, 12, 5, 1, tzinfo=UTC)
        late = datetime(2026, 7, 29, 12, 9, 59, tzinfo=UTC)

        assert entry.current_slot(early) == entry.current_slot(late)

    def test_it_moves_on_at_the_next_boundary(self):
        entry = PeriodicTask(task=every_minute, cron="*/5 * * * *")
        before = datetime(2026, 7, 29, 12, 9, 59, tzinfo=UTC)
        after = datetime(2026, 7, 29, 12, 10, 1, tzinfo=UTC)

        assert entry.current_slot(after) > entry.current_slot(before)


@pytest.mark.django_db
class TestDeferPeriodicTasks:
    def test_a_due_schedule_is_queued(self):
        register_every_minute()

        assert defer_periodic_tasks() == 1
        assert DBTaskResult.objects.filter(task_path=TASK_PATH).count() == 1

    def test_the_slot_timestamp_is_passed_to_the_task(self):
        entry = register_every_minute()
        now = timezone.now()

        defer_periodic_tasks(now)

        queued = DBTaskResult.objects.get(task_path=TASK_PATH)
        assert queued.args_kwargs["kwargs"]["timestamp"] == int(entry.current_slot(now).timestamp())

    def test_the_queue_name_override_is_applied(self):
        """The scheduler is the enqueue-site, so a queue the app would normally pass has to live here."""
        register_every_minute(queue_name="default")

        defer_periodic_tasks()

        assert DBTaskResult.objects.get(task_path=TASK_PATH).queue_name == "default"

    def test_extra_task_kwargs_are_passed_alongside_the_timestamp(self):
        periodic(cron="* * * * *", task_kwargs={"value": 7})(with_extra_kwargs)

        defer_periodic_tasks()

        queued = DBTaskResult.objects.get(task_path="tests.test_periodic.with_extra_kwargs")
        assert queued.args_kwargs["kwargs"]["value"] == 7

    def test_sweeping_twice_in_one_slot_queues_once(self):
        """The whole guarantee. Any number of schedulers, one queued task per slot."""
        register_every_minute()
        now = timezone.now()

        assert defer_periodic_tasks(now) == 1
        assert defer_periodic_tasks(now) == 0
        assert DBTaskResult.objects.filter(task_path=TASK_PATH).count() == 1

    def test_the_next_slot_queues_again(self):
        register_every_minute()
        now = timezone.now()

        defer_periodic_tasks(now)
        defer_periodic_tasks(now + timedelta(minutes=1))

        assert DBTaskResult.objects.filter(task_path=TASK_PATH).count() == 2

    def test_a_slot_older_than_the_max_delay_is_skipped(self):
        """A machine asleep overnight wakes to one current run, never a night's backlog."""
        register_every_minute(cron="0 3 * * *")
        eleven_hours_after_the_slot = timezone.now().replace(hour=14, minute=0, second=0, microsecond=0)

        assert defer_periodic_tasks(eleven_hours_after_the_slot) == 0
        assert not DBTaskResult.objects.exists()

    @override_settings(PERIODIC_MAX_DELAY_SECONDS=1)
    def test_the_max_delay_is_configurable(self):
        register_every_minute()
        just_past_the_minute = timezone.now().replace(second=30, microsecond=0)

        assert defer_periodic_tasks(just_past_the_minute) == 0

    def test_one_broken_schedule_does_not_stop_the_others(self):
        register_every_minute()
        PERIODIC_TASKS[("broken", "")] = PeriodicTask(task=every_minute, cron="* * * * *", periodic_id="broken")
        PERIODIC_TASKS[("broken", "")].task_kwargs["unexpected"] = object()  # unserialisable → enqueue raises

        assert defer_periodic_tasks() == 1

    def test_a_failed_enqueue_rolls_back_its_claim_so_the_slot_retries(self):
        """The claim and the enqueue share a transaction — a swallowed slot would be invisible."""
        entry = register_every_minute()
        entry.task_kwargs["unexpected"] = object()
        now = timezone.now()

        assert defer_periodic_tasks(now) == 0
        assert not PeriodicDefer.objects.exists()

    def test_an_empty_registry_is_a_no_op(self):
        assert defer_periodic_tasks() == 0


@pytest.mark.django_db
class TestPruning:
    def test_rows_past_the_retention_window_are_dropped(self):
        register_every_minute()
        now = timezone.now()
        defer_periodic_tasks(now)
        PeriodicDefer.objects.update(created=now - timedelta(days=30))

        assert prune_periodic_defers(now) == 1
        assert not PeriodicDefer.objects.exists()

    def test_recent_rows_are_kept(self):
        """Pruning a live row would let the same slot queue twice."""
        register_every_minute()
        now = timezone.now()
        defer_periodic_tasks(now)

        assert prune_periodic_defers(now) == 0
        assert PeriodicDefer.objects.count() == 1


class TestSecondsUntilNextSlot:
    """Sleeping to the next real slot is what keeps firing tight without a fast poll."""

    def test_it_sleeps_to_just_past_the_next_boundary(self):
        register_every_minute()
        thirty_seconds_in = timezone.now().replace(second=30, microsecond=0)

        assert seconds_until_next_slot(thirty_seconds_in, max_sleep=3600) == pytest.approx(30.5)

    def test_it_is_capped_by_max_sleep(self):
        """A once-a-day schedule must not commit the process to a 24-hour sleep."""
        register_every_minute(cron="0 3 * * *")

        assert seconds_until_next_slot(timezone.now(), max_sleep=15) == 15

    def test_an_empty_registry_falls_back_to_max_sleep(self):
        assert seconds_until_next_slot(timezone.now(), max_sleep=15) == 15

    def test_it_never_returns_a_negative_sleep(self):
        register_every_minute()

        assert seconds_until_next_slot(timezone.now(), max_sleep=3600) >= 0


class TestSchedulerThread:
    def test_it_runs_as_a_daemon_and_stops_on_its_event(self):
        """It rides inside someone else's worker, so the stop event is the only way to end it — and
        being a daemon is what keeps it from holding up that worker's shutdown."""
        stop_event = run_scheduler_thread(interval=0.01)
        thread = next(t for t in threading.enumerate() if t.name == "periodic-scheduler")

        assert thread.daemon is True

        stop_event.set()
        thread.join(timeout=5)

        assert thread.is_alive() is False
