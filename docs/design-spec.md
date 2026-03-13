# AIDD — Autonomous AI Development Daemon

> Design Spec | 2026-03-13 | Status: Draft | Version: 1

## 1. Overview

AIDD is a standalone, project-agnostic CLI tool that orchestrates autonomous AI-powered software development. It manages a feature queue, spawns headless Claude Code agents (`claude -p`) in isolated git worktrees, tracks progress, and provides remote monitoring/control via Telegram.

**Core value:** Developers queue features during the day, AIDD builds them overnight on their Max subscription ($0 extra cost), and PRs are ready for review by morning.

### Goals

- Autonomous overnight feature development without human intervention
- Remote monitoring and control via Telegram (phone-friendly)
- Project-agnostic — works on any codebase with a config file
- Zero additional cost — runs on Claude Max subscription via `claude -p`
- Shareable — any developer installs it, runs `aidd init`, and has an autonomous pipeline

### Non-Goals

- Replacing human judgment for architecture/design decisions
- Auto-merging to main without human approval
- Supporting non-Claude agents (future extension, not v1)
- Building a web dashboard (v1 is CLI + Telegram only)
- Multi-project management from a single daemon (v1 is one daemon per project; run separate instances for multiple projects)

## 2. Architecture

### Architecture: With OpenClaw

```
┌──────────────────────────────────────────────────────────────┐
│                     Developer's Machine                       │
│                                                              │
│  ┌─────────────────────────────────────────────────────────┐ │
│  │                    OpenClaw Gateway                      │ │
│  │  ┌──────────┐  ┌──────────┐  ┌───────────────────────┐ │ │
│  │  │ Telegram  │  │   Cron   │  │   ACP Backend         │ │ │
│  │  │ Adapter   │  │ Heartbeat│  │   (spawns claude -p)  │ │ │
│  │  └────┬─────┘  └────┬─────┘  └──────────┬────────────┘ │ │
│  │       └──────────────┴────────────────────┘              │ │
│  └───────────────────────┼──────────────────────────────────┘ │
│                          │                                    │
│  ┌───────────────────────▼──────────────────────────────────┐ │
│  │              AIDD Conductor                               │ │
│  │  ┌─────────────┐  ┌──────────────┐  ┌────────────────┐ │ │
│  │  │ Feature     │  │ Worktree     │  │ Human Gate     │ │ │
│  │  │ Queue       │  │ Manager      │  │ Manager        │ │ │
│  │  │ (SQLite)    │  │ (git)        │  │ (Telegram)     │ │ │
│  │  └──────┬──────┘  └──────┬───────┘  └───────┬────────┘ │ │
│  │         │                │                   │          │ │
│  └─────────┼────────────────┼───────────────────┼──────────┘ │
│            │                │                   │            │
│    ┌───────▼───────┐ ┌─────▼──────┐    ┌───────▼────────┐  │
│    │ claude -p     │ │ claude -p  │    │  Developer on  │  │
│    │ (worktree A)  │ │(worktree B)│    │  phone via     │  │
│    │ --permission  │ │--permission│    │  Telegram      │  │
│    │  -mode auto   │ │ -mode auto │    │                │  │
│    └───────────────┘ └────────────┘    └────────────────┘  │
└──────────────────────────────────────────────────────────────┘
```

### Architecture: Standalone (no OpenClaw)

```
┌──────────────────────────────────────────────────────────────┐
│                     Developer's Machine                       │
│                                                              │
│  ┌───────────────────────────────────────────────────────┐   │
│  │              AIDD Daemon (standalone)                   │   │
│  │  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐ │   │
│  │  │ Feature     │  │ Worktree     │  │ Telegram Bot │ │   │
│  │  │ Queue       │  │ Manager      │  │ (direct API) │ │   │
│  │  │ (SQLite)    │  │ (git)        │  │              │ │   │
│  │  └──────┬──────┘  └──────┬───────┘  └──────┬───────┘ │   │
│  │         │                │                  │         │   │
│  └─────────┼────────────────┼──────────────────┼─────────┘   │
│            │                │                  │             │
│    ┌───────▼───────┐ ┌─────▼──────┐   ┌───────▼────────┐   │
│    │ claude -p     │ │ claude -p  │   │  Developer on  │   │
│    │ (worktree A)  │ │(worktree B)│   │  phone         │   │
│    └───────────────┘ └────────────┘   └────────────────┘   │
└──────────────────────────────────────────────────────────────┘
```

### Three Layers

1. **OpenClaw Gateway** — Infrastructure layer. Handles Telegram adapter, cron scheduling, ACP process spawning. Already installed on the user's machine. Optional — AIDD can also run standalone as a daemon without OpenClaw, with reduced features (no Telegram chat mode, only webhook/telegram-bot-api notifications).

2. **AIDD Conductor** — Logic layer. Manages feature queue, git worktrees, human gates, state tracking, error recovery. This is the new tool we build. Registers as an OpenClaw plugin OR runs as a standalone Python daemon.

3. **Claude Code Agents** — Execution layer. Headless `claude -p` sessions doing actual implementation work. Each runs in an isolated git worktree. Uses Max subscription ($0 extra).

### OpenClaw Integration

OpenClaw's ACP (Agent Client Protocol) replaces direct API token usage:

| Before | After |
|--------|-------|
| OpenClaw → Claude API (token-based, ~$6+/day) | OpenClaw → ACP → `claude -p` (subscription, $0) |
| "Temporarily overloaded" errors | Subscription-tier priority, reliable |
| Text chat only | Full Claude Code toolset (files, bash, grep, edit) |
| Stateless per message | Resumable sessions, project-aware |

Two modes through the same infrastructure:

**Chat Mode:** Telegram message → OpenClaw ACP → `claude -p --permission-mode auto` in project dir → response back to Telegram. For ad-hoc queries.

**Pipeline Mode:** Conductor cron → pick feature → `cd <worktree> && claude -p "<prompt>" --permission-mode auto --output-format stream-json --model <model> --allowed-tools <tools>` → full autonomous implementation. For overnight runs.

## 3. Configuration

### Project Config: `aidd.yml`

Every project gets one config file. This is the only project-specific setup required.

```yaml
# aidd.yml — drop this in any project root
version: 1                        # config schema version

project:
  name: tradecore                # display name for notifications
  path: ~/Desktop/trade          # auto-detected, overridable

# Agent settings
agent:
  max_parallel: 2                # concurrent worktree agents
  model: opus                    # opus | sonnet | haiku
  permission_mode: auto          # auto | bypassPermissions (see Security section)
  output_format: stream-json     # stream-json for structured output parsing
  timeout_hours: 4               # kill agent after this many hours (safety net)
  allowed_tools:                 # restrict agent capabilities (sandbox model)
    - Edit
    - Write
    - Bash
    - Read
    - Grep
    - Glob

# Project-specific shell commands (what varies between projects)
commands:
  test: "pytest tests/ -x -q"         # shell command
  lint: "ruff check . --fix && ruff format ."  # shell command
  typecheck: ""                        # empty = skip
  build: ""                            # empty = skip

# Additional instructions injected into agent prompt (NOT shell commands)
prompt_instructions:
  pre_implement: |
    Before starting, read these files for project conventions:
    - CLAUDE.md
    - docs/reference/feature-dev-standards.md
  post_implement: |
    After all tests pass, run /validate to verify spec compliance.

# Where things live in THIS project
paths:
  specs: docs/specs/
  plans: docs/plans/
  worktrees: .worktrees/         # gitignored
  state: .aidd/                  # gitignored

# Implementation prompt template
# See "Prompt Template Variables" section below for complete reference
prompt_template: |
  You are executing an autonomous implementation task for {project_name}.

  Plan: {plan_path}
  Spec: {spec_path}

  {pre_implement_instructions}

  Instructions:
  1. Read the plan file — this is your source of truth
  2. Read CLAUDE.md for project conventions
  3. Follow TDD: test first → implement → verify → lint
  4. After each passing phase, commit with message: "[aidd:{feature_id}] phase N/M: <description>"
  5. After each commit, write progress to .aidd/progress/{feature_id}.json (see Progress Contract)
  6. If stuck after 3 attempts on any task, write STUCK status to progress file and stop

  Test command: {test_cmd}
  Lint command: {lint_cmd}

  {feedback}

  Do NOT install new dependencies unless the plan says to.

  {post_implement_instructions}

# Pipeline behavior
pipeline:
  pause_on_failure: false        # skip stuck features, continue queue
  auto_pr: true                  # create PR on completion
  auto_merge: false              # always require human approval for merge
  cleanup_worktrees: true        # remove worktree after merge

# Schedule (all times in configured timezone)
schedule:
  overnight_start: "22:00"
  overnight_end: "06:00"         # stop spawning new agents after this
  daily_summary: "06:00"
  evening_report: "18:00"
  health_check_interval: 30m
  timezone: Asia/Kolkata

# Notification channels
notifications:
  telegram:
    enabled: true
    bot_token_env: AIDD_TELEGRAM_TOKEN    # env var name, never hardcoded
    chat_id_env: AIDD_TELEGRAM_CHAT_ID
    allowed_users: []                      # Telegram user IDs, empty = owner only
    notify_on: [complete, failed, stuck, needs_approval, daily_summary]
  webhook:
    enabled: false
    url_env: AIDD_WEBHOOK_URL

# Human gates — what requires approval before proceeding
gates:
  spec_approval: true
  plan_approval: true
  pr_merge: true
  stuck_resolution: true
```

### Cross-Project Examples

The same tool, different projects:

```yaml
# Python/FastAPI project
commands:
  test: "pytest tests/ -x -q"
  lint: "ruff check . --fix"

# TypeScript/Next.js project
commands:
  test: "pnpm test --run"
  lint: "pnpm lint --fix"
  build: "pnpm build"
  typecheck: "pnpm tsc --noEmit"

# Rust project
commands:
  test: "cargo test"
  lint: "cargo clippy --fix"
  build: "cargo build --release"
```

## 4. Feature Queue

### Format: `feature-queue.yml`

```yaml
# feature-queue.yml — human-friendly format for editing; imported into SQLite via `aidd queue import`
version: 1
features:
  - id: data-platform-core
    spec: docs/specs/data-platform.md
    plan: docs/plans/data-platform-plan.md
    priority: 1
    status: queued               # queued → running → review → approved → merged → abandoned
    depends_on: []
    human_gates: [pr_merge]      # override global gates for this feature; empty = use global
    feedback: ""                 # filled by /reject with reason

  - id: strategy-engine
    spec: docs/specs/strategy-engine.md
    plan: docs/plans/strategy-engine-plan.md
    priority: 2
    status: queued
    depends_on: [data-platform-core]
    human_gates: []              # empty = use global gates config
    feedback: ""
```

### Status Lifecycle

```
                    ┌──────────────────────────────────────────────┐
                    │                                              │
queued ──→ running ──→ review ──→ merged                          │
              │          │                                         │
              ▼          ▼                                         │
           stuck    rejected ──→ queued (with feedback)            │
              │                                                    │
              ├──→ queued (via /skip, retry with feedback) ────────┘
              │
              └──→ abandoned (via /remove)
```

**Status definitions:**
- `queued` — waiting to be picked up by conductor
- `running` — agent actively working in a worktree
- `review` — implementation complete, PR created, waiting for human approval
- `merged` — PR approved and merged, worktree cleaned up
- `stuck` — agent hit 3-retry limit or max turns; waiting for human input
- `rejected` — human rejected the PR; transitions back to `queued` with feedback
- `abandoned` — removed from queue (kept in file for history)

### Queue Rules

1. Features are picked by priority (lowest number first), then by position in file
2. A feature is eligible only if all `depends_on` entries have status `merged`
3. Maximum `agent.max_parallel` features can have status `running` simultaneously
4. `depends_on` references other feature IDs in the same queue file
5. `rejected` features re-enter the queue with their `feedback` field populated; the feedback is injected into the agent prompt on the next attempt

## 5. Conductor Loop

The conductor is the core state machine. Runs every 5 minutes (configurable).

```
EVERY 5 MINUTES:
  1. Read feature queue from SQLite state DB
  2. Count features with status "running" → running_count
  3. If running_count >= max_parallel → skip, wait for next cycle
  4. Find next eligible feature:
     - status == "queued"
     - all depends_on have status "merged"
     - ordered by priority, then position
  5. If none eligible → idle, wait for next cycle
  6. Pre-flight checks (see Section 8)
  7. If checks pass:
     a. git worktree add <worktrees_path>/<id> -b feature/<id>
     b. Update feature status → "running" (atomic DB write)
     c. Create .aidd/progress/<id>.json in worktree
     d. Spawn in worktree directory:
        cd <worktrees_path>/<id> && claude -p "<constructed prompt>" \
          --permission-mode <permission_mode> \
          --output-format stream-json \
          --model <model> \
          --allowed-tools <allowed_tools>
     e. Tee stream-json output to .aidd/logs/<id>.log
     f. Monitor process (non-blocking)
  8. For each running agent, check:
     a. Read .aidd/progress/<id>.json for phase/status updates
     b. Process exited with code 0 + progress shows "complete":
        - Run test command in worktree to verify
        - Create PR via gh pr create
        - Status → "review" (atomic DB write)
        - Notify Telegram: "Feature complete, PR ready"
     c. Progress shows "stuck" OR process exited non-zero:
        - Status → "stuck" (atomic DB write)
        - Capture error from progress file + last 100 lines of log
        - Notify Telegram with error context
     d. Process still running:
        - Update agent stats from progress file
        - Continue monitoring
  9. For features in "review" status, check for human gate responses
  10. If feature approved:
      - Merge PR via gh pr merge
      - Cleanup worktree + branch
      - Status → "merged" (atomic DB write)
      - Re-evaluate queue (dependents may now be eligible)
      - Auto-rebase any running feature worktrees against updated main
  11. If feature rejected:
      - Store feedback in DB
      - Cleanup worktree
      - Status → "rejected" → immediately transitions to "queued"
```

### Inter-Feature Conflict Resolution

When features run in parallel and one merges:

1. After merge, conductor runs `git rebase main` in all running worktrees
2. If rebase succeeds → agent continues unaware
3. If rebase fails → mark feature as `stuck` with message "rebase conflict after <merged-feature> merged"
4. Notify Telegram with conflicting files
5. Human can resolve manually or `/skip` the feature

To minimize conflicts: features sharing a `depends_on` chain are serialized by default. Features with no dependency relationship run in parallel.

## 5a. Progress Contract

The agent and conductor communicate via a progress file. The agent writes it after each phase commit; the conductor reads it to track status.

### File: `.aidd/progress/<feature-id>.json`

```json
{
  "feature_id": "data-platform-core",
  "status": "running",
  "current_phase": 3,
  "total_phases": 5,
  "phases_completed": [
    {"phase": 1, "commit": "abc1234", "tests_passing": 8, "tests_failing": 0},
    {"phase": 2, "commit": "def5678", "tests_passing": 15, "tests_failing": 0}
  ],
  "stuck_reason": null,
  "last_updated": "2026-03-13T23:42:00+05:30"
}
```

**Status values:** `running`, `complete`, `stuck`

**How the agent writes it:** The prompt template instructs the agent to write this file after each phase commit. The agent parses its own test output and updates the counts.

**How the conductor reads it:** Every 5-minute cycle, conductor reads the progress file to update notifications and state. This is a one-way contract — conductor never writes to the progress file.

## 5b. Prompt Template Variables

Complete reference for all variables available in `prompt_template`:

| Variable | Source | Default if Missing |
|---|---|---|
| `{project_name}` | `aidd.yml` → `project.name` | Directory name |
| `{plan_path}` | `feature-queue.yml` → feature `plan` field | Required, error if missing |
| `{spec_path}` | `feature-queue.yml` → feature `spec` field | Empty string |
| `{feature_id}` | `feature-queue.yml` → feature `id` field | Required |
| `{test_cmd}` | `aidd.yml` → `commands.test` | Empty string (skip) |
| `{lint_cmd}` | `aidd.yml` → `commands.lint` | Empty string (skip) |
| `{feedback}` | `feature-queue.yml` → feature `feedback` field | Empty string |
| `{pre_implement_instructions}` | `aidd.yml` → `prompt_instructions.pre_implement` | Empty string |
| `{post_implement_instructions}` | `aidd.yml` → `prompt_instructions.post_implement` | Empty string |

**Undefined variables:** Left as literal `{var_name}` string (not silently emptied, not error). Logged as warning.

## 6. Telegram Interface

### Pipeline Control

| Command | Action |
|---|---|
| `/status` | Pipeline overview — running/queued/stuck/complete |
| `/status <id>` | Detailed: phase, tests, last activity, elapsed time |
| `/start` | Start conductor daemon |
| `/pause` | Pause — finish current agents, don't start new |
| `/resume` | Resume paused pipeline |
| `/abort <id>` | Kill running agent, mark stuck (requires confirmation) |

### Human Gates

| Command | Action |
|---|---|
| `/approve <id>` | Approve whatever gate the feature is waiting on |
| `/approve all` | Approve all features in "review" status |
| `/reject <id> <reason>` | Reject with feedback — feature re-queued with context |
| `/diff <id>` | Condensed PR diff summary |
| `/logs <id>` | Last 50 lines of agent output |

### Queue Management

| Command | Action |
|---|---|
| `/queue` | Full queue with priorities and dependencies |
| `/add <spec-path>` | Add new feature to queue |
| `/reprioritize <id> <n>` | Change priority |
| `/skip <id>` | Skip stuck feature, move to next |
| `/remove <id>` | Remove from queue (requires confirmation) |

### Chat Mode

Any non-command message routes through OpenClaw ACP to `claude -p` against the project directory:
- "what tests are failing?" → Claude reads test output
- "explain the strategy engine plan" → Claude reads plan file
- "what did the agent change?" → Claude runs git diff

### Notification Messages

**Feature complete:**
```
✅ data-platform-core complete
   Phase 5/5. 47 tests passing, 0 failing.
   PR #12 ready for review.
   → /approve data-platform-core  or  /diff data-platform-core
```

**Feature stuck:**
```
❌ strategy-engine stuck at Phase 3, Task 2
   3 fix attempts exhausted.
   Error: "Cannot resolve import for TA-Lib — not in pyproject.toml"
   → /logs strategy-engine  or  /skip strategy-engine
```

**Daily summary (pushed at configured time):**
```
🔄 AIDD Daily Summary — tradecore
   Overnight: 3 complete, 1 stuck, 2 queued
   PRs ready: #12 (data-platform), #14 (risk-engine), #15 (auth)
   Stuck: strategy-engine (TA-Lib import)
   Queue: 2 features waiting
   → /approve all  or review individually
```

**Evening report:**
```
📋 AIDD Evening Report — tradecore
   5 features queued for tonight.
   Estimated: ~8 hours autonomous runtime.
   → /start to begin  or  /queue to review
```

### Security

- Bot token and chat ID stored as env vars, never in config
- Only allowlisted Telegram user IDs can send commands
- Destructive commands (`/abort`, `/remove`) require confirmation reply
- No code content in notifications — feature names and test counts only

## 7. State Management

### State Store: `.aidd/state.db` (SQLite)

SQLite handles concurrent access natively — no file locking needed. The conductor loop, Telegram handlers, and CLI all read/write the same database safely.

**Tables:**

```sql
-- Pipeline state
CREATE TABLE pipeline (
  id INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
  status TEXT NOT NULL DEFAULT 'idle',     -- running | paused | idle
  started_at TEXT,
  version INTEGER NOT NULL DEFAULT 1       -- schema version for migrations
);

-- Feature queue (source of truth; feature-queue.yml is import/export format)
CREATE TABLE features (
  id TEXT PRIMARY KEY,
  spec_path TEXT NOT NULL,
  plan_path TEXT NOT NULL,
  priority INTEGER NOT NULL DEFAULT 10,
  status TEXT NOT NULL DEFAULT 'queued',
  depends_on TEXT DEFAULT '[]',            -- JSON array of feature IDs
  human_gates TEXT DEFAULT '[]',           -- JSON array, empty = use global
  feedback TEXT DEFAULT '',
  pr_number INTEGER,
  worktree_path TEXT,
  pid INTEGER,
  current_phase INTEGER DEFAULT 0,
  total_phases INTEGER DEFAULT 0,
  tests_passing INTEGER DEFAULT 0,
  tests_failing INTEGER DEFAULT 0,
  started_at TEXT,
  completed_at TEXT,
  last_activity TEXT,
  created_at TEXT DEFAULT (datetime('now')),
  position INTEGER NOT NULL                -- for ordering within same priority
);

-- Stats
CREATE TABLE stats (
  date TEXT PRIMARY KEY,                   -- YYYY-MM-DD
  features_completed INTEGER DEFAULT 0,
  features_stuck INTEGER DEFAULT 0
);
```

**Why SQLite over YAML:**
- Concurrent access from conductor + Telegram + CLI without race conditions
- Atomic writes (no half-written state files)
- Queryable (e.g., "show me all stuck features this week")
- Still a single file, easy to backup, gitignored

**Import/Export:** `feature-queue.yml` is the human-friendly format for editing and sharing. `aidd queue import` loads it into SQLite. `aidd queue export` dumps current state to YAML.

### Agent Log: `.aidd/logs/<id>.log`

Raw `stream-json` output from each `claude -p` session. Kept for debugging.

### Log/State Cleanup Config

```yaml
# In aidd.yml
maintenance:
  log_retention_days: 7          # auto-prune logs older than this
  worktree_cleanup_interval: 7d  # weekly orphan cleanup
  min_disk_space_gb: 2           # pre-flight threshold (configurable)
```

## 8. Error Handling & Recovery

### Failure Modes

| Failure | Detection | Auto-Recovery | Notification |
|---|---|---|---|
| Agent stuck (3 retries) | Progress file shows `status: stuck` | Skip to next feature | Error context + `/logs` |
| Agent running too long | Wall-clock timeout (configurable, default 4h) | Kill process, mark stuck | Suggest `/abort` or increase timeout |
| Tests failing | Exit code + output parse | 3 auto-fix attempts | Failing test names |
| Git conflict | Git exit code | Auto-rebase from main | If rebase fails |
| `claude -p` crash | Process exit != 0 | Retry once from last commit | If second attempt fails |
| OpenClaw down | Heartbeat missed | Auto-restart (systemd/launchd) | Fallback to log file |
| Disk space low | Pre-flight check | Pause pipeline | Disk usage stats |
| Subscription rate limit | Output parsing | Back off 10 min, retry | If 3 backoffs in a row |

### Pre-Flight Checks

Before spawning each agent:
1. Disk space > `maintenance.min_disk_space_gb` (default 2GB)
2. Git working tree clean on main
3. All `depends_on` features merged
4. Spec and plan files exist at configured paths
5. Test command runs successfully on main (baseline health)

If any check fails → skip feature, notify, try next in queue.

### State Recovery (after machine restart)

```
On AIDD startup:
  1. Read .aidd/state.db
  2. For each feature with status "running":
     a. Check if PID is still alive
     b. If dead:
        - Uncommitted changes in worktree → status = "stuck" (needs review)
        - Clean worktree → resume from last committed phase
  3. Resume conductor loop
```

### Worktree Cleanup

- **On merge:** Remove worktree, delete feature branch, remove agent state file
- **On abandon:** Same cleanup, status → "abandoned" in queue (kept for history)
- **Weekly auto-cleanup:** Remove orphaned worktrees, prune merged branches

## 9. CLI Interface

```bash
# Installation (Python package — primary distribution)
pip install aidd

# Project setup
aidd init                     # creates aidd.yml template + .gitignore entries
aidd init --template <name>   # init from a shared template (see Distribution)

# Pipeline control
aidd start                    # start conductor daemon (foreground)
aidd start -d                 # start as background daemon
aidd stop                     # stop daemon gracefully
aidd status                   # pipeline overview
aidd status <id>              # detailed feature status

# Queue management
aidd queue                    # show queue
aidd queue add <spec>         # add feature (auto-generates queue entry)
aidd queue add <spec> --plan <plan> --priority <n>
aidd queue remove <id>
aidd queue reprioritize <id> <n>

# Agent management
aidd logs <id>                # tail agent output
aidd logs <id> --follow       # stream live output
aidd abort <id>               # kill agent

# Human gates
aidd approve <id>             # approve PR/spec/plan
aidd reject <id> "<reason>"

# Configuration
aidd config show              # print resolved config
aidd config validate          # check aidd.yml for errors
aidd telegram setup           # interactive Telegram bot setup
```

## 10. Distribution & Sharing

### As a Python Package

AIDD lives in its own GitHub repo. Published to PyPI:

```bash
pip install aidd
```

Future: homebrew tap for easier cross-platform install.

### Shared Templates

Teams can share `aidd.yml` templates via a template directory in the AIDD package or a Git repo:

```bash
# Built-in templates
aidd init --template python-fastapi
aidd init --template typescript-nextjs
aidd init --template rust

# Remote template (copies aidd.yml from a Git repo subdirectory)
aidd init --template https://github.com/company/aidd-configs/python-fastapi
```

Templates contain pre-configured `aidd.yml` with:
- Standard test/lint commands for the stack
- Company notification channels
- Approved prompt templates
- Consistent gate policies

### .gitignore Entries

`aidd init` appends these to `.gitignore`:

```
# AIDD
.worktrees/
.aidd/
```

### What Ships in the Package

```
aidd/
├── cli.py                    # CLI entry point (typer)
├── config.py                 # aidd.yml parser + validator
├── conductor.py              # Core conductor loop
├── queue.py                  # Feature queue management (SQLite)
├── db.py                     # SQLite state store + migrations
├── worktree.py               # Git worktree operations
├── agent.py                  # claude -p process spawning + monitoring
├── progress.py               # Progress file reader (parses .aidd/progress/*.json)
├── prompt.py                 # Prompt template variable substitution
├── gates.py                  # Human gate state machine
├── notifications/
│   ├── telegram.py           # Telegram bot (python-telegram-bot)
│   └── webhook.py            # Generic webhook sender
├── recovery.py               # State recovery on restart
├── preflight.py              # Pre-flight checks
├── maintenance.py            # Log pruning, worktree cleanup
├── openclaw/
│   └── plugin.py             # OpenClaw plugin registration (optional)
├── templates/
│   ├── aidd.yml              # Default config template
│   ├── feature-queue.yml     # Default queue template
│   ├── python-fastapi/       # Stack-specific templates
│   ├── typescript-nextjs/
│   └── rust/
└── tests/
    ├── test_conductor.py
    ├── test_queue.py
    ├── test_worktree.py
    ├── test_gates.py
    ├── test_progress.py
    └── test_prompt.py
```

## 11. Integration Points

### OpenClaw (primary integration)

- AIDD registers as an OpenClaw skill/plugin
- OpenClaw provides: Telegram adapter, cron scheduling, ACP process spawning
- AIDD provides: conductor logic, queue management, prompt construction
- OpenClaw is optional — AIDD runs standalone with reduced features

### Second Brain (future integration)

- AIDD's `/evolve` output can be ingested by a personal knowledge base
- Knowledge base exposes `/import-learnings` command as entry point
- AIDD can trigger knowledge base updates via webhook on feature completion
- These integrations don't block v1 — entry points just need to exist

### CI/CD (future extension)

- AIDD-created PRs can trigger CI pipelines
- CI results can feed back into AIDD state (webhook from GitHub Actions)
- Not in v1 scope

## 12. Security Considerations

| Concern | Mitigation |
|---|---|
| Secrets in config | All tokens/keys stored as env vars, never in YAML |
| Unauthorized Telegram access | User ID allowlist in config |
| Agent running destructive commands | `--allowed-tools` restricts what `claude -p` can do |
| Runaway agent consuming resources | Wall-clock timeout (default 4h) + process kill |
| Code pushed without review | `auto_merge: false` by default, human gate on PR |
| Sensitive code in notifications | Only feature names and test counts, never code |
| Destructive Telegram commands | Confirmation required for `/abort`, `/remove` |
| Headless permission bypass | See below |
| Malicious feature queue entry | Plan/spec paths validated to be within project directory |

### Permission Mode Trade-off

Headless `claude -p` requires `--permission-mode auto` (or `bypassPermissions`) to run without human interaction. This is an intentional security trade-off:

- **`auto`** (default, recommended): Claude Code uses its built-in safety heuristics to auto-approve safe operations. Most Edit/Write/Read operations are approved. Potentially destructive Bash commands may still be blocked.
- **`bypassPermissions`**: All tool calls approved unconditionally. Only use this if `--allowed-tools` is restricted to safe tools (no unrestricted Bash).

The sandboxing model: `--permission-mode auto` + `--allowed-tools Edit,Write,Bash,Read,Grep,Glob` provides a reasonable security boundary. The agent can edit files and run tests but cannot access the network or install packages unless Bash is in the allowed list. For higher security, remove Bash from allowed tools (agent can still edit/write files but cannot execute arbitrary commands).

### Telegram User Allowlist

`allowed_users: []` means only the Telegram user whose chat ID matches `AIDD_TELEGRAM_CHAT_ID` can send commands. To allow additional users (e.g., team members), add their Telegram user IDs to the list. Commands from non-allowlisted users are silently ignored.

## 13. Cost

| Component | Cost |
|---|---|
| Claude Code agents (`claude -p`) | $0 — Max subscription |
| OpenClaw | $0 — open source, self-hosted |
| Telegram bot | $0 — Telegram Bot API is free |
| AIDD tool | $0 — open source |
| Git/GitHub | $0 — existing account |
| **Total** | **$0 beyond existing Max subscription** |

## 14. Implementation Phases

### Phase 1: Core Conductor (MVP)
- `aidd.yml` config parser and validator
- Feature queue reader and state machine
- Git worktree create/cleanup
- `claude -p` process spawning with prompt construction
- Basic output monitoring (exit code + test detection)
- CLI: `aidd init`, `aidd start`, `aidd status`, `aidd queue`
- State file management (.aidd/state.db)

### Phase 2: Telegram Integration
- Telegram bot setup (python-telegram-bot)
- Pipeline control commands (/status, /pause, /resume, /start)
- Human gate commands (/approve, /reject, /diff, /logs)
- Queue commands (/queue, /add, /skip, /remove)
- Notification push (complete, stuck, daily summary)
- Security: user allowlist, confirmation for destructive commands

### Phase 3: OpenClaw Integration
- ACP backend configuration (replace API tokens with `claude -p`)
- Register AIDD as OpenClaw plugin/skill
- Cron job setup for conductor loop and scheduled reports
- Heartbeat integration for health checks
- Chat mode routing (non-command Telegram messages → `claude -p`)

### Phase 4: Robustness
- Pre-flight checks before agent spawn
- Error recovery: crash detection, state recovery on restart
- Auto-rebase on git conflicts
- Subscription rate limit detection and backoff
- Worktree auto-cleanup (weekly)
- Log rotation and pruning

### Phase 5: Distribution
- Package as pip installable (homebrew as future nice-to-have)
- Template system (`aidd init --from <template>`)
- Documentation and README
- Tests for all core modules
