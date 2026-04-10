# claude-gleaner

A local dashboard that **gathers scattered Claude Code workspace data into one screen** — sessions, instructions, context usage, forks, skills, agents, connectors, hooks, plugins, and projects.

<img width="1429" height="1857" alt="image" src="https://github.com/user-attachments/assets/2d03bb5f-4a18-423c-8883-d97de72cc893" />

> **Read-only by default** — reads from `~/.claude/` and never modifies files unless you explicitly click Delete.

## Quick Start

**Linux / macOS:**
```bash
python3 server.py
```

**Windows:**
```powershell
python server.py
```

Open `http://localhost:8080`. That's it.

Custom port:
```bash
python3 server.py -p 3000           # port 3000
python3 server.py --host 127.0.0.1  # localhost only
```

- Python 3.9+ (stdlib only, zero dependencies)
- Works on Linux, macOS, and Windows
- Default: `0.0.0.0:8080` — accessible from other machines via `http://<your-ip>:8080`
- Data location: `~/.claude/` (Linux/macOS: `/home/<user>/.claude/`, Windows: `C:\Users\<user>\.claude\`)

## What It Solves

### Session confusion
**Problem**: Five terminals open, no idea which is doing what.
**Solution**: All active sessions in one view with name, project, and status. Click to see Context X-ray with real token usage.

### Scattered instructions
**Problem**: "Did I set up CLAUDE.md for this repo?" — configs spread across projects.
**Solution**: Global + per-project CLAUDE.md tree view. Missing projects flagged with alerts. Line count warnings when over 200 lines.

### Context blindness
**Problem**: No idea how full the context window is until you type `/context` or it auto-compacts.
**Solution**: Real-time token usage from `cache_read_input_tokens`. Breakdown by system, memory, agents, skills, messages. Recommendations for when to `/compact` or `/handoff`.

### Lost conversations
**Problem**: "Where was that fork I made last week?" — buried in jsonl files.
**Solution**: All forks auto-labeled with the user's message at the branch point. Full session search. Click to copy `claude --resume --session-id <id>`.

### Opaque configuration
**Problem**: "What skills, agents, hooks, connectors do I even have? Which are mine vs plugin?"
**Solution**: Everything listed with USER/PLUGIN badges. Click to expand full content. Delete button for user-created items.

### Cleanup burden
**Problem**: Old sessions, unused projects, test skills piling up in `~/.claude/`.
**Solution**: Delete buttons for projects, sessions, forks, skills, agents, and hooks — right from the dashboard.

### Gap analysis
**Problem**: "Is my workspace properly set up? What am I missing?"
**Solution**: Harness Score (0–100) checks 7 items. Alerts panel flags missing CLAUDE.md, high context usage, and configuration gaps.

## Features

| Tab | What it shows |
|-----|---------------|
| **Overview** | Draggable card grid — sessions, alerts, instructions, projects, forks, plugins, skills, agents, connectors, hooks. Reorder to your preference. |
| **Sessions** | Active sessions with Context X-ray. Session history with search. Click to copy resume command. Delete old sessions. |
| **Instructions** | Global + per-project CLAUDE.md tree. Line counts, content viewer, warnings for bloated files. |
| **Projects** | Per-project status — CLAUDE.md, memory files, settings, session count, first/latest prompt. Delete unused projects. |
| **Forks** | Conversation branch points with auto-labels. Click to copy resume. Delete old forks. |
| **Plugins** | Installed plugins with version, date, path. Click to see provided skills/agents/connectors. |
| **Skills** | USER vs PLUGIN sections. Full SKILL.md content. Delete user skills. |
| **Agents** | USER vs PLUGIN sections. Model, tools, full definition. Delete user agents. |
| **Connectors** | Local + Cloud MCP servers. Tool list per server. Auto-detects cloud connectors (e.g., Atlassian) from session data. |
| **Hooks** | USER vs PLUGIN sections. Event, type, command, matcher, timeout. Delete user hooks. |

## Real-time

- Sessions update every 5 seconds via SSE
- Alerts refresh every 30 seconds
- Active tab preserved across page refreshes

## Compatibility

Claude Gleaner reads from the `~/.claude/` directory created by **Claude Code** (CLI and IDE extensions). It does **not** work with Claude Desktop app (different data structure).

### Supported Claude Code environments

| Environment | `.claude/` location | Gleaner works? |
|-------------|---------------------|----------------|
| Linux / macOS CLI | `/home/<user>/.claude/` | ✅ |
| Windows CLI (npm install) | `C:\Users\<user>\.claude\` | ✅ |
| Windows WSL | `/home/<user>/.claude/` (inside WSL) | ✅ Run Gleaner inside WSL |
| VS Code extension | Same as CLI (per OS) | ✅ |
| JetBrains extension | Same as CLI (per OS) | ✅ |
| Claude Desktop app | Different structure | ❌ Not supported |

### How to verify

Check if `~/.claude/` exists:

```bash
# Linux / macOS / WSL
ls ~/.claude/

# Windows PowerShell
dir $HOME\.claude\
```

If the directory exists with `settings.json`, `projects/`, `sessions/` inside, Gleaner will pick it up.

## Network Access

```
http://<your-ip>:8080
```

Accessible from any machine on the same network.

## License

MIT
