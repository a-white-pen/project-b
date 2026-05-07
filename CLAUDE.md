# CLAUDE.md

All agent rules live in [`AGENTS.md`](AGENTS.md). Read that first. This file adds Claude Code-specific notes only — do not duplicate content from AGENTS.md here.

## Claude Code specifics

**Maintain the docs.** You and Codex are jointly responsible for keeping all `.md` files current as the project evolves. AGENTS.md, OVERVIEW.md, ARCHITECTURE.md, DATA.md, OPERATIONS.md — update them when your changes make them stale.

**Subdirectory agent files.** When a subfolder gains its own AGENTS.md, add a sibling CLAUDE.md as a symlink so both tools find their preferred filename:
```bash
ln -s AGENTS.md CLAUDE.md
```

**Memory.** You have persistent memory for this project at `~/.claude/projects/-Users-bwan-repo-project-b/memory/`. Use it to track decisions and context that would be lost between sessions.
