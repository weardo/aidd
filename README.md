# AIDD — Autonomous AI Development Daemon

Clone on any machine. Run `./setup.sh`. Get autonomous overnight AI development.

## What It Does

- Reads a feature queue (`feature-queue.yml`) from any project
- Spawns headless Claude Code (`claude -p`) per feature in isolated git worktrees
- Monitors progress, handles failures, creates PRs
- Notifies you via Telegram when features complete or get stuck
- Runs on your Claude Max subscription — $0 extra cost

## Quick Start

```bash
git clone git@github.com:<you>/aidd.git ~/.aidd
~/.aidd/setup.sh

# In any project:
cd ~/your-project
aidd init
# Edit aidd.yml and feature-queue.yml
aidd start
```

## Project Setup

```bash
aidd init                # creates aidd.yml + feature-queue.yml templates
aidd start               # start conductor (loops every 5 min)
aidd start --once        # single pass (for cron)
aidd start --dry-run     # show what would happen
aidd status              # pipeline overview
aidd queue               # view feature queue
aidd approve <id>        # mark feature for merge
aidd logs <id>           # view agent output
```

## How It Works

```
feature-queue.yml          You queue features with specs + plans
       |
   conductor.sh            Picks next eligible feature
       |
  git worktree add         Creates isolated branch
       |
  claude -p "<prompt>"     Headless Claude implements the feature
       |
  gh pr create             Creates PR when tests pass
       |
  Telegram notification    You review + approve from phone
```

## Config: `aidd.yml`

Drop in any project root. Teaches AIDD how to work with that project.

See `templates/aidd.yml` for full reference.

## Telegram Setup

1. Message @BotFather on Telegram, create a bot, get the token
2. Message your bot, then get your chat ID from `https://api.telegram.org/bot<TOKEN>/getUpdates`
3. Run `./setup.sh` and enter the token + chat ID (or edit `~/.aidd/.env`)

## Requirements

- `claude` CLI (Claude Code) with active Max subscription
- `git`
- `yq` and `jq` (installed by setup.sh)
- `gh` (GitHub CLI, optional — for PR creation)
