#!/usr/bin/env python3
"""AIDD Telegram Bot — two-way command interface with inline keyboards.

Uses only Python stdlib (no pip packages). Polls for updates, executes
aidd CLI commands, sends results with dynamic inline buttons.

Usage: aidd telegram [--project /path/to/project]
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import urllib.parse
import urllib.error

# ── Config ──────────────────────────────────────────────────────────────

AIDD_HOME = os.environ.get("AIDD_HOME", os.path.expanduser("~/.aidd"))

# Load .env
env_path = os.path.join(AIDD_HOME, ".env")
if os.path.exists(env_path):
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, val = line.split("=", 1)
                os.environ.setdefault(key.strip(), val.strip())

TOKEN = os.environ.get("AIDD_TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("AIDD_TELEGRAM_CHAT_ID", "")
API = f"https://api.telegram.org/bot{TOKEN}"

if not TOKEN or not CHAT_ID:
    print("ERROR: Set AIDD_TELEGRAM_TOKEN and AIDD_TELEGRAM_CHAT_ID in ~/.aidd/.env")
    sys.exit(1)

# Project directory
PROJECT_DIR = ""
for i, arg in enumerate(sys.argv[1:], 1):
    if arg.startswith("--project="):
        PROJECT_DIR = arg.split("=", 1)[1]
    elif arg == "--project" and i + 1 < len(sys.argv):
        PROJECT_DIR = sys.argv[i + 1]

if not PROJECT_DIR:
    if os.path.exists("aidd.yml") or os.path.exists("feature-queue.yml"):
        PROJECT_DIR = os.getcwd()

# Pending reject: waiting for reason text after user presses Reject button
# Maps chat_id -> feature_id
pending_reject = {}


# ── Telegram API ────────────────────────────────────────────────────────

def api_call(method: str, data: dict | None = None) -> dict:
    """Call Telegram Bot API. Returns parsed JSON response."""
    url = f"{API}/{method}"
    if data:
        payload = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            url, data=payload,
            headers={"Content-Type": "application/json"}
        )
    else:
        req = urllib.request.Request(url)
    try:
        with urllib.request.urlopen(req, timeout=35) as resp:
            return json.loads(resp.read().decode())
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        return {"ok": False, "description": str(e)}


def send(text: str, keyboard: list | None = None, chat_id: str = ""):
    """Send a message, optionally with inline keyboard."""
    chat_id = chat_id or CHAT_ID
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"

    data = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if keyboard:
        data["reply_markup"] = {"inline_keyboard": keyboard}

    result = api_call("sendMessage", data)
    # Retry without markdown if it fails (parse errors)
    if not result.get("ok"):
        data.pop("parse_mode", None)
        data["text"] = text
        api_call("sendMessage", data)


def edit_message(chat_id: str, message_id: int, text: str, keyboard: list | None = None):
    """Edit an existing message (updates in-place, no spam)."""
    if len(text) > 4000:
        text = text[:4000] + "\n...(truncated)"

    data = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": "Markdown",
    }
    if keyboard:
        data["reply_markup"] = {"inline_keyboard": keyboard}

    result = api_call("editMessageText", data)
    if not result.get("ok"):
        data.pop("parse_mode", None)
        api_call("editMessageText", data)


def answer_callback(callback_id: str, text: str = ""):
    """Acknowledge a callback query (removes loading indicator on button)."""
    api_call("answerCallbackQuery", {
        "callback_query_id": callback_id,
        "text": text,
    })


# ── AIDD CLI ────────────────────────────────────────────────────────────

def aidd_run(cmd: str, args: list | None = None) -> str:
    """Run an aidd CLI command in the project directory."""
    if not PROJECT_DIR:
        return "No project directory set."
    full_cmd = ["aidd", cmd] + (args or [])
    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True,
            timeout=120, cwd=PROJECT_DIR
        )
        return (result.stdout + result.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after 120s: aidd {cmd}"
    except Exception as e:
        return f"Error: {e}"


def shell_run(cmd: str) -> str:
    """Run a shell command in the project directory."""
    if not PROJECT_DIR:
        return "No project directory set."
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=PROJECT_DIR
        )
        output = (result.stdout + result.stderr).strip()
        # Limit output length
        lines = output.split("\n")
        if len(lines) > 50:
            output = "\n".join(lines[:50]) + "\n...(truncated)"
        return output or "(no output)"
    except subprocess.TimeoutExpired:
        return "Command timed out after 60s"
    except Exception as e:
        return f"Error: {e}"


# ── Feature Data ────────────────────────────────────────────────────────

def get_features() -> list[dict]:
    """Read features from feature-queue.yml."""
    if not PROJECT_DIR:
        return []
    try:
        result = subprocess.run(
            ["yq", "-r", ".features[]", "feature-queue.yml"],
            capture_output=True, text=True, cwd=PROJECT_DIR
        )
        if result.returncode != 0:
            return []
        # Parse yq JSON output
        result2 = subprocess.run(
            ["yq", "-o=json", ".features", "feature-queue.yml"],
            capture_output=True, text=True, cwd=PROJECT_DIR
        )
        return json.loads(result2.stdout) if result2.stdout.strip() else []
    except Exception:
        return []


def feature_buttons(feature_id: str, status: str) -> list[list[dict]]:
    """Generate inline keyboard buttons based on feature status."""
    buttons = []

    if status == "review":
        buttons = [
            [
                {"text": "Approve", "callback_data": f"approve:{feature_id}"},
                {"text": "Reject", "callback_data": f"reject:{feature_id}"},
            ],
            [
                {"text": "Logs", "callback_data": f"logs:{feature_id}"},
            ],
        ]
    elif status == "stuck":
        buttons = [
            [
                {"text": "Skip (re-queue)", "callback_data": f"skip:{feature_id}"},
                {"text": "Logs", "callback_data": f"logs:{feature_id}"},
            ],
        ]
    elif status == "running":
        buttons = [
            [{"text": "Logs", "callback_data": f"logs:{feature_id}"}],
        ]
    elif status == "queued":
        buttons = [
            [{"text": "Remove from queue", "callback_data": f"remove:{feature_id}"}],
        ]

    return buttons


STATUS_ICONS = {
    "running": "\U0001f504",   # 🔄
    "review": "\U0001f440",    # 👀
    "stuck": "\u274c",         # ❌
    "queued": "\U0001f4cb",    # 📋
    "merged": "\u2705",        # ✅
    "abandoned": "\U0001f5d1", # 🗑️
}


# ── Command Handlers ────────────────────────────────────────────────────

def cmd_start(chat_id: str):
    project_name = os.path.basename(PROJECT_DIR) if PROJECT_DIR else "none"
    keyboard = [
        [
            {"text": "\U0001f4ca Status", "callback_data": "cmd:status"},
            {"text": "\U0001f4cb Queue", "callback_data": "cmd:queue"},
        ],
        [
            {"text": "\u25b6\ufe0f Run Conductor", "callback_data": "cmd:conductor"},
            {"text": "\u2753 Help", "callback_data": "cmd:help"},
        ],
    ]
    send(
        f"*AIDD Bot Online*\nProject: `{project_name}`",
        keyboard=keyboard, chat_id=chat_id
    )


def cmd_help(chat_id: str):
    keyboard = [
        [
            {"text": "\U0001f4ca Status", "callback_data": "cmd:status"},
            {"text": "\U0001f4cb Queue", "callback_data": "cmd:queue"},
        ],
        [
            {"text": "\u25b6\ufe0f Run Conductor", "callback_data": "cmd:conductor"},
            {"text": "\U0001f3d3 Ping", "callback_data": "cmd:ping"},
        ],
    ]
    send(
        "*AIDD Commands*\n\n"
        "/status \u2014 Pipeline overview with actions\n"
        "/queue \u2014 Feature queue\n"
        "/logs <id> \u2014 Agent output\n"
        "/conductor \u2014 Run single conductor pass\n"
        "/run <cmd> \u2014 Shell command in project\n"
        "/ping \u2014 Health check\n"
        "/help \u2014 This message\n\n"
        "_Or tap a button below:_",
        keyboard=keyboard, chat_id=chat_id
    )


def cmd_status(chat_id: str):
    features = get_features()
    if not features:
        send("No features in queue.", chat_id=chat_id)
        return

    lines = ["*Pipeline Status*\n"]
    all_buttons = []

    for f in features:
        fid = f.get("id", "?")
        status = f.get("status", "?")
        icon = STATUS_ICONS.get(status, "\u2753")
        lines.append(f"{icon} `{fid}` \u2014 {status}")

        # Add action buttons for actionable features
        btns = feature_buttons(fid, status)
        if btns:
            all_buttons.extend(btns)

    # Add refresh button
    all_buttons.append([{"text": "\U0001f504 Refresh", "callback_data": "cmd:status"}])

    send("\n".join(lines), keyboard=all_buttons, chat_id=chat_id)


def cmd_queue(chat_id: str):
    output = aidd_run("queue")
    keyboard = [[{"text": "\U0001f4ca Full Status", "callback_data": "cmd:status"}]]
    send(f"```\n{output}\n```", keyboard=keyboard, chat_id=chat_id)


def cmd_conductor(chat_id: str):
    send("\u23f3 Running conductor (single pass)...", chat_id=chat_id)
    output = aidd_run("start", ["--once"])
    keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
    send(f"*Conductor finished:*\n```\n{output}\n```", keyboard=keyboard, chat_id=chat_id)


def cmd_logs(chat_id: str, feature_id: str):
    if not feature_id:
        # Show log buttons for all features that might have logs
        features = get_features()
        active = [f for f in features if f.get("status") in ("running", "review", "stuck")]
        if not active:
            send("No active features with logs.", chat_id=chat_id)
            return
        keyboard = [
            [{"text": f"\U0001f4c4 {f['id']}", "callback_data": f"logs:{f['id']}"}]
            for f in active
        ]
        send("Select feature to view logs:", keyboard=keyboard, chat_id=chat_id)
        return

    output = aidd_run("logs", [feature_id])
    # Get feature status for context buttons
    features = get_features()
    status = next((f["status"] for f in features if f["id"] == feature_id), "")
    keyboard = feature_buttons(feature_id, status)
    keyboard.append([{"text": "\u2b05\ufe0f Back to Status", "callback_data": "cmd:status"}])
    send(f"*Logs: {feature_id}*\n```\n{output}\n```", keyboard=keyboard, chat_id=chat_id)


def cmd_run(chat_id: str, cmd_text: str):
    if not cmd_text:
        send("Usage: `/run <command>`", chat_id=chat_id)
        return
    output = shell_run(cmd_text)
    send(f"```\n{output}\n```", chat_id=chat_id)


# ── Callback Handler ────────────────────────────────────────────────────

def handle_callback(callback_query: dict):
    """Handle inline keyboard button presses."""
    cb_id = callback_query["id"]
    data = callback_query.get("data", "")
    chat_id = str(callback_query["message"]["chat"]["id"])
    message_id = callback_query["message"]["message_id"]

    # Security check
    if chat_id != CHAT_ID:
        answer_callback(cb_id, "Unauthorized")
        return

    if data.startswith("cmd:"):
        cmd = data.split(":", 1)[1]
        answer_callback(cb_id)

        if cmd == "status":
            cmd_status(chat_id)
        elif cmd == "queue":
            cmd_queue(chat_id)
        elif cmd == "conductor":
            cmd_conductor(chat_id)
        elif cmd == "help":
            cmd_help(chat_id)
        elif cmd == "ping":
            send("pong \U0001f3d3", chat_id=chat_id)
        return

    if ":" not in data:
        answer_callback(cb_id, "Unknown action")
        return

    action, feature_id = data.split(":", 1)

    if action == "approve":
        answer_callback(cb_id, f"Approving {feature_id}...")
        output = aidd_run("approve", [feature_id])
        keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
        edit_message(chat_id, message_id,
                     f"\u2705 *Approved: {feature_id}*\n{output}",
                     keyboard=keyboard)

    elif action == "reject":
        answer_callback(cb_id, "Send rejection reason as next message")
        pending_reject[chat_id] = feature_id
        send(f"*Rejecting `{feature_id}`*\n\nType your rejection reason:",
             chat_id=chat_id)

    elif action == "skip":
        answer_callback(cb_id, f"Re-queuing {feature_id}...")
        output = aidd_run("skip", [feature_id])
        keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
        edit_message(chat_id, message_id,
                     f"\U0001f504 *Re-queued: {feature_id}*\n{output}",
                     keyboard=keyboard)

    elif action == "logs":
        answer_callback(cb_id)
        cmd_logs(chat_id, feature_id)

    elif action == "remove":
        answer_callback(cb_id, f"Removing {feature_id}...")
        # Set status to abandoned
        if PROJECT_DIR:
            subprocess.run(
                ["yq", "-i",
                 f'(.features[] | select(.id == "{feature_id}") | .status) = "abandoned"',
                 "feature-queue.yml"],
                cwd=PROJECT_DIR
            )
        keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
        edit_message(chat_id, message_id,
                     f"\U0001f5d1 *Removed: {feature_id}*",
                     keyboard=keyboard)

    else:
        answer_callback(cb_id, f"Unknown action: {action}")


# ── Message Handler ─────────────────────────────────────────────────────

def handle_message(message: dict):
    """Handle incoming text messages."""
    chat_id = str(message["chat"]["id"])
    text = message.get("text", "").strip()

    # Security check
    if chat_id != CHAT_ID:
        return

    if not text:
        return

    # Check for pending reject reason
    if chat_id in pending_reject and not text.startswith("/"):
        feature_id = pending_reject.pop(chat_id)
        output = aidd_run("reject", [feature_id, text])
        keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
        send(f"\u274c *Rejected: {feature_id}*\nReason: {text}\n\n{output}",
             keyboard=keyboard, chat_id=chat_id)
        return

    # Ignore non-commands
    if not text.startswith("/"):
        return

    # Clear pending reject if user sends a new command
    pending_reject.pop(chat_id, None)

    # Parse command
    parts = text[1:].split(None, 1)
    cmd = parts[0].lower() if parts else ""
    args = parts[1] if len(parts) > 1 else ""

    # Strip @botname suffix from commands
    if "@" in cmd:
        cmd = cmd.split("@")[0]

    if cmd == "start":
        cmd_start(chat_id)
    elif cmd == "help":
        cmd_help(chat_id)
    elif cmd == "status":
        cmd_status(chat_id)
    elif cmd == "queue":
        cmd_queue(chat_id)
    elif cmd == "conductor":
        cmd_conductor(chat_id)
    elif cmd == "logs":
        cmd_logs(chat_id, args)
    elif cmd == "approve":
        if not args:
            send("Usage: `/approve <feature-id>`", chat_id=chat_id)
        else:
            output = aidd_run("approve", [args])
            keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
            send(f"\u2705 *Approved: {args}*\n{output}", keyboard=keyboard, chat_id=chat_id)
    elif cmd == "reject":
        if not args:
            send("Usage: `/reject <feature-id> <reason>`", chat_id=chat_id)
        else:
            parts = args.split(None, 1)
            fid = parts[0]
            reason = parts[1] if len(parts) > 1 else "no reason given"
            output = aidd_run("reject", [fid, reason])
            keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
            send(f"\u274c *Rejected: {fid}*\nReason: {reason}\n\n{output}",
                 keyboard=keyboard, chat_id=chat_id)
    elif cmd == "skip":
        if not args:
            send("Usage: `/skip <feature-id>`", chat_id=chat_id)
        else:
            output = aidd_run("skip", [args])
            keyboard = [[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"}]]
            send(f"\U0001f504 *Re-queued: {args}*\n{output}",
                 keyboard=keyboard, chat_id=chat_id)
    elif cmd == "run":
        cmd_run(chat_id, args)
    elif cmd == "ping":
        send("pong \U0001f3d3", chat_id=chat_id)
    else:
        send(f"Unknown command: `{cmd}`\nSend /help for available commands.", chat_id=chat_id)


# ── Register Commands ───────────────────────────────────────────────────

def register_commands():
    """Register bot commands in Telegram's command menu."""
    commands = [
        {"command": "status", "description": "Pipeline overview with action buttons"},
        {"command": "queue", "description": "View feature queue"},
        {"command": "conductor", "description": "Run conductor (single pass)"},
        {"command": "logs", "description": "View agent output"},
        {"command": "run", "description": "Run shell command in project"},
        {"command": "ping", "description": "Health check"},
        {"command": "help", "description": "Show all commands"},
    ]
    result = api_call("setMyCommands", {"commands": commands})
    if result.get("ok"):
        print("  Commands registered in Telegram menu")
    else:
        print(f"  Warning: Failed to register commands: {result.get('description', '?')}")


# ── Main Loop ───────────────────────────────────────────────────────────

def main():
    project_name = os.path.basename(PROJECT_DIR) if PROJECT_DIR else "<none>"
    print("AIDD Telegram Bot started")
    print(f"  Project: {project_name} ({PROJECT_DIR})")
    print(f"  Chat ID: {CHAT_ID}")

    register_commands()

    # Send startup message with main menu
    cmd_start(CHAT_ID)

    print("  Polling for messages...")

    offset = 0
    while True:
        try:
            response = api_call("getUpdates", {
                "offset": offset,
                "timeout": 30,
                "allowed_updates": ["message", "callback_query"],
            })

            if not response.get("ok"):
                time.sleep(5)
                continue

            for update in response.get("result", []):
                offset = update["update_id"] + 1

                if "callback_query" in update:
                    cb = update["callback_query"]
                    print(f"  [{time.strftime('%H:%M:%S')}] Button: {cb.get('data', '?')}")
                    try:
                        handle_callback(cb)
                    except Exception as e:
                        print(f"  Error handling callback: {e}")

                elif "message" in update:
                    msg = update["message"]
                    text = msg.get("text", "")
                    if text:
                        print(f"  [{time.strftime('%H:%M:%S')}] Message: {text}")
                    try:
                        handle_message(msg)
                    except Exception as e:
                        print(f"  Error handling message: {e}")

        except KeyboardInterrupt:
            print("\nBot stopped.")
            break
        except Exception as e:
            print(f"  Poll error: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
