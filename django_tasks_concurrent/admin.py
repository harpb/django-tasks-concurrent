"""Read-only view of the periodic-defer ledger.

There is nothing to configure here — schedules live in code with ``@periodic``. This exists to answer
"did that periodic task actually get queued, and when", which is otherwise invisible.
"""

from django.contrib import admin

from django_tasks_concurrent.models import PeriodicDefer


@admin.register(PeriodicDefer)
class PeriodicDeferAdmin(admin.ModelAdmin):
    list_display = ["task_name", "periodic_id", "defer_at", "created"]
    list_filter = ["task_name"]
    search_fields = ["task_name", "periodic_id"]
    readonly_fields = ["task_name", "periodic_id", "defer_at", "created"]

    def has_add_permission(self, request):
        """The ledger is written by the scheduler; a hand-added row would suppress a real run."""
        return False

    def has_change_permission(self, request, obj=None):
        return False
