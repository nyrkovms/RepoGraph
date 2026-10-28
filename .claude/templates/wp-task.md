# WP <id>: <title>

**Stage / branch:** <e.g. E0-A / claude/me-e0a-schema>
**Spec:** <sections of SPEC-MULTI-ECOSYSTEM.md / IMPL-MULTI-ECOSYSTEM.md that define the behaviour>
**Start:** `git worktree add -b wp/<id> /Users/mikhail/Documents/Work/repograph/.claude/worktrees/wp-<id> <stage-head-sha>`,
work only there, check `git log --oneline -1` shows `<stage-head-sha>`, `cd frontend && npm ci` if you run frontend checks.

## Goal
<One paragraph: what exists after this WP and why.>

## Files you may change
- <paths; anything else is out of scope>

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
