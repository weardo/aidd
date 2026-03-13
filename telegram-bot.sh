#!/bin/bash
set -euo pipefail

# AIDD Telegram Bot — two-way command interface
# Polls for messages, executes aidd commands, sends results back
#
# Usage: aidd-telegram [--project /path/to/project]

AIDD_HOME="${AIDD_HOME:-$HOME/.aidd}"
source "$AIDD_HOME/.env" 2>/dev/null || true

if [ -z "${AIDD_TELEGRAM_TOKEN:-}" ] || [ -z "${AIDD_TELEGRAM_CHAT_ID:-}" ]; then
    echo "ERROR: Telegram not configured. Set AIDD_TELEGRAM_TOKEN and AIDD_TELEGRAM_CHAT_ID in ~/.aidd/.env"
    exit 1
fi

API="https://api.telegram.org/bot${AIDD_TELEGRAM_TOKEN}"
CHAT_ID="${AIDD_TELEGRAM_CHAT_ID}"
OFFSET=0
PROJECT_DIR=""

# Parse args
for arg in "$@"; do
    case $arg in
        --project=*) PROJECT_DIR="${arg#*=}" ;;
        --project) shift; PROJECT_DIR="${1:-}" ;;
    esac
done

# Auto-detect project directory
if [ -z "$PROJECT_DIR" ]; then
    if [ -f "aidd.yml" ] || [ -f "feature-queue.yml" ]; then
        PROJECT_DIR="$PWD"
    fi
fi

# ---- Send Message ----

send() {
    local text="$1"
    # Telegram max message length is 4096
    if [ ${#text} -gt 4000 ]; then
        text="${text:0:4000}...(truncated)"
    fi
    curl -s -X POST "$API/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d text="$text" \
        -d parse_mode="Markdown" > /dev/null 2>&1 || \
    # Retry without markdown if it fails (markdown parse errors)
    curl -s -X POST "$API/sendMessage" \
        -d chat_id="$CHAT_ID" \
        -d text="$text" > /dev/null 2>&1 || true
}

# ---- Command Handlers ----

handle_command() {
    local text="$1"
    local cmd args

    # Ignore non-command messages (no leading /)
    if [[ "$text" != /* ]]; then
        return
    fi

    # Strip leading /
    text="${text#/}"

    # Split into command and args
    cmd=$(echo "$text" | awk '{print $1}')
    args=$(echo "$text" | cut -d' ' -f2- -s)

    # Require project dir for most commands
    if [ -z "$PROJECT_DIR" ] && [[ "$cmd" != "help" ]] && [[ "$cmd" != "ping" ]] && [[ "$cmd" != "projects" ]]; then
        send "No project directory set. Start bot with: \`aidd telegram --project /path/to/project\`"
        return
    fi

    case "$cmd" in
        start)
            # Telegram's built-in /start — just greet
            send "AIDD bot ready. Send /help for commands."
            return
            ;;
        conductor)
            handle_start
            ;;
        status)
            handle_status
            ;;
        queue)
            handle_queue
            ;;
        approve)
            handle_approve "$args"
            ;;
        reject)
            handle_reject "$args"
            ;;
        skip)
            handle_skip "$args"
            ;;
        logs)
            handle_logs "$args"
            ;;
        run)
            handle_run "$args"
            ;;
        ping)
            send "pong"
            ;;
        help|"")
            handle_help
            ;;
        *)
            send "Unknown command: \`$cmd\`\nSend /help for available commands."
            ;;
    esac
}

handle_help() {
    send "AIDD Bot Commands:

/status — Pipeline overview
/queue — View feature queue
/approve <id> — Merge completed feature
/reject <id> <reason> — Reject with feedback
/skip <id> — Re-queue stuck feature
/logs <id> — Agent output (last 50 lines)
/conductor — Run conductor (single pass)
/run <cmd> — Run shell command in project
/ping — Check bot is alive
/help — This message"
}

handle_status() {
    local output
    output=$(cd "$PROJECT_DIR" && aidd status 2>&1) || true
    send "$output"
}

handle_queue() {
    local output
    output=$(cd "$PROJECT_DIR" && aidd queue 2>&1) || true
    send "$output"
}

handle_approve() {
    local feature_id="$1"
    if [ -z "$feature_id" ]; then
        send "Usage: /approve <feature-id>"
        return
    fi
    local output
    output=$(cd "$PROJECT_DIR" && aidd approve "$feature_id" 2>&1) || true
    send "$output"
}

handle_reject() {
    local input="$1"
    local feature_id reason
    feature_id=$(echo "$input" | awk '{print $1}')
    reason=$(echo "$input" | cut -d' ' -f2- -s)

    if [ -z "$feature_id" ]; then
        send "Usage: /reject <feature-id> <reason>"
        return
    fi
    reason="${reason:-no reason given}"

    local output
    output=$(cd "$PROJECT_DIR" && aidd reject "$feature_id" "$reason" 2>&1) || true
    send "$output"
}

handle_skip() {
    local feature_id="$1"
    if [ -z "$feature_id" ]; then
        send "Usage: /skip <feature-id>"
        return
    fi
    local output
    output=$(cd "$PROJECT_DIR" && aidd skip "$feature_id" 2>&1) || true
    send "$output"
}

handle_logs() {
    local feature_id="$1"
    if [ -z "$feature_id" ]; then
        send "Usage: /logs <feature-id>"
        return
    fi
    local output
    output=$(cd "$PROJECT_DIR" && aidd logs "$feature_id" 2>&1) || true
    # Wrap in code block for readability
    send "\`\`\`
$output
\`\`\`"
}

handle_start() {
    send "Starting conductor (single pass)..."
    local output
    output=$(cd "$PROJECT_DIR" && aidd start --once 2>&1) || true
    send "Conductor finished:
$output"
}

handle_run() {
    local subcmd="$1"
    if [ -z "$subcmd" ]; then
        send "Usage: /run <any shell command>"
        return
    fi
    # Only allow from authorized chat
    local output
    output=$(cd "$PROJECT_DIR" && eval "$subcmd" 2>&1 | head -50) || true
    send "\`\`\`
$output
\`\`\`"
}

# ---- Main Poll Loop ----

echo "AIDD Telegram Bot started"
echo "  Project: ${PROJECT_DIR:-<none>}"
echo "  Chat ID: $CHAT_ID"
echo "  Polling for messages..."

send "AIDD bot online. Project: \`${PROJECT_DIR:-none}\`
Send /help for commands."

while true; do
    # Long poll (30 second timeout)
    response=$(curl -s "$API/getUpdates?offset=$OFFSET&timeout=30" 2>/dev/null || echo '{"ok":false}')

    # Check if response is valid
    ok=$(echo "$response" | jq -r '.ok' 2>/dev/null || echo "false")
    if [ "$ok" != "true" ]; then
        sleep 5
        continue
    fi

    # Process each update
    updates=$(echo "$response" | jq -r '.result | length' 2>/dev/null || echo "0")

    for (( i=0; i<updates; i++ )); do
        update_id=$(echo "$response" | jq -r ".result[$i].update_id")
        from_id=$(echo "$response" | jq -r ".result[$i].message.chat.id // empty")
        text=$(echo "$response" | jq -r ".result[$i].message.text // empty")

        # Update offset
        OFFSET=$((update_id + 1))

        # Security: only respond to authorized chat
        if [ "$from_id" != "$CHAT_ID" ]; then
            echo "[$(date '+%H:%M:%S')] Ignored message from unauthorized chat: $from_id"
            continue
        fi

        # Skip empty messages
        [ -z "$text" ] && continue

        echo "[$(date '+%H:%M:%S')] Command: $text"
        handle_command "$text"
    done
done
