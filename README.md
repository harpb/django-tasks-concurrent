# django-tasks-concurrent

Concurrent async worker **and cron scheduler** for [Django Tasks](https://github.com/RealOrangeOne/django-tasks).

Runs multiple async tasks concurrently using `asyncio.TaskGroup`. While one task awaits I/O (API calls, database queries), others can execute. Optimized for I/O-bound workloads.

It also adds the piece Django Tasks deliberately leaves out. Django owns task definition, queuing, and results — cadence and execution belong to the worker layer — so there is no built-in way to say "run this every five minutes". [Periodic tasks](#periodic-tasks) fills that gap with a decorator you put next to the task.

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

## Periodic tasks

Declare a schedule in code, next to the task it runs:

```bash
from django_tasks import task
from django_tasks_concurrent import periodic


@periodic(cron="*/5 * * * *")
@task()
def cleanup_foobar(timestamp: int):
    ...
```

`@periodic` goes **above** `@task` and hands the task straight back, so `cleanup_foobar.enqueue()` still works and the decorated name is still the ordinary task object.

**Running a worker is all it takes** — `concurrent_worker` schedules by default, so there is no beat process to deploy and forget:

```bash
python manage.py concurrent_worker           # works and schedules
python manage.py concurrent_worker --no-scheduler
python manage.py task_scheduler --list       # what's registered?
python manage.py task_scheduler --once       # sweep once and exit
python manage.py task_scheduler              # standalone, if you'd rather split them
```

Hosting it in a worker you don't control (Django's own `db_worker`, or a wrapper around it) takes one call:

```bash
from django_tasks_concurrent.scheduler import run_scheduler_thread

run_scheduler_thread()   # daemon thread, returns a stop event
```

The scheduler only *enqueues* — it never runs task code — so it costs one indexed query per wake-up and, being off the execution path, can't drift behind a long-running job.

### Cron syntax

Standard five fields (minute, hour, day, month, weekday), evaluated in the project's local time (`settings.TIME_ZONE`), so `0 3 * * *` means 3am where you are. An optional sixth field adds seconds, making "once per second" expressible.

### The timestamp argument

A periodic task is called with a single `timestamp` integer — the Unix timestamp of the slot it was scheduled for, which may be slightly in the past. Take it if the work is time-dependent, ignore it with `*args` if not. Anything in `task_kwargs` is passed alongside it:

```bash
periodic(cron="*/5 * * * *", task_kwargs={"value": 1}, periodic_id="fast")(do_something)
```

Pass `periodic_id` when the same task is scheduled more than once — it's what tells the two schedules apart.

### Guarantees

- **At most once per slot**, across any number of scheduler processes. A slot is claimed by inserting one row into `PeriodicDefer` under a unique constraint, so the database picks the winner; losers move on. No leader election, no lock to wait on.
- **No catch-up.** Only the most recent slot is considered, and a slot older than `PERIODIC_MAX_DELAY_SECONDS` (default 600) is skipped — a machine asleep overnight wakes up and runs once, not three hundred times.
- **At-least-once execution, not exactly-once.** The claim and the enqueue share a transaction, so a failed enqueue rolls back its claim and the slot retries on the next poll.

The scheduler sleeps until the next real slot rather than polling on a tick, so a schedule fires within half a second of its boundary whatever its cadence, and an idle registry costs no queries at all. `--interval` (default 15s) is only the **ceiling** on a single sleep — it needs no tuning against your tightest cron.

### Registration

Schedules only exist once the module holding them is imported. The app config autodiscovers every app's `tasks` module on startup for exactly this reason — put `@periodic` tasks in `tasks.py` and they register themselves. `task_scheduler --list` is the fastest way to confirm.

Requires a migration: `python manage.py migrate django_tasks_concurrent`.


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
