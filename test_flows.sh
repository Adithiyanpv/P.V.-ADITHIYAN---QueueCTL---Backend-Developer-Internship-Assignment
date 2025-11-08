#!/bin/bash

echo "=== QueueCTL Test Script ==="
echo

# ---------------------------------------------------------------------
# 1. Environment Cleanup
# ---------------------------------------------------------------------
# Remove any existing queue database or stop signal file to ensure
# a clean environment before starting the test sequence.
echo "🧹 Cleaning up old database and stop files..."
rm -f ~/.queuectl/queue.db
rm -f ~/.queuectl/stop_workers

# Define Python commands to ensure consistent execution
# (Assumes the current environment/venv is already activated)
PY_CMD="python queuectl.py"
WORKER_CMD="python worker.py"
echo

# ---------------------------------------------------------------------
# 2. Check Initial Status
# ---------------------------------------------------------------------
# This will automatically create a fresh database if it does not exist.
echo "--- Checking initial system status ---"
$PY_CMD status
echo

# ---------------------------------------------------------------------
# 3. Configure Runtime Settings
# ---------------------------------------------------------------------
# Set configuration parameters to validate retry and backoff logic.
echo "--- Applying configuration ---"
$PY_CMD config set max_retries 2
$PY_CMD config set backoff_base 2
echo

# ---------------------------------------------------------------------
# 4. Enqueue Test Jobs
# ---------------------------------------------------------------------
# Add multiple jobs for validation, including one that is expected to fail.
echo "--- Enqueuing test jobs ---"
$PY_CMD enqueue "echo 'Job 1: Success'" --id "job-1"
$PY_CMD enqueue "echo 'Job 2: Success'" --id "job-2"
$PY_CMD enqueue "this-command-will-fail" --id "job-fail"
echo "# Note: 'job-fail' will use max_retries=2 per configuration."
echo

# ---------------------------------------------------------------------
# 5. Validate Job Queue State
# ---------------------------------------------------------------------
# Verify that all newly added jobs appear in the 'pending' state.
echo "--- Verifying pending jobs ---"
$PY_CMD list --state pending
echo

# ---------------------------------------------------------------------
# 6. Start Worker Process
# ---------------------------------------------------------------------
# Launch the worker in the background to process queued jobs.
# Logs are redirected to 'worker.log' for post-run inspection.
echo "--- Starting worker in background ---"
$WORKER_CMD > worker.log 2>&1 &
WORKER_PID=$!
echo "🚀 Worker started with PID: $WORKER_PID"
echo "   Logs are being written to: worker.log"
echo

# ---------------------------------------------------------------------
# 7. Allow Processing Time
# ---------------------------------------------------------------------
# Wait for the worker to process jobs.
# The sleep duration accommodates retries and backoff delays.
echo "--- Waiting for job processing to complete ---"
echo "⏳ Sleeping for 10 seconds..."
sleep 10
echo

# ---------------------------------------------------------------------
# 8. Post-Processing Status Check
# ---------------------------------------------------------------------
# After processing, we expect:
#   - 2 jobs marked as 'completed'
#   - 1 job moved to 'dead'
echo "--- Checking system status after processing ---"
$PY_CMD status
echo

# ---------------------------------------------------------------------
# 9. Review Dead Letter Queue (DLQ)
# ---------------------------------------------------------------------
# The failed job should now appear in the DLQ.
echo "--- Inspecting DLQ ---"
$PY_CMD dlq list
echo

# ---------------------------------------------------------------------
# 10. Retry Failed Job
# ---------------------------------------------------------------------
# Move the failed job from DLQ back into the queue for reprocessing.
echo "--- Retrying failed job ---"
$PY_CMD dlq retry "job-fail"
echo

# ---------------------------------------------------------------------
# 11. Verify Pending Jobs After Retry
# ---------------------------------------------------------------------
# The retried job should reappear in the pending list with 0 attempts.
echo "--- Verifying pending jobs after retry ---"
$PY_CMD list --state pending
echo

# ---------------------------------------------------------------------
# 12. Graceful Worker Shutdown
# ---------------------------------------------------------------------
# Send a stop signal to the worker and wait for it to exit cleanly.
echo "--- Initiating graceful shutdown ---"
$PY_CMD worker stop
echo "🚦 Stop signal sent. Waiting for worker (PID: $WORKER_PID) to exit..."
wait $WORKER_PID
echo " Worker has shut down successfully."
echo

# ---------------------------------------------------------------------
# 13. Final System Status
# ---------------------------------------------------------------------
# Display the final system summary after all operations.
echo "--- Final system status ---"
$PY_CMD status
echo

echo "=== Test Sequence Complete ==="
