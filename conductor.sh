#!/bin/bash
set -euo pipefail

# AIDD Conductor — Overnight autonomous feature builder
# Reads feature-queue.yml, spawns claude -p per feature in worktrees
#
# Usage: aidd-conductor [--once] [--dry-run]
#   --once    Run one pass through the queue then exit (for cron)
#   --dry-run Show what would be done without doing it

AIDD_HOME="${AIDD_HOME:-$HOME/.aidd}"
source "$AIDD_HOME/.env" 2>/dev/null || true

# Parse flags
ONCE=false
DRY_RUN=false
for arg in "$@"; do
    case $arg in
        --once) ONCE=true ;;
        --dry-run) DRY_RUN=true ;;
    esac
done

# Resolve project config — aidd.yml is optional, all fields have defaults
if [ ! -f "aidd.yml" ] && [ ! -f "feature-queue.yml" ]; then
    echo "ERROR: No aidd.yml or feature-queue.yml found. Run 'aidd init' first."
    exit 1
fi

# Auto-detect project name from directory if not in config
DIR_NAME=$(basename "$PWD")
PROJECT_NAME=$(yq -r '.project.name // ""' aidd.yml 2>/dev/null || echo "")
PROJECT_NAME="${PROJECT_NAME:-$DIR_NAME}"

# Commands — auto-detect test runner if not configured
TEST_CMD=$(yq -r '.commands.test // ""' aidd.yml 2>/dev/null || echo "")
LINT_CMD=$(yq -r '.commands.lint // ""' aidd.yml 2>/dev/null || echo "")
if [ -z "$TEST_CMD" ]; then
    # Auto-detect test command
    if [ -f "pytest.ini" ] || [ -f "pyproject.toml" ] || [ -f "setup.py" ]; then
        TEST_CMD="python -m pytest tests/ -x -q"
    elif [ -f "package.json" ]; then
        if grep -q '"vitest"' package.json 2>/dev/null; then
            TEST_CMD="npx vitest run"
        elif grep -q '"jest"' package.json 2>/dev/null; then
            TEST_CMD="npx jest"
        elif grep -q '"test"' package.json 2>/dev/null; then
            TEST_CMD="npm test"
        fi
    elif [ -f "go.mod" ]; then
        TEST_CMD="go test ./..."
    elif [ -f "Cargo.toml" ]; then
        TEST_CMD="cargo test"
    fi
fi

# Agent settings with defaults
MAX_PARALLEL=$(yq -r '.agent.max_parallel // 1' aidd.yml 2>/dev/null || echo "1")
MODEL=$(yq -r '.agent.model // "sonnet"' aidd.yml 2>/dev/null || echo "sonnet")
PERMISSION_MODE=$(yq -r '.agent.permission_mode // "auto"' aidd.yml 2>/dev/null || echo "auto")
TIMEOUT_HOURS=$(yq -r '.agent.timeout_hours // 4' aidd.yml 2>/dev/null || echo "4")

# Paths with defaults
WORKTREE_DIR=$(yq -r '.paths.worktrees // ".worktrees"' aidd.yml 2>/dev/null || echo ".worktrees")
WORKTREE_DIR="${WORKTREE_DIR%/}"  # strip trailing slash
STATE_DIR=$(yq -r '.paths.state // ".aidd"' aidd.yml 2>/dev/null || echo ".aidd")

# Prompt customization (optional overrides)
PRE_INSTRUCTIONS=$(yq -r '.prompt_instructions.pre_implement // ""' aidd.yml 2>/dev/null || echo "")
POST_INSTRUCTIONS=$(yq -r '.prompt_instructions.post_implement // ""' aidd.yml 2>/dev/null || echo "")
CUSTOM_PROMPT_TEMPLATE=$(yq -r '.prompt_template // ""' aidd.yml 2>/dev/null || echo "")

# Built-in default prompt template
DEFAULT_PROMPT_TEMPLATE='You are executing an autonomous implementation task for {project_name}.

Plan: {plan_path}
Spec: {spec_path}

{pre_implement_instructions}

Instructions:
1. Read the plan file — this is your source of truth
2. If CLAUDE.md exists, read it for project conventions
3. Follow TDD: test first → implement → verify
4. After each passing phase, commit with message: "[aidd:{feature_id}] phase N: <description>"
5. If stuck after 3 attempts on any task, stop and explain what is blocking you

Test command: {test_cmd}
Lint command: {lint_cmd}

{feedback}

Do NOT install new dependencies unless the plan says to.

{post_implement_instructions}'

# Use custom template if provided, otherwise use built-in default
PROMPT_TEMPLATE="${CUSTOM_PROMPT_TEMPLATE:-$DEFAULT_PROMPT_TEMPLATE}"

mkdir -p "$WORKTREE_DIR" "$STATE_DIR/logs" "$STATE_DIR/progress"

# ---- Telegram Notifications ----

notify() {
    local message="$1"
    if [ -n "${AIDD_TELEGRAM_TOKEN:-}" ] && [ -n "${AIDD_TELEGRAM_CHAT_ID:-}" ]; then
        curl -s -X POST "https://api.telegram.org/bot${AIDD_TELEGRAM_TOKEN}/sendMessage" \
            -d chat_id="${AIDD_TELEGRAM_CHAT_ID}" \
            -d text="$message" \
            -d parse_mode="Markdown" > /dev/null 2>&1 || true
    fi
    echo "[NOTIFY] $message"
}

# ---- Pre-Flight Checks ----

preflight() {
    local feature_id="$1"
    local spec plan

    spec=$(yq -r ".features[] | select(.id == \"$feature_id\") | .spec" feature-queue.yml)
    plan=$(yq -r ".features[] | select(.id == \"$feature_id\") | .plan" feature-queue.yml)

    # Check disk space (2GB minimum)
    local free_gb
    free_gb=$(df -m . | awk 'NR==2 {print int($4/1024)}' 2>/dev/null || echo "999")
    if [ "$free_gb" -lt 2 ]; then
        echo "PREFLIGHT FAIL: Only ${free_gb}GB free disk space"
        return 1
    fi

    # Check plan file exists
    if [ ! -f "$plan" ]; then
        echo "PREFLIGHT FAIL: Plan file not found: $plan"
        return 1
    fi

    # Check spec file exists (if specified)
    if [ -n "$spec" ] && [ "$spec" != "null" ] && [ ! -f "$spec" ]; then
        echo "PREFLIGHT FAIL: Spec file not found: $spec"
        return 1
    fi

    # Check dependencies are merged
    local deps
    deps=$(yq -r ".features[] | select(.id == \"$feature_id\") | .depends_on[]?" feature-queue.yml 2>/dev/null || true)
    for dep in $deps; do
        local dep_status
        dep_status=$(yq -r ".features[] | select(.id == \"$dep\") | .status" feature-queue.yml)
        if [ "$dep_status" != "merged" ]; then
            echo "PREFLIGHT FAIL: Dependency '$dep' not merged (status: $dep_status)"
            return 1
        fi
    done

    return 0
}

# ---- Build Prompt ----

build_prompt() {
    local feature_id="$1"
    local plan spec feedback

    plan=$(yq -r ".features[] | select(.id == \"$feature_id\") | .plan" feature-queue.yml)
    spec=$(yq -r ".features[] | select(.id == \"$feature_id\") | .spec // \"\"" feature-queue.yml)
    feedback=$(yq -r ".features[] | select(.id == \"$feature_id\") | .feedback // \"\"" feature-queue.yml)

    local prompt="$PROMPT_TEMPLATE"

    # Variable substitution
    prompt="${prompt//\{project_name\}/$PROJECT_NAME}"
    prompt="${prompt//\{plan_path\}/$plan}"
    prompt="${prompt//\{spec_path\}/$spec}"
    prompt="${prompt//\{feature_id\}/$feature_id}"
    prompt="${prompt//\{test_cmd\}/$TEST_CMD}"
    prompt="${prompt//\{lint_cmd\}/$LINT_CMD}"
    prompt="${prompt//\{pre_implement_instructions\}/$PRE_INSTRUCTIONS}"
    prompt="${prompt//\{post_implement_instructions\}/$POST_INSTRUCTIONS}"

    if [ -n "$feedback" ] && [ "$feedback" != "null" ] && [ "$feedback" != "" ]; then
        prompt="${prompt//\{feedback\}/IMPORTANT - Previous attempt was rejected. Feedback: $feedback}"
    else
        prompt="${prompt//\{feedback\}/}"
    fi

    echo "$prompt"
}

# ---- Run One Feature ----

run_feature() {
    local feature_id="$1"
    local plan worktree_path log_file

    plan=$(yq -r ".features[] | select(.id == \"$feature_id\") | .plan" feature-queue.yml)
    worktree_path="$WORKTREE_DIR/$feature_id"
    log_file="$STATE_DIR/logs/$feature_id.log"

    echo "[$(date '+%H:%M:%S')] Starting feature: $feature_id"

    # Build prompt first (needed for dry-run output)
    local prompt
    prompt=$(build_prompt "$feature_id")

    if [ "$DRY_RUN" = true ]; then
        echo "  [DRY RUN] Would create worktree at $worktree_path"
        echo "  [DRY RUN] Would run claude -p --model $MODEL --permission-mode $PERMISSION_MODE"
        echo "  [DRY RUN] Prompt:"
        echo "  ---"
        echo "$prompt" | sed 's/^/  /'
        echo "  ---"
        return 0
    fi

    # Create worktree
    if [ -d "$worktree_path" ]; then
        echo "  Worktree exists, cleaning up..."
        git worktree remove "$worktree_path" --force 2>/dev/null || true
        git branch -D "feature/$feature_id" 2>/dev/null || true
    fi

    git worktree add "$worktree_path" -b "feature/$feature_id" 2>/dev/null

    # Update status
    yq -i "(.features[] | select(.id == \"$feature_id\") | .status) = \"running\"" feature-queue.yml

    # Initialize progress file
    cat > "$STATE_DIR/progress/$feature_id.json" <<EOF
{"feature_id":"$feature_id","status":"running","current_phase":0,"total_phases":0,"phases_completed":[],"stuck_reason":null,"last_updated":"$(date -u +%Y-%m-%dT%H:%M:%SZ)"}
EOF

    notify "🚀 *$PROJECT_NAME* — Starting: \`$feature_id\`"

    # Spawn claude -p in worktree with timeout
    local exit_code=0
    timeout "${TIMEOUT_HOURS}h" bash -c "
        cd '$worktree_path' && \
        claude -p '$(echo "$prompt" | sed "s/'/'\\\\''/g")' \
            --permission-mode '$PERMISSION_MODE' \
            --output-format stream-json \
            --model '$MODEL'
    " > "$log_file" 2>&1 || exit_code=$?

    # Check result
    if [ $exit_code -eq 0 ]; then
        # Verify tests pass in worktree
        if [ -n "$TEST_CMD" ]; then
            if (cd "$worktree_path" && eval "$TEST_CMD" > /dev/null 2>&1); then
                handle_success "$feature_id"
            else
                handle_stuck "$feature_id" "Tests failing after implementation"
            fi
        else
            handle_success "$feature_id"
        fi
    elif [ $exit_code -eq 124 ]; then
        handle_stuck "$feature_id" "Timeout after ${TIMEOUT_HOURS} hours"
    else
        # Check progress file for stuck reason
        local stuck_reason
        stuck_reason=$(jq -r '.stuck_reason // "Agent exited with code '$exit_code'"' "$STATE_DIR/progress/$feature_id.json" 2>/dev/null || echo "Agent exited with code $exit_code")
        handle_stuck "$feature_id" "$stuck_reason"
    fi
}

handle_success() {
    local feature_id="$1"
    local worktree_path="$WORKTREE_DIR/$feature_id"

    yq -i "(.features[] | select(.id == \"$feature_id\") | .status) = \"review\"" feature-queue.yml

    # Create PR if gh is available
    local pr_url=""
    if command -v gh &> /dev/null; then
        (cd "$worktree_path" && git push -u origin "feature/$feature_id" 2>/dev/null) || true
        pr_url=$(cd "$worktree_path" && gh pr create \
            --title "feat($feature_id): autonomous implementation" \
            --body "Implemented by AIDD conductor. Review and approve." \
            --base main 2>/dev/null) || true
    fi

    local test_info=""
    if [ -f "$STATE_DIR/progress/$feature_id.json" ]; then
        local passing failing
        passing=$(jq '[.phases_completed[].tests_passing] | add // 0' "$STATE_DIR/progress/$feature_id.json" 2>/dev/null || echo "?")
        failing=$(jq '[.phases_completed[].tests_failing] | add // 0' "$STATE_DIR/progress/$feature_id.json" 2>/dev/null || echo "?")
        test_info="Tests: ${passing} passing, ${failing} failing."
    fi

    notify "✅ *$PROJECT_NAME* — Complete: \`$feature_id\`
${test_info}
${pr_url:+PR: $pr_url}
→ Reply \`/approve $feature_id\` to merge"

    echo "[$(date '+%H:%M:%S')] Feature complete: $feature_id"
}

handle_stuck() {
    local feature_id="$1"
    local reason="$2"

    yq -i "(.features[] | select(.id == \"$feature_id\") | .status) = \"stuck\"" feature-queue.yml

    # Get last few lines of log for context
    local log_tail=""
    if [ -f "$STATE_DIR/logs/$feature_id.log" ]; then
        log_tail=$(tail -5 "$STATE_DIR/logs/$feature_id.log" 2>/dev/null | head -c 500 || true)
    fi

    notify "❌ *$PROJECT_NAME* — Stuck: \`$feature_id\`
Reason: $reason
→ Reply \`/logs $feature_id\` or \`/skip $feature_id\`"

    echo "[$(date '+%H:%M:%S')] Feature stuck: $feature_id — $reason"
}

# ---- Cleanup ----

cleanup_feature() {
    local feature_id="$1"
    local worktree_path="$WORKTREE_DIR/$feature_id"

    if [ -d "$worktree_path" ]; then
        git worktree remove "$worktree_path" --force 2>/dev/null || true
    fi
    git branch -D "feature/$feature_id" 2>/dev/null || true
    rm -f "$STATE_DIR/progress/$feature_id.json"
}

# ---- Main Conductor Loop ----

conductor_pass() {
    if [ ! -f "feature-queue.yml" ]; then
        echo "No feature-queue.yml found."
        return
    fi

    # Count running features
    local running_count
    running_count=$(yq -r '[.features[] | select(.status == "running")] | length' feature-queue.yml)

    if [ "$running_count" -ge "$MAX_PARALLEL" ]; then
        echo "[$(date '+%H:%M:%S')] $running_count agents running (max: $MAX_PARALLEL). Waiting."
        return
    fi

    # Find next eligible feature
    local feature_ids
    feature_ids=$(yq -r '.features[] | select(.status == "queued") | .id' feature-queue.yml)

    for feature_id in $feature_ids; do
        # Check preflight
        local preflight_result
        preflight_result=$(preflight "$feature_id" 2>&1) || {
            echo "  Skipping $feature_id: $preflight_result"
            continue
        }

        # Run it
        run_feature "$feature_id"

        # Recheck parallel limit
        running_count=$(yq -r '[.features[] | select(.status == "running")] | length' feature-queue.yml)
        if [ "$running_count" -ge "$MAX_PARALLEL" ]; then
            break
        fi
    done
}

# ---- Entry Point ----

echo "========================================"
echo "  AIDD Conductor — $PROJECT_NAME"
echo "  $(date)"
echo "========================================"

if [ "$ONCE" = true ] || [ "$DRY_RUN" = true ]; then
    conductor_pass
    echo ""
    [ "$DRY_RUN" = true ] && echo "Dry run complete." || echo "Single pass complete."
else
    notify "🔄 *$PROJECT_NAME* — AIDD Conductor started"
    while true; do
        conductor_pass
        echo "[$(date '+%H:%M:%S')] Sleeping 5 minutes..."
        sleep 300
    done
fi
