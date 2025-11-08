# queuectl: Background Job Queue CLI

`queuectl` is a CLI-based background job queue system built in Python, as per the Backend Developer Internship Assignment. It manages background jobs with worker processes, handles retries using exponential backoff, and maintains a Dead Letter Queue (DLQ).

**[Click here to view the working demo of the CLI tool](https://drive.google.com/drive/folders/12Rk69RXsjGxjoYT2ZP_Q4EUis3QDCS8-?usp=sharing)**


---

##  Architecture Overview

The system is composed of three main Python components:

* **`queuectl.py` (The CLI):** A user-friendly interface built with **Typer**. It provides commands to enqueue, monitor, and manage jobs.

* **`worker.py` (The Engine):** A standalone script that runs one or more worker processes using Python's **`multiprocessing`** module. Each worker polls the database, executes jobs, and handles retries.

* **`db.py` (The State):** A persistence and logic module using **SQLite**. All job state and configuration are stored in a single database file at `~/.queuectl/queue.db`.

### Why SQLite?

The assignment allowed for JSON or SQLite. **SQLite was chosen** because it is a production-grade, embedded, transactional database. It trivially solves critical concurrency problems that are nearly impossible to handle with a flat JSON file, such as:

1. **Atomic Transactions:** Jobs are fetched and "locked" in a single, atomic `BEGIN IMMEDIATE ... COMMIT` transaction.

2. **Race Condition Prevention:** This atomic fetch (`fetch_pending_job`) guarantees that two workers cannot grab the same `pending` job at the same time.

3. **Concurrency:** SQLite's WAL (Write-Ahead Logging) mode is enabled, allowing many workers to read and write to the database concurrently and safely.

###  Job Lifecycle

Jobs move through a defined set of states. The retry and backoff logic is handled automatically by the worker.

1. **Enqueue:** A new job is created in the `pending` state.

2. **Fetch:** A worker fetches a `pending` job, atomically moving it to `processing`.

3. **Execute:**

   * **On Success (Exit Code 0):** Job moves to `completed`.

   * **On Failure (Non-zero Exit Code):** `attempts` is incremented.

4. **Retry & Backoff:**

   * If `attempts < max_retries`, the job is moved back to `pending`. Its `run_at` time is set to the future using **exponential backoff** (`delay = base ^ attempts`).

   * If `attempts >= max_retries`, the job has permanently failed and is moved to the `dead` state (DLQ).

5. **DLQ Retry:** A user can manually retry a `dead` job, which resets its `attempts` and moves it back to `pending`.

### ⚙️ Worker Management

* **`queuectl worker start --count N`** launches `N` new worker processes using `multiprocessing.Process`.

* **`queuectl worker stop`** implements a **"poison pill"** shutdown. It creates a file at `~/.queuectl/stop_workers`. Each worker checks for this file in its main loop. If found, it finishes its *current* job and exits gracefully, ensuring no work is lost.

---

## Setup Instructions

1. **Clone the repository:**

   ```bash
   git clone https://github.com/Adithiyanpv/P.V.-ADITHIYAN---QueueCTL---Backend-Developer-Internship-Assignment.git
   cd P.V.-ADITHIYAN---QueueCTL---Backend-Developer-Internship-Assignment
   ```

2. **Create a virtual environment:**

   ```bash
   python3 -m venv venv
   source venv/bin/activate  # On Windows: venv\Scripts\activate
   ```

3. **Install dependencies:**

   ```bash
   pip install -r requirements.txt
   ```

4. **Run (DB is auto-initialized):**

   The database will be automatically created in your user's home directory (`~/.queuectl/queue.db`) the first time you run any command.

   ```bash
   python queuectl.py --help
   ```

---

## Usage Examples

All commands are run from the main CLI.

### 1. Enqueue Jobs

`queuectl enqueue [COMMAND] [OPTIONS]`

```bash
# Enqueue a simple job with a custom ID
$ python queuectl.py enqueue "sleep 3; echo 'job done'" --id "sleepy-job"
 Successfully enqueued job:
   ID: sleepy-job
   Command: sleep 3; echo 'job done'
   Max Retries: 3

# Enqueue a job that will fail (uses config default retries)
$ python queuectl.py enqueue "cat /no/file/here" --id "fail-job"
 Successfully enqueued job:
   ID: fail-job
   Command: cat /no/file/here
   Max Retries: 3

# Enqueue a job with a custom retry count
$ python queuectl.py enqueue "flaky-script.sh" --id "flaky-1" --max-retries 5
 Successfully enqueued job:
   ID: flaky-1
   Command: flaky-script.sh
   Max Retries: 5
```

### 2. Manage Workers

`queuectl worker [start|stop]`

```bash
# Start 3 workers in the background
$ python queuectl.py worker start --count 3
Starting 3 worker(s) in the background...
Workers started successfully:
  - PID: 12345
  - PID: 12346
  - PID: 12347
Use 'queuectl worker stop' to shut them down gracefully.
Note: These processes run in the background.

# Signal all workers to stop gracefully
$ python queuectl.py worker stop
Creating stop file to signal workers...
Signal sent. Workers will shut down after finishing their current jobs.
```

**(For Testing) Run a worker in the foreground:** To see the worker's log output directly, you can run worker.py in its own terminal:

```bash
$ python worker.py
2025-11-07 23:50:00 [WORKER] [INFO] Worker starting... (PID: 12345)
2025-11-07 23:50:00 [WORKER] [INFO] Worker fetching job sleepy-job
2025-11-07 23:50:00 [WORKER] [INFO] Running job sleepy-job: sleep 3; echo 'job done'
...
```

### 3. Check System Status

`queuectl status`

```bash
# Get a high-level summary of all jobs and worker status
$ python queuectl.py status
--- Job Status Summary ---
┌────────────┬───────┐
│ State      │ Count │
├────────────┼───────┤
│ Pending    │ 1     │
│ Processing │ 3     │
│ Completed  │ 10    │
│ Dead (DLQ) │ 2     │
└────────────┴───────┘

--- Worker Status ---
Workers are (assumed) RUNNING.
Use 'ps aux | grep worker.py' to see active processes.
```

### 4. List Jobs by State

`queuectl list [OPTIONS]`

```bash
# List all pending jobs (the default)
$ python queuectl.py list
--- Jobs in 'pending' state ---
┌────────────────────────────────────┬──────────────────┬──────────┬─────────────┬─────────────────────┐
│ ID                                 │ Command          │ Attempts │ Max Retries │ Run At              │
├────────────────────────────────────┼──────────────────┼──────────┼─────────────┼─────────────────────┤
│ flaky-1                            │ 'flaky-script.s │        0 │           5 │ 2025-11-07 23:52:00 │
│ ...                                │ ...              │ ...      │ ...         │ ...                 │
└────────────────────────────────────┴──────────────────┴──────────┴─────────────┴─────────────────────┘

# List all completed jobs
$ python queuectl.py list --state completed
--- Jobs in 'completed' state ---
┌────────────────────────────────────┬──────────────────────────┬──────────┬─────────────┬─────────────────────┐
│ ID                                 │ Command                  │ Attempts │ Max Retries │ Run At              │
├────────────────────────────────────┼──────────────────────────┼──────────┼─────────────┼─────────────────────┤
│ sleepy-job                         │ 'sleep 3; echo 'job don │        0 │           3 │ 2025-11-07 23:50:00 │
│ ...                                │ ...                      │ ...      │ ...         │ ...                 │
└────────────────────────────────────┴──────────────────────────┴──────────┴─────────────┴─────────────────────┘
```

### 5. Manage the Dead Letter Queue (DLQ)

`queuectl dlq [list|retry]`

```bash
# List all jobs that have permanently failed
$ python queuectl.py dlq list
Listing jobs in Dead Letter Queue (state='dead')...
┌────────────────────────────────────┬─────────────────────┬──────────┬─────────────────────┐
│ ID                                 │ Command             │ Attempts │ Updated At          │
├────────────────────────────────────┼─────────────────────┼──────────┼─────────────────────┤
│ fail-job                           │ 'cat /no/file/here' │        3 │ 2025-11-07 23:51:30 │
└────────────────────────────────────┴─────────────────────┴──────────┴─────────────────────┘

# Retry a specific job from the DLQ
$ python queuectl.py dlq retry "fail-job"
Attempting to retry job fail-job...
Success: Job fail-job moved to 'pending' state.

# Verify it's back in pending
$ python queuectl.py list
...
(You will see 'fail-job' listed here now)
```

### 6. Manage Configuration

`queuectl config set [KEY] [VALUE]`

```bash
# Set the default max retries for all *new* jobs to 5
$ python queuectl.py config set max_retries 5
Setting config: max_retries = 5
Config updated successfully.

# Set the exponential backoff base (e.g., 3^attempts)
$ python queuectl.py config set backoff_base 3
Setting config: backoff_base = 3
Config updated successfully.
```

---

##  Testing Instructions

A shell script is provided to validate the core end-to-end flows. This script will clean the database, enqueue jobs, run a worker, wait for a job to fail and move to the DLQ, and then retry that job.

Ensure you are in your activated virtual environment.

Make the script executable:

```bash
chmod +x test_flows.sh
```

Run the script:

```bash
./test_flows.sh
```

You can also monitor the worker.log file in another terminal (`tail -f worker.log`).

---

##  Assumptions & Trade-offs

* **CLI Usability:** The spec suggested enqueue '{"json": "..."}'. I implemented a more user-friendly flag-based command: `enqueue "command" --id "..."`. This is a common-sense UX improvement.

* **shell=True:** The `subprocess.run` command uses `shell=True`. This is a security risk if the command source is untrusted, but it's necessary to correctly parse string commands like `"sleep 3; echo 'done'"`.

* **Simplicity:** The worker management is simple (background processes + poison pill). A more complex system might use a daemon or a proper process manager (like supervisorctl).

* **Job Output:** Job stdout/stderr is not currently stored, only the exit code. A bonus feature would be to log this to a file or the database.
