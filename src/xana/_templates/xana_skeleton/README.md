# .xana/

Project-level xana workspace. Created by `xana setup`.

Layout:

- `version` — xana version this layout matches
- `manifest.json` — bundled agents/skills tracked for this project
- `agents/` — project-scoped agent packages
- `skills/` — project-scoped skill packages
- `docs/` — project documentation surfaced to agents
- `data/` — runtime state (checkpoints, history, memory). Should be gitignored.

The actual user/machine config lives at `~/.xana/config/system.json` (or
`$XANA_HOME/config/system.json` if set). API keys never live under `.xana/`.
