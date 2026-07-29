"""
The periodic scheduler.

Sweeps the schedules declared with ``@periodic`` and queues any whose slot has arrived. It only
enqueues — it never runs task code — so it stays cheap and its poll cadence can't drift behind a long
job. Whatever worker you already run executes the result.

Safe in any number of processes. Each slot is claimed by inserting one row into ``PeriodicDefer``
under a unique constraint, so the database decides the winner; a loser catches IntegrityError and
moves on. There is no lock to wait on and no leader to elect.

Missed slots are not replayed. A slot more than ``PERIODIC_MAX_DELAY_SECONDS`` old is skipped, so a
machine that was asleep overnight comes back and runs the current slot once instead of catching up on
hundreds. Only the most recent slot is ever considered, which is what makes repeated sweeps within
one slot idempotent.
"""

import asyncio
import logging
import threading
from datetime import timedelta

from asgiref.sync import sync_to_async
from django.conf import settings
from django.db import IntegrityError, close_old_connections, transaction
from django.utils import timezone

from django_tasks_concurrent.models import PeriodicDefer
from django_tasks_concurrent.periodic_tasks import PeriodicTask, registered_periodic_tasks

logger = logging.getLogger("django_tasks_concurrent")

# A slot older than this is never queued — the scheduler was down, and running a stale slot now is
# almost never what the schedule meant. Matches Procrastinate's 10-minute default.
DEFAULT_MAX_DELAY_SECONDS = 600

# Ledger rows only exist to suppress a duplicate defer, so they are useless once no live slot could
# still collide with them. Pruned lazily, and only on a sweep that actually deferred something.
DEFER_RETENTION_DAYS = 7


# Deliberate oversleep so a wake-up lands just PAST the slot boundary rather than a hair before it,
# which would burn a whole extra poll discovering nothing is due yet.
TICK_MARGIN_SECONDS = 0.5


def max_delay_seconds() -> int:
    return getattr(settings, "PERIODIC_MAX_DELAY_SECONDS", DEFAULT_MAX_DELAY_SECONDS)


def seconds_until_next_slot(now=None, max_sleep: float = 15.0) -> float:
    """How long the scheduler may sleep before something is actually due.

    Sleeping to the next real slot instead of on a fixed tick is what makes a ``* * * * *`` task fire
    within half a second of the minute rather than up to a poll-interval late — and it means an idle
    registry costs no queries at all. ``max_sleep`` caps it so a once-a-day schedule doesn't commit
    the process to a 24-hour sleep that a clock change or a suspend would invalidate, and so an empty
    registry still ticks.
    """
    now = now or timezone.now()
    entries = registered_periodic_tasks()
    if not entries:
        return max_sleep

    soonest = min(entry.next_slot(now) for entry in entries)
    return max(0.0, min((soonest - now).total_seconds() + TICK_MARGIN_SECONDS, max_sleep))


def defer_periodic_tasks(now=None) -> int:
    """Queue every declared schedule whose current slot hasn't been queued yet. Returns how many fired.

    Synchronous and self-contained so it works from the poll loop, a test, or a one-off sweep alike.
    Each schedule is claimed independently — one broken schedule cannot stop the others.
    """
    now = now or timezone.now()
    cutoff = now - timedelta(seconds=max_delay_seconds())

    fired = 0
    for entry in registered_periodic_tasks():
        try:
            if defer_one(entry, now, cutoff):
                fired += 1
        except Exception as schedule_error:
            logger.exception(f"Periodic task {entry.title} could not be deferred: {schedule_error}")

    if fired:
        prune_periodic_defers(now)
    return fired


def defer_one(entry: PeriodicTask, now, cutoff) -> bool:
    """Claim ``entry``'s current slot and queue it. Returns whether it fired.

    The ledger insert and the enqueue share one transaction: if enqueueing raises, the claim rolls
    back too, so the next sweep retries the slot instead of silently swallowing it. IntegrityError is
    caught OUTSIDE the atomic block on purpose — a failed statement poisons the surrounding
    transaction, so it cannot be handled from inside it.
    """
    slot = entry.current_slot(now)
    if slot < cutoff:
        logger.debug(f"Skipping stale slot {slot} for {entry.title}")
        return False

    try:
        with transaction.atomic():
            PeriodicDefer.objects.create(task_name=entry.task_name, periodic_id=entry.periodic_id, defer_at=slot)
            entry.bound_task.enqueue(timestamp=int(slot.timestamp()), **entry.task_kwargs)
    except IntegrityError:
        # Another scheduler already claimed this slot. Expected, not an error.
        return False

    logger.info(f"Deferred periodic task {entry.title} for slot {slot}")
    return True


def prune_periodic_defers(now) -> int:
    """Drop ledger rows too old to suppress anything, and return how many went."""
    deleted, _ = PeriodicDefer.objects.filter(created__lt=now - timedelta(days=DEFER_RETENTION_DAYS)).delete()
    return deleted


async def defer_periodic_forever(interval: float = 15.0) -> None:
    """Sweep the schedules forever. The scheduler as a coroutine, for an async worker's task group.

    ``ConcurrentWorker`` runs this as a side task, so it is never started by hand — see
    ``run_worker``. It is cancelled at shutdown rather than asked to stop, which is why there is no
    stop flag: cancellation is how an asyncio task group ends anything, and an exception raised here
    propagates out of the group and takes the worker down with it. That is deliberate. A worker whose
    scheduler died is a worker that silently runs nothing on schedule, which is worse than one that
    exits loudly.

    Per-poll errors are still swallowed and logged — a single bad sweep (a lost connection, one
    broken schedule) is a hiccup, not a reason to stop.

    ``interval`` is the ceiling on one sleep, not a poll period — see ``seconds_until_next_slot``.
    """
    logger.info(f"Starting scheduler interval={interval} schedules={len(registered_periodic_tasks())}")
    while True:
        try:
            fired = await sync_to_async(defer_periodic_tasks)()
            if fired:
                logger.debug(f"Scheduler deferred {fired} periodic task(s)")
        except Exception as scheduler_error:
            logger.exception(f"Scheduler poll failed: {scheduler_error}")
        finally:
            # Same reasoning as the worker's sub-loop: a connection the database dropped has to be
            # discarded on the error path too, or every later poll reuses the dead one.
            await sync_to_async(close_old_connections)()
        await asyncio.sleep(seconds_until_next_slot(max_sleep=interval))


def run_scheduler_thread(interval: float = 15.0) -> threading.Event:
    """Run the scheduler on a daemon thread and return its stop event.

    For hosting the scheduler inside a worker this package does not own — Django's ``db_worker``, or
    a wrapper around it — where there is no task group to add a coroutine to. If you run
    ``ConcurrentWorker`` you do not need this: it schedules on its own.

    The loop is plain synchronous rather than ``defer_periodic_forever``: a sweep is one indexed
    query, and ``asyncio`` signal handlers can only be installed from the main thread anyway.

    Daemon, so it never keeps the process alive at shutdown. Swallows everything — a scheduler
    problem must not take down a worker that knows nothing about it. This is the one place that
    trade goes the other way from ``defer_periodic_forever``, because here we are a guest.
    """
    stop_event = threading.Event()

    def poll_until_stopped():
        while not stop_event.is_set():
            try:
                fired = defer_periodic_tasks()
                if fired:
                    logger.debug(f"Scheduler deferred {fired} periodic task(s)")
            except Exception as scheduler_error:
                logger.exception(f"Scheduler poll failed: {scheduler_error}")
            finally:
                close_old_connections()
            stop_event.wait(seconds_until_next_slot(max_sleep=interval))

    logger.info(f"Starting scheduler interval={interval} schedules={len(registered_periodic_tasks())}")
    threading.Thread(target=poll_until_stopped, name="periodic-scheduler", daemon=True).start()
    return stop_event
