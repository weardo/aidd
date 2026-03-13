# AIDD — Autonomous AI Development Daemon

Autonomous overnight AI development. Clone → setup → queue features → sleep.

## Install (for AI agents or humans)

```bash
git clone git@github-weardo:weardo/aidd.git ~/.aidd
~/.aidd/setup.sh
```

Prerequisites installed automatically: `yq`, `jq`. Required: `claude` (Claude Code CLI), `git`.

## Add to any project

```bash
cd ~/your-project
aidd init                # zero-config — auto-detects project name, test command
aidd start --dry-run     # verify prompt looks right
aidd start --once        # run one feature
```

That's it. Everything else has sensible defaults.

## AI Agent Setup Guide

**If you are an AI agent setting up AIDD for a project, read `~/.aidd/prompts/setup-project.md`** for step-by-step instructions.

## CLI Reference

| Command | Purpose |
|---------|---------|
| `aidd init` | Create config files + directories |
| `aidd start` | Start conductor (loops every 5 min) |
| `aidd start --once` | Single pass (for cron) |
| `aidd start --dry-run` | Preview without running |
| `aidd status` | Pipeline overview |
| `aidd queue` | View feature queue |
| `aidd approve <id>` | Merge completed feature |
| `aidd reject <id> "reason"` | Reject with feedback (re-queues) |
| `aidd skip <id>` | Re-queue stuck feature |
| `aidd logs <id>` | View agent output |

## How It Works

```
feature-queue.yml          Queue features with specs + plans
       |
   conductor.sh            Picks next queued feature
       |
  git worktree add         Isolated branch per feature
       |
  claude -p "<prompt>"     Headless Claude implements (TDD)
       |
  gh pr create             PR when tests pass
       |
  Telegram notification    Review + approve from phone
```

## Config: `aidd.yml` (optional)

All fields are optional. Defaults:
- **model**: sonnet
- **test command**: auto-detected (pytest/jest/vitest/go test/cargo test)
- **permission_mode**: auto
- **timeout**: 4 hours
- **worktrees**: `.worktrees/`

Only add config for values you want to override:
```yaml
version: 1
project:
  name: my-project
commands:
  test: "python -m pytest tests/ -x -q"
```

## Telegram (optional)

```bash
# Option 1: env vars during setup
AIDD_TELEGRAM_TOKEN=xxx AIDD_TELEGRAM_CHAT_ID=yyy ~/.aidd/setup.sh

# Option 2: edit directly
echo 'AIDD_TELEGRAM_TOKEN=xxx' >> ~/.aidd/.env
echo 'AIDD_TELEGRAM_CHAT_ID=yyy' >> ~/.aidd/.env
```

Get token from @BotFather. Get chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`.

## Requirements

- `claude` CLI with active Max subscription ($0 extra cost)
- `git` with SSH access to repo
- `gh` (GitHub CLI, optional — for PR creation)
