# AIDD Project Setup Prompt

Use this prompt when a user asks you to "set up AIDD", "configure AIDD", or "add AIDD to this project".

## Prerequisites

1. `~/.aidd/` must exist with `aidd` CLI and `conductor.sh`
2. If not installed: `git clone git@github-weardo:weardo/aidd.git ~/.aidd && ~/.aidd/setup.sh`
3. Required tools: `claude` (Claude Code CLI), `git`, `yq`, `jq`

## Setup Steps

### Step 1: Initialize

```bash
cd <project-directory>
aidd init
```

This creates `aidd.yml`, `feature-queue.yml`, `docs/specs/`, `docs/plans/`, and `.gitignore` entries.

### Step 2: Analyze the project and configure

Read the project to understand:
- **Language/framework** (Python, Node, Go, Rust, etc.)
- **Test command** (pytest, jest, vitest, go test, cargo test)
- **Lint command** (ruff, eslint, golangci-lint, clippy)
- **Project name** (from package.json, pyproject.toml, go.mod, or directory name)
- **Existing conventions** (CLAUDE.md, README, existing test patterns)

Then edit `aidd.yml` — only set values that differ from defaults:

```yaml
version: 1

project:
  name: <detected-name>

# Only add these if auto-detection won't work
commands:
  test: "<detected-test-command>"
  lint: "<detected-lint-command>"

# Only add if project has special conventions
prompt_instructions:
  pre_implement: |
    <any project-specific setup instructions for the agent>
```

**Defaults you DON'T need to set** (conductor handles these):
- `agent.model`: sonnet
- `agent.max_parallel`: 1
- `agent.permission_mode`: auto
- `agent.timeout_hours`: 4
- `paths.*`: .worktrees/, .aidd/, docs/specs/, docs/plans/
- `prompt_template`: built-in TDD template
- `commands.test`: auto-detected from project files

### Step 3: Verify CLAUDE.md exists

The AIDD agent reads CLAUDE.md for project conventions. If it doesn't exist, create a minimal one:

```markdown
# CLAUDE.md — <Project Name>

<One-line description>

## Commands
\`\`\`bash
# Test
<test command>

# Lint (if applicable)
<lint command>
\`\`\`

## Key Conventions
- <Any hard rules for this project>
```

### Step 4: Create a test feature (optional but recommended)

Create a trivial feature to verify the pipeline works:

**docs/specs/hello-world.md:**
```markdown
# Test Feature: Hello World

Create a minimal function and test to verify AIDD pipeline works end-to-end.

## Deliverable
- A function that returns "Hello, World!"
- A test that verifies it
```

**docs/plans/hello-world-plan.md:**
```markdown
# Implementation Plan: Hello World

## Phase 1: Create function and test

### Task 1: Write test
Create a test file that imports and tests the function.

### Task 2: Write implementation
Create the source file with the function.

### Task 3: Run tests
Run the test command and verify all tests pass.
```

Add to `feature-queue.yml`:
```yaml
features:
  - id: hello-world
    spec: docs/specs/hello-world.md
    plan: docs/plans/hello-world-plan.md
    priority: 1
    status: queued
    depends_on: []
    feedback: ""
```

### Step 5: Test the pipeline

```bash
aidd start --dry-run    # verify prompt construction
aidd start --once       # run one feature
aidd status             # check results
aidd logs hello-world   # inspect agent output
```

### Step 6: Commit setup

```bash
git add aidd.yml feature-queue.yml docs/ CLAUDE.md .gitignore
git commit -m "chore: initialize AIDD pipeline"
```

## Troubleshooting

- **"command not found: aidd"** → Run `~/.aidd/setup.sh` to symlink CLI
- **"command not found: yq"** → `brew install yq` (macOS) or `snap install yq` (Linux)
- **"command not found: claude"** → Install Claude Code: `npm install -g @anthropic-ai/claude-code`
- **Agent exits immediately** → Check `aidd logs <id>` for errors
- **Permission denied** → Ensure `agent.permission_mode: auto` in aidd.yml
