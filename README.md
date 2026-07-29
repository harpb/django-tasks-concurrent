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

Or from code — same options either way, since the command is a shell over `run_worker`:

```bash
from django_tasks_concurrent import run_worker, run_worker_async

run_worker(concurrency=5)               # creates the event loop and runs until shut down
await run_worker_async(concurrency=5)   # when you already have a loop
```

### Options

| Option | CLI flag | Default | Description |
|--------|----------|---------|-------------|
| `concurrency` | `--concurrency` | 3 | Number of concurrent sub-workers |
| `interval` | `--interval` | 1.0 | Polling interval (seconds) when no tasks |
| `queue_name` | `--queue-name` | `settings.TASK_QUEUE_NAME` or "default" | Queue to process |
| `backend_name` | `--backend` | "default" | Django Tasks backend name |
| `scheduler_interval` | `--scheduler-interval` | 15.0 | Ceiling on one `@periodic` scheduler sleep |

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

**Running a worker is all it takes.** `concurrent_worker` schedules as well as executes — no flag, no beat process to deploy and forget:

```bash
python manage.py concurrent_worker
```

Scheduling isn't optional because there's nothing to opt out of: the scheduler only *enqueues*, so it costs one indexed query per wake-up, and with no `@periodic` tasks declared it does nothing at all. Being off the execution path, it can't drift behind a long-running job.

It runs as a **side task** beside the sub-workers, which means it shares their fate in both directions. Shutting the worker down cancels it. And if the scheduler itself dies, the worker stops rather than carrying on — a worker that has quietly stopped scheduling looks perfectly healthy from the outside while every `@periodic` task silently stops firing.

### Not using the concurrent worker? `task_worker`

If you want the plain sequential worker, run `task_worker` rather than `db_worker` — it delegates to `db_worker` and adds the two things a deployment ends up adding anyway:

```bash
python manage.py task_worker                 # schedules, and reconnects
python manage.py task_worker --no-scheduler  # several workers, one schedule
python manage.py task_worker --no-reload     # don't restart on code edits
```

Scheduling is a *flag* here, unlike `concurrent_worker` where it's unconditional. The difference is ownership: this command hands control to upstream `db_worker` and gets it back only at shutdown, so the schedule rides a daemon thread nobody supervises — and being able to say "not this process" is what lets you run several workers and schedule from only one. For the same reason, a scheduler that fails to start is logged and skipped rather than stopping the worker: it's a passenger here, not part of the machine.

To host the scheduler in some other worker entirely, or on a thread of your own:

```bash
from django_tasks_concurrent.scheduler import run_scheduler_thread

run_scheduler_thread()   # daemon thread, returns a stop event
```

### Surviving a database that blinks

`db_worker`'s poll loop only catches SQLite's "database is locked". Every other database error — including the `OperationalError` a dropped connection raises at COMMIT — propagates out of `run()` and exits the process. That's fine when the database is a local file and merciless when it's on another host, where a container restart or an idle reap is routine.

`task_worker` installs the fix for you. If you build a `Worker` yourself (on a thread, say), install it directly:

```bash
from django_tasks_concurrent import install_resilient_run

install_resilient_run()   # patches Worker.run; idempotent
```

Those errors then force a fresh connection, back off exponentially to 30s, and re-enter the poll loop. A shutdown that races the error is re-raised rather than retried over.

To check what's registered, or to sweep once from a shell or an external timer:

```bash
from django_tasks_concurrent.periodic_tasks import registered_periodic_tasks
from django_tasks_concurrent.scheduler import defer_periodic_tasks

[entry.title for entry in registered_periodic_tasks()]
defer_periodic_tasks()   # queue whatever is due, return how many fired
```

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

The scheduler sleeps until the next real slot rather than polling on a tick, so a schedule fires within half a second of its boundary whatever its cadence, and an idle registry costs no queries at all. `run_scheduler_thread(interval=...)` (default 15s) is only the **ceiling** on a single sleep — it needs no tuning against your tightest cron.

### Registration

Schedules only exist once the module holding them is imported. The app config autodiscovers every app's `tasks` module on startup for exactly this reason — put `@periodic` tasks in `tasks.py` and they register themselves. `registered_periodic_tasks()` is the fastest way to confirm.

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
