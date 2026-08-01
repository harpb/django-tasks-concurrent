"""
The periodic-defer ledger.

Periodic tasks are declared in code (see ``periodic_tasks.py``), not configured in the database — so this
table is not configuration. It is a lock: one row per (task, periodic_id, slot) that has already been
queued, with a unique constraint doing the work. Any number of schedulers or workers can sweep at the
same time and the database decides which one wins each slot; the losers insert nothing and move on.

That is the same shape Procrastinate uses, and it is chosen over a config table on purpose. A config
row has to be seeded on every environment and can drift from the code it names, and when several
deployments share one database — which they do here — a row written by one machine is visible to all
of them. A code-declared schedule ships with the task it runs and can't be half-applied.
"""

from django.db import connections, models


class PeriodicDeferManager(models.Manager):
    def claim_slot(self, task_name: str, periodic_id: str, defer_at, created) -> bool:
        """Insert the row for one slot, and say whether this process is the one that got it.

        A losing claim must not raise. Every scheduler wakes on the same slot boundary, so on a busy
        minute they all insert within microseconds of each other and most of them lose — and a losing
        plain INSERT is a failed statement, which PostgreSQL writes to its server log as ERROR even
        though the caller handles it. One noisy log line per scheduler per slot, forever. So the
        conflict is resolved by the INSERT itself: ``ON CONFLICT DO NOTHING RETURNING id`` inserts or
        doesn't, never fails, and the presence of a returned row is the answer.

        Backends without both halves of that (MySQL, Oracle) fall back to a plain insert and let
        IntegrityError travel to the caller, which is where it was always handled.
        """
        connection = connections[self.db]
        if not (connection.features.supports_ignore_conflicts and connection.features.can_return_columns_from_insert):
            self.create(task_name=task_name, periodic_id=periodic_id, defer_at=defer_at)
            return True

        quote_name = connection.ops.quote_name
        column_names = ("task_name", "periodic_id", "defer_at", "created")
        columns = ", ".join(quote_name(column_name) for column_name in column_names)
        values = [
            task_name,
            periodic_id,
            connection.ops.adapt_datetimefield_value(defer_at),
            connection.ops.adapt_datetimefield_value(created),
        ]
        statement = (
            f"INSERT INTO {quote_name(self.model._meta.db_table)} ({columns}) VALUES (%s, %s, %s, %s) "
            f"ON CONFLICT DO NOTHING RETURNING {quote_name('id')}"
        )

        with connection.cursor() as cursor:
            cursor.execute(statement, values)
            return cursor.fetchone() is not None


class PeriodicDefer(models.Model):
    """One periodic slot that has already been queued.

    Rows are pruned once they age past the point where they could still suppress a duplicate — see
    ``prune_periodic_defers``. Nothing reads them for scheduling decisions beyond the unique
    constraint, so the table stays small and boring.
    """

    task_name = models.CharField(max_length=500)
    periodic_id = models.CharField(max_length=200, blank=True, default="")
    defer_at = models.DateTimeField(help_text="The scheduled slot this defer covers, not when it ran.")
    created = models.DateTimeField(auto_now_add=True, db_index=True)

    objects = PeriodicDeferManager()

    class Meta:
        ordering = ["-id"]
        constraints = [
            models.UniqueConstraint(
                fields=["task_name", "periodic_id", "defer_at"],
                name="unique_periodic_defer_slot",
            )
        ]

    @property
    def title(self) -> str:
        label = f"{self.task_name}[{self.periodic_id}]" if self.periodic_id else self.task_name
        return f"{label} @ {self.defer_at}"
