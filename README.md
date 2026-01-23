# django-tasks-concurrent

Concurrent async worker for [Django Tasks](https://github.com/RealOrangeOne/django-tasks).

Runs multiple async tasks concurrently using `asyncio.TaskGroup`. While one task awaits I/O (API calls, database queries), others can execute. Optimized for I/O-bound workloads.

## Installation

```bash
pip install django-tasks-concurrent
```

Add to `INSTALLED_APPS`:

```python
INSTALLED_APPS = [
    ...
    "django_tasks",
    "django_tasks.backends.database",
    "django_tasks_concurrent",
]
```

## Usage

Run the worker:

```bash
# 3 concurrent workers (default)
python manage.py concurrent_worker

# 5 concurrent workers with 0.5s polling interval
python manage.py concurrent_worker --concurrency=5 --interval=0.5

# Specify queue and backend
python manage.py concurrent_worker --queue-name=high-priority --backend=default
```

### Options

| Option | Default | Description |
|--------|---------|-------------|
| `--concurrency` | 3 | Number of concurrent sub-workers |
| `--interval` | 1.0 | Polling interval (seconds) when no tasks |
| `--queue-name` | `settings.TASK_QUEUE_NAME` or "default" | Queue to process |
| `--backend` | "default" | Django Tasks backend name |

## How It Works

The worker spawns N sub-workers as asyncio coroutines. Each sub-worker:

1. Claims a task using `SELECT FOR UPDATE SKIP LOCKED`
2. Executes the task (async tasks run natively, sync tasks via thread pool)
3. Marks task as succeeded/failed
4. Repeats

This allows true concurrent execution of async tasks - while one awaits an API response, others continue processing.

## When to Use

**Use `concurrent_worker` for:**
- I/O-bound tasks (API calls, LLM inference, HTTP requests)
- Tasks that spend most time awaiting external services
- Workloads where you want N tasks running simultaneously

**Use standard `db_worker` for:**
- CPU-bound tasks
- Tasks that don't benefit from async
- Simple sequential processing

## Async Task Example

```python
from django_tasks import task

@task
async def call_llm_api(prompt: str) -> str:
    async with httpx.AsyncClient() as client:
        response = await client.post(
            "https://api.anthropic.com/v1/messages",
            json={"prompt": prompt},
        )
        return response.json()["content"]
```

With `--concurrency=3`, three LLM calls can be in-flight simultaneously.

## Requirements

- Python 3.12+
- Django 6.0+
- django-tasks 0.5.0+

## License

MIT
