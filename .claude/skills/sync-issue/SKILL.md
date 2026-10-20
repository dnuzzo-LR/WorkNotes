---
name: sync-issue
description: Use when Dan invokes /sync-issue or asks to sync/refresh his TODO.md or "issues assigned to me" from GitHub. `todo` (the default) regenerates ~/WorkNotes/TODO.md from his open lightriversoftware/netflex issues, grouped by netFLEX_Development board Status with Sprint labels.
---

# sync-issue

## `todo` (default)

Run the script; it does all the work and needs only `gh` (logged in, `project` scope):

```bash
~/WorkNotes/.claude/skills/sync-issue/sync-todo.py
```

- Fetches open issues in `lightriversoftware/netflex` assigned to the gh user (`dnuzzo-LR`).
- Groups them under `### <Status>` in the netFLEX_Development (project #16) board's column order; issues not on the board go under `### No Status`.
- Writes `~/WorkNotes/TODO.md` atomically. Ticked `[x]` boxes are kept for issues still open; any other hand edits are overwritten.
- Prints a one-line summary to stderr (count, issues added, issues removed). Report that summary to Dan, not the whole file.
- `--dry-run` prints the result without writing it.

obsidian-git commits the change on its next vault backup, so do not commit TODO.md yourself.

## `all`

Not implemented. The old version also wrote one note per issue into `~/WorkNotes/Issues/<Status>/`; that code was lost. Say so instead of improvising it.
