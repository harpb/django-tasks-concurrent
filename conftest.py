"""
Pytest configuration for django-tasks-concurrent tests.
"""

import django
import pytest
from django.conf import settings


def pytest_configure():
    """Set up Django before test collection."""
    if not settings.configured:
        settings.configure(
            SECRET_KEY="test-secret-key-for-testing-only",
            DEBUG=True,
            INSTALLED_APPS=[
                "django.contrib.contenttypes",
                "django.contrib.auth",
                "django_tasks",
                "django_tasks.backends.database",
                "django_tasks_concurrent",
            ],
            DATABASES={
                "default": {
                    "ENGINE": "django.db.backends.sqlite3",
                    "NAME": ":memory:",
                }
            },
            TASKS={
                "default": {
                    "BACKEND": "django_tasks.backends.database.DatabaseBackend",
                    "QUEUES": ["default"],
                }
            },
            TASK_QUEUE_NAME="default",
            USE_TZ=True,
            DEFAULT_AUTO_FIELD="django.db.models.BigAutoField",
        )
    django.setup()


@pytest.fixture
def worker_config():
    """Default worker configuration for tests."""
    return {
        "concurrency": 3,
        "interval": 0.1,
        "queue_name": "default",
        "backend_name": "default",
    }
