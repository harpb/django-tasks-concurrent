"""
Declare a Django Tasks task as periodic, in code.

    from django_tasks import task
    from django_tasks_concurrent import periodic

    @periodic(cron="*/5 * * * *")
    @task()
    def cleanup_foobar(timestamp: int):
        ...

The decorator wraps a ``@task`` — it registers the schedule and hands the task straight back, so the
decorated name is still the ordinary task object and ``cleanup_foobar.enqueue()`` still works. It
does not create a database row, does not need a migration to change a cadence, and cannot drift from
the code it schedules. This mirrors Procrastinate's ``@app.periodic``, deliberately: a schedule is a
property of the task, so it belongs next to it in version control rather than in an admin form.

Cron is the standard five fields (minute, hour, day, month, weekday), evaluated in the project's
local time. An optional sixth field adds seconds, so "every second" is expressible.

The task is called with a single ``timestamp`` integer — the Unix timestamp of the slot it was
scheduled for, which may be a little in the past. Take it as an argument if the work is
time-dependent; ignore it with ``*args`` if not. Extra ``task_kwargs`` are passed alongside it.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime

from croniter import croniter
from django.utils import timezone

logger = logging.getLogger("django_tasks_concurrent")


@dataclass(frozen=True)
class PeriodicTask:
    """A registered schedule: which task, how often, and what to call it with."""

    task: object
    cron: str
    periodic_id: str = ""
    queue_name: str = ""
    task_kwargs: dict = field(default_factory=dict)

    @property
    def bound_task(self):
        """The task with this schedule's queue applied, ready to enqueue.

        A periodic task is enqueued by the scheduler, not by application code, so there is no
        enqueue-site to pass a queue at — the schedule has to carry it. Blank leaves the task's own
        queue alone.
        """
        return self.task.using(queue_name=self.queue_name) if self.queue_name else self.task

    @property
    def task_name(self) -> str:
        """The dotted path django-tasks itself records as ``task_path`` on the queued result.

        Reusing ``Task.module_path`` rather than rebuilding it from ``__module__``/``__qualname__``
        keeps the ledger key identical to what lands in DBTaskResult, so the two can be joined.
        """
        return self.task.module_path

    @property
    def key(self) -> tuple[str, str]:
        return (self.task_name, self.periodic_id)

    @property
    def title(self) -> str:
        label = f"{self.task_name}[{self.periodic_id}]" if self.periodic_id else self.task_name
        return f"{label} ({self.cron})"

    def next_slot(self, now: datetime) -> datetime:
        """The next firing strictly after ``now``, as an aware UTC datetime.

        Used to decide how long the scheduler may sleep, so it wakes when something is actually due
        instead of on a fixed tick.
        """
        local_now = timezone.localtime(now)
        return croniter(self.cron, local_now).get_next(datetime).astimezone(UTC)

    def current_slot(self, now: datetime) -> datetime:
        """The most recent firing at or before ``now``, as an aware UTC datetime.

        Looking BACKWARDS is what makes the sweep idempotent: every scheduler polling within the same
        slot computes the same value, so they all contend for one ledger row instead of each picking a
        different future time.
        """
        local_now = timezone.localtime(now)
        return croniter(self.cron, local_now).get_prev(datetime).astimezone(UTC)


PERIODIC_TASKS: dict[tuple[str, str], PeriodicTask] = {}


def periodic(*, cron: str, periodic_id: str = "", queue_name: str = "", task_kwargs: dict | None = None):
    """Register a ``@task`` to run on ``cron``. Returns the task unchanged.

    Give ``periodic_id`` when the same task is scheduled more than once — it is what separates the
    two schedules in the ledger, and without it the second registration would replace the first.
    Give ``queue_name`` when the task must land on a queue other than its own; the scheduler is the
    enqueue-site, so this is the only place to say so.
    """
    if not croniter.is_valid(cron):
        raise ValueError(f"{cron!r} is not a valid cron expression.")

    def register(task_object):
        if not hasattr(task_object, "enqueue"):
            raise TypeError(
                f"@periodic must wrap a django-tasks @task — apply it above the @task decorator, got {task_object!r}."
            )

        entry = PeriodicTask(
            task=task_object,
            cron=cron,
            periodic_id=periodic_id,
            queue_name=queue_name,
            task_kwargs=task_kwargs or {},
        )
        if entry.key in PERIODIC_TASKS and PERIODIC_TASKS[entry.key].cron != cron:
            raise ValueError(
                f"{entry.task_name} is already registered as periodic with a different cron. "
                "Pass a distinct periodic_id to schedule the same task more than once."
            )
        PERIODIC_TASKS[entry.key] = entry
        logger.debug(f"Registered periodic task {entry.title}")
        return task_object

    return register


def registered_periodic_tasks() -> list[PeriodicTask]:
    """Every schedule declared so far, in registration order."""
    return list(PERIODIC_TASKS.values())
