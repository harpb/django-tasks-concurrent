"""App config for django-tasks-concurrent."""

from django.apps import AppConfig
from django.utils.module_loading import autodiscover_modules


class DjangoTasksConcurrentConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "django_tasks_concurrent"
    verbose_name = "Django Tasks Concurrent"

    def ready(self):
        """Import every app's ``tasks`` module so ``@periodic`` registrations actually happen.

        django-tasks has no autodiscovery of its own — a task is only registered when its module is
        imported, which normally happens lazily at the first enqueue. That is too late for periodic
        tasks: the scheduler sweeps a registry, so a schedule in a module nobody imported is a
        schedule that silently never runs. Importing here is what makes ``@periodic`` declarative.
        """
        autodiscover_modules("tasks")
