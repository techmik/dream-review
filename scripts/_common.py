"""Shared path, time and lock logic for dream-review's scripts (stdlib only).

Every script takes the same two inputs:

  --project-dir  the Claude Code project being reviewed (default: the
                 CLAUDE_PROJECT_DIR environment variable, then the cwd)
  --data-dir     where working files live (default: the DREAM_REVIEW_HOME
                 environment variable, then ~/.dream-review)

All working files live under <data-dir>/projects/<project-slug>/.

Why not the plugin's own folders: the install folder is replaced on every
update (it would wipe the checkpoint and the report), and the plugin data
folder (~/.claude/plugins/data/...) sits under ~/.claude/, which Claude Code
treats as a protected path: Claude's own writes there always need a manual
approval, and permission rules can't pre-approve them, so an unattended pass
could never write its report.
"""
import hashlib
import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path

REPORT_NAME = "dream_report.md"
ARCHIVE_NAME = "dream_report_archive.md"
STATE_NAME = "dream_state.json"
PATTERNS_NAME = "skill_patterns.json"
CONFIG_NAME = "config.json"
MANIFEST_NAME = "scan_manifest.json"
LOCK_NAME = "pass.lock"
SECTION_PREFIX = "## Night of"

# A pass that died without releasing its lock stops blocking after this long.
LOCK_STALE_SECONDS = 2 * 60 * 60

DEFAULT_CONFIG = {
    # Optional: a tab-separated log your own blocking hooks append to
    # (<utc-iso-time> <hook> <tool> <reason-label> <session-id>). Leave null
    # to skip rule candidates entirely.
    "hook_block_log": None,
    # Reviewed report sections older than this move to the archive file.
    "archive_threshold_days": 7,
    # Per-turn text cap; longer turns are cut and counted in the manifest.
    "max_turn_chars": 4000,
}


def add_common_args(parser):
    parser.add_argument("--project-dir", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)


def resolve_project_dir(arg):
    if arg:
        return Path(arg).expanduser().resolve()
    env = os.environ.get("CLAUDE_PROJECT_DIR")
    if env:
        return Path(env).expanduser().resolve()
    return Path.cwd().resolve()


def resolve_data_dir(arg):
    if arg:
        return Path(arg).expanduser()
    env = os.environ.get("DREAM_REVIEW_HOME")
    if env:
        return Path(env).expanduser()
    return Path.home() / ".dream-review"


# ---------------------------------------------------------------- time

def parse_iso_utc(value):
    """Parse an ISO-8601 time to an aware UTC datetime, or return None.
    Handles a trailing 'Z' (which datetime.fromisoformat rejects before
    Python 3.11) and treats a time with no offset as UTC."""
    if not isinstance(value, str) or not value:
        return None
    s = value.strip()
    if s.endswith(("Z", "z")):
        s = s[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


# ---------------------------------------------------------------- paths

def _encodings(path):
    """Claude Code names a project's folder under ~/.claude/projects/ after
    its absolute path with separators and other punctuation turned into '-'.
    The exact character set isn't documented, so try the broad form
    (everything but letters and digits) and a narrow one."""
    s = str(path)
    broad = re.sub(r"[^A-Za-z0-9]", "-", s)
    narrow = re.sub(r"[:\\/ ]", "-", s)
    return [broad] if broad == narrow else [broad, narrow]


def git_root(path):
    """The main repository's root for `path`, or None outside a repo.

    In a linked worktree `.git` is a file ("gitdir: <main>/.git/worktrees/X");
    follow it to the shared .git folder (via its `commondir`) so every
    worktree of one repo maps to the same root, as Claude Code's memory does."""
    for candidate in [path, *path.parents]:
        dot_git = candidate / ".git"
        if dot_git.is_dir():
            return candidate
        if dot_git.is_file():
            try:
                line = dot_git.read_text(encoding="utf-8").strip()
            except (OSError, UnicodeDecodeError):
                return candidate
            if not line.startswith("gitdir:"):
                return candidate
            gitdir = Path(line[len("gitdir:"):].strip())
            if not gitdir.is_absolute():
                gitdir = (candidate / gitdir).resolve()
            common = gitdir
            try:
                rel = (gitdir / "commondir").read_text(encoding="utf-8").strip()
                common = (gitdir / rel).resolve()
            except (OSError, UnicodeDecodeError):
                pass
            # <main>/.git is the common dir of a normal repo; its parent is the root.
            if common.name == ".git":
                return common.parent
            return candidate
    return None


def sessions_dir(project_dir):
    """Folder holding this project's session transcripts (*.jsonl)."""
    base = Path.home() / ".claude" / "projects"
    for enc in _encodings(project_dir):
        if (base / enc).is_dir():
            return base / enc
    return base / _encodings(project_dir)[0]


def project_slug(project_dir):
    """Workspace folder name: readable path plus a short hash of the exact
    path, so 'my_app' and 'my-app' never share a workspace."""
    readable = _encodings(project_dir)[0].strip("-")[-60:] or "project"
    digest = hashlib.sha1(str(project_dir).encode("utf-8")).hexdigest()[:8]
    return f"{readable}-{digest}"


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return None


def memory_dir(project_dir):
    """Where auto-memory lives, per the Claude Code docs: autoMemoryDirectory
    if set (local settings, then project, then user), otherwise the
    per-project default ~/.claude/projects/<project>/memory/, where <project>
    comes from the git repository root (so worktrees share one folder), or
    the project folder outside a repo.

    Note: Claude Code honours the project and local keys only once the folder
    is trusted. This read-only scanner can't tell, so it honours them always."""
    candidates = [
        project_dir / ".claude" / "settings.local.json",
        project_dir / ".claude" / "settings.json",
        Path.home() / ".claude" / "settings.json",
    ]
    for settings_file in candidates:
        settings = read_json(settings_file)
        if isinstance(settings, dict) and isinstance(settings.get("autoMemoryDirectory"), str) \
                and settings["autoMemoryDirectory"].strip():
            return Path(settings["autoMemoryDirectory"].strip()).expanduser()
    root = git_root(project_dir) or project_dir
    base = Path.home() / ".claude" / "projects"
    for enc in _encodings(root):
        if (base / enc / "memory").is_dir():
            return base / enc / "memory"
    return base / _encodings(root)[0] / "memory"


def workspace(data_dir, project_dir):
    ws = data_dir / "projects" / project_slug(project_dir)
    ws.mkdir(parents=True, exist_ok=True)
    return ws


def load_config(ws):
    config = dict(DEFAULT_CONFIG)
    user = read_json(ws / CONFIG_NAME)
    if isinstance(user, dict):
        config.update({k: v for k, v in user.items() if k in DEFAULT_CONFIG})
    return config


def atomic_write(path, text):
    """Write to a temp file beside `path`, flush it to disk, then replace
    `path` in one step, so a crash never leaves a half-written file."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        f.write(text)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------- lock

def read_lock(ws):
    """Return the lock's contents if a live (non-stale) lock exists, else None."""
    lock = ws / LOCK_NAME
    info = read_json(lock)
    if not isinstance(info, dict):
        if lock.exists():
            try:
                age = time.time() - lock.stat().st_mtime
            except OSError:
                return None
            return {"run_id": "?", "started": "?"} if age < LOCK_STALE_SECONDS else None
        return None
    if time.time() - float(info.get("epoch", 0)) >= LOCK_STALE_SECONDS:
        return None
    return info


def acquire_lock(ws, run_id):
    """Take the workspace lock for one pass. Returns (True, None) or
    (False, holder_info). A lock older than LOCK_STALE_SECONDS is replaced."""
    lock = ws / LOCK_NAME
    holder = read_lock(ws)
    if holder is not None:
        return False, holder
    if lock.exists():
        try:
            lock.unlink()  # stale
        except OSError:
            pass
    info = {"run_id": run_id, "epoch": time.time(),
            "started": datetime.now(timezone.utc).isoformat(), "pid": os.getpid()}
    try:
        fd = os.open(str(lock), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False, read_json(lock) or {"run_id": "?"}
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(info, f)
    return True, None


def release_lock(ws, run_id):
    """Release the lock only if this run holds it. Returns True if released."""
    lock = ws / LOCK_NAME
    info = read_json(lock)
    if isinstance(info, dict) and info.get("run_id") == run_id:
        try:
            lock.unlink()
            return True
        except OSError:
            return False
    return False
