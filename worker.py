import logging
import subprocess
import time
import os
import db

# ---------------------------------------------------------------------
# Worker Logging Configuration
# ---------------------------------------------------------------------
# This logger is dedicated to the background worker process.
# It operates independently from the CLI logger to ensure separation
# of runtime and operational logs.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

def execute_job(job: db.sqlite3.Row) -> int:
    """
    Execute a job command as a subprocess.

    Args:
        job: A database row object containing the job details.

    Returns:
        int: The subprocess exit code.
             0 indicates success; any non-zero value indicates failure.
             -1 indicates a timeout.
             -2 indicates an unexpected exception.
    """
    job_id = job['id']
    command = job['command']
    
    logging.info(f"Running job {job_id}: {command}")
    
    try:
        # Note:
        # Using shell=True introduces a security risk if commands
        # originate from untrusted sources. It’s used here solely
        # to handle commands like "echo 'Hello'" for this assignment.
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,  # Capture both stdout and stderr
            text=True,
            timeout=3600  # Enforce a 1-hour execution limit
        )
        
        if result.returncode == 0:
            logging.info(f"Job {job_id} completed successfully.")
            logging.debug(f"Stdout: {result.stdout}")
        else:
            logging.warning(f"Job {job_id} failed with code {result.returncode}.")
            logging.error(f"Stderr: {result.stderr}")
            
        return result.returncode

    except subprocess.TimeoutExpired:
        logging.error(f"Job {job_id} timed out.")
        return -1  # Timeout indicator
    except Exception as e:
        logging.error(f"Job {job_id} failed with exception: {e}")
        return -2  # Generic failure indicator


def process_job(job: db.sqlite3.Row):
    """
    Handles the end-to-end job lifecycle:
    - Executes the job command.
    - Updates its status in the database based on outcome.
    - Applies retry or dead-letter queue logic if applicable.
    """
    return_code = execute_job(job)
    
    if return_code == 0:
        # Job executed successfully
        db.update_job_status(job['id'], 'completed')
    else:
        # Job failed — delegate retry or DLQ handling to DB logic
        new_state = db.update_job_on_failure(job)
        logging.info(f"Job {job['id']} failed and moved to state '{new_state}'")


def start_worker():
    """
    Main worker loop responsible for continuously polling and executing jobs.

    Features:
    - Graceful shutdown support using a stop file.
    - Periodic polling for new jobs.
    - Basic error handling and backoff strategy.
    """
    logging.info("Worker starting... (PID: %s)", os.getpid())
    
    # -----------------------------------------------------------------
    # Check for shutdown signal before entering the loop.
    # If a stop file exists, remove it and exit immediately.
    # -----------------------------------------------------------------
    if db.STOP_FILE_PATH.exists():
        logging.info("Stop file found on startup. Removing and exiting.")
        db.STOP_FILE_PATH.unlink()
        return

    # -----------------------------------------------------------------
    # Continuous polling loop.
    # -----------------------------------------------------------------
    while True:
        # --- 1. Check for graceful shutdown signal ---
        if db.STOP_FILE_PATH.exists():
            logging.info("Stop file detected. Shutting down gracefully...")
            db.STOP_FILE_PATH.unlink()
            break

        # --- 2. Attempt to fetch and process a pending job ---
        job = None
        try:
            job = db.fetch_pending_job()
            
            if job:
                process_job(job)
            else:
                # No available jobs; short sleep before polling again
                logging.debug("No pending jobs found. Sleeping for 1s.")
                time.sleep(1)
                
        except Exception as e:
            # Capture any unexpected runtime errors in the worker loop
            logging.error(f"Unhandled error in worker loop: {e}")
            if job:
                # Ensure failed job is not left in a pending state
                db.update_job_status(job['id'], 'failed')
            # Apply a brief backoff before retrying
            time.sleep(5)

    logging.info("Worker (PID: %s) has shut down.", os.getpid())


if __name__ == "__main__":
    # Ensure database is initialized before starting the worker
    db.initialize_database()
    start_worker()
