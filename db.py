import sqlite3
import logging
from pathlib import Path
from typing import Optional
import datetime

# --- Configuration ---
# Use a hidden file in the user's home directory for persistent storage
# This makes the CLI work from anywhere, like a real tool (e.g., git, docker)
DB_PATH = Path.home() / ".queuectl" / "queue.db"
STOP_FILE_PATH = Path.home() / ".queuectl" / "stop_workers"  # <-- ADD THIS
DB_PATH.parent.mkdir(parents=True, exist_ok=True)  # Ensure the .queuectl directory exists
# ... (imports) ...


# --- Core Functions ---

def get_db_connection():
    """Establishes a connection to the SQLite database."""
    try:
        # isolation_level=None enables autocommit mode.
        # We will manage transactions manually with BEGIN/COMMIT.
        conn = sqlite3.connect(DB_PATH, isolation_level=None)
        conn.row_factory = sqlite3.Row  # Access columns by name
        logging.debug(f"Database connection established to {DB_PATH}")
        return conn
    except sqlite3.Error as e:
        logging.error(f"Error connecting to database: {e}")
        raise

def initialize_database():
    """
    Initializes the database and creates the necessary tables if they don't exist.
    This is designed to be safe to run every time the app starts.
    """
    logging.info("Initializing database...")
    
    # The 'jobs' table schema
    # We add 'run_at' for future scheduling/backoff
    # We add 'updated_at' to track changes
    create_jobs_table_sql = """
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
    
    # A simple key-value table for configuration
    create_config_table_sql = """
    CREATE TABLE IF NOT EXISTS config (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """
    
    # Indexes to make searching for 'pending' jobs fast
    create_state_index_sql = "CREATE INDEX IF NOT EXISTS idx_jobs_state_run_at ON jobs (state, run_at);"
    
    try:
        conn = get_db_connection()
        with conn:  # This will automatically BEGIN and COMMIT (or ROLLBACK on error)
            conn.execute("PRAGMA journal_mode=WAL;")  # Enable WAL mode for better concurrency
            conn.execute(create_jobs_table_sql)
            conn.execute(create_config_table_sql)
            conn.execute(create_state_index_sql)
            
            # Set default configuration values if not present
            conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('max_retries', '3');")
            conn.execute("INSERT OR IGNORE INTO config (key, value) VALUES ('backoff_base', '2');")
            
        logging.info("Database initialized successfully.")
    
    except sqlite3.Error as e:
        logging.error(f"Database initialization failed: {e}")
        raise
    finally:
        if conn:
            conn.close()

# ... (all the code from Step 1) ...

# --- Configuration Helpers ---

def get_config_value(key: str) -> str:
    """Fetches a specific configuration value from the config table."""
    conn = None
    try:
        conn = get_db_connection()
        # Use a tuple for the parameter to prevent SQL injection
        cursor = conn.execute("SELECT value FROM config WHERE key = ?;", (key,))
        row = cursor.fetchone()
        if row:
            return row['value']
        else:
            logging.warning(f"Config key '{key}' not found.")
            return None
    except sqlite3.Error as e:
        logging.error(f"Error fetching config '{key}': {e}")
        return None
    finally:
        if conn:
            conn.close()

# --- Job Helpers ---

def insert_job(job_id: str, command: str, max_retries: int) -> bool:
    """
    Inserts a new job into the database in a 'pending' state.
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
        logging.info(f"Successfully enqueued job {job_id}")
        return True
    except sqlite3.IntegrityError:
        # This happens if the job ID (PRIMARY KEY) already exists
        logging.error(f"Job ID '{job_id}' already exists. Use a unique ID.")
        return False
    except sqlite3.Error as e:
        logging.error(f"Error enqueuing job {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

# ... (all the code from before) ...
import datetime

# ... (insert_job function) ...

def fetch_pending_job() -> Optional[sqlite3.Row]:
    """
    Atomically fetches a single 'pending' job and updates its state to 'processing'.
    
    This function uses a transaction with "BEGIN IMMEDIATE" to acquire
    an immediate write-lock on the database, preventing other workers
    from fetching the same job.
    
    It finds a job that is 'pending' and its 'run_at' time is in the past.
    
    Returns:
        A sqlite3.Row object if a job is found, otherwise None.
    """
    conn = None
    try:
        conn = get_db_connection()
        
        # We use a 'with' block on the connection itself, which will:
        # 1. Automatically BEGIN a transaction.
        # 2. Automatically COMMIT on success.
        # 3. Automatically ROLLBACK on error.
        # We set it to IMMEDIATE to get a write lock *now*.
        with conn:
            conn.execute("BEGIN IMMEDIATE;")
            
            # Find the oldest, runnable, pending job
            sql_find = """
            SELECT * FROM jobs
            WHERE state = 'pending' AND run_at <= CURRENT_TIMESTAMP
            ORDER BY created_at ASC
            LIMIT 1;
            """
            cursor = conn.execute(sql_find)
            job = cursor.fetchone()
            
            if job:
                # If we found a job, lock it by updating its state
                logging.info(f"Worker fetching job {job['id']}")
                sql_lock = """
                UPDATE jobs
                SET state = 'processing', updated_at = CURRENT_TIMESTAMP
                WHERE id = ?;
                """
                conn.execute(sql_lock, (job['id'],))
                
                # The 'with' block will COMMIT both the SELECT and UPDATE
                return job
            
        # If no job was found, the 'with' block auto-commits an empty transaction
        return None

    except sqlite3.Error as e:
        # A "database is locked" error is common if another worker is
        # in this *exact* block. We can just log it and try again later.
        if "database is locked" in str(e):
            logging.warning(f"Database was locked. Another worker may be fetching.")
        else:
            logging.error(f"Error fetching pending job: {e}")
        return None
    finally:
        if conn:
            conn.close()

def update_job_status(job_id: str, new_state: str):
    """
    Updates a job's status. This is a simple, non-transactional update
    as the job is already 'locked' (in 'processing' state).
    """
    sql = "UPDATE jobs SET state = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            conn.execute(sql, (new_state, job_id))
        logging.info(f"Updated job {job_id} to state '{new_state}'")
    except sqlite3.Error as e:
        logging.error(f"Error updating job {job_id} status: {e}")
    finally:
        if conn:
            conn.close()

# ... (all the code from before) ...
import math # Add this import at the top of db.py

# ... (after update_job_status function) ...

def update_job_on_failure(job: sqlite3.Row) -> str:
    """
    Handles the logic for a failed job:
    1. Increments attempt counter.
    2. If attempts < max_retries, calculates backoff and sets to 'pending'.
    3. If attempts >= max_retries, sets to 'dead' (DLQ).
    
    Returns:
        The new state ('pending' or 'dead').
    """
    job_id = job['id']
    new_attempts = job['attempts'] + 1
    
    conn = None
    try:
        if new_attempts >= job['max_retries']:
            # --- Move to Dead Letter Queue (DLQ) ---
            new_state = 'dead'
            logging.warning(f"Job {job_id} failed on final attempt. Moving to DLQ.")
            sql = "UPDATE jobs SET state = ?, attempts = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?;"
            params = (new_state, new_attempts, job_id)
        
        else:
            # --- Retry with Exponential Backoff ---
            new_state = 'pending'
            
            # Get backoff base from config (default to 2)
            try:
                base_str = get_config_value('backoff_base')
                backoff_base = int(base_str)
            except:
                backoff_base = 2
                
            # Calculate delay: base ^ attempts (in seconds)
            delay_seconds = math.pow(backoff_base, new_attempts)
            
            # Calculate the next run_at time
            next_run_at = datetime.datetime.utcnow() + datetime.timedelta(seconds=delay_seconds)
            
            logging.info(f"Job {job_id} failed. Retrying in {delay_seconds}s (attempt {new_attempts}).")
            
            sql = """
            UPDATE jobs
            SET state = ?, attempts = ?, run_at = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?;
            """
            params = (new_state, new_attempts, next_run_at, job_id)

        # --- Execute the update ---
        conn = get_db_connection()
        with conn:
            conn.execute(sql, params)
        
        return new_state

    except Exception as e:
        logging.error(f"Critical error updating failed job {job_id}: {e}")
        # If this fails, we can't do much else, but at least it's logged.
        return "error"
    finally:
        if conn:
            conn.close()
# ... (all the code from before) ...

# --- Read/Query Helpers ---

def get_job_status_summary() -> dict:
    """
    Returns a dictionary of job counts by state.
    e.g., {'pending': 10, 'completed': 5, 'dead': 2}
    """
    summary = {
        'pending': 0,
        'processing': 0,
        'completed': 0,
        'failed': 0, # Should be 0 if retry logic is working
        'dead': 0
    }
    
    sql = "SELECT state, COUNT(*) as count FROM jobs GROUP BY state;"
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.execute(sql)
        rows = cursor.fetchall()
        
        for row in rows:
            if row['state'] in summary:
                summary[row['state']] = row['count']
        return summary
        
    except sqlite3.Error as e:
        logging.error(f"Error getting job status summary: {e}")
        return summary # Return the empty summary on error
    finally:
        if conn:
            conn.close()

def list_jobs_by_state(state: str) -> list[sqlite3.Row]:
    """
    Lists all jobs matching a specific state.
    """
    sql = "SELECT * FROM jobs WHERE state = ? ORDER BY created_at ASC;"
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.execute(sql, (state,))
        jobs = cursor.fetchall()
        return jobs
    except sqlite3.Error as e:
        logging.error(f"Error listing jobs by state '{state}': {e}")
        return []
    finally:
        if conn:
            conn.close()
# ... (all the code from before) ...

# --- Action Helpers ---

def retry_dead_job(job_id: str) -> bool:
    """
    Finds a 'dead' job and resets it to 'pending' to be retried.
    Resets attempts to 0 and sets run_at to now.
    
    Returns True if the job was found and updated, False otherwise.
    """
    sql = """
    UPDATE jobs
    SET state = 'pending',
        attempts = 0,
        run_at = CURRENT_TIMESTAMP,
        updated_at = CURRENT_TIMESTAMP
    WHERE id = ? AND state = 'dead';
    """
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            cursor = conn.execute(sql, (job_id,))
            
            if cursor.rowcount == 0:
                # This means no row was updated.
                # The ID either didn't exist or the job wasn't 'dead'.
                logging.warning(f"Failed to retry job {job_id}: Job not found in DLQ.")
                return False
        
        logging.info(f"Retried job {job_id}. Moved from 'dead' to 'pending'.")
        return True
        
    except sqlite3.Error as e:
        logging.error(f"Error retrying job {job_id}: {e}")
        return False
    finally:
        if conn:
            conn.close()

def set_config_value(key: str, value: str) -> bool:
    """
    Sets a configuration value in the config table.
    Uses 'INSERT OR REPLACE' (upsert) to create or update the key.
    """
    sql = "INSERT OR REPLACE INTO config (key, value) VALUES (?, ?);"
    conn = None
    try:
        conn = get_db_connection()
        with conn:
            conn.execute(sql, (key, value))
        logging.info(f"Config updated: {key} = {value}")
        return True
    except sqlite3.Error as e:
        logging.error(f"Error setting config {key}={value}: {e}")
        return False
    finally:
        if conn:
            conn.close()

          
# --- Main execution ---
if __name__ == "__main__":
    # This allows us to run `python db.py` to set up the DB manually
    logging.basicConfig(level=logging.INFO)
    initialize_database()
    print(f"Database initialized at {DB_PATH}")