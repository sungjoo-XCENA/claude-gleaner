#!/usr/bin/env python3
"""
Claude Gleaner — Claude Code workspace monitoring dashboard backend
Python 3.9+ / no external dependencies (stdlib only)
"""

import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlparse, parse_qs

# ─── Configuration ───────────────────────────────────────────────────────────
import argparse

_parser = argparse.ArgumentParser(description="Claude Gleaner — workspace dashboard")
_parser.add_argument("-p", "--port", type=int, default=8080, help="Port to bind (default: 8080)")
_parser.add_argument("--host", default="0.0.0.0", help="Host to bind (default: 0.0.0.0)")
_args = _parser.parse_args()

BIND_HOST = _args.host
BIND_PORT = _args.port
INTERNAL_IP = socket.gethostbyname(socket.gethostname())

CLAUDE_DIR = Path.home() / ".claude"
CLAUDE_JSON = Path.home() / ".claude.json"
DIST_DIR = Path(__file__).parent / "dist"

SSE_INTERVAL = 5  # seconds


# ─── Docker Session Scanning ─────────────────────────────────────────────────

_DOCKER_CACHE_DIR = CLAUDE_DIR / "docker-sessions"
_DOCKER_CACHE_TTL = 30  # seconds
_docker_cache_ts: float = 0.0
_docker_container_info: dict = {}  # container_id -> {"name": ..., "projects_path": ...}


def _sync_docker_sessions() -> Path:
    """Sync ~/.claude/projects from running Docker containers to a persistent local dir.
    Returns the cache directory path. Uses TTL-based caching.
    Cached data persists even when containers stop."""
    global _docker_cache_ts, _docker_container_info

    now = time.time()
    if now - _docker_cache_ts < _DOCKER_CACHE_TTL and _DOCKER_CACHE_DIR.is_dir():
        return _DOCKER_CACHE_DIR

    _DOCKER_CACHE_DIR.mkdir(parents=True, exist_ok=True)

    # Load existing cached containers (persisted from previous syncs)
    _docker_container_info = {}
    for cached_dir in _DOCKER_CACHE_DIR.iterdir():
        if not cached_dir.is_dir() or not cached_dir.name.startswith("docker-"):
            continue
        meta_file = cached_dir / "meta.json"
        if meta_file.is_file():
            try:
                meta = json.loads(read_text(meta_file) or "{}")
                short_id = cached_dir.name.replace("docker-", "")
                _docker_container_info[short_id] = {
                    "name": meta.get("name", short_id),
                    "projects_path": str(cached_dir / "projects"),
                    "sessions_path": str(cached_dir / "sessions"),
                    "history_path": str(cached_dir / "history.jsonl"),
                    "claude_md_path": str(cached_dir / "CLAUDE.md"),
                    "active_session_ids": set(),  # offline by default
                    "is_running": False,
                }
            except Exception:
                continue

    # Check if docker is available
    running_ids = set()
    try:
        result = subprocess.run(
            ["docker", "ps", "-q"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            running_ids = {cid.strip()[:12] for cid in result.stdout.splitlines() if cid.strip()}
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Sync running containers
    for cid_full in running_ids:
        short_id = cid_full[:12]

        # Get container name
        try:
            name_result = subprocess.run(
                ["docker", "inspect", "--format", "{{.Name}}", cid_full],
                capture_output=True, text=True, timeout=5
            )
            container_name = name_result.stdout.strip().lstrip("/") if name_result.returncode == 0 else short_id
        except Exception:
            container_name = short_id

        # Try common paths for .claude/projects inside the container
        claude_paths = ["/root/.claude/projects", "/home/*/.claude/projects"]
        found_path = None

        for path_pattern in claude_paths:
            try:
                if "*" in path_pattern:
                    check = subprocess.run(
                        ["docker", "exec", cid_full, "sh", "-c", f"ls -d {path_pattern} 2>/dev/null | head -1"],
                        capture_output=True, text=True, timeout=5
                    )
                    if check.returncode == 0 and check.stdout.strip():
                        found_path = check.stdout.strip()
                        break
                else:
                    check = subprocess.run(
                        ["docker", "exec", cid_full, "test", "-d", path_pattern],
                        capture_output=True, text=True, timeout=5
                    )
                    if check.returncode == 0:
                        found_path = path_pattern
                        break
            except Exception:
                continue

        if not found_path:
            continue

        claude_root = Path(found_path).parent
        sessions_path = str(claude_root / "sessions")

        dest = _DOCKER_CACHE_DIR / f"docker-{short_id}"
        projects_dest = dest / "projects"
        sessions_dest = dest / "sessions"

        try:
            if dest.is_dir():
                shutil.rmtree(dest)
            dest.mkdir(parents=True, exist_ok=True)

            subprocess.run(["docker", "cp", f"{cid_full}:{found_path}", str(projects_dest)], capture_output=True, timeout=30)
            subprocess.run(["docker", "cp", f"{cid_full}:{sessions_path}", str(sessions_dest)], capture_output=True, timeout=10)
            subprocess.run(["docker", "cp", f"{cid_full}:{claude_root}/history.jsonl", str(dest / "history.jsonl")], capture_output=True, timeout=10)
            subprocess.run(["docker", "cp", f"{cid_full}:{claude_root}/CLAUDE.md", str(dest / "CLAUDE.md")], capture_output=True, timeout=10)

            # Save metadata for persistence
            meta = {"name": container_name, "container_id": cid_full}
            with open(dest / "meta.json", "w") as f:
                json.dump(meta, f)

            # Check if claude is running inside the container
            active_sids = set()
            try:
                pgrep = subprocess.run(
                    ["docker", "exec", cid_full, "sh", "-c", "pgrep -f claude 2>/dev/null || true"],
                    capture_output=True, text=True, timeout=5
                )
                docker_pids = {int(p.strip()) for p in pgrep.stdout.splitlines() if p.strip().isdigit()}
                if docker_pids and sessions_dest.is_dir():
                    for sf in sessions_dest.glob("*.json"):
                        try:
                            sd = json.loads(read_text(sf) or "{}")
                            if sd.get("pid") in docker_pids and sd.get("sessionId"):
                                active_sids.add(sd["sessionId"])
                        except Exception:
                            continue
            except Exception:
                pass

            _docker_container_info[short_id] = {
                "name": container_name,
                "projects_path": str(projects_dest),
                "sessions_path": str(sessions_dest),
                "history_path": str(dest / "history.jsonl"),
                "claude_md_path": str(dest / "CLAUDE.md"),
                "active_session_ids": active_sids,
                "is_running": True,
            }
        except Exception:
            continue

    _docker_cache_ts = now
    return _DOCKER_CACHE_DIR


# ─── Utilities ───────────────────────────────────────────────────────────────

def read_json(path: Path) -> Optional[Any]:
    """Safely read a JSON file. Returns None on failure."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, PermissionError, json.JSONDecodeError):
        return None


def read_text(path: Path, max_chars: int = 0) -> Optional[str]:
    """Safely read a text file. Truncates if max_chars > 0."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        if max_chars > 0 and len(content) > max_chars:
            return content[:max_chars]
        return content
    except (FileNotFoundError, PermissionError, UnicodeDecodeError):
        return None


def parse_frontmatter(text: str) -> dict:
    """Minimal YAML frontmatter parser (--- block). Extracts key: value pairs without PyYAML."""
    result = {}
    if not text.startswith("---"):
        return result
    end = text.find("---", 3)
    if end == -1:
        return result
    block = text[3:end].strip()
    for line in block.splitlines():
        line = line.strip()
        if ":" not in line:
            continue
        key, _, val = line.partition(":")
        key = key.strip().lower()
        val = val.strip().strip('"').strip("'")
        # Handle list values like tools (inline [a, b, c])
        if val.startswith("[") and val.endswith("]"):
            val = [v.strip().strip('"').strip("'") for v in val[1:-1].split(",") if v.strip()]
        result[key] = val
    return result


def encode_project_path(abs_path: str) -> str:
    """Inverse of decode_project_path: '/home/sungjoo/repo' → '-home-sungjoo-repo'."""
    if not abs_path.startswith("/"):
        raise ValueError("project path must be absolute")
    return "-" + abs_path.strip("/").replace("/", "-")


_HOST_RE = re.compile(r"^(?:[A-Za-z0-9_.\-]+@)?[A-Za-z0-9_.\-]+(?::\d{1,5})?$")
_PROJECT_PATH_RE = re.compile(r"^/[A-Za-z0-9_.\-/]+$")


def _is_valid_ssh_host(host: str) -> bool:
    """Accept forms: host, user@host, host:port, user@host:port."""
    return bool(host) and len(host) <= 256 and bool(_HOST_RE.match(host))


def _is_valid_project_path(path: str) -> bool:
    if not path or len(path) > 512:
        return False
    if not path.startswith("/"):
        return False
    if ".." in path.split("/"):
        return False
    return bool(_PROJECT_PATH_RE.match(path))


def _sanitize_jsonl_for_resume(src_path: Path) -> Path:
    """Copy session JSONL to a tempfile, fixing fields that break claude's resume renderer.

    Known issue: tool_use_result entries with `originalFile: null` crash the resume UI
    (`null is not an object (evaluating 'A.split')`). Replace null with empty string.

    Returns the tempfile path; caller must delete it.
    """
    fd, tmp_str = tempfile.mkstemp(suffix=".jsonl", prefix="gleaner_xfer_")
    os.close(fd)
    tmp_path = Path(tmp_str)

    def _walk(x):
        if isinstance(x, dict):
            for k in list(x.keys()):
                if k == "originalFile" and x[k] is None:
                    x[k] = ""
                else:
                    _walk(x[k])
        elif isinstance(x, list):
            for v in x:
                _walk(v)

    try:
        with src_path.open("rb") as f_in, tmp_path.open("wb") as f_out:
            for raw in f_in:
                try:
                    obj = json.loads(raw)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    f_out.write(raw)
                    continue
                _walk(obj)
                f_out.write((json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8"))
        return tmp_path
    except Exception:
        try:
            tmp_path.unlink()
        except Exception:
            pass
        raise


def decode_project_path(folder_name: str) -> str:
    """Convert a project folder name to its actual path. '-home-sungjoo-repo' → '/home/sungjoo/repo'

    Claude Code replaces '/' with '-' to form folder names.
    Greedily searches for an existing path to distinguish from directories containing hyphens.
    """
    parts = folder_name.lstrip("-").split("-")
    if not parts:
        return "/" + folder_name.lstrip("-")

    # Greedy: try the longest match from the left, testing both '-' and '_' separators
    best_segments = []
    i = 0
    while i < len(parts):
        found = False
        for j in range(len(parts), i, -1):
            sub = parts[i:j]
            # Try both '-' and '_' joins (Claude Code encodes _ as - too)
            for sep in ("-", "_"):
                candidate = sep.join(sub)
                test_path = "/" + "/".join(best_segments + [candidate])
                if os.path.exists(test_path):
                    best_segments.append(candidate)
                    i = j
                    found = True
                    break
            if found:
                break
        if not found:
            best_segments.append(parts[i])
            i += 1

    return "/" + "/".join(best_segments)


# ─── Data Collection Functions ──────────────────────────────────────────────

def get_health() -> dict:
    """Harness health score (0-100), checks 7 items."""
    items = {}

    # 1) claude_md
    items["claude_md"] = (CLAUDE_DIR / "CLAUDE.md").is_file()

    # 2) permissions — check if settings.json has a permissions key
    settings = read_json(CLAUDE_DIR / "settings.json")
    items["permissions"] = bool(settings and "permissions" in settings)

    # 3) hooks — check if settings.json has a hooks key
    items["hooks"] = bool(settings and "hooks" in settings)

    # 4) agents — check if any agents exist (default path + plugin cache)
    items["agents"] = len(get_agents()["agents"]) > 0

    # 5) skills — check if any skills exist (default path + plugin cache)
    items["skills"] = len(get_skills()["skills"]) > 0

    # 6) connectors — check if any MCP servers exist (full scan)
    items["connectors"] = len(get_connectors()["connectors"]) > 0

    # 7) plugins — check if installed_plugins.json exists and has plugin entries
    plugins_data = read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json")
    if plugins_data and isinstance(plugins_data, dict):
        plugins_map = plugins_data.get("plugins", {})
        items["plugins"] = len(plugins_map) > 0
    else:
        items["plugins"] = False

    total = 100
    per_item = total // len(items)  # 14 points each, remainder handled at end
    score = sum(per_item for v in items.values() if v)
    # Correction: if all 7 are True, score is 100
    if all(items.values()):
        score = 100

    return {"score": score, "total": total, "items": items}


def get_sessions() -> dict:
    """List of active claude processes for the current user."""
    import platform
    sessions = []

    if platform.system() == "Windows":
        # Windows: use tasklist /FO CSV
        try:
            result = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=5
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"sessions": sessions}

        # CSV format: "image_name","pid","session_name","session#","mem_usage"
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            # Strip surrounding quotes then split on ","
            parts = line.strip('"').split('","')
            if len(parts) < 2:
                continue
            image_name = parts[0].lower()
            if "claude" not in image_name:
                continue
            try:
                pid = int(parts[1])
            except ValueError:
                continue
            sessions.append({
                "pid": pid,
                "state": "active",
                "tty": "N/A",
                "started": "",
                "command": parts[0],
                "cwd": "",
            })
    else:
        # Linux / macOS: use `ps -eo uid=,...` with explicit no-header format.
        # NOTE: `ps aux`'s USER column truncates to 8 chars for long usernames,
        # which silently breaks string-based user matching. Use numeric UID instead.
        my_uid = os.getuid()

        try:
            result = subprocess.run(
                ["ps", "-eo", "uid=,pid=,pcpu=,pmem=,vsz=,rss=,tty=,stat=,start_time=,time=,command="],
                capture_output=True, text=True, timeout=5,
                env={**os.environ, "LC_ALL": "C"}
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return {"sessions": sessions}

        # Format: uid pid %cpu %mem vsz rss tty stat start time command
        for line in result.stdout.splitlines():
            parts = line.split(None, 10)
            if len(parts) < 11:
                continue
            uid_str, pid_str, _cpu, _mem, _vsz, _rss, tty, stat, start, time_field, command = parts[0], parts[1], parts[2], parts[3], parts[4], parts[5], parts[6], parts[7], parts[8], parts[9], parts[10]

            try:
                if int(uid_str) != my_uid:
                    continue
            except ValueError:
                continue

            # Filter to claude processes only (claude binary or node claude)
            cmd_lower = command.lower()
            if "claude" not in cmd_lower:
                continue
            # Exclude auxiliary processes (MCP servers, node, bash wrappers, etc.)
            if any(skip in cmd_lower for skip in [
                "mcp-server", "server.py", "claude-gleaner", "node ", "bridge",
                "/bin/bash", "cwd",
            ]):
                continue

            pid = int(pid_str)

            # Map process state
            if stat.startswith("T"):
                state = "stopped"
            elif stat.startswith("R"):
                state = "running"
            else:
                state = "active"  # S, Sl+, etc.

            # Get cwd
            cwd = ""
            try:
                cwd = os.readlink(f"/proc/{pid}/cwd")
            except (FileNotFoundError, PermissionError, OSError):
                pass

            sessions.append({
                "pid": pid,
                "state": state,
                "tty": tty,
                "started": start,
                "command": command.split("/")[-1] if "/" in command else command,
                "cwd": cwd,
            })

    return {"sessions": sessions}


def get_activity() -> dict:
    """Today's command count + recent activity summary from history.jsonl."""
    history_path = CLAUDE_DIR / "history.jsonl"
    if not history_path.is_file():
        return {"today_count": 0, "recent": []}

    now_ms = int(time.time() * 1000)
    today_start_ms = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)

    today_count = 0
    recent = []

    try:
        text = read_text(history_path)
        if not text:
            return {"today_count": 0, "recent": []}
        lines = text.splitlines()

        # Today's command count (full scan)
        for line in lines:
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", 0)
                if ts >= today_start_ms:
                    today_count += 1
            except Exception:
                continue

        # Last 10 entries (reversed from the end)
        for line in reversed(lines[-50:]):
            if len(recent) >= 10:
                break
            try:
                entry = json.loads(line)
                ts = entry.get("timestamp", 0)
                display = entry.get("display", "").strip()
                if not display:
                    continue
                project = entry.get("project", "")
                project_name = Path(project).name if project else ""
                age_seconds = max(0, (now_ms - ts) // 1000) if ts else 0
                recent.append({
                    "text": display[:100] + ("..." if len(display) > 100 else ""),
                    "project": project_name,
                    "ageSeconds": int(age_seconds),
                })
            except Exception:
                continue
    except Exception:
        pass

    return {"today_count": today_count, "recent": recent}


def _truncate(s: str, max_len: int) -> str:
    return s[:max_len] + "..." if len(s) > max_len else s


def get_projects_summary() -> dict:
    """Per-project command count / first request / last result based on history.jsonl."""
    # Collect all history.jsonl files (local + docker)
    history_files = []
    history_path = CLAUDE_DIR / "history.jsonl"
    if history_path.is_file():
        history_files.append({"path": history_path, "docker_name": None})
    try:
        _sync_docker_sessions()
        for cid, info in _docker_container_info.items():
            hp = Path(info.get("history_path", ""))
            if hp.is_file():
                history_files.append({"path": hp, "docker_name": info["name"]})
    except Exception:
        pass

    if not history_files:
        return {"projects": []}

    by_project: dict = {}
    for hf in history_files:
        try:
            text = read_text(hf["path"])
            if not text:
                continue
            docker_name = hf["docker_name"]

            for line in text.splitlines()[-5000:]:
                try:
                    entry = json.loads(line)
                except Exception:
                    continue
                proj = entry.get("project")
                ts = entry.get("timestamp")
                if not proj or not isinstance(ts, (int, float)):
                    continue
                display = (entry.get("display") or "").strip()
                proj_key = f"docker:{docker_name}:{proj}" if docker_name else proj
                proj_display = docker_name or (Path(proj).name or proj)

                slot = by_project.setdefault(proj_key, {
                    "name": proj_display,
                    "cwd": proj,
                    "command_count": 0,
                    "last_activity_ms": 0,
                    "first_request": "",
                    "first_ts": 0,
                    "last_result": "",
                    "is_docker": bool(docker_name),
                })
                slot["command_count"] += 1
                if ts > slot["last_activity_ms"]:
                    slot["last_activity_ms"] = ts
                    if display:
                        slot["last_result"] = display[:160]
                if slot["first_ts"] == 0 or ts < slot["first_ts"]:
                    slot["first_ts"] = ts
                    if display:
                        slot["first_request"] = display[:160]
        except Exception:
            continue

    now_ms = int(time.time() * 1000)
    result = []
    for v in by_project.values():
        age = max(0, (now_ms - v["last_activity_ms"]) // 1000) if v["last_activity_ms"] else 0
        result.append({
            "name": v["name"],
            "cwd": v["cwd"],
            "command_count": v["command_count"],
            "last_activity_ago": int(age),
            "first_request": v["first_request"],
            "last_result": v["last_result"],
        })
        v.pop("first_ts", None)

    result.sort(key=lambda x: x["last_activity_ago"])
    return {"projects": result[:20]}


def get_instructions() -> dict:
    """CLAUDE.md files (global + per-project)."""
    result: dict[str, Any] = {"global": {"exists": False}, "projects": []}
    max_content = 50000  # show full content

    # Global CLAUDE.md
    global_path = CLAUDE_DIR / "CLAUDE.md"
    if global_path.is_file():
        content = read_text(global_path)
        if content is not None:
            size = global_path.stat().st_size
            line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
            truncated = len(content) > max_content
            result["global"] = {
                "exists": True,
                "content": content[:max_content],
                "size": size,
                "line_count": line_count,
                "truncated": truncated,
            }

    # Per-project CLAUDE.md
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        # Collect all registered project paths (for sub-project filtering)
        all_project_paths = []
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                all_project_paths.append(decode_project_path(pd.name))
        all_project_paths.sort(key=len, reverse=True)  # longer (more specific) paths first
        seen_files = set()  # prevent full_path duplicates

        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            claude_md = proj_dir / "CLAUDE.md"
            decoded_path = decode_project_path(proj_dir.name)
            if claude_md.is_file():
                content = read_text(claude_md)
                if content is not None:
                    size = claude_md.stat().st_size
                    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                    truncated = len(content) > max_content
                    result["projects"].append({
                        "path": decoded_path,
                        "content": content[:max_content],
                        "size": size,
                        "line_count": line_count,
                        "truncated": truncated,
                    })
            # Scan CLAUDE.md tree within project directory (root + subdirectories)
            project_root = Path(decoded_path)
            if project_root.is_dir():
                # Other registered project paths under this project (skip those)
                sub_projects = [p for p in all_project_paths
                                if p != decoded_path and p.startswith(decoded_path + "/")]
                try:
                    for md_file in project_root.rglob("CLAUDE.md"):
                        md_str = str(md_file)
                        # Prevent duplicates
                        if md_str in seen_files:
                            continue
                        # Exclude hidden directories like .git
                        if any(p.startswith(".") for p in md_file.relative_to(project_root).parts[:-1]):
                            continue
                        # Skip if inside another registered project's area
                        parent_dir = str(md_file.parent)
                        skip = False
                        for sp in sub_projects:
                            if parent_dir.startswith(sp):
                                skip = True
                                break
                        if skip:
                            continue
                        seen_files.add(md_str)
                        content = read_text(md_file)
                        if content is None:
                            continue
                        size = md_file.stat().st_size
                        line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                        truncated = len(content) > max_content
                        rel = str(md_file.relative_to(project_root).parent)
                        sub_label = "" if rel == "." else rel
                        result["projects"].append({
                            "path": decoded_path,
                            "sub_path": sub_label,
                            "full_path": str(md_file),
                            "local": True,
                            "content": content[:max_content],
                            "size": size,
                            "line_count": line_count,
                            "truncated": truncated,
                        })
                except (PermissionError, OSError):
                    pass

    # Docker container CLAUDE.md files
    try:
        _sync_docker_sessions()
        for cid, info in _docker_container_info.items():
            md_path = Path(info.get("claude_md_path", ""))
            if md_path.is_file():
                content = read_text(md_path)
                if content:
                    size = md_path.stat().st_size
                    line_count = content.count("\n") + (1 if content and not content.endswith("\n") else 0)
                    truncated = len(content) > max_content
                    result["projects"].append({
                        "path": info["name"],
                        "content": content[:max_content],
                        "size": size,
                        "line_count": line_count,
                        "truncated": truncated,
                        "is_docker": True,
                    })
    except Exception:
        pass

    return result


def get_skills() -> dict:
    """Skill scan: ~/.claude/skills/ + skills/ inside plugin cache."""
    skills = []
    seen = set()

    def _scan_skills_dir(skills_dir: Path, source: str = "user"):
        if not skills_dir.is_dir():
            return
        for d in sorted(skills_dir.iterdir()):
            if not d.is_dir():
                continue
            skill_md = d / "SKILL.md"
            if not skill_md.is_file():
                continue
            content = read_text(skill_md)
            if content is None:
                continue
            fm = parse_frontmatter(content)
            name = fm.get("name", d.name)
            if name in seen:
                continue
            seen.add(name)
            # Extract body after frontmatter
            body = content
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    body = content[end + 3:].strip()
            skills.append({
                "name": name,
                "description": fm.get("description", ""),
                "path": str(skill_md),
                "source": source,
                "content": body[:2000],
            })

    # Default path
    _scan_skills_dir(CLAUDE_DIR / "skills")

    # Skills inside plugin cache — extract plugin name
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for skills_subdir in plugins_cache.rglob("skills"):
            if skills_subdir.is_dir():
                # Extract plugin name from path: cache/<org>/<plugin-name>/<ver>/skills
                parts = skills_subdir.relative_to(plugins_cache).parts
                plugin_name = parts[1] if len(parts) >= 2 else "plugin"
                _scan_skills_dir(skills_subdir, source=f"plugin:{plugin_name}")

    return {"skills": skills}


def get_agents() -> dict:
    """Agent scan: ~/.claude/agents/ + agents/ inside plugin cache."""
    agents = []
    seen = set()

    def _scan_agents_dir(agents_dir: Path, source: str = "user"):
        if not agents_dir.is_dir():
            return
        for f in sorted(agents_dir.glob("*.md")):
            content = read_text(f)
            if content is None:
                continue
            fm = parse_frontmatter(content)
            name = fm.get("name", f.stem)
            if name in seen:
                continue
            seen.add(name)
            tools = fm.get("tools", [])
            if isinstance(tools, str):
                tools = [t.strip() for t in tools.split(",") if t.strip()]
            body = content
            if content.startswith("---"):
                end = content.find("---", 3)
                if end != -1:
                    body = content[end + 3:].strip()
            agents.append({
                "name": name,
                "description": fm.get("description", ""),
                "model": fm.get("model", ""),
                "tools": tools,
                "source": source,
                "content": body[:2000],
            })

    # Default path
    _scan_agents_dir(CLAUDE_DIR / "agents")

    # Agents inside plugin cache
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for d in plugins_cache.rglob("agents"):
            if d.is_dir():
                parts = d.relative_to(plugins_cache).parts
                plugin_name = parts[1] if len(parts) >= 2 else "plugin"
                _scan_agents_dir(d, source=f"plugin:{plugin_name}")

    return {"agents": agents}


def get_connectors() -> dict:
    """MCP server scan: ~/.claude.json + .mcp.json in plugins + extract cloud MCP from session JSONL."""
    connectors = []
    seen = set()  # "<source>:<name>" keys already added

    def _add_mcp_servers(data: dict, source: str = "", plugin_name: str = ""):
        if not data or "mcpServers" not in data:
            return
        mcp_servers = data["mcpServers"]
        if not isinstance(mcp_servers, dict):
            return
        for name, config in sorted(mcp_servers.items()):
            if not isinstance(config, dict):
                continue
            key = f"{source}:{name}" if source else name
            if key in seen:
                continue
            seen.add(key)
            # For plugin servers, include plugin name in display name
            display_name = name
            if plugin_name and name != plugin_name:
                display_name = f"{plugin_name} ({name})"
            connectors.append({
                "name": display_name,
                "_mcp_name": name,          # actual MCP server name (for matching)
                "_plugin_name": plugin_name, # plugin name (for matching)
                "command": config.get("command", ""),
                "type": config.get("type", "local"),
                "args": config.get("args", []),
                "source": source,
                "tools": [],
                "tool_count": 0,
            })

    # ~/.claude.json
    _add_mcp_servers(read_json(CLAUDE_JSON), "global")

    # .mcp.json files inside plugins — scan only cache to avoid duplicates (same as marketplaces)
    plugins_cache = CLAUDE_DIR / "plugins" / "cache"
    if plugins_cache.is_dir():
        for mcp_file in plugins_cache.rglob(".mcp.json"):
            data = read_json(mcp_file)
            # Extract plugin name from path: cache/<org>/<plugin-name>/<ver>/.mcp.json
            parts = mcp_file.relative_to(plugins_cache).parts
            plugin_name = parts[1] if len(parts) >= 2 else mcp_file.parent.name
            _add_mcp_servers(data, f"plugin:{plugin_name}", plugin_name)

    # ── Second pass: extract mcp__ patterns from session JSONL (discover cloud + plugin MCP servers) ──

    # Valid MCP tool name pattern: mcp__<alphanum_hyphen>__<alphanum_hyphen>
    _VALID_MCP_RE = re.compile(r"^mcp__[A-Za-z0-9][A-Za-z0-9_-]*__[A-Za-z][A-Za-z0-9_-]*$")

    def _parse_mcp_prefix(prefix: str):
        """Decompose an mcp server prefix into (provider, server_name, mcp_type, source).

        Examples:
          claude_ai_Atlassian -> ('claude_ai', 'Atlassian', 'cloud', 'claude.ai')
          plugin_oh-my-claudecode_t -> ('plugin', 'oh-my-claudecode', 'local', 'plugin:oh-my-claudecode')
        """
        parts = prefix.split("_")
        # If any segment contains a hyphen, it's a plugin format
        for i, p in enumerate(parts):
            if "-" in p:
                provider = "_".join(parts[:i]) if i > 0 else "plugin"
                server = p
                return provider, server, "local", f"plugin:{server}"
        # No hyphen: last segment is server name, rest is provider
        if len(parts) >= 2:
            server = parts[-1]
            provider = "_".join(parts[:-1])
            return provider, server, "cloud", "claude.ai"
        return prefix, prefix, "cloud", "claude.ai"

    # Collect active session JSONL files (sessions/*.json → sessionId → projects/**/<sessionId>.jsonl)
    sessions_dir = CLAUDE_DIR / "sessions"
    jsonl_paths: set = set()
    if sessions_dir.is_dir():
        projects_dir = CLAUDE_DIR / "projects"
        for sess_file in sessions_dir.glob("*.json"):
            sess_data = read_json(sess_file)
            if not sess_data or not isinstance(sess_data, dict):
                continue
            session_id = sess_data.get("sessionId", "")
            if not session_id:
                continue
            if projects_dir.is_dir():
                for jsonl_file in projects_dir.rglob(f"{session_id}.jsonl"):
                    jsonl_paths.add(jsonl_file)

    # If no JSONL files found, also include recent JSONL files from all projects (fallback)
    if not jsonl_paths:
        projects_dir = CLAUDE_DIR / "projects"
        if projects_dir.is_dir():
            for proj_dir in projects_dir.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl_file in proj_dir.glob("*.jsonl"):
                    if "subagent" not in jsonl_file.name:
                        jsonl_paths.add(jsonl_file)

    # Extract "mcp__" patterns from each JSONL via grep
    # server_name -> {tools: set, type, source, command}
    server_tools: dict = {}
    for jsonl_path in jsonl_paths:
        try:
            result = subprocess.run(
                ["grep", "-o", '"mcp__[^"]*"', str(jsonl_path)],
                capture_output=True, text=True, timeout=10,
            )
            if not result.stdout:
                continue
            for raw in result.stdout.splitlines():
                tool_name = raw.strip().strip('"')
                # Strict validation: only allow valid mcp__ patterns
                if not _VALID_MCP_RE.match(tool_name):
                    continue
                # mcp__<prefix>__<tool>
                inner = tool_name[5:]  # strip "mcp__"
                sep_idx = inner.find("__")
                if sep_idx == -1:
                    continue
                prefix = inner[:sep_idx]
                tool = inner[sep_idx + 2:]
                if not prefix or not tool:
                    continue
                _, server_name, mcp_type, source = _parse_mcp_prefix(prefix)
                if server_name not in server_tools:
                    server_tools[server_name] = {
                        "tools": set(),
                        "type": mcp_type,
                        "source": source,
                        "command": "cloud" if mcp_type == "cloud" else "node",
                    }
                server_tools[server_name]["tools"].add(tool)
        except Exception:
            continue

    # Merge with local scan results
    for server_name, info in sorted(server_tools.items()):
        tools_list = sorted(info["tools"])
        tool_count = len(tools_list)

        # Matching priority:
        # 1) Direct name match
        # 2) Server registered under the same plugin source (e.g. plugin:oh-my-claudecode)
        existing = next((c for c in connectors if c.get("_mcp_name") == server_name), None)
        if existing is None and info["source"].startswith("plugin:"):
            existing = next(
                (c for c in connectors if c.get("source") == info["source"]),
                None,
            )
        if existing:
            # Update tools list only
            existing["tools"] = tools_list
            existing["tool_count"] = tool_count
        else:
            # Add new server (cloud MCP or plugin discovered only in jsonl)
            key = f"{info['source']}:{server_name}"
            if key in seen:
                continue
            seen.add(key)
            connectors.append({
                "name": server_name,
                "_mcp_name": server_name,
                "_plugin_name": "",
                "command": info["command"],
                "type": info["type"],
                "args": [],
                "source": info["source"],
                "tools": tools_list,
                "tool_count": tool_count,
            })

    # Remove internal matching keys (not needed in API response)
    for c in connectors:
        c.pop("_mcp_name", None)
        c.pop("_plugin_name", None)

    return {"connectors": connectors}


def get_hooks() -> dict:
    """~/.claude/settings.json → hooks."""
    hooks_list = []
    settings = read_json(CLAUDE_DIR / "settings.json")
    if not settings or "hooks" not in settings:
        return {"hooks": hooks_list}

    hooks = settings["hooks"]
    if isinstance(hooks, dict):
        for event, handlers in hooks.items():
            if not isinstance(handlers, list):
                handlers = [handlers]
            for h in handlers:
                if not isinstance(h, dict):
                    continue
                matcher = h.get("matcher", "")
                # New structure: {"matcher": "...", "hooks": [{...}]}
                sub_hooks = h.get("hooks", [])
                if isinstance(sub_hooks, list) and sub_hooks:
                    for sh in sub_hooks:
                        if isinstance(sh, dict):
                            entry = {
                                "event": event,
                                "matcher": matcher,
                                "type": sh.get("type", "command"),
                                "command": sh.get("command", ""),
                                "prompt": sh.get("prompt", ""),
                                "url": sh.get("url", ""),
                                "description": sh.get("description", ""),
                                "statusMessage": sh.get("statusMessage", ""),
                                "timeout": sh.get("timeout"),
                                "model": sh.get("model", ""),
                                "once": sh.get("once", False),
                                "async": sh.get("async", False),
                                "asyncRewake": sh.get("asyncRewake", False),
                                "if": sh.get("if", ""),
                                "shell": sh.get("shell", ""),
                            }
                            # Remove empty values
                            entry = {k: v for k, v in entry.items() if v}
                            entry.setdefault("event", event)
                            entry.setdefault("type", "command")
                            hooks_list.append(entry)
                # Old structure: {"command": "...", "description": "..."}
                elif "command" in h:
                    hooks_list.append({
                        "event": event,
                        "matcher": "",
                        "type": "command",
                        "command": h["command"],
                        "description": h.get("description", ""),
                        "timeout": h.get("timeout"),
                    })

    # Add source tag to user hooks
    for h in hooks_list:
        if "source" not in h:
            h["source"] = "user"

    # Scan plugin hooks
    plugins_dir = CLAUDE_DIR / "plugins"
    if plugins_dir.is_dir():
        for hooks_json in plugins_dir.rglob("hooks/hooks.json"):
            try:
                data = json.loads(hooks_json.read_text(encoding="utf-8"))
                plugin_hooks = data.get("hooks", {})
                # Extract plugin name
                parts = str(hooks_json).split("/")
                plugin_name = "plugin"
                for i, p in enumerate(parts):
                    if p == "cache" and i + 3 < len(parts):
                        plugin_name = parts[i + 2]
                        break
                    if p == "plugins" and i + 2 < len(parts) and parts[i + 1] != "cache":
                        plugin_name = parts[i + 2] if parts[i + 1] == "marketplaces" else parts[i + 1]
                        break

                if isinstance(plugin_hooks, dict):
                    for event, handlers in plugin_hooks.items():
                        if not isinstance(handlers, list):
                            handlers = [handlers]
                        for h in handlers:
                            if not isinstance(h, dict):
                                continue
                            sub_hooks = h.get("hooks", [])
                            matcher = h.get("matcher", "")
                            for sh in (sub_hooks if isinstance(sub_hooks, list) else []):
                                if not isinstance(sh, dict):
                                    continue
                                entry = {
                                    "event": event,
                                    "matcher": matcher,
                                    "type": sh.get("type", "command"),
                                    "command": sh.get("command", ""),
                                    "description": sh.get("description", sh.get("statusMessage", "")),
                                    "timeout": sh.get("timeout"),
                                    "source": f"plugin:{plugin_name}",
                                }
                                entry = {k: v for k, v in entry.items() if v}
                                entry.setdefault("event", event)
                                entry.setdefault("type", "command")
                                entry.setdefault("source", f"plugin:{plugin_name}")
                                hooks_list.append(entry)
            except Exception:
                continue

    return {"hooks": hooks_list}


def get_forks() -> dict:
    """Search for fork points (branches) in JSONL session files."""
    forks = []
    projects_dir = CLAUDE_DIR / "projects"

    seen_texts = set()

    # Collect all project dirs (local + docker)
    all_proj_dirs = []
    _docker_proj_names = {}
    if projects_dir.is_dir():
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                all_proj_dirs.append(pd)
    try:
        _sync_docker_sessions()
        for cid, info in _docker_container_info.items():
            dp = Path(info["projects_path"])
            if dp.is_dir():
                for pd in dp.iterdir():
                    if pd.is_dir():
                        all_proj_dirs.append(pd)
                        _docker_proj_names[str(pd)] = info["name"]
    except Exception:
        pass

    for proj_dir in all_proj_dirs:
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                text = read_text(jsonl_file)
                if not text:
                    continue
                lines = text.splitlines()
                # Process only last 3000 lines
                lines = lines[-3000:]

                # Build parent→children map
                parent_to_children: dict[str, list[dict]] = {}
                entries_by_uuid: dict[str, dict] = {}

                for line in lines:
                    try:
                        entry = json.loads(line)
                    except Exception:
                        continue
                    uuid = entry.get("uuid")
                    parent_uuid = entry.get("parentUuid")
                    if uuid:
                        entries_by_uuid[uuid] = entry
                    if parent_uuid:
                        parent_to_children.setdefault(parent_uuid, []).append(entry)

                # Find fork points: parents with 2 or more children
                project_name = _docker_proj_names.get(str(proj_dir)) or decode_project_path(proj_dir.name)
                session_id = jsonl_file.stem

                for parent_uuid, children in parent_to_children.items():
                    if len(children) <= 1:
                        continue
                    # Extract user messages from each branch child
                    for child in children:
                        if child.get("type") != "user":
                            continue
                        msg_text = child.get("message", {}).get("content", "") if isinstance(child.get("message"), dict) else ""
                        if not msg_text and isinstance(child.get("content"), str):
                            msg_text = child["content"]
                        if not msg_text:
                            # content is a list
                            content = child.get("message", {}).get("content", []) if isinstance(child.get("message"), dict) else child.get("content", [])
                            if isinstance(content, list):
                                for block in content:
                                    if isinstance(block, dict) and block.get("type") == "text":
                                        msg_text = block.get("text", "")
                                        break
                                    elif isinstance(block, str):
                                        msg_text = block
                                        break

                        if not msg_text or len(msg_text) < 5:
                            continue
                        if msg_text.startswith("{"):
                            continue

                        display_text = msg_text[:100]
                        dedup_key = msg_text[:50]
                        if dedup_key in seen_texts:
                            continue
                        seen_texts.add(dedup_key)

                        ts = child.get("timestamp", "")

                        forks.append({
                            "text": display_text,
                            "project": project_name,
                            "session": session_id,
                            "timestamp": ts,
                        })
            except Exception:
                continue

    # Sort by timestamp descending
    forks.sort(key=lambda x: x.get("timestamp", ""), reverse=True)

    return {"forks": forks, "total": len(forks)}


def get_project_status() -> dict:
    """Scan per-project management status: CLAUDE.md, memory, settings, sessions, etc."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"projects": []}

    result = []
    for proj_dir in sorted(projects_dir.iterdir()):
        if not proj_dir.is_dir():
            continue

        decoded_path = decode_project_path(proj_dir.name)
        name = Path(decoded_path).name or proj_dir.name

        # 1) CLAUDE.md in project config
        claude_md_path = proj_dir / "CLAUDE.md"
        project_claude_md = claude_md_path.is_file()
        project_claude_md_size = 0
        if project_claude_md:
            try:
                project_claude_md_size = claude_md_path.stat().st_size
            except OSError:
                pass

        # 2) Memory files
        memory_dir = proj_dir / "memory"
        memory_count = 0
        if memory_dir.is_dir():
            try:
                memory_count = sum(1 for f in memory_dir.iterdir() if f.suffix == ".md")
            except OSError:
                pass

        # 3) settings.local.json
        has_settings = (proj_dir / "settings.local.json").is_file()

        # 4) Session count (.jsonl files, not in subagent dirs)
        session_count = 0
        try:
            for f in proj_dir.glob("*.jsonl"):
                if "subagent" not in f.name:
                    session_count += 1
        except OSError:
            pass

        # 5) Local CLAUDE.md (in the decoded project path)
        local_claude_md = Path(decoded_path, "CLAUDE.md").is_file()

        # 6) Local .claude/ directory
        has_local_claude_dir = Path(decoded_path, ".claude").is_dir()

        result.append({
            "name": name,
            "path": decoded_path,
            "raw_dir": proj_dir.name,
            "project_claude_md": project_claude_md,
            "project_claude_md_size": project_claude_md_size,
            "local_claude_md": local_claude_md,
            "memory_count": memory_count,
            "has_settings": has_settings,
            "session_count": session_count,
            "has_local_claude_dir": has_local_claude_dir,
        })

    # Sort by session_count descending
    result.sort(key=lambda x: x["session_count"], reverse=True)
    return {"projects": result}


def get_plugins() -> dict:
    """installed_plugins.json + settings.json enabledPlugins."""
    plugins = []

    # Read installed plugins
    plugins_data = read_json(CLAUDE_DIR / "plugins" / "installed_plugins.json")
    if not plugins_data or not isinstance(plugins_data, dict):
        return {"plugins": plugins}

    plugins_map = plugins_data.get("plugins", {})
    if not isinstance(plugins_map, dict):
        return {"plugins": plugins}

    # List of enabled plugins
    settings = read_json(CLAUDE_DIR / "settings.json")
    enabled_plugins = {}
    if settings and "enabledPlugins" in settings:
        ep = settings["enabledPlugins"]
        if isinstance(ep, dict):
            enabled_plugins = ep  # {"name@scope": true/false}
        elif isinstance(ep, list):
            enabled_plugins = {name: True for name in ep}

    for plugin_key, entries in plugins_map.items():
        # entries is a list of install info
        if isinstance(entries, list) and entries:
            entry = entries[0]  # first install info entry
            if isinstance(entry, dict):
                # Extract name: "oh-my-claudecode@omc" → "oh-my-claudecode"
                name = plugin_key.split("@")[0] if "@" in plugin_key else plugin_key
                # Count skills/agents/connectors belonging to this plugin
                plugin_source = f"plugin:{name}"
                n_skills = len([s for s in get_skills()["skills"] if s.get("source") == plugin_source])
                n_agents = len([a for a in get_agents()["agents"] if a.get("source") == plugin_source])
                n_connectors = len([c for c in get_connectors()["connectors"] if c.get("source") == plugin_source])

                plugins.append({
                    "name": name,
                    "version": entry.get("version", "unknown"),
                    "enabled": bool(enabled_plugins.get(plugin_key, False)),
                    "scope": entry.get("scope", ""),
                    "install_path": entry.get("installPath", ""),
                    "installed_at": entry.get("installedAt", ""),
                    "last_updated": entry.get("lastUpdated", ""),
                    "git_sha": entry.get("gitCommitSha", "")[:8],
                    "skills_count": n_skills,
                    "agents_count": n_agents,
                    "connectors_count": n_connectors,
                })

    return {"plugins": plugins}


# ─── Session Detail / Search ─────────────────────────────────────────────────

def _read_last_n_lines(path: Path, n: int) -> list[str]:
    """Efficiently read the last N lines of a file."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            fsize = f.tell()
            if fsize == 0:
                return []
            # JSONL lines can be several KB each, so read generously
            read_size = min(fsize, n * 5000)
            f.seek(max(0, fsize - read_size))
            data = f.read().decode("utf-8", errors="replace")
            lines = data.splitlines()
            return lines[-n:] if len(lines) > n else lines
    except (FileNotFoundError, PermissionError, OSError):
        return []


def _read_first_n_lines(path: Path, n: int) -> list[str]:
    """Read the first N lines of a file."""
    try:
        lines = []
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= n:
                    break
                lines.append(line.rstrip("\n"))
        return lines
    except (FileNotFoundError, PermissionError, OSError):
        return []


def _extract_user_message_text(entry: dict) -> str:
    """Extract user message text from a JSONL entry."""
    if entry.get("type") != "user":
        return ""
    # Skip meta messages (local-command-caveat, etc.)
    if entry.get("isMeta"):
        return ""
    msg = entry.get("message", {})
    if isinstance(msg, dict):
        content = msg.get("content", "")
    else:
        content = entry.get("content", "")

    if isinstance(content, str):
        # Skip local-command-caveat content
        if "<local-command-caveat>" in content:
            return ""
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text", "")
                if "<local-command-caveat>" in text:
                    return ""
                return text
            elif isinstance(block, str):
                if "<local-command-caveat>" in block:
                    return ""
                return block
    return ""


def get_session_detail() -> dict:
    """Detailed info for all sessions (active + history)."""
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"sessions": []}

    # Collect active session IDs: sessionId of sessions with running PIDs from sessions/*.json
    active_sessions = get_sessions().get("sessions", [])
    active_pids = {s["pid"] for s in active_sessions if s["state"] != "stopped"}
    active_session_ids = set()
    sid_to_pid = {}
    sid_to_name = {}
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sd = json.loads(read_text(sf) or "{}")
                pid = sd.get("pid")
                sid = sd.get("sessionId")
                name = sd.get("name")
                if sid and name:
                    sid_to_name[sid] = name
                if pid in active_pids and sid:
                    active_session_ids.add(sid)
                    sid_to_pid[sid] = pid
            except Exception:
                continue

    sessions = []

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                session_id = jsonl_file.stem
                file_size = jsonl_file.stat().st_size
                file_size_kb = round(file_size / 1024, 1)

                decoded_path = decode_project_path(proj_dir.name)
                project_name = Path(decoded_path).name or proj_dir.name

                # Extract slug from last 5 lines
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                last_timestamp = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                        if not last_timestamp and entry.get("timestamp"):
                            last_timestamp = entry["timestamp"]
                    except Exception:
                        continue
                # customTitle only exists in type:"custom-title" entries — search Python-native
                if not custom_title:
                    try:
                        with open(jsonl_file, "rb") as _f:
                            _f.seek(0, 2)
                            _fsize = _f.tell()
                            _read_size = min(_fsize, 500000)
                            _f.seek(max(0, _fsize - _read_size))
                            _chunk = _f.read().decode("utf-8", errors="replace")
                        _matches = re.findall(r'"customTitle":"([^"]*)"', _chunk)
                        if _matches:
                            custom_title = _matches[-1]
                    except Exception:
                        pass
                slug = sid_to_name.get(session_id) or custom_title or slug

                # Estimate message count (sample last 500 lines)
                last_500 = _read_last_n_lines(jsonl_file, 500)
                message_count = 0
                for line in last_500:
                    try:
                        entry = json.loads(line)
                        t = entry.get("type", "")
                        if t in ("user", "assistant"):
                            message_count += 1
                    except Exception:
                        continue

                # First user message (first 20 lines)
                first_20 = _read_first_n_lines(jsonl_file, 20)
                first_message = ""
                for line in first_20:
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and len(text) >= 3 and not text.startswith("{"):
                            first_message = text[:120]
                            break
                    except Exception:
                        continue

                # Last user message (last 20 lines)
                last_20 = _read_last_n_lines(jsonl_file, 20)
                last_message = ""
                for line in reversed(last_20):
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and len(text) >= 3 and not text.startswith("{"):
                            last_message = text[:120]
                            break
                    except Exception:
                        continue

                # Fork count (sample last 1000 lines)
                last_1000 = _read_last_n_lines(jsonl_file, 1000)
                parent_children: dict[str, int] = {}
                for line in last_1000:
                    try:
                        entry = json.loads(line)
                        parent_uuid = entry.get("parentUuid")
                        if parent_uuid:
                            parent_children[parent_uuid] = parent_children.get(parent_uuid, 0) + 1
                    except Exception:
                        continue
                fork_count = sum(1 for c in parent_children.values() if c > 1)

                # Check if active (session ID is in active session list)
                is_active = session_id in active_session_ids

                sessions.append({
                    "session_id": session_id,
                    "slug": slug,
                    "project": decoded_path,
                    "project_name": project_name,
                    "file_size_kb": file_size_kb,
                    "message_count": message_count,
                    "first_message": first_message,
                    "last_message": last_message,
                    "last_timestamp": last_timestamp,
                    "is_active": is_active,
                    "fork_count": fork_count,
                    "pid": sid_to_pid.get(session_id),
                })
            except Exception:
                continue

    # Also scan Docker container sessions
    try:
        _sync_docker_sessions()
        for cid, info in _docker_container_info.items():
            docker_projects = Path(info["projects_path"])
            if not docker_projects.is_dir():
                continue

            # Build sid_to_name from docker sessions dir
            docker_sessions_dir = Path(info.get("sessions_path", ""))
            if docker_sessions_dir.is_dir():
                for sf in docker_sessions_dir.glob("*.json"):
                    try:
                        sd = json.loads(read_text(sf) or "{}")
                        sid = sd.get("sessionId")
                        name = sd.get("name")
                        if sid and name:
                            sid_to_name[sid] = name
                    except Exception:
                        continue

            for proj_dir in docker_projects.iterdir():
                if not proj_dir.is_dir():
                    continue
                for jsonl_file in proj_dir.glob("*.jsonl"):
                    if "subagent" in str(jsonl_file):
                        continue
                    try:
                        session_id = jsonl_file.stem
                        file_size = jsonl_file.stat().st_size
                        file_size_kb = round(file_size / 1024, 1)

                        decoded_path = decode_project_path(proj_dir.name)
                        project_name = info['name']

                        # Extract slug
                        last_5 = _read_last_n_lines(jsonl_file, 5)
                        slug = ""
                        last_timestamp = ""
                        custom_title = ""
                        for line in reversed(last_5):
                            try:
                                entry = json.loads(line)
                                if not slug and entry.get("slug"):
                                    slug = entry["slug"]
                                if not last_timestamp and entry.get("timestamp"):
                                    last_timestamp = entry["timestamp"]
                            except Exception:
                                continue

                        if not custom_title:
                            try:
                                with open(jsonl_file, "rb") as _f:
                                    _head = _f.read(min(jsonl_file.stat().st_size, 5000)).decode("utf-8", errors="replace")
                                _matches = re.findall(r'"customTitle":"([^"]*)"', _head)
                                if _matches:
                                    custom_title = _matches[-1]
                            except Exception:
                                pass
                        slug = sid_to_name.get(session_id) or custom_title or slug

                        # Message count
                        last_500 = _read_last_n_lines(jsonl_file, 500)
                        message_count = sum(1 for line in last_500
                            if '"type"' in line and ('"user"' in line or '"assistant"' in line))

                        # First/last messages
                        first_20 = _read_first_n_lines(jsonl_file, 20)
                        first_message = ""
                        for line in first_20:
                            try:
                                entry = json.loads(line)
                                text = _extract_user_message_text(entry)
                                if text and len(text) >= 3 and not text.startswith("{"):
                                    first_message = text[:120]
                                    break
                            except Exception:
                                continue

                        last_20 = _read_last_n_lines(jsonl_file, 20)
                        last_message = ""
                        for line in reversed(last_20):
                            try:
                                entry = json.loads(line)
                                text = _extract_user_message_text(entry)
                                if text and len(text) >= 3 and not text.startswith("{"):
                                    last_message = text[:120]
                                    break
                            except Exception:
                                continue

                        # Fork count
                        last_1000 = _read_last_n_lines(jsonl_file, 1000)
                        parent_children: dict[str, int] = {}
                        for line in last_1000:
                            try:
                                entry = json.loads(line)
                                parent_uuid = entry.get("parentUuid")
                                if parent_uuid:
                                    parent_children[parent_uuid] = parent_children.get(parent_uuid, 0) + 1
                            except Exception:
                                continue
                        fork_count = sum(1 for c in parent_children.values() if c > 1)

                        sessions.append({
                            "session_id": session_id,
                            "slug": slug,
                            "project": decoded_path,
                            "project_name": project_name,
                            "file_size_kb": file_size_kb,
                            "message_count": message_count,
                            "first_message": first_message,
                            "last_message": last_message,
                            "last_timestamp": last_timestamp,
                            "is_active": session_id in info.get("active_session_ids", set()),
                            "fork_count": fork_count,
                            "pid": None,
                            "is_docker": True,
                        })
                    except Exception:
                        continue
    except Exception:
        pass

    # Sort: active first, then last_timestamp descending
    def _sort_ts(s):
        ts = s.get("last_timestamp", "")
        if isinstance(ts, (int, float)):
            return ts
        if isinstance(ts, str) and ts:
            # ISO string: parse to epoch for consistent sorting
            try:
                from datetime import timezone
                dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                return dt.timestamp()
            except Exception:
                return 0
        return 0
    sessions.sort(key=lambda x: (not x["is_active"], -_sort_ts(x)))

    return {"sessions": sessions}


def _extract_token_usage(lines: list[str]) -> dict:
    """Extract usage.cache_read_input_tokens from the last assistant message in a list of JSONL lines.

    Returns dict with 'context_tokens' (int) or empty dict if not found.
    """
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        usage = entry.get("usage")
        if not isinstance(usage, dict):
            # usage may be inside the message object
            msg = entry.get("message")
            if isinstance(msg, dict):
                usage = msg.get("usage")
        if isinstance(usage, dict) and "cache_read_input_tokens" in usage:
            return {
                "context_tokens": usage["cache_read_input_tokens"],
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0),
            }
    return {}


def _format_token_display(tokens: int) -> str:
    """Format a token count as a human-readable string. e.g. 318029 → '318k'"""
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f}M"
    elif tokens >= 1000:
        return f"{tokens / 1000:.1f}k"
    return str(tokens)


def get_session_xray(session_id: str) -> dict:
    """Session context X-ray: based on actual token usage (cache_read_input_tokens) from JSONL."""
    if not session_id:
        return {"error": "session_id required"}

    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"error": "projects dir not found"}

    # Find the jsonl file for this session
    jsonl_path = None
    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        candidate = proj_dir / f"{session_id}.jsonl"
        if candidate.is_file():
            jsonl_path = candidate
            break

    if not jsonl_path:
        return {"error": f"session {session_id} not found"}

    # Read last 20 lines to find real token usage
    last_20 = _read_last_n_lines(jsonl_path, 20)
    token_data = _extract_token_usage(last_20)

    context_max = 1_000_000  # 1M context model
    context_tokens = token_data.get("context_tokens", 0)
    context_pct = round(context_tokens / context_max * 100) if context_max > 0 else 0

    # Count compacts and messages since last compact from last 500 lines
    last_500 = _read_last_n_lines(jsonl_path, 500)
    compacts_total = 0
    last_compact_timestamp = None
    messages_since_compact = 0
    found_compact = False

    # First pass: count all compacts (need full file for total count)
    # Read entire file just for compact counting (type=="summary" lines are rare and small)
    try:
        with open(jsonl_path, "rb") as f:
            raw = f.read().decode("utf-8", errors="replace")
        for raw_line in raw.splitlines():
            try:
                entry = json.loads(raw_line)
            except Exception:
                continue
            if entry.get("type") == "summary":
                compacts_total += 1
                last_compact_timestamp = entry.get("timestamp")
    except (FileNotFoundError, PermissionError, OSError):
        pass

    # Second pass on last 500: messages since last compact
    for line in reversed(last_500):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        entry_type = entry.get("type", "")
        if entry_type == "summary":
            found_compact = True
            break
        if entry_type in ("user", "assistant"):
            messages_since_compact += 1

    # If no compact found in last 500 lines but compacts exist, messages_since_compact
    # is approximate (lower bound from the 500-line window)

    # Format display string
    context_display = f"{_format_token_display(context_tokens)} / 1M tokens"

    # Breakdown estimate: based on file size
    breakdown = []
    autocompact_buffer = 33000  # ~33k tokens (roughly fixed)

    # System prompt + tools (~15k fixed)
    system_tokens = 15000
    breakdown.append({"name": "System (prompt + tools)", "tokens": system_tokens})

    # CLAUDE.md + memory files
    memory_tokens = 0
    claude_md = CLAUDE_DIR / "CLAUDE.md"
    if claude_md.is_file():
        memory_tokens += claude_md.stat().st_size // 4
    memory_dir = CLAUDE_DIR / "projects"
    if memory_dir.is_dir():
        for md in memory_dir.rglob("memory/*.md"):
            try:
                memory_tokens += md.stat().st_size // 4
            except Exception:
                pass
    breakdown.append({"name": "Memory files", "tokens": memory_tokens})

    # Custom agents
    agent_tokens = 0
    agents_data = get_agents().get("agents", [])
    agent_tokens = len(agents_data) * 35  # ~35 tokens per agent definition
    breakdown.append({"name": "Custom agents", "tokens": agent_tokens})

    # Skills
    skills_data = get_skills().get("skills", [])
    skill_tokens = len(skills_data) * 22  # ~22 tokens per skill
    breakdown.append({"name": "Skills", "tokens": skill_tokens})

    # Messages = total - overhead
    overhead = system_tokens + memory_tokens + agent_tokens + skill_tokens
    message_tokens = max(0, context_tokens - overhead)
    breakdown.append({"name": "Messages", "tokens": message_tokens})

    # Free space
    free_tokens = max(0, context_max - context_tokens - autocompact_buffer)
    breakdown.append({"name": "Free space", "tokens": free_tokens})
    breakdown.append({"name": "Autocompact buffer", "tokens": autocompact_buffer})

    # Add pct to each
    for b in breakdown:
        b["pct"] = round(b["tokens"] / context_max * 100, 1)
        b["display"] = _format_token_display(b["tokens"])

    # Generate recommendation based on real token percentage
    if context_pct > 80:
        recommendation = f"Context nearly full ({context_pct}%). Use /compact or /handoff immediately"
    elif context_pct > 60:
        recommendation = f"Context getting large ({context_pct}%). Consider /compact soon"
    elif context_pct > 40:
        recommendation = f"Context moderate ({context_pct}%). Healthy for now"
    else:
        recommendation = f"Context healthy ({context_pct}%)"

    return {
        "session_id": session_id,
        "context_tokens": context_tokens,
        "context_max": context_max,
        "context_pct": context_pct,
        "context_display": context_display,
        "breakdown": breakdown,
        "messages_since_compact": messages_since_compact,
        "last_compact_timestamp": last_compact_timestamp,
        "compacts_total": compacts_total,
        "recommendation": recommendation,
    }


def get_session_search(query: str = "") -> dict:
    """Session search: search user messages in JSONL files."""
    if not query or len(query.strip()) < 1:
        return {"results": [], "query": query}

    query_lower = query.lower().strip()
    projects_dir = CLAUDE_DIR / "projects"
    if not projects_dir.is_dir():
        return {"results": [], "query": query}

    sid_to_name = {}
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sd = json.loads(read_text(sf) or "{}")
                sid = sd.get("sessionId")
                name = sd.get("name")
                if sid and name:
                    sid_to_name[sid] = name
            except Exception:
                continue

    results = []

    for proj_dir in projects_dir.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in str(jsonl_file):
                continue

            try:
                session_id = jsonl_file.stem
                decoded_path = decode_project_path(proj_dir.name)
                project_name = Path(decoded_path).name or proj_dir.name

                # Extract slug/customTitle
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                    except Exception:
                        continue
                # customTitle only exists in type:"custom-title" entries — search Python-native
                if not custom_title:
                    try:
                        with open(jsonl_file, "rb") as _f:
                            _f.seek(0, 2)
                            _fsize = _f.tell()
                            _read_size = min(_fsize, 500000)
                            _f.seek(max(0, _fsize - _read_size))
                            _chunk = _f.read().decode("utf-8", errors="replace")
                        _matches = re.findall(r'"customTitle":"([^"]*)"', _chunk)
                        if _matches:
                            custom_title = _matches[-1]
                    except Exception:
                        pass
                slug = sid_to_name.get(session_id) or custom_title or slug

                # Search last 2000 lines
                lines = _read_last_n_lines(jsonl_file, 2000)
                for line in reversed(lines):
                    try:
                        entry = json.loads(line)
                        text = _extract_user_message_text(entry)
                        if text and query_lower in text.lower():
                            ts = entry.get("timestamp", "")
                            results.append({
                                "session_id": session_id,
                                "slug": slug,
                                "project": project_name,
                                "matched_text": text[:150],
                                "timestamp": ts,
                            })
                            break  # only first match per session
                    except Exception:
                        continue

                if len(results) >= 20:
                    break
            except Exception:
                continue
        if len(results) >= 20:
            break

    return {"results": results[:20], "query": query}


# ─── Alerts / Monitoring ─────────────────────────────────────────────────────

def _count_lines(path: Path) -> int:
    """Return the number of lines in a file."""
    try:
        content = read_text(path)
        if content is None:
            return 0
        return content.count("\n") + (1 if content and not content.endswith("\n") else 0)
    except Exception:
        return 0


def get_alerts() -> dict:
    """Aggregate alert/monitoring info: CLAUDE.md status, session context, etc."""
    alerts: list[dict] = []

    # ── 1. CLAUDE.md check ─────────────────────────────────────────────
    # 1a. Global CLAUDE.md
    global_md = CLAUDE_DIR / "CLAUDE.md"
    if not global_md.is_file():
        alerts.append({
            "level": "warn",
            "category": "claude_md",
            "title": "Global CLAUDE.md not found",
            "detail": "No global instructions file at ~/.claude/CLAUDE.md",
            "action": "Create ~/.claude/CLAUDE.md",
        })
    else:
        line_count = _count_lines(global_md)
        if line_count > 200:
            alerts.append({
                "level": "warn",
                "category": "claude_md",
                "title": f"Global CLAUDE.md is {line_count} lines",
                "detail": "Recommended to keep under 200 lines for optimal performance",
                "action": "Review and trim CLAUDE.md",
            })
        else:
            alerts.append({
                "level": "ok",
                "category": "claude_md",
                "title": f"Global CLAUDE.md healthy ({line_count} lines)",
                "detail": "",
                "action": "",
            })

    # 1b. Per-project CLAUDE.md
    projects_dir = CLAUDE_DIR / "projects"
    if projects_dir.is_dir():
        for proj_dir in sorted(projects_dir.iterdir()):
            if not proj_dir.is_dir():
                continue
            decoded_path = decode_project_path(proj_dir.name)
            project_name = Path(decoded_path).name or proj_dir.name

            config_md = proj_dir / "CLAUDE.md"
            local_md = Path(decoded_path) / "CLAUDE.md"

            config_exists = config_md.is_file()
            local_exists = local_md.is_file()

            if config_exists:
                lc = _count_lines(config_md)
                if lc > 200:
                    alerts.append({
                        "level": "warn",
                        "category": "claude_md",
                        "title": f"Project \"{project_name}\" config CLAUDE.md is {lc} lines",
                        "detail": f"~/.claude/projects/.../{project_name}/CLAUDE.md — recommended under 200",
                        "action": "Review and trim project CLAUDE.md",
                    })

            if local_exists:
                lc = _count_lines(local_md)
                if lc > 200:
                    alerts.append({
                        "level": "warn",
                        "category": "claude_md",
                        "title": f"Project \"{project_name}\" local CLAUDE.md is {lc} lines",
                        "detail": f"{decoded_path}/CLAUDE.md — recommended under 200",
                        "action": "Review and trim project CLAUDE.md",
                    })

            if not config_exists and not local_exists:
                alerts.append({
                    "level": "warn",
                    "category": "claude_md",
                    "title": f"Project \"{project_name}\" has no CLAUDE.md",
                    "detail": f"Neither config nor local CLAUDE.md found for {decoded_path}",
                    "action": f"Create CLAUDE.md in {decoded_path}",
                })

    # ── 2. Active session context check (based on real token data) ────
    # Collect active session IDs
    active_sessions_data = get_sessions().get("sessions", [])
    active_pids = {s["pid"] for s in active_sessions_data if s.get("state") != "stopped"}
    active_session_ids = set()
    sid_to_name = {}
    sessions_dir = CLAUDE_DIR / "sessions"
    if sessions_dir.is_dir():
        for sf in sessions_dir.glob("*.json"):
            try:
                sd = json.loads(read_text(sf) or "{}")
                pid = sd.get("pid")
                sid = sd.get("sessionId")
                name = sd.get("name")
                if sid and name:
                    sid_to_name[sid] = name
                if pid in active_pids and sid:
                    active_session_ids.add(sid)
            except Exception:
                continue

    # Extract actual token usage from active session JSONL files
    session_ok_count = 0
    if projects_dir.is_dir():
        for proj_dir in projects_dir.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl_file in proj_dir.glob("*.jsonl"):
                if "subagent" in str(jsonl_file):
                    continue
                session_id = jsonl_file.stem
                if session_id not in active_session_ids:
                    continue

                # Extract slug/customTitle
                last_5 = _read_last_n_lines(jsonl_file, 5)
                slug = ""
                custom_title = ""
                for line in reversed(last_5):
                    try:
                        entry = json.loads(line)
                        if not slug and entry.get("slug"):
                            slug = entry["slug"]
                    except Exception:
                        continue
                if not custom_title:
                    try:
                        with open(jsonl_file, "rb") as _f:
                            _f.seek(0, 2)
                            _fsize = _f.tell()
                            _read_size = min(_fsize, 500000)
                            _f.seek(max(0, _fsize - _read_size))
                            _chunk = _f.read().decode("utf-8", errors="replace")
                        _matches = re.findall(r'"customTitle":"([^"]*)"', _chunk)
                        if _matches:
                            custom_title = _matches[-1]
                    except Exception:
                        pass
                slug = sid_to_name.get(session_id) or custom_title or slug
                project_path = decode_project_path(proj_dir.name)
                project_name = Path(project_path).name or proj_dir.name
                display_name = (slug or session_id[:16]) + f" ({project_name})"

                # Extract actual token usage from last 20 lines
                last_20 = _read_last_n_lines(jsonl_file, 20)
                token_data = _extract_token_usage(last_20)
                context_tokens = token_data.get("context_tokens", 0)
                context_pct = round(context_tokens / 1_000_000 * 100) if context_tokens > 0 else 0

                if context_tokens > 800_000:  # >80%: CRITICAL
                    alerts.append({
                        "level": "critical",
                        "category": "context",
                        "title": f"Session \"{display_name}\" context nearly full ({context_pct}%)",
                        "detail": f"{_format_token_display(context_tokens)} / 1M tokens used. Use /compact or /handoff immediately",
                        "action": "/compact or /handoff",
                    })
                elif context_tokens > 600_000:  # >60%: WARN
                    alerts.append({
                        "level": "warn",
                        "category": "context",
                        "title": f"Session \"{display_name}\" context getting large ({context_pct}%)",
                        "detail": f"{_format_token_display(context_tokens)} / 1M tokens used. Consider /compact soon",
                        "action": "/compact",
                    })
                else:
                    session_ok_count += 1

    if session_ok_count > 0:
        alerts.append({
            "level": "ok",
            "category": "context",
            "title": f"Session context healthy ({session_ok_count} session{'s' if session_ok_count != 1 else ''})",
            "detail": "",
            "action": "",
        })

    if len(active_session_ids) == 0:
        alerts.append({
            "level": "info",
            "category": "context",
            "title": "No active sessions detected",
            "detail": "Start a Claude session to see context monitoring",
            "action": "",
        })

    # ── Summary calculation ─────────────────────────────────────────────
    summary = {"critical": 0, "warn": 0, "info": 0, "ok": 0}
    for a in alerts:
        level = a.get("level", "info")
        if level in summary:
            summary[level] += 1

    # Sort: critical/warn first, ok last
    level_order = {"critical": 0, "warn": 1, "info": 2, "ok": 3}
    alerts.sort(key=lambda a: level_order.get(a.get("level", "info"), 2))

    return {"alerts": alerts, "summary": summary}


# ─── Token Usage (ported from codeburn) ───────────────────────────────────────

FALLBACK_PRICING: dict[str, dict] = {
    "claude-opus-4-7": {"input": 5e-6, "output": 25e-6, "cache_write": 6.25e-6, "cache_read": 0.5e-6, "web_search": 0.01, "fast_mult": 6},
    "claude-opus-4-6": {"input": 5e-6, "output": 25e-6, "cache_write": 6.25e-6, "cache_read": 0.5e-6, "web_search": 0.01, "fast_mult": 6},
    "claude-opus-4-5": {"input": 5e-6, "output": 25e-6, "cache_write": 6.25e-6, "cache_read": 0.5e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-opus-4-1": {"input": 15e-6, "output": 75e-6, "cache_write": 18.75e-6, "cache_read": 1.5e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-opus-4": {"input": 15e-6, "output": 75e-6, "cache_write": 18.75e-6, "cache_read": 1.5e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-sonnet-4-7": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-sonnet-4-6": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-sonnet-4-5": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-sonnet-4": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-3-7-sonnet": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-3-5-sonnet": {"input": 3e-6, "output": 15e-6, "cache_write": 3.75e-6, "cache_read": 0.3e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-haiku-4-7": {"input": 1e-6, "output": 5e-6, "cache_write": 1.25e-6, "cache_read": 0.1e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-haiku-4-5": {"input": 1e-6, "output": 5e-6, "cache_write": 1.25e-6, "cache_read": 0.1e-6, "web_search": 0.01, "fast_mult": 1},
    "claude-3-5-haiku": {"input": 0.8e-6, "output": 4e-6, "cache_write": 1e-6, "cache_read": 0.08e-6, "web_search": 0.01, "fast_mult": 1},
}

_SHORT_MODEL_NAMES: dict[str, str] = {
    "claude-opus-4-7": "Opus 4.7",
    "claude-opus-4-6": "Opus 4.6",
    "claude-opus-4-5": "Opus 4.5",
    "claude-opus-4-1": "Opus 4.1",
    "claude-opus-4": "Opus 4",
    "claude-sonnet-4-7": "Sonnet 4.7",
    "claude-sonnet-4-6": "Sonnet 4.6",
    "claude-sonnet-4-5": "Sonnet 4.5",
    "claude-sonnet-4": "Sonnet 4",
    "claude-3-7-sonnet": "Sonnet 3.7",
    "claude-3-5-sonnet": "Sonnet 3.5",
    "claude-haiku-4-7": "Haiku 4.7",
    "claude-haiku-4-5": "Haiku 4.5",
    "claude-3-5-haiku": "Haiku 3.5",
}

# Classifier regex patterns (ported from codeburn classifier.ts)
_RE_TEST = re.compile(r"\b(test|pytest|vitest|jest|mocha|spec|coverage|npm\s+test|npx\s+vitest|npx\s+jest)\b", re.I)
_RE_GIT = re.compile(r"\bgit\s+(push|pull|commit|merge|rebase|checkout|branch|stash|log|diff|status|add|reset|cherry-pick|tag)\b", re.I)
_RE_BUILD = re.compile(r"\b(npm\s+run\s+build|npm\s+publish|pip\s+install|docker|deploy|make\s+build|npm\s+run\s+dev|npm\s+start|pm2|systemctl|brew|cargo\s+build)\b", re.I)
_RE_INSTALL = re.compile(r"\b(npm\s+install|pip\s+install|brew\s+install|apt\s+install|cargo\s+add)\b", re.I)
_RE_DEBUG = re.compile(r"\b(fix|bug|error|broken|failing|crash|issue|debug|traceback|exception|stack\s*trace|not\s+working|wrong|unexpected|status\s+code|404|500|401|403)\b", re.I)
_RE_FEATURE = re.compile(r"\b(add|create|implement|new|build|feature|introduce|set\s*up|scaffold|generate|make\s+(?:a|me|the)|write\s+(?:a|me|the))\b", re.I)
_RE_REFACTOR = re.compile(r"\b(refactor|clean\s*up|rename|reorganize|simplify|extract|restructure|move|migrate|split)\b", re.I)
_RE_BRAINSTORM = re.compile(r"\b(brainstorm|idea|what\s+if|explore|think\s+about|approach|strategy|design|consider|how\s+should|what\s+would|opinion|suggest|recommend)\b", re.I)
_RE_RESEARCH = re.compile(r"\b(research|investigate|look\s+into|find\s+out|check|search|analyze|review|understand|explain|how\s+does|what\s+is|show\s+me|list|compare)\b", re.I)
_RE_FILE = re.compile(r"\.(py|js|ts|tsx|jsx|json|yaml|yml|toml|sql|sh|go|rs|java|rb|php|css|html|md|csv|xml)\b", re.I)
_RE_SCRIPT = re.compile(r"\b(run\s+\S+\.\w+|execute|scrip?t|curl|api\s+\S+|endpoint|request\s+url|fetch\s+\S+|query|database|db\s+\S+)\b", re.I)
_RE_URL = re.compile(r"https?://\S+", re.I)

_EDIT_TOOLS = {"Edit", "Write", "FileEditTool", "FileWriteTool", "NotebookEdit", "cursor:edit"}
_READ_TOOLS = {"Read", "Grep", "Glob", "FileReadTool", "GrepTool", "GlobTool"}
_BASH_TOOLS = {"Bash", "BashTool", "PowerShellTool"}
_TASK_TOOLS = {"TaskCreate", "TaskUpdate", "TaskGet", "TaskList", "TaskOutput", "TaskStop", "TodoWrite"}
_SEARCH_TOOLS = {"WebSearch", "WebFetch", "ToolSearch"}

_CATEGORY_LABELS: dict[str, str] = {
    "coding": "Coding",
    "debugging": "Debugging",
    "feature": "Feature Dev",
    "refactoring": "Refactoring",
    "testing": "Testing",
    "exploration": "Exploration",
    "planning": "Planning",
    "delegation": "Delegation",
    "git": "Git Ops",
    "build/deploy": "Build/Deploy",
    "conversation": "Conversation",
    "brainstorming": "Brainstorming",
    "general": "General",
}


def _get_model_costs(model: str) -> Optional[dict]:
    """Strip date suffix and match against FALLBACK_PRICING with prefix matching.
    For unknown new models, falls back to latest known pricing of the same tier."""
    canonical = re.sub(r"-\d{8}$", "", model)
    # Exact match
    if canonical in FALLBACK_PRICING:
        return FALLBACK_PRICING[canonical]
    # Prefix match (e.g., "claude-opus-4-6-20260415" matches "claude-opus-4-6")
    for key, costs in FALLBACK_PRICING.items():
        if canonical.startswith(key + "-"):
            return costs
    # Tier fallback: find latest known model in same tier (opus/sonnet/haiku)
    m = re.match(r"^claude-(opus|sonnet|haiku)", canonical) \
        or re.match(r"^claude-\d+-\d+-(opus|sonnet|haiku)", canonical)
    if m:
        tier = m.group(1)
        # Pick the first FALLBACK_PRICING entry whose key contains this tier
        # (dict insertion order = newest first by convention)
        for key, costs in FALLBACK_PRICING.items():
            if tier in key:
                return costs
    return None


def _calculate_cost(model: str, input_tokens: int, output_tokens: int,
                    cache_creation: int, cache_read: int,
                    web_search_requests: int, speed: str = "standard") -> float:
    """Calculate USD cost for a single API call."""
    costs = _get_model_costs(model)
    if not costs:
        return 0.0
    mult = costs["fast_mult"] if speed == "fast" else 1
    return mult * (
        input_tokens * costs["input"]
        + output_tokens * costs["output"]
        + cache_creation * costs["cache_write"]
        + cache_read * costs["cache_read"]
        + web_search_requests * costs["web_search"]
    )


def _get_short_model_name(model: str) -> str:
    """Map full model name to display name like 'Opus 4.6'.
    Falls back to auto-extraction for unknown future models."""
    canonical = re.sub(r"-\d{8}$", "", model)
    # Exact/prefix match against known table first
    for key, name in _SHORT_MODEL_NAMES.items():
        if canonical.startswith(key):
            return name
    # Auto-extract: "claude-opus-4-7" → "Opus 4.7", "claude-3-5-sonnet" → "Sonnet 3.5"
    # Pattern A: claude-{tier}-{major}-{minor}  (e.g. claude-opus-4-7)
    m = re.match(r"^claude-(opus|sonnet|haiku)-(\d+)(?:-(\d+))?", canonical)
    if m:
        tier, major, minor = m.group(1), m.group(2), m.group(3)
        version = f"{major}.{minor}" if minor else major
        return f"{tier.capitalize()} {version}"
    # Pattern B: claude-{major}-{minor}-{tier}  (e.g. claude-3-5-sonnet)
    m = re.match(r"^claude-(\d+)-(\d+)-(opus|sonnet|haiku)", canonical)
    if m:
        return f"{m.group(3).capitalize()} {m.group(1)}.{m.group(2)}"
    return canonical


def _classify_by_tools(tools: list[str], user_msg: str,
                       has_plan_mode: bool, has_agent_spawn: bool) -> Optional[str]:
    """Classify a turn by its tool usage pattern."""
    if not tools:
        return None
    if has_plan_mode:
        return "planning"
    if has_agent_spawn:
        return "delegation"

    has_edits = bool(set(tools) & _EDIT_TOOLS)
    has_reads = bool(set(tools) & _READ_TOOLS)
    has_bash = bool(set(tools) & _BASH_TOOLS)
    has_tasks = bool(set(tools) & _TASK_TOOLS)
    has_search = bool(set(tools) & _SEARCH_TOOLS)
    has_mcp = any(t.startswith("mcp__") for t in tools)
    has_skill = "Skill" in tools

    if has_bash and not has_edits:
        if _RE_TEST.search(user_msg):
            return "testing"
        if _RE_GIT.search(user_msg):
            return "git"
        if _RE_BUILD.search(user_msg) or _RE_INSTALL.search(user_msg):
            return "build/deploy"

    if has_edits:
        return "coding"
    if has_bash and has_reads:
        return "exploration"
    if has_bash:
        return "coding"
    if has_search or has_mcp:
        return "exploration"
    if has_reads and not has_edits:
        return "exploration"
    if has_tasks and not has_edits:
        return "planning"
    if has_skill:
        return "general"
    return None


def _refine_by_keywords(category: str, user_msg: str) -> str:
    """Refine tool-based category with keyword analysis."""
    if category == "coding":
        if _RE_DEBUG.search(user_msg):
            return "debugging"
        if _RE_REFACTOR.search(user_msg):
            return "refactoring"
        if _RE_FEATURE.search(user_msg):
            return "feature"
        return "coding"
    if category == "exploration":
        if _RE_RESEARCH.search(user_msg):
            return "exploration"
        if _RE_DEBUG.search(user_msg):
            return "debugging"
        return "exploration"
    return category


def _classify_conversation(user_msg: str) -> str:
    """Classify a turn with no tools by keyword analysis."""
    if _RE_BRAINSTORM.search(user_msg):
        return "brainstorming"
    if _RE_RESEARCH.search(user_msg):
        return "exploration"
    if _RE_DEBUG.search(user_msg):
        return "debugging"
    if _RE_FEATURE.search(user_msg):
        return "feature"
    if _RE_FILE.search(user_msg):
        return "coding"
    if _RE_SCRIPT.search(user_msg):
        return "coding"
    if _RE_URL.search(user_msg):
        return "exploration"
    return "conversation"


def _classify_turn(user_msg: str, tools: list[str],
                   has_plan_mode: bool = False, has_agent_spawn: bool = False) -> str:
    """Classify a turn into one of 13 categories."""
    if not tools:
        return _classify_conversation(user_msg)
    tool_cat = _classify_by_tools(tools, user_msg, has_plan_mode, has_agent_spawn)
    if tool_cat:
        return _refine_by_keywords(tool_cat, user_msg)
    return _classify_conversation(user_msg)


def _count_retries(tool_sequence: list[str]) -> int:
    """Count edit→bash→edit retry cycles in a tool sequence."""
    saw_edit_before_bash = False
    saw_bash_after_edit = False
    retries = 0
    for t in tool_sequence:
        is_edit = t in _EDIT_TOOLS
        is_bash = t in _BASH_TOOLS
        if is_edit:
            if saw_bash_after_edit:
                retries += 1
            saw_edit_before_bash = True
            saw_bash_after_edit = False
        if is_bash and saw_edit_before_bash:
            saw_bash_after_edit = True
    return retries


def _extract_bash_commands(command: str) -> list[str]:
    """Extract top-level command names from a bash command string."""
    if not command or not command.strip():
        return []
    # Strip quoted strings to avoid splitting on separators inside quotes
    stripped = re.sub(r'"[^"]*"|\'[^\']*\'', lambda m: " " * len(m.group()), command)
    # Find separator positions
    parts = []
    cursor = 0
    for m in re.finditer(r"\s*(?:&&|;|\|)\s*", stripped):
        parts.append((cursor, m.start()))
        cursor = m.end()
    parts.append((cursor, len(command)))

    cmds = []
    for start, end in parts:
        segment = command[start:end].strip()
        if not segment:
            continue
        first_token = segment.split()[0]
        base = os.path.basename(first_token)
        if base and base != "cd":
            cmds.append(base)
    return cmds


# Cache for token usage results
_token_cache: dict = {}
_TOKEN_CACHE_TTL = 60


def _parse_token_usage(period: str) -> dict:
    """Parse JSONL files and aggregate token usage data."""
    now = datetime.now()
    if period == "today":
        period_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        period_start = now - timedelta(days=7)
    period_end = now

    period_start_ts = period_start.timestamp()

    projects_dir = CLAUDE_DIR / "projects"

    # Aggregation accumulators
    seen_ids: set[str] = set()
    total_cost = 0.0
    total_calls = 0
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_write = 0
    session_ids: set[str] = set()

    daily_map: dict[str, dict] = {}       # date_str -> {costUSD, calls}
    project_map: dict[str, dict] = {}     # project_name -> {costUSD, sessions: set}
    model_map: dict[str, dict] = {}       # short_name -> {costUSD, calls}
    activity_map: dict[str, dict] = {}    # category -> {costUSD, turns, retries, editTurns, oneShotTurns}
    core_tools_map: dict[str, int] = {}
    shell_cmds_map: dict[str, int] = {}
    mcp_servers_map: dict[str, int] = {}

    # Collect all project directories (local + docker)
    all_proj_dirs: list[Path] = []
    docker_project_names: dict[str, str] = {}  # proj_dir_path -> display_name
    if projects_dir.is_dir():
        for pd in projects_dir.iterdir():
            if pd.is_dir():
                all_proj_dirs.append(pd)
    try:
        _sync_docker_sessions()
        for cid, info in _docker_container_info.items():
            dp = Path(info["projects_path"])
            if dp.is_dir():
                for pd in dp.iterdir():
                    if pd.is_dir():
                        all_proj_dirs.append(pd)
                        decoded = decode_project_path(pd.name)
                        docker_project_names[str(pd)] = info['name']
    except Exception:
        pass

    for proj_dir in all_proj_dirs:

        project_name = docker_project_names.get(str(proj_dir)) or (Path(decode_project_path(proj_dir.name)).name or proj_dir.name)

        for jsonl_file in proj_dir.glob("*.jsonl"):
            if "subagent" in jsonl_file.name:
                continue

            # Optimization: skip files not modified since period start (for "today")
            if period == "today":
                try:
                    if os.path.getmtime(jsonl_file) < period_start_ts:
                        continue
                except OSError:
                    continue

            try:
                with open(jsonl_file, "r", encoding="utf-8", errors="replace") as f:
                    raw_lines = f.readlines()
            except (FileNotFoundError, PermissionError, OSError):
                continue

            session_id = jsonl_file.stem

            # Parse entries
            entries: list[dict] = []
            for raw in raw_lines:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue

            if not entries:
                continue

            # Group into turns: user message + following assistant calls
            current_user_msg = ""
            current_calls: list[dict] = []  # list of parsed assistant call dicts
            current_tools: list[str] = []   # flat tool sequence for retry counting

            def _flush_turn():
                nonlocal total_cost, total_calls, total_input, total_output
                nonlocal total_cache_read, total_cache_write, current_user_msg
                nonlocal current_calls, current_tools

                if not current_calls:
                    current_user_msg = ""
                    current_calls = []
                    current_tools = []
                    return

                # Classify the turn
                all_tools = []
                has_plan = False
                has_agent = False
                for c in current_calls:
                    all_tools.extend(c["tools"])
                    if "EnterPlanMode" in c["tools"]:
                        has_plan = True
                    if "Agent" in c["tools"]:
                        has_agent = True

                category = _classify_turn(current_user_msg, all_tools, has_plan, has_agent)
                retries = _count_retries(current_tools)
                has_edits = bool(set(all_tools) & _EDIT_TOOLS)

                turn_cost = sum(c["cost"] for c in current_calls)

                # Activity aggregation
                cat_slot = activity_map.setdefault(category, {
                    "costUSD": 0.0, "turns": 0, "retries": 0,
                    "editTurns": 0, "oneShotTurns": 0,
                })
                cat_slot["turns"] += 1
                cat_slot["costUSD"] += turn_cost
                if has_edits:
                    cat_slot["editTurns"] += 1
                    cat_slot["retries"] += retries
                    if retries == 0:
                        cat_slot["oneShotTurns"] += 1

                current_user_msg = ""
                current_calls = []
                current_tools = []

            for entry in entries:
                etype = entry.get("type")

                if etype == "user":
                    # Flush previous turn
                    _flush_turn()
                    # Extract user message text
                    msg = entry.get("message", {})
                    if isinstance(msg, dict):
                        content = msg.get("content", "")
                    else:
                        content = ""
                    if isinstance(content, str):
                        current_user_msg = content
                    elif isinstance(content, list):
                        texts = []
                        for block in content:
                            if isinstance(block, dict) and block.get("type") == "text":
                                texts.append(block.get("text", ""))
                            elif isinstance(block, str):
                                texts.append(block)
                        current_user_msg = " ".join(texts)

                elif etype == "assistant":
                    msg = entry.get("message")
                    if not isinstance(msg, dict):
                        continue
                    usage = msg.get("usage")
                    model = msg.get("model")
                    if not usage or not model:
                        continue

                    # Deduplicate by message id
                    msg_id = msg.get("id")
                    if msg_id:
                        if msg_id in seen_ids:
                            continue
                        seen_ids.add(msg_id)

                    # Filter by timestamp
                    ts_str = entry.get("timestamp", "")
                    if ts_str:
                        try:
                            ts_dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                            # Make naive for comparison
                            ts_naive = ts_dt.replace(tzinfo=None)
                            if ts_naive < period_start or ts_naive > period_end:
                                continue
                        except (ValueError, TypeError):
                            continue
                    else:
                        continue

                    input_tokens = usage.get("input_tokens", 0)
                    output_tokens = usage.get("output_tokens", 0)
                    cache_creation = usage.get("cache_creation_input_tokens", 0)
                    cache_read_tokens = usage.get("cache_read_input_tokens", 0)
                    web_search_reqs = 0
                    server_tool_use = usage.get("server_tool_use")
                    if isinstance(server_tool_use, dict):
                        web_search_reqs = server_tool_use.get("web_search_requests", 0)
                    speed = usage.get("speed", "standard")

                    cost = _calculate_cost(model, input_tokens, output_tokens,
                                           cache_creation, cache_read_tokens,
                                           web_search_reqs, speed)

                    # Extract tool names from content blocks
                    tools: list[str] = []
                    bash_cmds: list[str] = []
                    msg_content = msg.get("content", [])
                    if isinstance(msg_content, list):
                        for block in msg_content:
                            if isinstance(block, dict) and block.get("type") == "tool_use":
                                tool_name = block.get("name", "")
                                if tool_name:
                                    tools.append(tool_name)
                                    # Extract bash commands
                                    if tool_name in _BASH_TOOLS:
                                        cmd = (block.get("input") or {}).get("command", "")
                                        if isinstance(cmd, str):
                                            bash_cmds.extend(_extract_bash_commands(cmd))

                    # Accumulate totals
                    total_cost += cost
                    total_calls += 1
                    total_input += input_tokens
                    total_output += output_tokens
                    total_cache_read += cache_read_tokens
                    total_cache_write += cache_creation
                    session_ids.add(session_id)

                    # Daily
                    date_str = ts_naive.strftime("%Y-%m-%d")
                    day_slot = daily_map.setdefault(date_str, {"costUSD": 0.0, "calls": 0})
                    day_slot["costUSD"] += cost
                    day_slot["calls"] += 1

                    # Project
                    proj_slot = project_map.setdefault(project_name, {"costUSD": 0.0, "sessions": set()})
                    proj_slot["costUSD"] += cost
                    proj_slot["sessions"].add(session_id)

                    # Model
                    short_name = _get_short_model_name(model)
                    model_slot = model_map.setdefault(short_name, {"costUSD": 0.0, "calls": 0})
                    model_slot["costUSD"] += cost
                    model_slot["calls"] += 1

                    # Core tools
                    for t in tools:
                        if t.startswith("mcp__"):
                            # MCP server
                            parts = t.split("__")
                            if len(parts) >= 2:
                                server_name = parts[1]
                                mcp_servers_map[server_name] = mcp_servers_map.get(server_name, 0) + 1
                        else:
                            core_tools_map[t] = core_tools_map.get(t, 0) + 1

                    # Shell commands
                    for cmd in bash_cmds:
                        shell_cmds_map[cmd] = shell_cmds_map.get(cmd, 0) + 1

                    # Store for turn grouping
                    current_calls.append({"cost": cost, "tools": tools})
                    current_tools.extend(tools)

            # Flush last turn
            _flush_turn()

    # Build response
    total_cache_total = total_cache_read + total_cache_write + total_input
    cache_hit_pct = round(total_cache_read / total_cache_total * 100, 1) if total_cache_total > 0 else 0.0

    overview = {
        "totalCostUSD": round(total_cost, 4),
        "totalCalls": total_calls,
        "totalSessions": len(session_ids),
        "totalInputTokens": total_input,
        "totalOutputTokens": total_output,
        "totalCacheReadTokens": total_cache_read,
        "totalCacheWriteTokens": total_cache_write,
        "cacheHitPct": cache_hit_pct,
    }

    daily = sorted(
        [{"date": d, "costUSD": round(v["costUSD"], 4), "calls": v["calls"]}
         for d, v in daily_map.items()],
        key=lambda x: x["date"],
    )

    by_project = sorted(
        [{"name": name, "costUSD": round(v["costUSD"], 4), "sessions": len(v["sessions"])}
         for name, v in project_map.items()],
        key=lambda x: x["costUSD"], reverse=True,
    )

    by_model = sorted(
        [{"name": name, "costUSD": round(v["costUSD"], 4), "calls": v["calls"]}
         for name, v in model_map.items()],
        key=lambda x: x["costUSD"], reverse=True,
    )

    by_activity = []
    for cat, v in activity_map.items():
        edit_turns = v["editTurns"]
        one_shot = v["oneShotTurns"]
        one_shot_pct = round(one_shot / edit_turns * 100, 1) if edit_turns > 0 else 0.0
        by_activity.append({
            "category": _CATEGORY_LABELS.get(cat, cat.title()),
            "costUSD": round(v["costUSD"], 4),
            "turns": v["turns"],
            "oneShotPct": one_shot_pct,
        })
    by_activity.sort(key=lambda x: x["costUSD"], reverse=True)

    core_tools = sorted(
        [{"name": n, "calls": c} for n, c in core_tools_map.items()],
        key=lambda x: x["calls"], reverse=True,
    )

    shell_commands = sorted(
        [{"name": n, "calls": c} for n, c in shell_cmds_map.items()],
        key=lambda x: x["calls"], reverse=True,
    )

    mcp_servers = sorted(
        [{"name": n, "calls": c} for n, c in mcp_servers_map.items()],
        key=lambda x: x["calls"], reverse=True,
    )

    return {
        "period": period,
        "overview": overview,
        "daily": daily,
        "byProject": by_project,
        "byModel": by_model,
        "byActivity": by_activity,
        "coreTools": core_tools,
        "shellCommands": shell_commands,
        "mcpServers": mcp_servers,
    }


def _empty_token_response(period: str) -> dict:
    """Return an empty token usage response."""
    return {
        "period": period,
        "overview": {
            "totalCostUSD": 0, "totalCalls": 0, "totalSessions": 0,
            "totalInputTokens": 0, "totalOutputTokens": 0,
            "totalCacheReadTokens": 0, "totalCacheWriteTokens": 0,
            "cacheHitPct": 0,
        },
        "daily": [],
        "byProject": [],
        "byModel": [],
        "byActivity": [],
        "coreTools": [],
        "shellCommands": [],
        "mcpServers": [],
    }


def get_token_usage(period: str = "week") -> dict:
    """Token usage API with in-memory caching."""
    now = time.time()
    cached = _token_cache.get(period)
    if cached and now - cached["ts"] < _TOKEN_CACHE_TTL:
        return cached["data"]
    result = _parse_token_usage(period)
    _token_cache[period] = {"data": result, "ts": now}
    return result


# ─── HTTP Handler ────────────────────────────────────────────────────────────

# API router: path → handler mapping
API_ROUTES: dict[str, callable] = {
    "/api/health": get_health,
    "/api/sessions": get_sessions,
    "/api/activity": get_activity,
    "/api/projects-summary": get_projects_summary,
    "/api/instructions": get_instructions,
    "/api/skills": get_skills,
    "/api/agents": get_agents,
    "/api/connectors": get_connectors,
    "/api/hooks": get_hooks,
    "/api/plugins": get_plugins,
    "/api/forks": get_forks,
    "/api/project-status": get_project_status,
    "/api/session-detail": get_session_detail,
    "/api/alerts": get_alerts,
    "/api/token-usage": get_token_usage,
}


class GleanerHandler(BaseHTTPRequestHandler):
    """Claude Gleaner HTTP request handler."""

    # Simplify log output
    def log_message(self, format, *args):
        # Simple one-line log
        print(f"[{self.log_date_time_string()}] {args[0] if args else ''}")

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query_params = parse_qs(parsed.query)

        # API endpoint: token-usage (accepts period query parameter)
        if path == "/api/token-usage":
            period = query_params.get("period", ["week"])[0]
            if period not in ("today", "week"):
                period = "week"
            self._json_response(get_token_usage(period))
            return

        # API endpoint: session-search (requires query parameter)
        if path == "/api/session-search":
            q = query_params.get("q", [""])[0]
            self._json_response(get_session_search(q))
            return

        # API endpoint: session-xray (requires query parameter)
        if path == "/api/session-xray":
            sid = query_params.get("id", [""])[0]
            self._json_response(get_session_xray(sid))
            return

        # API endpoint
        if path in API_ROUTES:
            self._json_response(API_ROUTES[path]())
            return

        # SSE stream
        if path == "/sse/live":
            self._sse_stream()
            return

        # Serve static files
        self._serve_static(path)

    def _read_json_body(self) -> tuple[Optional[dict], Optional[str]]:
        """Read POST request body and decode as JSON. Returns (data, error_message)."""
        try:
            content_length = int(self.headers.get("Content-Length", "0") or "0")
        except ValueError:
            return None, "invalid Content-Length"
        if content_length <= 0:
            return {}, None
        if content_length > 1_000_000:
            return None, "body too large"
        try:
            raw = self.rfile.read(content_length)
            return json.loads(raw.decode("utf-8")), None
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return None, f"invalid JSON body: {e}"

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        query_params = parse_qs(parsed.query)

        if path == "/api/transfer-session":
            session_id = query_params.get("id", [""])[0]
            if not session_id or ".." in session_id or "/" in session_id:
                self._json_response({"error": "id parameter required"}, 400)
                return

            body, err = self._read_json_body()
            if err is not None:
                self._json_response({"error": err}, 400)
                return

            host = (body.get("host") or "").strip()
            if not _is_valid_ssh_host(host):
                self._json_response({"error": "invalid host (expected [user@]hostname[:port])"}, 400)
                return

            target_path_override = (body.get("target_project_path") or "").strip()
            password = body.get("password")  # optional; used only for one-shot ssh-copy-id
            if password is not None and not isinstance(password, str):
                self._json_response({"error": "password must be a string"}, 400)
                return

            # Locate source JSONL (and optional subagent dir)
            projects_dir = CLAUDE_DIR / "projects"
            source_jsonl: Optional[Path] = None
            source_proj_dir: Optional[Path] = None
            if projects_dir.is_dir():
                for jsonl_file in projects_dir.rglob(f"{session_id}.jsonl"):
                    source_jsonl = jsonl_file
                    source_proj_dir = jsonl_file.parent
                    break
            if not source_jsonl or not source_proj_dir:
                self._json_response({"error": f"session {session_id} not found"}, 404)
                return

            source_folder = source_proj_dir.name
            source_decoded = decode_project_path(source_folder)
            source_subagent_dir = source_proj_dir / session_id  # may not exist

            # Determine target project folder (encoded) and target decoded path (for resume cmd)
            if target_path_override:
                if not _is_valid_project_path(target_path_override):
                    self._json_response({"error": "invalid target_project_path (must be absolute, no shell metachars)"}, 400)
                    return
                target_folder = encode_project_path(target_path_override)
                target_decoded = target_path_override
            else:
                target_folder = source_folder
                target_decoded = source_decoded

            remote_dir = f".claude/projects/{target_folder}"

            ssh_opts = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "StrictHostKeyChecking=accept-new"]
            # mkdir both: (1) session storage dir under ~/.claude/projects, (2) target project dir for cd
            ssh_mkdir = ["ssh", *ssh_opts, host,
                         f"mkdir -p {remote_dir} && mkdir -p {target_decoded}"]

            # Sanitize session JSONL into a tempfile — claude's resume renderer crashes
            # on tool_use_result entries with `originalFile: null`. Replace with "".
            try:
                sanitized_jsonl = _sanitize_jsonl_for_resume(source_jsonl)
            except Exception as e:
                self._json_response({"error": f"failed to sanitize session: {e}"}, 500)
                return

            # scp the sanitized file but keep the original filename on the remote
            scp_jsonl = ["scp", *ssh_opts, str(sanitized_jsonl),
                         f"{host}:{remote_dir}/{source_jsonl.name}"]
            scp_subagent = None
            if source_subagent_dir.is_dir():
                scp_subagent = ["scp", *ssh_opts, "-r", str(source_subagent_dir), f"{host}:{remote_dir}/"]

            resume_cmd = f"cd {target_decoded} && claude --resume {session_id}"

            def _is_auth_error(text: str) -> bool:
                t = (text or "").lower()
                return ("permission denied" in t) or ("publickey" in t and "password" in t)

            sshpass_path = shutil.which("sshpass")

            try:
                r1 = subprocess.run(ssh_mkdir, capture_output=True, text=True, timeout=30)
                if r1.returncode != 0:
                    msg = (r1.stderr or r1.stdout or "").strip() or f"exit {r1.returncode}"
                    if _is_auth_error(msg):
                        if not password:
                            # Ask frontend to prompt for a one-shot password
                            self._json_response({
                                "error": msg,
                                "code": "auth_required",
                                "can_setup": sshpass_path is not None,
                            }, 401)
                            return
                        if not sshpass_path:
                            self._json_response({
                                "error": "sshpass is not installed on this server, cannot auto-register key",
                                "code": "sshpass_missing",
                            }, 500)
                            return
                        # One-shot ssh-copy-id using SSHPASS env var (not argv → invisible to `ps`)
                        copyid_cmd = [sshpass_path, "-e", "ssh-copy-id",
                                      "-o", "StrictHostKeyChecking=accept-new",
                                      "-o", "ConnectTimeout=10", host]
                        copyid_env = {**os.environ, "SSHPASS": password}
                        try:
                            rc = subprocess.run(copyid_cmd, capture_output=True, text=True,
                                                timeout=60, env=copyid_env)
                        finally:
                            # Wipe sensitive data from the local dict ASAP
                            copyid_env["SSHPASS"] = ""
                            password = ""
                            body["password"] = ""
                        if rc.returncode != 0:
                            err = (rc.stderr or rc.stdout or "").strip() or f"exit {rc.returncode}"
                            # Clean stderr noise — strip duplicate "Permission denied" lines
                            err = "\n".join([ln for ln in err.splitlines() if ln.strip()])
                            self._json_response({
                                "error": f"ssh-copy-id failed: {err}",
                                "code": "setup_failed",
                            }, 500)
                            return
                        # Key registered — retry the original mkdir
                        r1 = subprocess.run(ssh_mkdir, capture_output=True, text=True, timeout=30)
                        if r1.returncode != 0:
                            msg = (r1.stderr or r1.stdout or "").strip() or f"exit {r1.returncode}"
                            self._json_response({"error": f"ssh mkdir failed after key setup: {msg}"}, 500)
                            return
                    else:
                        self._json_response({"error": f"ssh mkdir failed: {msg}"}, 500)
                        return
                r2 = subprocess.run(scp_jsonl, capture_output=True, text=True, timeout=300)
                if r2.returncode != 0:
                    msg = (r2.stderr or r2.stdout or "").strip() or f"exit {r2.returncode}"
                    self._json_response({"error": f"scp session file failed: {msg}"}, 500)
                    return
                subagent_result = None
                if scp_subagent is not None:
                    r3 = subprocess.run(scp_subagent, capture_output=True, text=True, timeout=600)
                    subagent_result = {
                        "ok": r3.returncode == 0,
                        "error": (r3.stderr or "").strip() if r3.returncode != 0 else None,
                    }
                self._json_response({
                    "ok": True,
                    "transferred": str(source_jsonl.relative_to(CLAUDE_DIR)),
                    "subagent": subagent_result,
                    "target_host": host,
                    "target_project": target_decoded,
                    "resume_cmd": resume_cmd,
                })
            except subprocess.TimeoutExpired:
                self._json_response({"error": "ssh/scp timed out"}, 504)
            except FileNotFoundError:
                self._json_response({"error": "ssh or scp not found on this machine"}, 500)
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            finally:
                try:
                    sanitized_jsonl.unlink()
                except Exception:
                    pass
            return

        if path == "/api/delete-project":
            dir_name = query_params.get("dir", [""])[0]
            if not dir_name:
                self._json_response({"error": "dir parameter required"}, 400)
                return
            # Safety check: only allow deletion of folders inside projects directory
            target = CLAUDE_DIR / "projects" / dir_name
            if not target.is_dir() or ".." in dir_name:
                self._json_response({"error": "invalid project directory"}, 400)
                return
            try:
                shutil.rmtree(target)
                self._json_response({"ok": True, "deleted": dir_name})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-skill":
            skill_name = query_params.get("name", [""])[0]
            if not skill_name or ".." in skill_name:
                self._json_response({"error": "name parameter required"}, 400)
                return
            # User skills only: ~/.claude/skills/{name}/
            target = CLAUDE_DIR / "skills" / skill_name
            plugin_cache = CLAUDE_DIR / "plugins" / "cache"
            # Verify target is NOT inside plugin cache
            try:
                target.resolve().relative_to(plugin_cache.resolve())
                self._json_response({"error": "cannot delete plugin skills"}, 403)
                return
            except ValueError:
                pass  # Not in plugin cache — OK
            if not target.is_dir():
                self._json_response({"error": "skill not found"}, 404)
                return
            try:
                shutil.rmtree(target)
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-agent":
            agent_name = query_params.get("name", [""])[0]
            if not agent_name or ".." in agent_name:
                self._json_response({"error": "name parameter required"}, 400)
                return
            # User agents only: ~/.claude/agents/{name}.md
            target = CLAUDE_DIR / "agents" / (agent_name + ".md")
            plugin_cache = CLAUDE_DIR / "plugins" / "cache"
            # Verify target is NOT inside plugin cache
            try:
                target.resolve().relative_to(plugin_cache.resolve())
                self._json_response({"error": "cannot delete plugin agents"}, 403)
                return
            except ValueError:
                pass  # Not in plugin cache — OK
            if not target.is_file():
                self._json_response({"error": "agent not found"}, 404)
                return
            try:
                target.unlink()
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path == "/api/delete-hook":
            event = query_params.get("event", [""])[0]
            index = query_params.get("index", ["0"])[0]
            if not event:
                self._json_response({"error": "event parameter required"}, 400)
                return
            try:
                idx = int(index)
            except ValueError:
                self._json_response({"error": "invalid index"}, 400)
                return
            settings_path = CLAUDE_DIR / "settings.json"
            try:
                settings = json.loads(settings_path.read_text(encoding="utf-8"))
                hooks = settings.get("hooks", {})
                if event not in hooks:
                    self._json_response({"error": f"event {event} not found"}, 404)
                    return
                handlers = hooks[event]
                if not isinstance(handlers, list) or idx >= len(handlers):
                    self._json_response({"error": "invalid index"}, 400)
                    return
                handlers.pop(idx)
                if not handlers:
                    del hooks[event]
                if not hooks:
                    del settings["hooks"]
                settings_path.write_text(json.dumps(settings, indent=2, ensure_ascii=False), encoding="utf-8")
                self._json_response({"ok": True})
            except Exception as e:
                self._json_response({"error": str(e)}, 500)
            return

        if path in ("/api/delete-session", "/api/delete-fork"):
            session_id = query_params.get("id", [""])[0]
            if not session_id or ".." in session_id or "/" in session_id:
                self._json_response({"error": "id parameter required"}, 400)
                return
            # Check against active sessions
            active_session_ids = set()
            sessions_dir = CLAUDE_DIR / "sessions"
            if sessions_dir.is_dir():
                running_pids = set()
                try:
                    ps = subprocess.run(["ps", "aux"], capture_output=True, text=True, timeout=5,
                                        env={**os.environ, "LC_ALL": "C"})
                    for line in ps.stdout.splitlines()[1:]:
                        parts = line.split(None, 10)
                        if len(parts) >= 11 and "claude" in parts[10].lower():
                            try:
                                running_pids.add(int(parts[1]))
                            except ValueError:
                                pass
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
                for sf in sessions_dir.glob("*.json"):
                    try:
                        pid = int(sf.stem)
                        if pid in running_pids:
                            sess_data = read_json(sf)
                            if sess_data:
                                sid = sess_data.get("sessionId", "")
                                if sid:
                                    active_session_ids.add(sid)
                    except ValueError:
                        pass
            if session_id in active_session_ids:
                self._json_response({"error": "cannot delete active session"}, 400)
                return
            deleted_anything = False
            errors = []
            # Delete JSONL file(s) across all project dirs
            projects_dir = CLAUDE_DIR / "projects"
            if projects_dir.is_dir():
                for jsonl_file in projects_dir.rglob(f"{session_id}.jsonl"):
                    try:
                        jsonl_file.unlink()
                        deleted_anything = True
                    except Exception as e:
                        errors.append(str(e))
                # Delete subagent directory if exists
                for subdir in projects_dir.rglob(session_id):
                    if subdir.is_dir():
                        try:
                            shutil.rmtree(subdir)
                            deleted_anything = True
                        except Exception as e:
                            errors.append(str(e))
            # Remove sessions/*.json where sessionId matches
            if sessions_dir.is_dir():
                for sf in sessions_dir.glob("*.json"):
                    try:
                        sess_data = read_json(sf)
                        if sess_data and sess_data.get("sessionId", "") == session_id:
                            sf.unlink()
                            deleted_anything = True
                    except Exception as e:
                        errors.append(str(e))
            if errors:
                self._json_response({"error": "; ".join(errors)}, 500)
                return
            self._json_response({"ok": True})
            return

        self._json_response({"error": "not found"}, 404)

    def _json_response(self, data: Any, status: int = 200):
        """Send JSON response."""
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _sse_stream(self):
        """Server-Sent Events stream. Sends sessions/activity every 5s, alerts every 30s."""
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        alerts_counter = 0  # alerts every 30s (6 * 5s)

        try:
            while True:
                # sessions event
                sessions_data = get_sessions()
                event = {
                    "type": "sessions",
                    "data": sessions_data,
                }
                self._send_sse_event(event)

                # activity event
                activity_data = get_activity()
                event = {
                    "type": "activity",
                    "data": activity_data,
                }
                self._send_sse_event(event)

                # alerts event (every 30s)
                if alerts_counter % 6 == 0:
                    alerts_data = get_alerts()
                    event = {
                        "type": "alerts",
                        "data": alerts_data,
                    }
                    self._send_sse_event(event)
                alerts_counter += 1

                self.wfile.flush()
                time.sleep(SSE_INTERVAL)
        except (BrokenPipeError, ConnectionResetError, OSError):
            # Client disconnected
            pass

    def _send_sse_event(self, data: dict):
        """Send a single SSE event."""
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self.wfile.write(f"data: {payload}\n\n".encode("utf-8"))

    def _serve_static(self, path: str):
        """Serve static files from dist/ directory."""
        # / → /dist/index.html
        if path == "/":
            file_path = DIST_DIR / "index.html"
        elif path.startswith("/dist/"):
            file_path = DIST_DIR / path[len("/dist/"):]
        elif path.startswith("/assets/"):
            file_path = DIST_DIR / path[1:]  # assets/ as-is
        else:
            # Look inside dist
            file_path = DIST_DIR / path.lstrip("/")

        # Security: prevent path traversal
        try:
            file_path = file_path.resolve()
            dist_resolved = DIST_DIR.resolve()
            if not str(file_path).startswith(str(dist_resolved)):
                self._error_response(403, "Forbidden")
                return
        except (ValueError, OSError):
            self._error_response(400, "Bad Request")
            return

        if not file_path.is_file():
            # SPA routing: serve index.html for unknown paths
            index = DIST_DIR / "index.html"
            if index.is_file() and not path.startswith("/api/"):
                file_path = index
            else:
                self._error_response(404, "Not Found")
                return

        # Determine MIME type
        mime_type, _ = mimetypes.guess_type(str(file_path))
        mime_map = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".mjs": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".json": "application/json; charset=utf-8",
            ".woff2": "font/woff2",
            ".woff": "font/woff",
        }
        suffix = file_path.suffix.lower()
        content_type = mime_map.get(suffix, mime_type or "application/octet-stream")

        try:
            with open(file_path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "public, max-age=3600")
            self.end_headers()
            self.wfile.write(body)
        except (FileNotFoundError, PermissionError):
            self._error_response(500, "Internal Server Error")

    def _error_response(self, status: int, message: str):
        """Send error JSON response."""
        self._json_response({"error": message}, status=status)


# ─── Server startup ──────────────────────────────────────────────────────────

class ThreadedHTTPServer(HTTPServer):
    """Threaded HTTP server (required for SSE support)."""
    allow_reuse_address = True
    daemon_threads = True

    def process_request(self, request, client_address):
        """Handle each request in a separate thread."""
        t = threading.Thread(target=self._handle_request_thread,
                             args=(request, client_address))
        t.daemon = True
        t.start()

    def _handle_request_thread(self, request, client_address):
        try:
            self.finish_request(request, client_address)
        except Exception:
            self.handle_error(request, client_address)
        finally:
            self.shutdown_request(request)


def main():
    server = ThreadedHTTPServer((BIND_HOST, BIND_PORT), GleanerHandler)
    print(f"Serving on http://{BIND_HOST}:{BIND_PORT}")
    print(f"→ Internal network: http://{INTERNAL_IP}:{BIND_PORT}")
    print(f"→ Static files: {DIST_DIR}")
    print()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down...")
        server.shutdown()


if __name__ == "__main__":
    main()
