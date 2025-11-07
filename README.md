<<<<<<< HEAD
# P.V.-ADITHIYAN---QueueCTL---Backend-Developer-Internship-Assignment
=======
# queuectl: Background Job Queue CLI

`queuectl` is a CLI-based background job queue system built in Python, as per the Backend Developer Internship Assignment. It manages background jobs with worker processes, handles retries using exponential backoff, and maintains a Dead Letter Queue (DLQ).

[Link to your CLI demo video]

---

## 🏛️ Architecture Overview

The system is composed of three main Python components:

* **`queuectl.py` (The CLI):** A user-friendly interface built with **Typer**. It provides commands to enqueue, monitor, and manage jobs.
* **`worker.py` (The Engine):** A standalone script that runs one or more worker processes using Python's **`multiprocessing`** module. Each worker polls the database, executes jobs, and handles retries.
* **`db.py` (The State):** A persistence and logic module using **SQLite**. All job state and configuration are stored in a single database file at `~/.queuectl/queue.db`.

### Why SQLite?

The assignment allowed for JSON or SQLite. **SQLite was chosen** because it is a production-grade, embedded, transactional database. It trivially solves critical concurrency problems that are nearly impossible to handle with a flat JSON file, such as:
1.  **Atomic Transactions:** Jobs are fetched and "locked" in a single, atomic `BEGIN IMMEDIATE ... COMMIT` transaction.
2.  **Race Condition Prevention:** This atomic fetch (`fetch_pending_job`) guarantees that two workers cannot grab the same `pending` job at the same time.
3.  **Concurrency:** SQLite's WAL (Write-Ahead Logging) mode is enabled, allowing many workers to read and write to the database concurrently and safely.

### 🔄 Job Lifecycle

Jobs move through a defined set of states. The retry and backoff logic is handled automatically by the worker.



1.  **Enqueue:** A new job is created in the `pending` state.
2.  **Fetch:** A worker fetches a `pending` job, atomically moving it to `processing`.
3.  **Execute:**
    * **On Success (Exit Code 0):** Job moves to `completed`.
    * **On Failure (Non-zero Exit Code):** `attempts` is incremented.
4.  **Retry & Backoff:**
    * If `attempts < max_retries`, the job is moved back to `pending`. Its `run_at` time is set to the future using **exponential backoff** (`delay = base ^ attempts`).
    * If `attempts >= max_retries`, the job has permanently failed and is moved to the `dead` state (DLQ).
5.  **DLQ Retry:** A user can manually retry a `dead` job, which resets its `attempts` and moves it back to `pending`.

### ⚙️ Worker Management

* **`queuectl worker start --count N`** launches `N` new worker processes using `multiprocessing.Process`.
* **`queuectl worker stop`** implements a **"poison pill"** shutdown. It creates a file at `~/.queuectl/stop_workers`. Each worker checks for this file in its main loop. If found, it finishes its *current* job and exits gracefully, ensuring no work is lost.

---

## Setup Instructions

1.  **Clone the repository:**
    ```bash
    git clone [your-repo-link]
    cd queuectl
    ```

2.  **Create a virtual environment:**
    ```bash
    python3 -m venv venv
    source venv/bin/activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

4.  **Run (DB is auto-initialized):**
    ```bash
    python queuectl.py --help
    ```

---

## Usage Examples

All commands are run from the main CLI.

### 1. Enqueue Jobs

```bash
# Enqueue a simple job with a custom ID
$ python queuectl.py enqueue "sleep 3; echo 'job done'" --id "sleepy-job"

# Enqueue a job that will fail (uses config default retries)
$ python queuectl.py enqueue "cat /no/file/here" --id "fail-job"

# Enqueue a job with custom retries
$ python queuectl.py enqueue "flaky-script.sh" --id "flaky-1" --max-retries 5
>>>>>>> 0e4bc00 (Initial commit: Add complete queuectl project)
