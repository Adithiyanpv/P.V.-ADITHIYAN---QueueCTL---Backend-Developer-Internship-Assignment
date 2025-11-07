#!/bin/bash

echo "--- QueueCTL Test Script ---"

# --- 1. Cleanup ---
echo "🧹 Cleaning up old database and stop files..."
rm -f ~/.queuectl/queue.db
rm -f ~/.queuectl/stop_workers
# Ensure python is using the right interpreter
# (This assumes you are in the venv)
PY_CMD="python queuectl.py"
WORKER_CMD="python worker.py"

echo "--- 2. Initial Status (DB will be created) ---"
$PY_CMD status

echo "--- 3. Test Config Set ---"
$PY_CMD config set max_retries 2
$PY_CMD config set backoff_base 2

echo "--- 4. Test Enqueue ---"
$PY_CMD enqueue "echo 'Job 1: Success'" --id "job-1"
$PY_CMD enqueue "echo 'Job 2: Success'" --id "job-2"
$PY_CMD enqueue "this-command-will-fail" --id "job-fail"
# Note: job-fail will use max_retries=2 from the config we just set

echo "--- 5. Test List ---"
$PY_CMD list --state pending

echo "--- 6. Start Worker in Background ---"
# Start a single worker process in the background
$WORKER_CMD > worker.log 2>&1 &
WORKER_PID=$!
echo "🚀 Worker started with PID: $WORKER_PID"
echo "   (Logs will be in worker.log)"

echo "--- 7. Wait for processing ---"
echo "⏳ Waiting 10 seconds for worker to process jobs..."
# (2s for fail 1 + 4s for fail 2 + buffer)
sleep 10

echo "--- 8. Test Status After Processing ---"
$PY_CMD status
# We expect: 2 completed, 1 dead

echo "--- 9. Test DLQ List ---"
$PY_CMD dlq list
# We expect 'job-fail' to be here

echo "--- 10. Test DLQ Retry ---"
$PY_CMD dlq retry "job-fail"

echo "--- 11. Test Status After Retry ---"
$PY_CMD list --state pending
# We expect 'job-fail' to be here (with 0 attempts)

echo "--- 12. Test Graceful Shutdown ---"
$PY_CMD worker stop
echo "🚦 Sent stop signal. Waiting for worker (PID: $WORKER_PID) to exit..."
wait $WORKER_PID
echo "✅ Worker has shut down."

echo "--- 13. Final Status ---"
$PY_CMD status

echo "--- Test Complete ---"