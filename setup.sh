#!/bin/bash
set -euo pipefail

# AIDD — Autonomous AI Development Daemon
# One-command setup on any machine
# Usage: git clone git@github.com:<you>/aidd.git ~/.aidd && ~/.aidd/setup.sh

AIDD_DIR="$(cd "$(dirname "$0")" && pwd)"
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo "========================================="
echo "  AIDD Setup"
echo "========================================="
echo ""

# 1. Check prerequisites
echo -e "${YELLOW}Checking prerequisites...${NC}"

if ! command -v claude &> /dev/null; then
    echo -e "${RED}ERROR: claude CLI not found. Install Claude Code first.${NC}"
    echo "  npm install -g @anthropic-ai/claude-code"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} claude CLI $(claude --version 2>/dev/null || echo 'found')"

if ! command -v git &> /dev/null; then
    echo -e "${RED}ERROR: git not found.${NC}"
    exit 1
fi
echo -e "  ${GREEN}✓${NC} git $(git --version | head -1)"

if ! command -v gh &> /dev/null; then
    echo -e "${YELLOW}  ⚠ gh (GitHub CLI) not found — PR creation won't work. Install: brew install gh${NC}"
else
    echo -e "  ${GREEN}✓${NC} gh CLI found"
fi

# Check for yq (YAML parser)
if ! command -v yq &> /dev/null; then
    echo -e "${YELLOW}  ⚠ yq not found. Installing...${NC}"
    if command -v brew &> /dev/null; then
        brew install yq
    elif command -v pip3 &> /dev/null; then
        pip3 install yq
    else
        echo -e "${RED}  Cannot install yq. Install manually: brew install yq${NC}"
        exit 1
    fi
fi
echo -e "  ${GREEN}✓${NC} yq found"

# Check for jq
if ! command -v jq &> /dev/null; then
    echo -e "${YELLOW}  ⚠ jq not found. Installing...${NC}"
    if command -v brew &> /dev/null; then
        brew install jq
    else
        echo -e "${RED}  Cannot install jq. Install manually: brew install jq${NC}"
        exit 1
    fi
fi
echo -e "  ${GREEN}✓${NC} jq found"

# 2. Symlink conductor to PATH
echo ""
echo -e "${YELLOW}Setting up CLI...${NC}"

LINK_DIR="$HOME/.local/bin"
mkdir -p "$LINK_DIR"

ln -sf "$AIDD_DIR/conductor.sh" "$LINK_DIR/aidd-conductor"
ln -sf "$AIDD_DIR/aidd" "$LINK_DIR/aidd"
chmod +x "$AIDD_DIR/conductor.sh" "$AIDD_DIR/aidd"

echo -e "  ${GREEN}✓${NC} aidd → $LINK_DIR/aidd"
echo -e "  ${GREEN}✓${NC} aidd-conductor → $LINK_DIR/aidd-conductor"

# Ensure ~/.local/bin is in PATH
if [[ ":$PATH:" != *":$LINK_DIR:"* ]]; then
    echo ""
    echo -e "${YELLOW}Add to your shell profile (~/.zshrc or ~/.bashrc):${NC}"
    echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
fi

# 3. Telegram setup (non-interactive — configure via env vars or .env file)
echo ""
echo -e "${YELLOW}Telegram setup...${NC}"

if [ -f "$AIDD_DIR/.env" ]; then
    echo -e "  ${GREEN}✓${NC} .env already exists, skipping"
elif [ -n "${AIDD_TELEGRAM_TOKEN:-}" ] && [ -n "${AIDD_TELEGRAM_CHAT_ID:-}" ]; then
    # Accept from environment variables (agent-friendly)
    cat > "$AIDD_DIR/.env" <<EOF
AIDD_TELEGRAM_TOKEN=$AIDD_TELEGRAM_TOKEN
AIDD_TELEGRAM_CHAT_ID=$AIDD_TELEGRAM_CHAT_ID
EOF
    chmod 600 "$AIDD_DIR/.env"
    echo -e "  ${GREEN}✓${NC} Telegram configured from env vars"
else
    cat > "$AIDD_DIR/.env" <<EOF
# AIDD_TELEGRAM_TOKEN=your-bot-token
# AIDD_TELEGRAM_CHAT_ID=your-chat-id
EOF
    chmod 600 "$AIDD_DIR/.env"
    echo -e "  ⚠ Telegram not configured — edit ~/.aidd/.env or re-run with:"
    echo "    AIDD_TELEGRAM_TOKEN=xxx AIDD_TELEGRAM_CHAT_ID=yyy ~/.aidd/setup.sh"
fi

# 4. Done
echo ""
echo "========================================="
echo -e "  ${GREEN}AIDD Setup Complete${NC}"
echo "========================================="
echo ""
echo "Usage:"
echo "  cd <your-project>"
echo "  aidd init          # creates aidd.yml + feature-queue.yml"
echo "  aidd start         # start overnight conductor"
echo "  aidd status        # check pipeline"
echo "  aidd queue         # view feature queue"
echo ""
echo "Telegram: edit ~/.aidd/.env to configure"
echo ""
