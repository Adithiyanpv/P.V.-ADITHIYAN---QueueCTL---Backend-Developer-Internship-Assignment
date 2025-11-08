import logging
import uuid
from typing import Optional
import multiprocessing  # Required for parallel worker management
import os               # For file path and environment operations

import typer
import db
import worker
from rich.table import Table
from rich.console import Console

# ---------------------------------------------------------------------
# Logging Configuration
# ---------------------------------------------------------------------
# Configure logging for the CLI layer.
# In a production-grade app, this could be externally configured via
# environment variables or a YAML logging config.
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler()],
)

# ---------------------------------------------------------------------
# CLI Application Setup
# ---------------------------------------------------------------------
# Typer handles subcommands and argument parsing for queuectl.
app = typer.Typer(
    help="queuectl: A CLI tool for managing the background job queue."
)
console = Console()  # For displaying formatted Rich tables

# =====================================================================
#  STATUS COMMAND
# =====================================================================
@app.command()
def status():
    """
    Display an overview of current job states and worker activity.
    """
    typer.secho("--- Job Status Summary ---", bold=True)
    summary = db.get_job_status_summary()

    # --- Job Summary Table ---
    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("State", style="dim")
    table.add_column("Count")
    table.add_row("Pending", f"[bold green]{summary['pending']}[/bold green]")
    table.add_row("Processing", f"[bold yellow]{summary['processing']}[/bold yellow]")
    table.add_row("Completed", f"[cyan]{summary['completed']}[/cyan]")
    table.add_row("Dead (DLQ)", f"[bold red]{summary['dead']}[/bold red]")
    console.print(table)

    # --- Worker Status ---
    typer.secho("\n--- Worker Status ---", bold=True)
    if db.STOP_FILE_PATH.exists():
        typer.secho("Workers are signaled to STOP.", fg=typer.colors.YELLOW)
    else:
        typer.secho("Workers are (assumed) RUNNING.", fg=typer.colors.GREEN)
        typer.echo("Use 'ps aux | grep worker.py' to verify active processes.")


# =====================================================================
#  JOB LIST COMMAND
# =====================================================================
@app.command()
def list(
    state: str = typer.Option(
        "pending",
        "--state",
        "-s",
        help="Job state to filter by (pending, completed, dead, etc.)",
    ),
):
    """
    List jobs filtered by a given state.
    """
    typer.secho(f"--- Jobs in '{state}' state ---", bold=True)
    jobs = db.list_jobs_by_state(state)

    if not jobs:
        typer.echo(f"No jobs found with state '{state}'.")
        raise typer.Exit()

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
#  DEAD LETTER QUEUE (DLQ) COMMANDS
# =====================================================================
dlq_app = typer.Typer(help="Manage the Dead Letter Queue (DLQ).")
app.add_typer(dlq_app, name="dlq")


@dlq_app.command("list")
def dlq_list():
    """
    Display all jobs currently in the Dead Letter Queue.
    Equivalent to: 'queuectl list --state dead'
    """
    typer.echo("Listing jobs in Dead Letter Queue (state='dead')...")
    jobs = db.list_jobs_by_state("dead")

    if not jobs:
        typer.echo("No jobs found in DLQ.")
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


@dlq_app.command("retry")
def dlq_retry(
    job_id: str = typer.Argument(..., help="ID of the dead job to retry."),
):
    """
    Move a failed (dead) job back to the 'pending' queue for reprocessing.
    """
    typer.echo(f"Attempting to retry job {job_id}...")
    success = db.retry_dead_job(job_id)

    if success:
        typer.secho(
            f" Success: Job {job_id} moved to 'pending' state.",
            fg=typer.colors.GREEN,
        )
    else:
        typer.secho(
            f" Error: Could not retry job {job_id}.",
            fg=typer.colors.RED,
        )
        typer.echo("   (Ensure the job ID exists and is currently in 'dead' state.)")
        raise typer.Exit(code=1)


# =====================================================================
#  CONFIGURATION COMMANDS
# =====================================================================
config_app = typer.Typer(help="Manage queuectl configuration settings.")
app.add_typer(config_app, name="config")


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Configuration key (e.g., max_retries)."),
    value: str = typer.Argument(..., help="Value to set for the key."),
):
    """
    Update a configuration value (e.g., max_retries, backoff_base).
    """
    valid_keys = {"max_retries", "backoff_base"}
    if key not in valid_keys:
        typer.secho(f"Warning: '{key}' is not a standard config key.", fg=typer.colors.YELLOW)
        typer.echo(f"Standard keys are: {', '.join(valid_keys)}")

    if key in {"max_retries", "backoff_base"} and not value.isdigit():
        typer.secho(f"Error: '{key}' must be an integer.", fg=typer.colors.RED)
        raise typer.Exit(code=1)

    typer.echo(f"Setting config: {key} = {value}")
    success = db.set_config_value(key, value)

    if success:
        typer.secho(" Config updated successfully.", fg=typer.colors.GREEN)
    else:
        typer.secho(" Error updating config.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


# =====================================================================
#  ENQUEUE COMMAND
# =====================================================================
@app.command()
def enqueue(
    command: str = typer.Argument(
        ..., help="Command string to execute (e.g., 'sleep 5')."
    ),
    id: Optional[str] = typer.Option(
        None,
        "--id",
        "-i",
        help="Optional custom ID for the job. Defaults to a new UUID.",
    ),
    max_retries: Optional[int] = typer.Option(
        None,
        "--max-retries",
        "-r",
        help="Override max retries for this job (default: config value).",
    ),
):
    """
    Enqueue a new job for background execution.
    """
    logging.info(f"Attempting to enqueue command: '{command}'")

    # Determine Job ID
    final_job_id = id if id else str(uuid.uuid4())

    # Determine Retry Count
    if max_retries is None:
        try:
            config_retries = db.get_config_value("max_retries")
            final_max_retries = int(config_retries)
        except Exception as e:
            logging.warning(f"Could not read 'max_retries' from config. Defaulting to 3. Error: {e}")
            final_max_retries = 3
    else:
        final_max_retries = max_retries

    # Insert Job into Queue
    success = db.insert_job(
        job_id=final_job_id,
        command=command,
        max_retries=final_max_retries,
    )

    # CLI Feedback
    if success:
        typer.secho(" Successfully enqueued job:", fg=typer.colors.GREEN)
        typer.echo(f"   ID: {final_job_id}")
        typer.echo(f"   Command: {command}")
        typer.echo(f"   Max Retries: {final_max_retries}")
    else:
        typer.secho(" Error enqueuing job. Check logs for details.", fg=typer.colors.RED)
        raise typer.Exit(code=1)


# =====================================================================
#  WORKER MANAGEMENT COMMANDS
# =====================================================================
worker_app = typer.Typer(help="Manage background worker processes.")
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
    Launch one or more worker processes in the background.
    """
    if db.STOP_FILE_PATH.exists():
        typer.echo("Removing existing stop file...")
        db.STOP_FILE_PATH.unlink()

    typer.echo(f"Starting {count} worker(s) in the background...")

    processes = []
    for _ in range(count):
        process = multiprocessing.Process(target=worker.start_worker)
        process.start()
        processes.append(process)

    typer.secho("Workers started successfully:", fg=typer.colors.GREEN)
    for process in processes:
        typer.echo(f"  - PID: {process.pid}")

    typer.echo("\nUse 'queuectl worker stop' to terminate gracefully.")
    typer.echo("Note: Workers will continue running in the background.")


@worker_app.command("stop")
def worker_stop():
    """
    Signal all running workers to shut down gracefully.
    """
    if db.STOP_FILE_PATH.exists():
        typer.echo("Stop file already exists. Workers may already be shutting down.")
        return

    typer.echo("Creating stop file to signal workers...")
    try:
        db.STOP_FILE_PATH.touch()
        typer.secho(
            "Signal sent. Workers will stop after completing current jobs.",
            fg=typer.colors.YELLOW,
        )
    except Exception as e:
        typer.secho(f"Error creating stop file: {e}", fg=typer.colors.RED)
        raise typer.Exit(code=1)


# =====================================================================
#  MAIN ENTRY POINT
# =====================================================================
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context):
    """
    CLI entry point executed before all commands.
    Ensures database initialization and proper setup.
    """
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit()

    try:
        db.initialize_database()
        logging.debug("Database initialized successfully.")
    except Exception as e:
        logging.error(f"Failed to initialize database: {e}")
        typer.echo("FATAL: Could not initialize database.", err=True)
        raise typer.Exit(code=1)


if __name__ == "__main__":
    app()
