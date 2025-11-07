import logging
import subprocess
import time
import os
import db

# Configure logging for the worker
# This is separate from the CLI's logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [WORKER] [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

def execute_job(job: db.sqlite3.Row) -> int:
    """
    Executes the job's command in a subprocess.
    
    Returns:
        The exit code (return code) of the subprocess.
        0 for success, non-zero for failure.
    """
    job_id = job['id']
    command = job['command']
    
    logging.info(f"Running job {job_id}: {command}")
    
    try:
        # DANGER: shell=True is a security risk if the command comes
        # from an untrusted source. For this assignment, it's
        # necessary to interpret commands like "echo 'Hello'".
        # In a real-world app, we'd parse the command and args.
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,  # Capture stdout/stderr
            text=True,
            timeout=3600  # 1-hour timeout (good practice)
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
        return -1  # Use a custom code for timeout
    except Exception as e:
        logging.error(f"Job {job_id} failed with exception: {e}")
        return -2  # Use a custom code for other exceptions


# ... (execute_job function) ...

def process_job(job: db.sqlite3.Row):
    """
    Fetches, executes, and updates a job.
    This version includes retry and DLQ logic.
    """
    return_code = execute_job(job)
    
    if return_code == 0:
        # Job was successful
        db.update_job_status(job['id'], 'completed')
    else:
        # Job failed, use our new failure logic
        new_state = db.update_job_on_failure(job)
        logging.info(f"Job {job['id']} failed and was moved to state '{new_state}'")

# ... (start_worker function) ...

def start_worker():
    """
    The main worker loop.
    Continuously polls the database for new jobs.
    Includes a graceful shutdown mechanism.
    """
    logging.info("Worker starting... (PID: %s)", os.getpid())
    
    # --- Graceful Shutdown Check ---
    # Check for the stop file *before* starting the loop
    if db.STOP_FILE_PATH.exists():
        logging.info("Stop file found on startup. Deleting and exiting.")
        db.STOP_FILE_PATH.unlink() # Delete it so we don't block next start
        return # Exit immediately

    while True:
        # --- 1. Check for Stop Signal ---
        if db.STOP_FILE_PATH.exists():
            logging.info("Stop file detected. Shutting down gracefully...")
            db.STOP_FILE_PATH.unlink() # Clean up the file
            break # Exit the loop

        # --- 2. Process Job (existing logic) ---
        job = None
        try:
            job = db.fetch_pending_job()
            
            if job:
                process_job(job)
            else:
                # No job found, wait a bit before polling again
                logging.debug("No jobs found. Sleeping for 1s.")
                time.sleep(1)
                
        except Exception as e:
            logging.error(f"Unhandled error in worker loop: {e}")
            if job:
                # Try to mark the job as failed so it doesn't get stuck
                db.update_job_status(job['id'], 'failed')
            time.sleep(5) # Back off on major errors

    logging.info("Worker (PID: %s) has shut down.", os.getpid())

if __name__ == "__main__":
    import os
    # We must initialize the DB from the worker too
    db.initialize_database()
    start_worker()