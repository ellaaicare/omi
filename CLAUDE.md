# Coding Guidelines

## Behavior

- Never ask for permission to access folders, run commands, search the web, or use tools. Just do it.
- Never ask for confirmation. Just act. Make decisions autonomously and proceed without checking in.

## Setup

### Install Pre-commit Hook
Run once to enable auto-formatting on commit:
```bash
ln -s -f ../../scripts/pre-commit .git/hooks/pre-commit
```

## Backend

### No In-Function Imports
All imports must be at the module top level. Never import inside functions.

```python
# Bad
def my_function():
    from database.redis_db import r  # Don't do this
    r.get('key')

# Good
from database.redis_db import r

def my_function():
    r.get('key')
```

### Import from Lower-Level Modules
Follow the module hierarchy when importing. Higher-level modules import from lower-level modules, never the reverse.

**Module hierarchy (lowest to highest):**
1. `database/` - Database connections, cache instances
2. `utils/` - Utility functions, helpers
3. `routers/` - API endpoints
4. `main.py` - Application entry point

```python
# Bad - utils importing from routers or main
# utils/apps.py
from main import memory_cache  # Don't import from higher level
from routers.apps import some_function  # Don't import from higher level

# Good - utils importing from database
# utils/apps.py
from database.cache import get_memory_cache
from database.redis_db import r
```

### Memory Management

Free large objects immediately after use. E.g., `del` for byte arrays after processing, `.clear()` for dicts/lists holding data.

## App (Flutter)

### Localization Required

- All user-facing strings must use l10n. Use `context.l10n.keyName` instead of hardcoded strings. Add new keys to ARB files using `jq` (never read full ARB files - they're large and will burn tokens). See skill `add-a-new-localization-key-l10n-arb` for details.

- After modifying ARB files in `app/lib/l10n/`, regenerate the localization files:
```bash
cd app && flutter gen-l10n
```

## Formatting

Always format code after making changes. The pre-commit hook handles this automatically, but you can also run manually:

### Dart (app/)
```bash
dart format --line-length 120 <files>
```
Note: Files ending in `.gen.dart` or `.g.dart` are auto-generated and should not be formatted manually.

### Python (backend/)
```bash
black --line-length 120 --skip-string-normalization <files>
```

### C/C++ (firmware: omi/, omiGlass/)
```bash
clang-format -i <files>
```

## Git

- Never squash merge PRs — use regular merge
- Make individual commits per file, not bulk commits
- **RELEASE command**: When the user says "RELEASE", perform the full release flow:
  1. Create a new branch from main
  2. Make individual commits per changed file
  3. Push and create a PR
  4. Merge the PR (no squash — regular merge)
  5. Switch back to main and pull

## Testing

### Always Run Tests Before Committing
After making changes, always run the appropriate test script to verify your changes.

- **Backend changes**: Run `backend/test.sh`
- **App changes**: Run `app/test.sh`

## Ella Session State

**IMPORTANT**: On every new session, read `ELLA_SESSION_STATE.md` in the repo root FIRST before doing anything else. It contains the current work-in-progress, team status, decisions made, and what to do next. Update it whenever significant progress is made or the session ends.

If the user says "continue" or asks what's going on, consult that file immediately — it's your persistent memory across sessions.

# Agent Team Coordination Rules

## CRITICAL: Team Lifecycle Management

### DO NOT tear down the team until:
1. Every task in the shared task list is marked COMPLETE
2. The user has explicitly confirmed "done" or "ship it" or "tear down"
3. All tests pass and the user has reviewed the output
4. A final state summary has been written to `.agent-state/` (see below)

### DO NOT do work yourself as the lead. Your job is:
- Break down tasks and assign them to teammates
- Route information between teammates when needed
- Monitor task list progress
- Wait patiently for teammates to finish before moving on
- Ask the user before concluding ANY phase

### If a teammate finishes their task:
- Assign them the next unfinished task
- Do NOT shut them down unless there is truly nothing left
- If unsure, ask the user: "Teammate X finished. Should I assign them to Y or keep them on standby?"

---

## MANDATORY: State Persistence via `.agent-state/`

Before ANY team action (spawn, task completion, phase transition, teardown), the lead MUST maintain state files.

### Directory structure:
```
.agent-state/
├── team-registry.md        # Current team members, roles, status
├── task-ledger.md          # All tasks, owners, status, blockers
├── decisions.md            # Key decisions made and rationale
├── phase-log.md            # What phase we're in, what's done
└── members/
    ├── teammate-1.md       # That member's context, findings, progress
    ├── teammate-2.md       # ...
    └── teammate-3.md
```

### team-registry.md format:
```markdown
# Team Registry
Last updated: [timestamp]
Phase: [current phase]

| Member | Role | Status | Current Task | Key Findings |
|--------|------|--------|-------------|--------------|
| lead   | coordinator | active | orchestrating | ... |
| tm-1   | [role] | active/done | [task] | [summary] |
```

### Rules for state files:
- **On every task completion**: teammate MUST write a summary to their `.agent-state/members/[name].md` file BEFORE reporting done
- **On every phase transition**: lead MUST update `phase-log.md` and `task-ledger.md`
- **On every decision**: lead MUST append to `decisions.md`
- **Before spawning new teammates**: lead MUST read ALL files in `.agent-state/` and include relevant context in the spawn prompt
- **Teammate spawn prompts MUST include**: "Read `.agent-state/` before starting. Write your progress to `.agent-state/members/[your-name].md` after each significant step."

---

## If the team MUST be respawned:

1. Lead reads ALL `.agent-state/` files first
2. New teammate spawn prompts include:
   - The full contents of that member's previous state file
   - Current phase from `phase-log.md`
   - Relevant decisions from `decisions.md`
   - Their specific task assignment from `task-ledger.md`
3. New teammate's FIRST action is to read `.agent-state/` and confirm they have context

---

## Iteration Protocol

Each iteration follows this loop. Do NOT skip steps:

1. **PLAN** — Lead breaks down work, writes to `task-ledger.md`, assigns to teammates
2. **EXECUTE** — Teammates work. Each writes progress to their state file
3. **SYNC** — Lead collects results, updates `phase-log.md` and `decisions.md`
4. **REVIEW** — Lead presents results to user. Waits for feedback
5. **USER DECISION** — Only the user decides: iterate again, move to next phase, or tear down

The lead NEVER autonomously decides the team is "done." Only the user makes that call.

---

## Teammate Instructions (include in every spawn prompt)

```
You are part of an agent team. Follow these rules:
1. Read `.agent-state/` before starting any work
2. Write your progress and findings to `.agent-state/members/[your-name].md` after each significant step
3. When you complete a task, update your state file FIRST, then mark the task complete
4. Do NOT stop working unless the lead tells you to or you have no remaining tasks
5. If you hit a blocker, message the lead — do not silently stop
6. Include enough detail in your state file that a replacement could pick up where you left off
```
