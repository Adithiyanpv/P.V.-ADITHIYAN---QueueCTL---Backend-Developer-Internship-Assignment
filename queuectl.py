import logging
import uuid
from typing import Optional
import multiprocessing  # <-- ADD THIS IMPORT
import os               # <-- ADD THIS IMPORT

import typer
import db
import worker # Our database module
from rich.table import Table
from rich.console import Console
# --- Setup ---

# Configure logging
# In a real app, this would be more complex (e.g., in a config file)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],  # Log to console
)

# Create the Typer application
app = typer.Typer(
    help="queuectl: A CLI for managing a background job queue."
)

console = Console() # For printing rich tables

@app.command()
def status():
    """
    Show a summary of all job states & active workers.
    """
    typer.secho("--- Job Status Summary ---", bold=True)
    
    summary = db.get_job_status_summary()
    
    # --- 1. Job Summary Table ---
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("State", style="dim")
    table.add_column("Count")
    
    table.add_row("Pending", f"[bold green]{summary['pending']}[/bold green]")
    table.add_row("Processing", f"[bold yellow]{summary['processing']}[/bold yellow]")
    table.add_row("Completed", f"[cyan]{summary['completed']}[/cyan]")
    table.add_row("Dead (DLQ)", f"[bold red]{summary['dead']}[/bold red]")
    
    console.print(table)
    
    # --- 2. Worker Status ---
    # We can't *truly* know the state of other processes without
    # complex IPC (Inter-Process Communication).
    # But we *can* check if the stop file exists.
    typer.secho("\n--- Worker Status ---", bold=True)
    if db.STOP_FILE_PATH.exists():
        typer.secho("Workers are signaled to STOP.", fg=typer.colors.YELLOW)
    else:
        # This is an assumption, but it's the best we can do easily
        typer.secho("Workers are (assumed) RUNNING.", fg=typer.colors.GREEN)
        typer.echo("Use 'ps aux | grep worker.py' to see active processes.")


@app.command()
def list(
    state: str = typer.Option(
        "pending",
        "--state",
        "-s",
        help="The job state to list (pending, completed, dead, etc.)",
    ),
):
    """
    List jobs by their state.
    """
    typer.secho(f"--- Jobs in '{state}' state ---", bold=True)
    
    jobs = db.list_jobs_by_state(state)
    
    if not jobs:
        typer.echo(f"No jobs found with state '{state}'.")
        raise typer.Exit()

    # --- Jobs Table ---
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("ID", style="dim", width=36)
    table.add_column("Command")
    table.add_column("Attempts", justify="right")
    table.add_column("Max Retries", justify="right")
    table.add_column("Run At")
    
    for job in jobs:
        table.add_row(
            job['id'],
            f"[cyan]'{job['command']}'[/cyan]",
            str(job['attempts']),
            str(job['max_retries']),
            str(job['run_at']),
        )
    
    console.print(table)


# =====================================================================
#  DLQ COMMANDS
# =====================================================================

# Create a 'dlq' sub-app
dlq_app = typer.Typer(
    help="Manage the Dead Letter Queue (DLQ)."
)
app.add_typer(dlq_app, name="dlq")


@dlq_app.command("list")
def dlq_list():
    """
    View all jobs in the Dead Letter Queue.
    (Shortcut for 'queuectl list --state dead')
    """
    # This is a cool trick: we just call the *other* command's code
    typer.echo("Listing jobs in Dead Letter Queue (state='dead')...")
    
    # We can't call 'list(state="dead")' directly,
    # so we just re-implement the core logic.
    jobs = db.list_jobs_by_state("dead")
    
    if not jobs:
        typer.echo(f"No jobs found in DLQ.")
        raise typer.Exit()

    table = Table(show_header=True, header_style="bold red")
    table.add_column("ID", style="dim", width=36)
    table.add_column("Command")
    table.add_column("Attempts", justify="right")
    table.add_column("Updated At")
    
    for job in jobs:
        table.add_row(
            job['id'],
            f"[cyan]'{job['command']}'[/cyan]",
            str(job['attempts']),
            str(job['updated_at']),
        )
    
    console.print(table)

# --- CLI Callback ---

@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,  # ctx is the "context", gives us info about the command
):
    """
    Main CLI entry point. This function runs *before* any command.
    We use it to initialize the database on every run.
    """
    if ctx.invoked_subcommand is None:
        # If no command is given (just 'queuectl'), show the help menu
        typer.echo(ctx.get_help())
        raise typer.Exit()
        
    # --- Database Initialization ---
    # This is our "run-on-start" logic.
    try:
        db.initialize_database()
        logging.debug("Database initialized.")
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")
        typer.echo(f"FATAL: Could not initialize database. Exiting.", err=True)
        raise typer.Exit(code=1)


# --- CLI Commands ---
# ... (all the code from before) ...

# ... (at the end of the DLQ COMMANDS section) ...

@dlq_app.command("retry")
def dlq_retry(
    job_id: str = typer.Argument(
        ...,
        help="The ID of the 'dead' job to retry.",
    ),
):
    """
    Reset a specific job from the DLQ back to 'pending'.
    """
    typer.echo(f"Attempting to retry job {job_id}...")
    
    success = db.retry_dead_job(job_id)
    
    if success:
        typer.secho(f"✅ Success: Job {job_id} moved to 'pending' state.", fg=typer.colors.GREEN)
    else:
        typer.secho(
            f"❌ Error: Could not retry job {job_id}.",
            fg=typer.colors.RED
        )
        typer.echo("   (Ensure the job ID exists and is in the 'dead' state).")
        raise typer.Exit(code=1)


# =S====================================================================
#  CONFIG COMMANDS
# =====================================================================

# Create a 'config' sub-app
config_app = typer.Typer(
    help="Manage CLI configuration."
)
app.add_typer(config_app, name="config")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="The config key (e.g., 'max_retries')."),
    value: str = typer.Argument(..., help="The value to set."),
):
    """
    Set a configuration value (e.g., 'max_retries', 'backoff_base').
    """
    
    # --- A bit of validation (good practice) ---
    valid_keys = {"max_retries", "backoff_base"}
    if key not in valid_keys:
        typer.secho(f"Warning: '{key}' is not a standard config key.", fg=typer.colors.YELLOW)
        typer.echo(f"Standard keys are: {', '.join(valid_keys)}")

    if key == "max_retries" and not value.isdigit():
        typer.secho(f"Error: 'max_retries' must be an integer.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    if key == "backoff_base" and not value.isdigit():
        typer.secho(f"Error: 'backoff_base' must be an integer.", fg=typer.colors.RED)
        raise typer.Exit(code=1)
    
    typer.echo(f"Setting config: {key} = {value}")
    
    success = db.set_config_value(key, value)
    
    if success:
        typer.secho("✅ Config updated successfully.", fg=typer.colors.GREEN)
    else:
        typer.secho("❌ Error updating config.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

# ... (if __name__ == "__main__":) ...

@app.command()
def enqueue(
    command: str = typer.Argument(
        ...,  # The '...' means this argument is REQUIRED
        help="The command string to execute (e.g., \"sleep 5\").",
    ),
    id: Optional[str] = typer.Option(
        None,
        "--id",
        "-i",
        help="Unique ID for the job. [default: a new UUID]",
    ),
    max_retries: Optional[int] = typer.Option(
        None,
        "--max-retries",
        "-r",
        help="Max retries for this job. [default: from config]",
    ),
):
    """
    Add a new job to the queue.
    """
    logging.info(f"Attempting to enqueue command: '{command}'")
    
    # 1. Determine the Job ID
    final_job_id = id if id else str(uuid.uuid4())

    # 2. Determine Max Retries
    # If the user didn't specify --max-retries, get it from our config table
    if max_retries is None:
        try:
            config_retries = db.get_config_value("max_retries")
            final_max_retries = int(config_retries)
        except Exception as e:
            logging.warning(f"Could not read 'max_retries' from config. Defaulting to 3. Error: {e}")
            final_max_retries = 3
    else:
        final_max_retries = max_retries

    # 3. Insert the job using our DB helper
    success = db.insert_job(
        job_id=final_job_id,
        command=command,
        max_retries=final_max_retries
    )

    # 4. Give user feedback
    if success:
        typer.secho(f"✅ Successfully enqueued job:", fg=typer.colors.GREEN)
        typer.echo(f"   ID: {final_job_id}")
        typer.echo(f"   Command: {command}")
        typer.echo(f"   Max Retries: {final_max_retries}")
    else:
        typer.secho(f"❌ Error enqueuing job. Check logs for details.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

# ... (logging setup, app definition, main callback) ...

# ... (enqueue command) ...

# =====================================================================
#  WORKER COMMANDS
# =====================================================================

# Create a 'worker' sub-app
worker_app = typer.Typer(
    help="Manage worker processes."
)
# Add this sub-app to our main 'app'
app.add_typer(worker_app, name="worker")


@worker_app.command("start")
def worker_start(
    count: int = typer.Option(
        1,
        "--count",
        "-c",
        help="Number of worker processes to start.",
    ),
):
    """
    Start one or more worker processes in the background.
    """
    # 1. Ensure the stop file does NOT exist
    if db.STOP_FILE_PATH.exists():
        typer.echo("Removing existing stop file...")
        db.STOP_FILE_PATH.unlink()

    typer.echo(f"Starting {count} worker(s) in the background...")
    
    processes = []
    for _ in range(count):
        # We target the 'start_worker' function from our worker.py
        p = multiprocessing.Process(target=worker.start_worker)
        p.start()
        processes.append(p)

    typer.secho("Workers started successfully:", fg=typer.colors.GREEN)
    for p in processes:
        typer.echo(f"  - PID: {p.pid}")
        
    typer.echo("\nUse 'queuectl worker stop' to shut them down gracefully.")
    typer.echo("Note: These processes run in the background.")


@worker_app.command("stop")
def worker_stop():
    """
    Signal all running workers to shut down gracefully.
    """
    if db.STOP_FILE_PATH.exists():
        typer.echo("Stop file already exists. Workers may already be stopping.")
        return

    typer.echo("Creating stop file to signal workers...")
    try:
        # This is the "poison pill"
        db.STOP_FILE_PATH.touch()
        typer.secho(
            "Signal sent. Workers will shut down after finishing their current jobs.",
            fg=typer.colors.YELLOW
        )
    except Exception as e:
        typer.secho(f"Error creating stop file: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)
# --- Main Entry Point ---

if __name__ == "__main__":
    app()