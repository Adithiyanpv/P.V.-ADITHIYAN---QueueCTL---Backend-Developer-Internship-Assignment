# queuectl Architecture Diagrams

This document provides comprehensive architecture diagrams for the queuectl background job queue system.

---

## System Architecture Overview

```mermaid
graph TB
    subgraph "User Interface Layer"
        CLI[queuectl.py<br/>CLI Interface<br/>Typer Framework]
    end
    
    subgraph "Processing Layer"
        W1[Worker Process 1<br/>worker.py]
        W2[Worker Process 2<br/>worker.py]
        W3[Worker Process N<br/>worker.py]
        MP[multiprocessing<br/>Process Manager]
    end
    
    subgraph "Data Layer"
        DB[(SQLite Database<br/>~/.queuectl/queue.db<br/>WAL Mode Enabled)]
        CONFIG[Config Table<br/>max_retries<br/>backoff_base]
        JOBS[Jobs Table<br/>id, command, state<br/>attempts, max_retries<br/>run_at, created_at]
    end
    
    subgraph "File System"
        STOP[stop_workers<br/>Poison Pill File]
        LOG[worker.log<br/>Log File]
    end
    
    CLI -->|enqueue/list/status/dlq/config| DB
    CLI -->|worker start| MP
    CLI -->|worker stop| STOP
    MP -->|spawns| W1
    MP -->|spawns| W2
    MP -->|spawns| W3
    W1 -->|poll & fetch| DB
    W2 -->|poll & fetch| DB
    W3 -->|poll & fetch| DB
    W1 -->|execute jobs| EXEC[Subprocess Execution]
    W2 -->|execute jobs| EXEC
    W3 -->|execute jobs| EXEC
    W1 -->|check| STOP
    W2 -->|check| STOP
    W3 -->|check| STOP
    W1 -->|write logs| LOG
    W2 -->|write logs| LOG
    W3 -->|write logs| LOG
    DB -->|stores| CONFIG
    DB -->|stores| JOBS
    
    style CLI fill:#e1f5ff
    style W1 fill:#fff4e1
    style W2 fill:#fff4e1
    style W3 fill:#fff4e1
    style DB fill:#e8f5e9
    style STOP fill:#ffebee
```

---

## Job Lifecycle State Machine

```mermaid
stateDiagram-v2
    [*] --> Pending: User enqueues job
    
    Pending --> Processing: Worker fetches job<br/>(Atomic transaction)
    
    Processing --> Completed: Execution succeeds<br/>(Exit code 0)
    Processing --> RetryCheck: Execution fails<br/>(Non-zero exit code)
    
    RetryCheck --> Pending: attempts < max_retries<br/>Set run_at = now + base^attempts<br/>(Exponential backoff)
    RetryCheck --> Dead: attempts >= max_retries<br/>(Move to DLQ)
    
    Dead --> Pending: User retries from DLQ<br/>(Reset attempts)
    
    Completed --> [*]
    Dead --> [*]: (Manual retry required)
    
    note right of Pending
        Jobs wait here until
        run_at time is reached
        and a worker is available
    end note
    
    note right of Processing
        Only one worker can
        process a job at a time
        (Atomic fetch prevents races)
    end note
    
    note right of Dead
        Dead Letter Queue (DLQ)
        Permanent failures
        Manual intervention required
    end note
```

---

## Complete Job Execution Flow

```mermaid
sequenceDiagram
    participant User
    participant CLI as queuectl.py
    participant DB as SQLite DB
    participant Worker as worker.py
    participant Subprocess
    
    User->>CLI: enqueue "command" --id "job-1"
    CLI->>DB: INSERT job (state='pending')
    DB-->>CLI: Job created
    CLI-->>User: ✅ Job enqueued
    
    Note over Worker: Worker polling loop
    Worker->>DB: BEGIN IMMEDIATE TRANSACTION
    Worker->>DB: SELECT * FROM jobs WHERE state='pending'<br/>AND run_at <= NOW() LIMIT 1
    DB-->>Worker: Return job (if available)
    Worker->>DB: UPDATE job SET state='processing'
    Worker->>DB: COMMIT TRANSACTION
    Note over Worker,DB: Atomic fetch prevents race conditions
    
    alt Job found
        Worker->>Subprocess: Execute command (shell=True)
        Subprocess-->>Worker: Exit code
        
        alt Exit code = 0 (Success)
            Worker->>DB: UPDATE job SET state='completed'
            DB-->>Worker: ✅ Job completed
        else Exit code != 0 (Failure)
            Worker->>DB: UPDATE job SET attempts = attempts + 1
            Worker->>DB: Check: attempts < max_retries?
            
            alt attempts < max_retries
                Worker->>DB: UPDATE job SET state='pending',<br/>run_at = NOW() + base^attempts
                DB-->>Worker: Job scheduled for retry
                Note over Worker,DB: Exponential backoff applied
            else attempts >= max_retries
                Worker->>DB: UPDATE job SET state='dead'
                DB-->>Worker: Job moved to DLQ
            end
        end
    else No job found
        Worker->>Worker: Sleep (poll interval)
    end
    
    Worker->>Worker: Check stop_workers file
    
    alt stop_workers file exists
        Worker->>Worker: Finish current job
        Worker->>Worker: Exit gracefully
    else stop_workers file not found
        Worker->>Worker: Continue polling
    end
```

---

## Worker Management Flow

```mermaid
graph LR
    subgraph "Worker Start Process"
        START[queuectl worker start<br/>--count N] --> MP[multiprocessing.Process]
        MP --> W1[Worker 1<br/>PID: 12345]
        MP --> W2[Worker 2<br/>PID: 12346]
        MP --> WN[Worker N<br/>PID: 12347]
        W1 --> LOOP1[Main Loop]
        W2 --> LOOP2[Main Loop]
        WN --> LOOPN[Main Loop]
    end
    
    subgraph "Worker Stop Process"
        STOP[queuectl worker stop] --> CREATE[Create stop_workers file<br/>~/.queuectl/stop_workers]
        CREATE --> CHECK1[Worker 1 checks file]
        CREATE --> CHECK2[Worker 2 checks file]
        CREATE --> CHECKN[Worker N checks file]
        CHECK1 --> FIN1[Finish current job<br/>Exit gracefully]
        CHECK2 --> FIN2[Finish current job<br/>Exit gracefully]
        CHECKN --> FINN[Finish current job<br/>Exit gracefully]
    end
    
    subgraph "Worker Main Loop"
        POLL[Poll database<br/>for pending jobs] --> FETCH{Job<br/>available?}
        FETCH -->|Yes| EXEC[Execute job]
        FETCH -->|No| SLEEP[Sleep interval]
        EXEC --> UPDATE[Update job state]
        UPDATE --> CHECK[Check stop_workers file]
        SLEEP --> CHECK
        CHECK -->|File exists| EXIT[Exit gracefully]
        CHECK -->|File not found| POLL
    end
    
    style START fill:#e1f5ff
    style STOP fill:#ffebee
    style EXIT fill:#ffebee
```

---

## Database Schema & Concurrency Model

```mermaid
erDiagram
    JOBS {
        string id PK "Unique job identifier"
        string command "Shell command to execute"
        string state "pending|processing|completed|dead"
        int attempts "Current retry attempt count"
        int max_retries "Maximum retry attempts"
        datetime run_at "Scheduled execution time"
        datetime created_at "Job creation timestamp"
        datetime updated_at "Last update timestamp"
    }
    
    CONFIG {
        string key PK "Configuration key"
        string value "Configuration value"
    }
```

### State Transitions

The `JOBS` table tracks jobs through the following state transitions:

- **pending** → **processing** → **completed** (successful execution)
- **pending** → **processing** → **pending** (retry with exponential backoff)
- **pending** → **processing** → **dead** (moved to DLQ after max retries)
- **dead** → **pending** (manual retry from DLQ)

### Configuration Defaults

The `CONFIG` table stores system-wide settings:

- `max_retries`: Default maximum retry attempts (default: 3)
- `backoff_base`: Base for exponential backoff calculation (default: 2)

---

## Concurrency & Race Condition Prevention

```mermaid
sequenceDiagram
    participant W1 as Worker 1
    participant W2 as Worker 2
    participant DB as SQLite DB<br/>(WAL Mode)
    
    Note over W1,W2: Both workers polling simultaneously
    
    W1->>DB: BEGIN IMMEDIATE TRANSACTION
    W2->>DB: BEGIN IMMEDIATE TRANSACTION<br/>(Waits - W1 has lock)
    
    W1->>DB: SELECT * FROM jobs<br/>WHERE state='pending'<br/>AND run_at <= NOW()<br/>LIMIT 1 FOR UPDATE
    DB-->>W1: Job: job-1
    
    W1->>DB: UPDATE jobs SET state='processing'<br/>WHERE id='job-1'
    W1->>DB: COMMIT TRANSACTION
    Note over W1,DB: Atomic operation complete
    
    DB-->>W2: Transaction lock released
    W2->>DB: SELECT * FROM jobs<br/>WHERE state='pending'<br/>AND run_at <= NOW()<br/>LIMIT 1 FOR UPDATE
    DB-->>W2: Job: job-2 (different job)
    
    W2->>DB: UPDATE jobs SET state='processing'<br/>WHERE id='job-2'
    W2->>DB: COMMIT TRANSACTION
    
    Note over W1,W2: ✅ No race condition:<br/>Each worker gets a unique job
```

---

## Exponential Backoff Calculation

```mermaid
graph TD
    START[Job fails execution] --> INCR[Increment attempts]
    INCR --> CHECK{attempts < max_retries?}
    
    CHECK -->|Yes| CALC["Calculate delay:<br/>delay = backoff_base ^ attempts"]
    CHECK -->|No| DLQ["Move to DLQ<br/>state='dead'"]
    
    CALC --> EXAMPLES["Examples with base=2:<br/>attempt 1: 2^1 = 2 seconds<br/>attempt 2: 2^2 = 4 seconds<br/>attempt 3: 2^3 = 8 seconds"]
    
    EXAMPLES --> UPDATE_TIME["Update run_at time<br/>run_at = current_time + delay"]
    UPDATE_TIME --> RESET["Reset state to 'pending'"]
    RESET --> WAIT["Job waits until run_at"]
    WAIT --> FETCH["Worker fetches when<br/>run_at time reached"]
    
    style CALC fill:#fff4e1
    style DLQ fill:#ffebee
    style EXAMPLES fill:#e8f5e9
```

---

## Complete System Interaction Flow

```mermaid
graph TB
    subgraph "User Operations"
        ENQ[Enqueue Job]
        LIST[List Jobs]
        STATUS[Check Status]
        DLQ[DLQ Operations]
        CONFIG[Set Configuration]
        START_W[Start Workers]
        STOP_W[Stop Workers]
    end
    
    subgraph "CLI Layer (queuectl.py)"
        CLI[Typer CLI Router]
    end
    
    subgraph "Data Access Layer (db.py)"
        DB_OPS[Database Operations:<br/>- enqueue_job<br/>- fetch_pending_job<br/>- update_job_state<br/>- list_jobs<br/>- get_config<br/>- set_config]
    end
    
    subgraph "Worker Layer (worker.py)"
        WORKER[Worker Process]
        POLL[Poll Database]
        EXEC[Execute Command]
        RETRY[Handle Retries]
        BACKOFF[Calculate Backoff]
    end
    
    subgraph "Storage"
        SQLITE[(SQLite Database<br/>queue.db)]
        STOP_FILE[stop_workers file]
    end
    
    ENQ --> CLI
    LIST --> CLI
    STATUS --> CLI
    DLQ --> CLI
    CONFIG --> CLI
    START_W --> CLI
    STOP_W --> CLI
    
    CLI --> DB_OPS
    CLI --> START_W
    CLI --> STOP_FILE
    
    DB_OPS --> SQLITE
    
    START_W --> WORKER
    WORKER --> POLL
    POLL --> DB_OPS
    DB_OPS --> EXEC
    EXEC --> RETRY
    RETRY --> BACKOFF
    BACKOFF --> DB_OPS
    
    WORKER --> STOP_FILE
    
    style CLI fill:#e1f5ff
    style WORKER fill:#fff4e1
    style SQLITE fill:#e8f5e9
    style STOP_FILE fill:#ffebee
```

---

## Key Architectural Decisions

### 1. **SQLite over JSON**
- **Reason:** Atomic transactions prevent race conditions
- **Benefit:** Multiple workers can safely fetch jobs concurrently
- **Implementation:** `BEGIN IMMEDIATE ... COMMIT` transactions

### 2. **Poison Pill Shutdown**
- **Reason:** Graceful worker termination without losing jobs
- **Benefit:** Workers finish current job before exiting
- **Implementation:** File-based signal (`~/.queuectl/stop_workers`)

### 3. **Exponential Backoff**
- **Reason:** Prevent overwhelming system with failed retries
- **Benefit:** Gradually increasing delays reduce load
- **Implementation:** `delay = base ^ attempts` seconds

### 4. **Atomic Job Fetching**
- **Reason:** Prevent multiple workers from processing same job
- **Benefit:** Guaranteed job uniqueness per worker
- **Implementation:** Transaction with `FOR UPDATE` lock

### 5. **WAL Mode**
- **Reason:** Enable concurrent reads and writes
- **Benefit:** Multiple workers can operate simultaneously
- **Implementation:** SQLite WAL (Write-Ahead Logging) enabled

---

## Component Responsibilities

| Component | Responsibility | Key Functions |
|-----------|---------------|---------------|
| **queuectl.py** | CLI Interface | - Parse user commands<br/>- Route to appropriate operations<br/>- Display formatted output<br/>- Manage worker processes |
| **worker.py** | Job Execution Engine | - Poll database for jobs<br/>- Execute shell commands<br/>- Handle retries and backoff<br/>- Graceful shutdown |
| **db.py** | Data Persistence | - Database initialization<br/>- Job CRUD operations<br/>- Atomic job fetching<br/>- Configuration management |

---

## Data Flow Summary

1. **Enqueue Flow:** User → CLI → DB (insert pending job)
2. **Execution Flow:** Worker → DB (atomic fetch) → Subprocess → DB (update state)
3. **Retry Flow:** Worker → Calculate backoff → DB (update run_at) → Wait → Fetch again
4. **DLQ Flow:** Worker → Max retries reached → DB (state='dead') → User → CLI → DB (retry)
5. **Shutdown Flow:** User → CLI → Create stop file → Worker → Check file → Exit gracefully

---

*This architecture ensures reliability, concurrency safety, and graceful error handling while maintaining simplicity and ease of use.*

