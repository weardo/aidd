#!/usr/bin/env python3
"""AIDD Telegram Bot — two-way command interface with inline keyboards.

Uses only Python stdlib (no pip packages). Polls for updates, executes
aidd CLI commands, sends results with dynamic inline buttons.

Supports multiple projects via ~/.aidd/projects.yml registry.

Usage: aidd telegram [--project /path/to/project]
"""

import json
import os
import subprocess
import sys
import threading
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

# ── Project Registry ────────────────────────────────────────────────────

PROJECTS_FILE = os.path.join(AIDD_HOME, "projects.yml")


def load_projects() -> dict[str, str]:
    """Load project registry from ~/.aidd/projects.yml."""
    if not os.path.exists(PROJECTS_FILE):
        return {}
    try:
        result = subprocess.run(
            ["yq", "-o=json", ".projects", PROJECTS_FILE],
            capture_output=True, text=True
        )
        data = json.loads(result.stdout) if result.stdout.strip() else {}
        return {k: v for k, v in (data or {}).items() if v and os.path.isdir(str(v))}
    except Exception:
        return {}


def save_project(name: str, path: str):
    """Add a project to the registry."""
    subprocess.run(
        ["yq", "-i", f'.projects."{name}" = "{path}"', PROJECTS_FILE],
        capture_output=True
    )


# Load projects and set active
projects = load_projects()

# CLI --project flag registers and activates
cli_project = ""
for i, arg in enumerate(sys.argv[1:], 1):
    if arg.startswith("--project="):
        cli_project = arg.split("=", 1)[1]
    elif arg == "--project" and i + 1 < len(sys.argv):
        cli_project = sys.argv[i + 1]

if cli_project and os.path.isdir(cli_project):
    name = os.path.basename(cli_project)
    if name not in projects:
        save_project(name, cli_project)
    projects[name] = cli_project

if not projects:
    if os.path.exists("aidd.yml") or os.path.exists("feature-queue.yml"):
        name = os.path.basename(os.getcwd())
        projects[name] = os.getcwd()
        save_project(name, os.getcwd())

# Active project (first one, or the --project one)
active_project = os.path.basename(cli_project) if cli_project else ""
if not active_project and projects:
    active_project = next(iter(projects))

# Pending reject: waiting for reason text after user presses Reject button
# Maps chat_id -> feature_id
pending_reject = {}

# Running agent: tracks the active claude -p subprocess
# Only one agent runs at a time to avoid resource contention
running_agent: dict | None = None  # {"proc": Popen, "label": str, "chat_id": str}


def get_project_dir() -> str:
    """Get active project directory."""
    return projects.get(active_project, "")


def resolve_claude_bin() -> str:
    """Find the claude binary."""
    for path in [
        os.path.expanduser("~/.local/bin/claude"),
        "/usr/local/bin/claude",
        "/opt/homebrew/bin/claude",
    ]:
        if os.path.isfile(path) and os.access(path, os.X_OK):
            return path
    # Fallback: hope it's on PATH
    return "claude"


CLAUDE_BIN = resolve_claude_bin()


STREAM_INTERVAL = 3  # seconds between Telegram message updates


def run_claude(prompt: str, chat_id: str, label: str = "Agent",
               timeout_minutes: int = 30):
    """Run claude -p in background thread, stream output to chat via message edits."""
    global running_agent
    pdir = get_project_dir()
    if not pdir:
        send("No active project. Use /projects to select one.", chat_id=chat_id)
        return
    if running_agent and running_agent["proc"].poll() is None:
        send(
            f"\u26a0\ufe0f Agent already running: *{running_agent['label']}*\n"
            "Use /cancel to stop it first.",
            chat_id=chat_id,
        )
        return

    # Clean env for child process
    child_env = {k: v for k, v in os.environ.items()
                 if k not in ("CLAUDECODE", "CLAUDE_CODE_SSE_PORT",
                              "CLAUDE_CODE_ENTRYPOINT")}

    proc = subprocess.Popen(
        [CLAUDE_BIN, "-p", prompt,
         "--permission-mode", "bypassPermissions",
         "--model", "sonnet",
         "--verbose"],
        cwd=pdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        env=child_env,
    )
    running_agent = {"proc": proc, "label": label, "chat_id": chat_id}

    # Send initial message and capture its ID for streaming edits
    result = api_call("sendMessage", {
        "chat_id": chat_id,
        "text": f"\U0001f916 *{label}* started on `{active_project}`\n\n`waiting for output...`",
        "parse_mode": "Markdown",
    })
    stream_msg_id = None
    if result.get("ok"):
        stream_msg_id = result["result"]["message_id"]

    def _tail(text: str, max_chars: int = 3500) -> str:
        """Get the tail of text that fits in a Telegram message."""
        if len(text) <= max_chars:
            return text
        # Find a newline near the cut point
        cut = text[-(max_chars):]
        nl = cut.find("\n")
        if nl > 0:
            cut = cut[nl + 1:]
        return "...(streaming)\n" + cut

    def _stream():
        global running_agent
        output_lines = []
        last_update = 0
        deadline = time.time() + timeout_minutes * 60

        try:
            for line in proc.stdout:
                output_lines.append(line.rstrip("\n"))

                now = time.time()
                if now > deadline:
                    proc.kill()
                    _finish_stream(output_lines, stream_msg_id, label,
                                   chat_id, timed_out=True)
                    running_agent = None
                    return

                # Throttled update to Telegram
                if stream_msg_id and (now - last_update) >= STREAM_INTERVAL:
                    last_update = now
                    display = _tail("\n".join(output_lines))
                    edit_message(
                        chat_id, stream_msg_id,
                        f"\U0001f916 *{label}* running...\n```\n{display}\n```",
                        keyboard=[[{"text": "\U0001f6d1 Cancel",
                                    "callback_data": "cmd:cancel"}]],
                    )
        except Exception:
            pass

        proc.wait()
        _finish_stream(output_lines, stream_msg_id, label, chat_id)
        running_agent = None

    t = threading.Thread(target=_stream, daemon=True)
    t.start()


def _finish_stream(output_lines: list, msg_id: int | None, label: str,
                   chat_id: str, timed_out: bool = False):
    """Send final message when agent completes."""
    output = "\n".join(output_lines).strip()
    output = _final_output(output)

    if timed_out:
        text = f"\u23f0 *{label}* timed out\n```\n{output}\n```"
    else:
        rc = running_agent["proc"].returncode if running_agent else -1
        icon = "\u2705" if rc == 0 else "\u274c"
        text = f"{icon} *{label}* finished (exit {rc})\n```\n{output}\n```"

    keyboard = [[
        {"text": "\U0001f4ca Status", "callback_data": "cmd:status"},
        {"text": "\U0001f916 New Task", "callback_data": "cmd:help"},
    ]]

    if msg_id:
        edit_message(chat_id, msg_id, text, keyboard=keyboard)
    else:
        send(text, keyboard=keyboard, chat_id=chat_id)


def _final_output(output: str, max_chars: int = 3500) -> str:
    """Trim output for final message."""
    if len(output) <= max_chars:
        return output or "(no output)"
    lines = output.split("\n")
    return "...(truncated)\n" + "\n".join(lines[-80:])


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
    """Run an aidd CLI command in the active project directory."""
    pdir = get_project_dir()
    if not pdir:
        return "No active project. Use /projects to select one."
    full_cmd = ["aidd", cmd] + (args or [])
    try:
        result = subprocess.run(
            full_cmd, capture_output=True, text=True,
            timeout=120, cwd=pdir
        )
        return (result.stdout + result.stderr).strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"Command timed out after 120s: aidd {cmd}"
    except Exception as e:
        return f"Error: {e}"


def shell_run(cmd: str) -> str:
    """Run a shell command in the active project directory."""
    pdir = get_project_dir()
    if not pdir:
        return "No active project. Use /projects to select one."
    try:
        result = subprocess.run(
            cmd, shell=True, capture_output=True, text=True,
            timeout=60, cwd=pdir
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
    pdir = get_project_dir()
    if not pdir:
        return []
    try:
        result = subprocess.run(
            ["yq", "-r", ".features[]", "feature-queue.yml"],
            capture_output=True, text=True, cwd=pdir
        )
        if result.returncode != 0:
            return []
        # Parse yq JSON output
        result2 = subprocess.run(
            ["yq", "-o=json", ".features", "feature-queue.yml"],
            capture_output=True, text=True, cwd=pdir
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
    pdir = get_project_dir()
    project_label = f"`{active_project}`" if active_project else "none"
    n_projects = len(projects)
    keyboard = [
        [
            {"text": "\U0001f4d1 Specs & Plans", "callback_data": "cmd:specs"},
            {"text": "\U0001f4ca Status", "callback_data": "cmd:status"},
        ],
        [
            {"text": "\u25b6\ufe0f Conductor", "callback_data": "cmd:conductor"},
            {"text": "\u2753 Help", "callback_data": "cmd:help"},
        ],
    ]
    if n_projects > 1:
        keyboard.append([
            {"text": f"\U0001f4c1 Switch Project ({n_projects})", "callback_data": "cmd:projects"},
        ])
    send(
        f"*AIDD Bot Online*\nProject: {project_label}",
        keyboard=keyboard, chat_id=chat_id
    )


def cmd_projects(chat_id: str):
    """Show project list with switch buttons."""
    global projects
    projects = load_projects()  # Refresh

    if not projects:
        send("No projects registered.\nUse `aidd init` in a project directory to register it.",
             chat_id=chat_id)
        return

    lines = ["*Registered Projects*\n"]
    keyboard = []

    for name, path in projects.items():
        marker = "\u25c9" if name == active_project else "\u25cb"  # ◉ vs ○
        lines.append(f"{marker} `{name}` \u2014 `{path}`")
        if name != active_project:
            keyboard.append([
                {"text": f"\u25b6\ufe0f Switch to {name}", "callback_data": f"switch:{name}"}
            ])

    keyboard.append([{"text": "\u2b05\ufe0f Back", "callback_data": "cmd:start"}])
    send("\n".join(lines), keyboard=keyboard, chat_id=chat_id)


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
        "*Agent:*\n"
        "Just type a message \u2014 sent directly to Claude\n"
        "/specs \u2014 Browse specs & plans, tap to implement\n"
        "/implement <name> \u2014 Run a plan\n"
        "/cancel \u2014 Stop running agent\n\n"
        "*Pipeline:*\n"
        "/status \u2014 Pipeline overview\n"
        "/queue \u2014 Feature queue\n"
        "/logs <id> \u2014 Agent output\n"
        "/conductor \u2014 Run conductor pass\n\n"
        "*System:*\n"
        "/projects \u2014 Switch project\n"
        "/run <cmd> \u2014 Shell command\n"
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


def cmd_specs(chat_id: str):
    """List available specs and plans in the active project."""
    pdir = get_project_dir()
    if not pdir:
        send("No active project.", chat_id=chat_id)
        return

    # Find specs and plans directories (from aidd.yml or defaults)
    specs_dir = os.path.join(pdir, "docs", "specs")
    plans_dir = os.path.join(pdir, "docs", "plans")

    lines = [f"*Specs & Plans* (`{active_project}`)\n"]
    keyboard = []

    # List specs
    if os.path.isdir(specs_dir):
        specs = sorted([f for f in os.listdir(specs_dir)
                        if f.endswith(".md") and f not in ("INDEX.md", "TEMPLATE.md")])
        if specs:
            lines.append(f"*Specs* ({len(specs)}):")
            for s in specs[-10:]:  # Last 10
                name = s.replace(".md", "")
                lines.append(f"  \u2022 `{name}`")
            if len(specs) > 10:
                lines.append(f"  _...and {len(specs) - 10} more_")

    # List plans
    if os.path.isdir(plans_dir):
        plans = sorted([f for f in os.listdir(plans_dir)
                        if f.endswith(".md") and f not in ("INDEX.md", "TEMPLATE.md")])
        if plans:
            lines.append(f"\n*Plans* ({len(plans)}):")
            for p in plans[-10:]:
                name = p.replace(".md", "")
                lines.append(f"  \u2022 `{name}`")
                # Add implement button for each plan
                # Truncate callback_data to 64 bytes (Telegram limit)
                cb_name = name[:50]
                keyboard.append([
                    {"text": f"\u25b6\ufe0f Implement: {name[:30]}", "callback_data": f"impl:{cb_name}"}
                ])
            if len(plans) > 10:
                lines.append(f"  _...and {len(plans) - 10} more_")

    if len(lines) == 1:
        lines.append("No specs or plans found in `docs/specs/` or `docs/plans/`.")

    keyboard.append([{"text": "\u2b05\ufe0f Back", "callback_data": "cmd:start"}])
    send("\n".join(lines), keyboard=keyboard, chat_id=chat_id)


def cmd_implement(chat_id: str, name: str):
    """Spawn claude -p to implement a spec/plan."""
    pdir = get_project_dir()
    if not pdir:
        send("No active project.", chat_id=chat_id)
        return

    if not name:
        send("Usage: `/implement <plan-name>`\nOr use /specs to browse.", chat_id=chat_id)
        return

    # Find matching plan/spec files
    plan_file = ""
    spec_file = ""
    for d in ["docs/plans", "docs/specs"]:
        full_dir = os.path.join(pdir, d)
        if not os.path.isdir(full_dir):
            continue
        for f in os.listdir(full_dir):
            if name in f and f.endswith(".md"):
                if "plan" in d:
                    plan_file = os.path.join(d, f)
                else:
                    spec_file = os.path.join(d, f)

    if not plan_file and not spec_file:
        send(f"No plan or spec matching `{name}` found.", chat_id=chat_id)
        return

    # Build prompt
    parts = ["You are implementing a feature autonomously.\n"]
    if plan_file:
        parts.append(f"Read the plan: {plan_file}")
    if spec_file:
        parts.append(f"Read the spec: {spec_file}")
    parts.append("Read CLAUDE.md for project conventions.")
    parts.append("Follow TDD: write tests first, then implement, then verify.")
    parts.append("Commit after each passing phase.")
    parts.append("If stuck after 3 attempts, stop and explain what's blocking.")

    prompt = "\n".join(parts)
    label = f"Implement {name}"
    run_claude(prompt, chat_id, label=label, timeout_minutes=60)


def cmd_cancel(chat_id: str):
    """Cancel the running agent."""
    global running_agent
    if not running_agent or running_agent["proc"].poll() is not None:
        send("No agent running.", chat_id=chat_id)
        running_agent = None
        return
    label = running_agent["label"]
    running_agent["proc"].kill()
    running_agent = None
    send(f"\U0001f6d1 *Cancelled:* {label}", chat_id=chat_id)


def cmd_ask(chat_id: str, text: str):
    """Send a raw message to claude -p as a direct prompt."""
    if not text:
        send("Send any message without / to ask Claude directly.", chat_id=chat_id)
        return
    run_claude(text, chat_id, label="Ask", timeout_minutes=5)


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
        elif cmd == "start":
            cmd_start(chat_id)
        elif cmd == "projects":
            cmd_projects(chat_id)
        elif cmd == "specs":
            cmd_specs(chat_id)
        elif cmd == "cancel":
            cmd_cancel(chat_id)
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

    elif action == "switch":
        global active_project
        new_project = feature_id  # reusing the split — "switch:projectname"
        if new_project in projects:
            active_project = new_project
            answer_callback(cb_id, f"Switched to {new_project}")
            pdir = get_project_dir()
            edit_message(chat_id, message_id,
                         f"\u2705 *Switched to `{new_project}`*\n`{pdir}`",
                         keyboard=[[{"text": "\U0001f4ca Status", "callback_data": "cmd:status"},
                                    {"text": "\U0001f4c1 Projects", "callback_data": "cmd:projects"}]])
        else:
            answer_callback(cb_id, f"Unknown project: {new_project}")

    elif action == "impl":
        answer_callback(cb_id, f"Implementing {feature_id}...")
        cmd_implement(chat_id, feature_id)

    elif action == "remove":
        answer_callback(cb_id, f"Removing {feature_id}...")
        # Set status to abandoned
        pdir = get_project_dir()
        if pdir:
            subprocess.run(
                ["yq", "-i",
                 f'(.features[] | select(.id == "{feature_id}") | .status) = "abandoned"',
                 "feature-queue.yml"],
                cwd=pdir
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

    # Raw messages → send directly to claude -p
    if not text.startswith("/"):
        cmd_ask(chat_id, text)
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
    elif cmd == "specs":
        cmd_specs(chat_id)
    elif cmd in ("implement", "impl"):
        cmd_implement(chat_id, args)
    elif cmd == "cancel":
        cmd_cancel(chat_id)
    elif cmd == "projects":
        cmd_projects(chat_id)
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
        {"command": "specs", "description": "Browse specs & plans"},
        {"command": "implement", "description": "Run a plan with Claude agent"},
        {"command": "cancel", "description": "Stop running agent"},
        {"command": "status", "description": "Pipeline overview"},
        {"command": "queue", "description": "View feature queue"},
        {"command": "conductor", "description": "Run conductor pass"},
        {"command": "logs", "description": "View agent output"},
        {"command": "projects", "description": "Switch active project"},
        {"command": "run", "description": "Shell command in project"},
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
    pdir = get_project_dir()
    project_name = active_project if active_project else "<none>"
    print("AIDD Telegram Bot started")
    print(f"  Active project: {project_name} ({pdir})")
    print(f"  Registered projects: {', '.join(projects.keys()) or 'none'}")
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
