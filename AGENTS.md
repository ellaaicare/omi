# Codex Agent Rules

These rules apply to Codex when working in this repository.

## Repository Boundaries

- **Code and pull requests:** `ellaaicare/omi`.
- **All Ella issues:** private `ellaaicare/ella-ai`. The public OMI fork's Issues feature is intentionally disabled.
- Every issue command must name the private repository explicitly, for example:
  `gh issue create --repo ellaaicare/ella-ai ...`.
- Every pull-request command must name the code fork explicitly, for example:
  `gh pr create --repo ellaaicare/omi ...`.
- Never use a bare issue reference such as `#123` or `Closes #123`. Use the fully qualified
  `ellaaicare/ella-ai#123`; use a closing keyword only when merging the PR should close the entire private issue.
- `BasedHardware/omi` is fetch-only upstream. Never push to it or create issues, comments, reviews, or pull requests
  there.
- Run `scripts/setup-ella-repository-guardrails.sh` once per clone before GitHub operations.

## Setup

- Install repository guardrails and hooks: `scripts/setup-ella-repository-guardrails.sh`

## Coding Guidelines

### Backend

- No in-function imports. All imports must be at the module top level.
- Follow the module hierarchy when importing. Higher-level modules import from lower-level modules, never the reverse.

Module hierarchy (lowest to highest):
1. `database/`
2. `utils/`
3. `routers/`
4. `main.py`

- Memory management: free large objects immediately after use. E.g., `del` for byte arrays after processing, `.clear()` for dicts/lists holding data.

### App (Flutter)

- All user-facing strings must use l10n (`context.l10n.keyName`). Add keys to ARB files using `jq` to avoid reading large files.
- After modifying ARB files in `app/lib/l10n/`, regenerate localizations: `cd app && flutter gen-l10n`

## Formatting

Always format code after making changes. The pre-commit hook handles this automatically, but you can also run manually:

- **Dart (app/)**: `dart format --line-length 120 <files>`
  - Files ending in `.gen.dart` or `.g.dart` are auto-generated and should not be formatted manually.
- **Python (backend/)**: `black --line-length 120 --skip-string-normalization <files>`
- **C/C++ (firmware: omi/, omiGlass/)**: `clang-format -i <files>`

## Testing

- Always run tests before committing:
  - Backend changes: run `backend/test.sh`
  - App changes: run `app/test.sh`
