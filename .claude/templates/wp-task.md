# WP <id>: <title>

**Stage / branch:** <e.g. E0-A / claude/me-e0a-schema, or the fix/docs branch>
**Spec:** <spec sections that define the behaviour (SPEC-MULTI-ECOSYSTEM.md, a feature spec, the bug issue)>
**WP_DIR:** `/Users/mikhail/Developer/repograph/.claude/worktrees/wp-<id>`
**Start:**
1. `git worktree add -b wp/<id> <WP_DIR> <stage-head-sha>`; `git -C <WP_DIR> log --oneline -1` must show `<stage-head-sha>`.
2. `cd <WP_DIR>/frontend && npm ci` (`make check` needs it).
3. Your shell's working directory is reset to another checkout between commands: start every command with
   `cd <WP_DIR> &&` (or use `git -C <WP_DIR>`), and read/edit files only by absolute paths under `<WP_DIR>`.
4. Before committing: `git -C <WP_DIR> branch --show-current` must print `wp/<id>`.

## Goal
<One paragraph: what exists after this WP and why.>

## Files you may change
- <paths; anything else is out of scope>

## Docs to update (CLAUDE.md rule 10)
- <documents and sections this change makes stale, or "none"; list them under "Files you may change" too>

## Sample to follow
- <existing file or function whose structure, naming and style to copy>

## Requirements
1. <concrete, checkable>
2. ...

## Out of scope / do not
- <explicit exclusions, protected paths>

## Done when
- <tests that must exist and pass, behaviour visible on the stand, etc.>

## Check command
```bash
<command(s) that must be green, at least `make check`>
```
