"""Memory sources for dream-review 0.2.0 (stdlib only).

A source is somewhere memory lives as markdown files:

  native  Claude Code's own auto-memory folder (the default, and the only
          source when config.json has no "sources")
  vault   a folder of notes (an Obsidian vault, a notes folder): a few
          "core" notes are read in full every pass, every other note is
          indexed (path, headings, size, hash) and searched on demand.

Everything here is read-only. Settings problems are returned as a list of
plain sentences; a non-empty list means the pass must stop rather than fall
back to native-only behaviour without saying so.
"""
import fnmatch
import hashlib
import json
import os
import re
import string
from datetime import date
from pathlib import Path

import _common as c

DEFAULT_EXCLUDES = [".obsidian/**", ".trash/**", ".git/**", ".dream-review/**"]
SOURCE_ID = re.compile(r"[A-Za-z0-9_-]{1,32}")
# The index is read every pass: keep each entry small (measured on a real
# 386-note vault: 12 x 80 gave 7 shards).
MAX_HEADINGS = 6
MAX_HEADING_CHARS = 60

# Settings 0.2.0 adds on top of _common.DEFAULT_CONFIG.
EXTRA_CONFIG = {
    # List of sources; None = [{"id": "native", "type": "native"}].
    "sources": None,
    # Optional vault note (as "<source-id>:<relative path>") whose formatting
    # conventions the review follows. Never read during the pass.
    "rules_note": None,
    # Optional {"<folder>": "<source-id>:<template path>"} for new notes.
    "templates": None,
    # Optional plain-language routing hint for two-source setups, e.g.
    # "facts about me and feedback go to native; project progress to the vault".
    "routing": None,
    # Per-pass caps for find.py.
    "max_searches": 20,
    "max_fetches": 15,
    # Daily-note backfill (0.3.0); None = off. See resolve_daily().
    "daily_notes": None,
}
ALL_KEYS = {**c.DEFAULT_CONFIG, **EXTRA_CONFIG}


# ------------------------------------------------------------- settings

def read_config(ws):
    """(config, problems). Unknown keys and unreadable JSON are problems,
    not silently ignored: a typo must not quietly restore native-only."""
    config = dict(ALL_KEYS)
    path = ws / c.CONFIG_NAME
    if not path.exists():
        return config, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError) as e:
        return config, [f"config.json can't be read ({e.__class__.__name__}: {e})"]
    if not isinstance(raw, dict):
        return config, ["config.json must be a JSON object"]
    problems = []
    unknown = sorted(k for k in raw if k not in ALL_KEYS)
    if unknown:
        problems.append("unknown setting(s) in config.json: " + ", ".join(unknown))
    config.update({k: val for k, val in raw.items() if k in ALL_KEYS})
    for key in ("max_searches", "max_fetches"):
        val = config[key]
        if isinstance(val, bool) or not isinstance(val, int) or val < 0:
            problems.append(f"{key} must be a whole number >= 0")
            config[key] = EXTRA_CONFIG[key]
    return config, problems


def _rel_ok(rel):
    """A relative path inside a source: no absolute path, no '..'."""
    if not isinstance(rel, str) or not rel.strip():
        return False
    p = Path(rel)
    return not p.is_absolute() and not p.drive and ".." not in p.parts


def resolve_sources(config, ws):
    """(sources, problems). Each source is a dict: id, type, and for a vault
    root (resolved Path), core (relative posix paths), exclude (patterns)."""
    raw = config.get("sources")
    if raw is None:
        return [{"id": "native", "type": "native"}], []
    if not isinstance(raw, list) or not raw:
        return [], ["sources must be a non-empty list"]
    problems, out, ids = [], [], set()
    for n, s in enumerate(raw, 1):
        where = f"sources[{n}]"
        if not isinstance(s, dict):
            problems.append(f"{where} must be an object")
            continue
        sid, typ = s.get("id"), s.get("type")
        if not isinstance(sid, str) or not SOURCE_ID.fullmatch(sid):
            problems.append(f"{where}: id must be 1-32 letters, digits, - or _")
            continue
        if sid in ids:
            problems.append(f"{where}: id '{sid}' is used twice")
            continue
        ids.add(sid)
        if typ == "native":
            extra = sorted(set(s) - {"id", "type"})
            if extra:
                problems.append(f"source '{sid}': a native source takes no other keys ({', '.join(extra)})")
            if any(o["type"] == "native" for o in out):
                problems.append(f"source '{sid}': only one native source is allowed")
            out.append({"id": sid, "type": "native"})
            continue
        if typ != "vault":
            problems.append(f"source '{sid}': type must be \"native\" or \"vault\"")
            continue
        extra = sorted(set(s) - {"id", "type", "path", "core", "exclude"})
        if extra:
            problems.append(f"source '{sid}': unknown key(s) {', '.join(extra)}")
        path = s.get("path")
        if not isinstance(path, str) or not path.strip():
            problems.append(f"source '{sid}': path is required")
            continue
        root = Path(path.strip()).expanduser()
        if not root.is_absolute():
            problems.append(f"source '{sid}': path must be absolute ({path})")
            continue
        if not root.is_dir():
            problems.append(f"source '{sid}': folder not found or not readable ({path})")
            continue
        root = root.resolve()
        core = s.get("core", [])
        exclude = s.get("exclude", [])
        if not isinstance(core, list) or not all(_rel_ok(x) for x in core):
            problems.append(f"source '{sid}': core must be a list of paths relative to the vault")
            core = []
        if not isinstance(exclude, list) or not all(isinstance(x, str) and x.strip() for x in exclude):
            problems.append(f"source '{sid}': exclude must be a list of patterns")
            exclude = []
        core = [Path(x).as_posix() for x in core]
        for rel in core:
            if not (root / rel).is_file():
                problems.append(f"source '{sid}': core note not found ({rel})")
        out.append({"id": sid, "type": "vault", "root": root, "core": core,
                    "exclude": [x.strip().replace("\\", "/") for x in exclude]})
    vaults = [s for s in out if s["type"] == "vault"]
    for a in vaults:
        for b in vaults:
            if a is not b and (a["root"] == b["root"] or a["root"] in b["root"].parents):
                problems.append(f"sources '{a['id']}' and '{b['id']}' overlap (one folder is inside the other)")
    ws_resolved = ws.resolve()
    for s in vaults:
        # A vault inside the workspace would index the plugin's own copies.
        # (The reverse, the workspace inside a vault, is fine: the walker
        # skips it.)
        if s["root"] == ws_resolved or ws_resolved in s["root"].parents:
            problems.append(f"source '{s['id']}' is inside dream-review's own workspace")
    return out, problems


def fingerprint(sources):
    """Stable hash of the sources settings, stored with the checkpoint so a
    change can trigger a reconciliation window."""
    norm = []
    for s in sources:
        if s["type"] == "native":
            norm.append({"id": s["id"], "type": "native"})
        else:
            norm.append({"id": s["id"], "type": "vault", "root": str(s["root"]),
                         "core": sorted(s["core"]), "exclude": sorted(s["exclude"])})
    return hashlib.sha1(json.dumps(norm, sort_keys=True).encode("utf-8")).hexdigest()[:12]


DEFAULT_FINGERPRINT = fingerprint([{"id": "native", "type": "native"}])


# ------------------------------------------------------------- daily notes

MONTHS = ["January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]
WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
DAILY_TOKENS = {"YYYY", "MM", "DD", "Month", "Mon", "Weekday"}


def daily_rel_path(pattern, day):
    """The note path for a date. Month and weekday names are English, on
    purpose: they mustn't change with the machine's language settings."""
    return pattern.format(YYYY=f"{day.year:04d}", MM=f"{day.month:02d}", DD=f"{day.day:02d}",
                          Month=MONTHS[day.month - 1], Mon=MONTHS[day.month - 1][:3],
                          Weekday=WEEKDAYS[day.weekday()])


def _pattern_problem(pattern):
    """None if path_pattern is usable, else a sentence saying why not. Parsed
    the way str.format will read it, so a stray or doubled brace, a format
    spec, or a pattern that renders the same path for every date is caught
    here rather than crashing (or misfiling) during a pass."""
    if not isinstance(pattern, str) or not _rel_ok(pattern) or not pattern.lower().endswith(".md"):
        return ("daily_notes.path_pattern must be a relative .md path, "
                "e.g. \"Daily Notes/{YYYY}-{MM}-{DD}.md\"")
    try:
        fields = list(string.Formatter().parse(pattern))
    except ValueError:
        return "daily_notes.path_pattern has an unmatched { or }"
    tokens = set()
    for literal, field, spec, conversion in fields:
        if "{" in literal or "}" in literal:
            return "daily_notes.path_pattern can't contain doubled braces ({{ or }})"
        if field is None:
            continue
        if spec or conversion:
            return f"daily_notes.path_pattern: write {{{field}}} without a format spec"
        tokens.add(field)
    unknown = tokens - DAILY_TOKENS
    if unknown:
        return ("daily_notes.path_pattern: unknown placeholder(s) "
                + ", ".join("{" + t + "}" for t in sorted(unknown)))
    if not {"YYYY", "DD"} <= tokens or not ({"MM", "Month", "Mon"} & tokens):
        return "daily_notes.path_pattern needs {YYYY}, {DD} and a month ({MM}, {Month} or {Mon})"
    try:
        a = daily_rel_path(pattern, date(2001, 2, 3))
        b = daily_rel_path(pattern, date(2002, 4, 5))
    except (ValueError, KeyError, IndexError) as e:
        return f"daily_notes.path_pattern can't be filled in ({e.__class__.__name__})"
    if a == b or not _rel_ok(a):
        return "daily_notes.path_pattern must give a different path for each date"
    return None


def resolve_daily(config, sources):
    """(daily, problems). daily is None when off, else a dict: source (the
    vault source dict), pattern, template (source, path) or None,
    create_missing, tz (a tzinfo, or None = this machine's local time) and
    tz_name."""
    raw = config.get("daily_notes")
    if raw is None:
        return None, []
    if not isinstance(raw, dict):
        return None, ["daily_notes must be an object"]
    problems = []
    extra = sorted(set(raw) - {"source", "path_pattern", "template", "timezone", "create_missing"})
    if extra:
        problems.append("daily_notes: unknown key(s) " + ", ".join(extra))
    vaults = {s["id"]: s for s in sources if s["type"] == "vault"}
    src = vaults.get(raw.get("source"))
    if src is None:
        problems.append("daily_notes.source must be the id of a vault source")
    pattern = raw.get("path_pattern")
    pattern_problem = _pattern_problem(pattern)
    if pattern_problem:
        problems.append(pattern_problem)
    template = None
    tpl = raw.get("template")
    if tpl is not None:
        tsid, sep, trel = tpl.partition(":") if isinstance(tpl, str) else ("", "", "")
        tsrc = vaults.get(tsid)
        tpath = note_path(tsrc, trel) if (sep and tsrc) else None
        if tpath is None or not tpath.is_file():
            problems.append("daily_notes.template must be \"<vault id>:<path>\" of an existing note")
        else:
            template = (tsid, Path(trel).as_posix())
    create_missing = raw.get("create_missing", False)
    if not isinstance(create_missing, bool):
        problems.append("daily_notes.create_missing must be true or false")
        create_missing = False
    if create_missing and template is None:
        problems.append("daily_notes.create_missing needs a template")
    tz, tz_name = None, "local"
    name = raw.get("timezone")
    if name is not None:
        try:
            from zoneinfo import ZoneInfo
            tz, tz_name = ZoneInfo(str(name)), str(name)
        except Exception:  # noqa: BLE001 - ZoneInfoNotFoundError, ValueError, ImportError
            problems.append(f"daily_notes.timezone '{name}' isn't available here (on Windows, "
                            "Python needs the tzdata package for named zones); leave it out to "
                            "use this computer's local time")
    if problems:
        return None, problems
    return {"source": src, "pattern": pattern, "template": template,
            "create_missing": create_missing, "tz": tz, "tz_name": tz_name}, []


def local_date(ts, tz):
    """The calendar date of an aware UTC datetime in tz (None = this
    machine's local time, DST included, through the operating system)."""
    return (ts.astimezone(tz) if tz else ts.astimezone()).date()


def logged_ids(text):
    """dream-review entry ids already in a note: <!-- dream-review:<session>:<date> -->."""
    return set(re.findall(r"<!--\s*dream-review:([^\s:>]+:\d{4}-\d{2}-\d{2})\s*-->", text or ""))


# ------------------------------------------------------------- vault walk

def _excluded(rel, patterns):
    """rel is a posix path relative to the vault root; a folder ends in '/'."""
    bare = rel.rstrip("/")
    for pat in patterns:
        if pat.endswith("/**"):
            base = pat[:-3].rstrip("/")
            if bare == base or bare.startswith(base + "/"):
                return True
        elif fnmatch.fnmatchcase(bare, pat) or fnmatch.fnmatchcase(bare, pat.rstrip("/")):
            return True
    return False


def _is_link(path):
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)  # Python 3.12+
        return bool(is_junction and is_junction())
    except OSError:
        return True


def iter_notes(source, ws=None):
    """Yield (rel_posix, Path) for every included .md note, sorted, never
    following folder links, never entering the workspace."""
    root = source["root"]
    patterns = DEFAULT_EXCLUDES + source.get("exclude", [])
    ws_resolved = ws.resolve() if ws else None
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        here = Path(dirpath)
        rel_dir = here.relative_to(root).as_posix()
        keep = []
        for d in sorted(dirnames):
            full = here / d
            rel = d if rel_dir == "." else f"{rel_dir}/{d}"
            if _is_link(full) or _excluded(rel + "/", patterns):
                continue
            if ws_resolved and full.resolve() == ws_resolved:
                continue
            keep.append(d)
        dirnames[:] = keep
        for f in sorted(filenames):
            if not f.lower().endswith(".md"):
                continue
            rel = f if rel_dir == "." else f"{rel_dir}/{f}"
            if not _excluded(rel, patterns):
                yield rel, here / f


def note_path(source, rel):
    """The note's Path if rel is a valid, included note path in this vault
    source, else None (blocks '..', absolute paths, excluded folders, links
    out of the vault)."""
    if not _rel_ok(rel):
        return None
    rel = Path(rel).as_posix()
    if not rel.lower().endswith(".md"):
        return None
    if _excluded(rel, DEFAULT_EXCLUDES + source.get("exclude", [])):
        return None
    full = source["root"] / rel
    try:
        resolved = full.resolve()
    except OSError:
        return None
    if source["root"] not in resolved.parents:
        return None
    return full


def read_note(path):
    """(text, sha, error). sha = first 16 hex of SHA-256 of the raw bytes."""
    try:
        data = path.read_bytes()
    except OSError as e:
        return None, None, e.__class__.__name__
    sha = hashlib.sha256(data).hexdigest()[:16]
    try:
        return data.decode("utf-8"), sha, None
    except UnicodeDecodeError:
        return None, sha, "UnicodeDecodeError"


def headings(text):
    """Up to MAX_HEADINGS level 1-3 headings, outside fenced code."""
    out, fenced = [], False
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fenced = not fenced
            continue
        if fenced:
            continue
        m = re.match(r"(#{1,3})\s+(.+)", s)
        if m:
            out.append(m.group(2).strip()[:MAX_HEADING_CHARS])
            if len(out) >= MAX_HEADINGS:
                break
    return out


# ------------------------------------------------------------- redirect

ROUTING_PATTERNS = [
    r"never here",
    r"no separate memory",
    r"single source of truth",
    r"sources? of truth",
    r"write it to its place",
    r"lives? in (?:the|my|your|an?) (?:obsidian )?(?:vault|notes?)",
    r"memory (?:is|lives) (?:in|at)",
    r"redirect",
]
_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s`'\"<>|*?\]\)]*")
_POSIX_PATH = re.compile(r"(?<![\w.:/])(?:~|/)[^\s`'\"<>|*?\]\)]+")


def candidate_paths(text):
    """Existing folders named in text (absolute or ~), in order, no repeats."""
    found = []
    for m in list(_WIN_PATH.finditer(text)) + list(_POSIX_PATH.finditer(text)):
        raw = m.group(0).rstrip(".,;:")
        if "[" in raw:
            continue
        p = Path(raw).expanduser()
        try:
            if p.is_absolute() and p.is_dir():
                s = str(p.resolve())
                if s not in found:
                    found.append(s)
        except OSError:
            continue
    return found


def detect_redirect(mem_dir):
    """Classify native MEMORY.md: "none" (a normal index or no file),
    "redirect" (routing language, no other memory files), or "mixed"
    (routing language, but older memory files still sit beside it).
    Detection never follows a path; it only reports candidates."""
    index = mem_dir / "MEMORY.md"
    result = {"status": "none", "signals": [], "candidate_paths": [], "other_memory_files": 0}
    if not index.is_file():
        return result
    try:
        text = index.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return result
    signals = [p for p in ROUTING_PATTERNS if re.search(p, text, re.IGNORECASE)]
    links = re.findall(r"\]\(([^)\s]+\.md)\)", text)
    others = [f for f in mem_dir.glob("*.md") if f.name != "MEMORY.md"]
    result["signals"] = signals
    result["candidate_paths"] = candidate_paths(text)
    result["other_memory_files"] = len(others)
    # A real index links to topic files; a redirect is short and points away.
    if signals and not links and len(text) <= 4000:
        result["status"] = "mixed" if others else "redirect"
    return result


# ------------------------------------------------------------- setup

# README.md is left out on purpose: every code repo has one.
HUB_NAMES = ["VAULT-INDEX.md", "Home.md", "Index.md"]
PRIORITY_NAMES = ["Active Priorities.md", "Priorities.md", "Tasks.md", "TODO.md", "Now.md"]


def suggest_setup(project_dir, mem_dir):
    """Read-only: what the setup looks like and a draft config. Nothing is
    written; the skill shows this to the user and writes config.json only on
    their yes."""
    redirect = detect_redirect(mem_dir)
    texts = []
    for f in (project_dir / "CLAUDE.md", mem_dir / "MEMORY.md"):
        try:
            texts.append(f.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError):
            pass
    joined = "\n".join(texts)
    vaults = []
    claude_home = (Path.home() / ".claude").resolve()
    for p in candidate_paths(joined):
        root = Path(p)
        if root == project_dir or root in project_dir.parents or root == mem_dir:
            continue
        # Claude Code's own folder is config, never a notes vault.
        if root == claude_home or claude_home in root.parents:
            continue
        # A vault: an Obsidian folder, or a hub/priorities note at its root.
        # Any folder that merely holds some .md files doesn't count.
        if not (root / ".obsidian").is_dir() and not any((root / n).is_file() for n in HUB_NAMES + PRIORITY_NAMES):
            continue
        mentioned = set(re.findall(r"([\w][\w .'()-]*\.md)", joined))
        core = [n for n in HUB_NAMES + PRIORITY_NAMES if (root / n).is_file()]
        core += sorted(n for n in mentioned if (root / n).is_file() and n not in core)
        vaults.append({"path": str(root), "obsidian": (root / ".obsidian").is_dir(), "core": core})
    native_has_memories = mem_dir.is_dir() and any(f.name != "MEMORY.md" for f in mem_dir.glob("*.md"))
    sources = []
    if redirect["status"] == "none" and native_has_memories:
        sources.append({"id": "native", "type": "native"})
    for n, vlt in enumerate(vaults, 1):
        sources.append({"id": "vault" if n == 1 else f"vault{n}", "type": "vault",
                        "path": vlt["path"], "core": vlt["core"], "exclude": []})
    return {
        "native_memory_dir": str(mem_dir),
        "native_status": redirect,
        "native_has_memories": native_has_memories,
        "vaults_found": vaults,
        "draft_config": {"sources": sources} if sources else None,
        "note": "Draft only. Check the paths and core notes, add folders to exclude "
                "(web clippings, archives, private areas), then save it as config.json "
                "in the workspace.",
    }
