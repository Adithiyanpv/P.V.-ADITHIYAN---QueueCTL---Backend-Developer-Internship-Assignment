import sqlite3
import logging
from pathlib import Path
from typing import Optional
import datetime
import math  # Required for exponential backoff calculations

# ---------------------------------------------------------------------
# Persistent Storage Configuration
# ---------------------------------------------------------------------
# Queue data is stored in the user's home directory to allow the CLI
# to run from any location while maintaining a single shared queue DB.
DB_PATH = Path.home() / ".queuectl" / "queue.db"
STOP_FILE_PATH = Path.home() / ".queuectl" / "stop_workers"

# Ensure the application data directory exists
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------
# Database Connection
# ---------------------------------------------------------------------
def get_db_connection():
    """
    Create and return a SQLite connection.
    
    Autocommit is enabled; transactions are explicitly controlled in
    code where necessary (BEGIN/COMMIT blocks used for job locking).
    """
    try:
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        conn.row_factory = sqlite3.Row
        logging.debug(f"Database connection established at {DB_PATH}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Database connection failed: {e}")
        raise


# ---------------------------------------------------------------------
# Database Initialization
# ---------------------------------------------------------------------
def initialize_database():
    """
    Create required tables and defaults if they do not already exist.
    Safe to run on every tool invocation.
    """
    logging.info("Initializing database...")

    jobs_table_sql = """
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        state TEXT NOT NULL DEFAULT 'pending',
        attempts INTEGER NOT NULL DEFAULT 0,
        max_retries INTEGER NOT NULL DEFAULT 3,
        run_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
    );
    """

    config_table_sql = """
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """

    state_index_sql = """
    CREATE INDEX IF NOT EXISTS idx_jobs_state_run_at
    ON jobs (state, run_at);
    """

    try:
        conn = get_db_connection()
        with conn:
            conn.execute("PRAGMA journal_mode=WAL;")
            conn.execute(jobs_table_sql)
            conn.execute(config_table_sql)
            conn.execute(state_index_sql)

            # Default configuration values
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('max_retries', '3');"
            )
            conn.execute(
                "INSERT OR IGNORE INTO config (key, value) VALUES ('backoff_base', '2');"
            )

        logging.info("Database initialized successfully.")

    except sqlite3.Error as e:
        logging.error(f"Database initialization error: {e}")
        raise
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Configuration Helpers
# ---------------------------------------------------------------------
def get_config_value(key: str) -> str:
    """
    Retrieve a configuration value.
    Returns None if the key does not exist.
    """
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.execute("SELECT value FROM config WHERE key = ?;", (key,))
        row = cursor.fetchone()
        if row:
            return row['value']
        logging.warning(f"Config key not found: {key}")
        return None
    except sqlite3.Error as e:
        logging.error(f"Failed to retrieve config [{key}]: {e}")
        return None
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Job Insertion
# ---------------------------------------------------------------------
def insert_job(job_id: str, command: str, max_retries: int) -> bool:
    """
    Insert a new job in pending state.
    Returns True on success, False on failure.
    """
    sql = """
    INSERT INTO jobs (id, command, max_retries, state, run_at, created_at, updated_at)
    VALUES (?, ?, ?, 'pending', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP);
    """
    conn = None

    try:
        conn = get_db_connection()
        with conn:
            conn.execute(sql, (job_id, command, max_retries))
        logging.info(f"Job created: {job_id}")
        return True
    except sqlite3.IntegrityError:
        logging.error(f"Job ID already exists: {job_id}")
        return False
    except sqlite3.Error as e:
        logging.error(f"Error inserting job {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Job Retrieval / Locking
# ---------------------------------------------------------------------
def fetch_pending_job() -> Optional[sqlite3.Row]:
    """
    Retrieve one available pending job and mark it as processing.
    Uses an IMMEDIATE lock to ensure only one worker claims the job.
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            conn.execute("BEGIN IMMEDIATE;")

            cursor = conn.execute(
                """
                SELECT * FROM jobs
                WHERE state = 'pending' AND run_at <= CURRENT_TIMESTAMP
                ORDER BY created_at ASC
                LIMIT 1;
                """
            )
            job = cursor.fetchone()

            if job:
                logging.info(f"Worker claimed job {job['id']}")
                conn.execute(
                    """
                    UPDATE jobs
                    SET state = 'processing', updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?;
                    """,
                    (job['id'],),
                )
                return job

        return None

    except sqlite3.Error as e:
        if "database is locked" in str(e):
            logging.warning("Database lock encountered during job fetch.")
        else:
            logging.error(f"Pending job fetch failed: {e}")
        return None
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Job State Management
# ---------------------------------------------------------------------
def update_job_status(job_id: str, new_state: str):
    """
    Update job state (processing → completed/failed/etc.).
    """
    sql = "UPDATE jobs SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
    conn = None

    try:
        conn = get_db_connection()
        with conn:
            conn.execute(sql, (new_state, job_id))
        logging.info(f"Job {job_id} → {new_state}")
    except sqlite3.Error as e:
        logging.error(f"Failed to update job {job_id}: {e}")
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Retry / DLQ Handling
# ---------------------------------------------------------------------
def update_job_on_failure(job: sqlite3.Row) -> str:
    """
    Handle job failure and determine next state:
    - Increment attempts
    - Reset as pending with future run_at (retry)
    - Move to DLQ after final attempt
    """
    job_id = job['id']
    new_attempts = job['attempts'] + 1

    conn = None
    try:
        if new_attempts >= job['max_retries']:
            # Transition to DLQ
            new_state = 'dead'
            logging.warning(f"Job {job_id} exceeded retry limit. Moving → DLQ.")

            sql = "UPDATE jobs SET state = ?, attempts = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
            params = (new_state, new_attempts, job_id)

        else:
            # Retry with exponential backoff
            new_state = 'pending'
            try:
                base = int(get_config_value('backoff_base'))
            except:
                base = 2

            delay = math.pow(base, new_attempts)
            next_run_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay)

            logging.info(f"Job {job_id} retry in {delay}s (attempt {new_attempts})")

            sql = """
            UPDATE jobs
            SET state = ?, attempts = ?, run_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """
            params = (new_state, new_attempts, next_run_at, job_id)

        conn = get_db_connection()
        with conn:
            conn.execute(sql, params)

        return new_state

    except Exception as e:
        logging.error(f"Failed job recovery error [{job_id}]: {e}")
        return "error"
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Query Helpers
# ---------------------------------------------------------------------
def get_job_status_summary() -> dict:
    """
    Return aggregate job counts keyed by state.
    """
    summary = { 'pending': 0, 'processing': 0, 'completed': 0, 'failed': 0, 'dead': 0 }
    sql = "SELECT state, COUNT(*) as count FROM jobs GROUP BY state;"

    conn = None
    try:
        conn = get_db_connection()
        rows = conn.execute(sql).fetchall()

        for row in rows:
            if row['state'] in summary:
                summary[row['state']] = row['count']

        return summary
    except sqlite3.Error as e:
        logging.error(f"Status summary query failed: {e}")
        return summary
    finally:
        if conn:
            conn.close()


def list_jobs_by_state(state: str) -> list[sqlite3.Row]:
    """
    Retrieve all jobs with a specific state, oldest first.
    """
    sql = "SELECT * FROM jobs WHERE state = ? ORDER BY created_at ASC;"
    conn = None

    try:
        conn = get_db_connection()
        return conn.execute(sql, (state,)).fetchall()
    except sqlite3.Error as e:
        logging.error(f"Query failed for state '{state}': {e}")
        return []
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# DLQ Actions
# ---------------------------------------------------------------------
def retry_dead_job(job_id: str) -> bool:
    """
    Move a job from DLQ back into pending state with zero attempts.
    """
    sql = """
    UPDATE jobs
    SET state = 'pending', attempts = 0, run_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
    WHERE id = ? AND state = 'dead';
    """
    conn = None

    try:
        conn = get_db_connection()
        with conn:
            cursor = conn.execute(sql, (job_id,))
            if cursor.rowcount == 0:
                logging.warning(f"Retry failed. Job not in DLQ: {job_id}")
                return False

        logging.info(f"Job restored from DLQ → pending: {job_id}")
        return True

    except sqlite3.Error as e:
        logging.error(f"DLQ retry failed [{job_id}]: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Config Setter
# ---------------------------------------------------------------------
def set_config_value(key: str, value: str) -> bool:
    """
    Insert or update a configuration value.
    """
    sql = "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?);"
    conn = None

    try:
        conn = get_db_connection()
        with conn:
            conn.execute(sql, (key, value))
        logging.info(f"Config updated → {key}={value}")
        return True
    except sqlite3.Error as e:
        logging.error(f"Config update error [{key}]: {e}")
        return False
    finally:
        if conn:
            conn.close()


# ---------------------------------------------------------------------
# Manual Execution Entry Point
# ---------------------------------------------------------------------
if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    initialize_database()
    print(f"Database initialized at {DB_PATH}")
