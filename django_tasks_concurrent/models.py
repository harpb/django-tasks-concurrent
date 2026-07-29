"""
The periodic-defer ledger.

Periodic tasks are declared in code (see ``periodic_tasks.py``), not configured in the database — so this
table is not configuration. It is a lock: one row per (task, periodic_id, slot) that has already been
queued, with a unique constraint doing the work. Any number of schedulers or workers can sweep at the
same time and the database decides which one wins each slot; the losers get an IntegrityError and
move on.

That is the same shape Procrastinate uses, and it is chosen over a config table on purpose. A config
row has to be seeded on every environment and can drift from the code it names, and when several
deployments share one database — which they do here — a row written by one machine is visible to all
of them. A code-declared schedule ships with the task it runs and can't be half-applied.
"""

from django.db import models


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
