"""
Make Django's own ``db_worker`` survive a database that goes away for a moment.

``db_worker``'s poll loop only catches SQLite's "database is locked". Every other database error —
including the ``OperationalError`` a dropped connection raises at COMMIT — propagates out of
``run()``, kills the worker thread, and exits the process. That is fine when the database is a local
file and merciless when it is on another host: a container restart, an idle reap, or a network blip
ends the worker, and whether anything restarts it is somebody else's problem.

``install_resilient_run`` wraps ``Worker.run`` so those errors force a fresh connection, back off, and
re-enter the poll loop. It patches the class rather than subclassing it, so it applies to every way
the worker gets built — the management command, or a ``Worker`` you drive yourself on a thread.

It catches ``Exception``, not a list of database error classes, for the same reason the scheduler's
poll loop does: the poll loop is the last thing standing between a queue and nobody draining it, and
the ways it can die are not enumerable in advance. A per-task failure never reaches here — upstream
``run_task`` already catches ``BaseException`` and marks the task failed — so anything that does get
this far is the worker's own plumbing, and the useful response to all of it is the same. Retrying a
genuine bug forever is the lesser evil: it is loud in the log every time round, where a worker that
exited on the first surprise is silent, and looks identical to one with nothing to do.
"""

import logging
import time

from django.db import close_old_connections
from django_tasks_db.management.commands.db_worker import Worker

logger = logging.getLogger("django_tasks_concurrent")

# Ceiling on the exponential backoff between reconnection attempts.
RECONNECT_MAX_BACKOFF_SECONDS = 30.0


def install_resilient_run(max_backoff_seconds: float = RECONNECT_MAX_BACKOFF_SECONDS) -> None:
    """Wrap ``Worker.run`` so a transient database disconnect is a hiccup, not a dead process.

    Idempotent — calling it twice does not stack wrappers, which matters because both the management
    command and an inline worker may install it in the same process.

    A shutdown that races the error is re-raised rather than retried: ``self.running`` going false
    means somebody asked the worker to stop, and reconnecting then would ignore them.
    """
    if getattr(Worker.run, "is_resilient_wrapper", False):
        return

    original_run = Worker.run

    def resilient_run(self):
        backoff_seconds = 1.0
        while self.running:
            try:
                original_run(self)
                return  # Clean exit: batch complete, max-tasks reached, or shutdown.
            except Exception as worker_error:
                if not self.running:
                    # Shutdown was requested while the error fired — let it propagate.
                    raise
                logger.warning(
                    f"Worker poll loop failed ({worker_error=}); reconnecting in {backoff_seconds:.0f}s.",
                    exc_info=True,
                )
                close_old_connections()
                time.sleep(backoff_seconds)
                backoff_seconds = min(backoff_seconds * 2, max_backoff_seconds)

    resilient_run.is_resilient_wrapper = True
    Worker.run = resilient_run
