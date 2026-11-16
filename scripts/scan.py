#!/usr/bin/env python3
"""dream-review scanner: mechanical extraction only.

Reads this project's recent Claude Code session transcripts and its existing
auto-memory files and writes them as JSON shards the dream-review skill can
Read. It drafts no proposals, calls no model and never touches a memory file.
It writes only inside its own workspace (<data-dir>/projects/<slug>/): the
pass lock, the manifest, this run's intake folder, and it removes older
runs' intake folders.

What it keeps from a transcript: the visible text of "user" and "assistant"
turns. Tool calls, tool results, images, thinking blocks and meta records
(text Claude Code injects, such as loaded skill bodies) are dropped on
purpose. Subagent transcripts (in per-session subfolders) are not read.

Nothing is skipped silently: unreadable files, malformed records, torn lines
and cut-off turns are all counted in the manifest's "skipped" block. If a
transcript couldn't be read, the manifest says "complete": false and
mark_success.py refuses to move the checkpoint past it.

The window ends 10 minutes before "now", so turns still being written (the
running pass's own session, for one) are left for the next run instead of
being cut off mid-line and then skipped for good.

Usage:
    python scan.py [--project-dir P] [--data-dir D] [--hours N] [--paths-only]

Without --hours the window starts at the last successful run's checkpoint
(written by mark_success.py), so a missed night is picked up by the next run
that succeeds. With no checkpoint yet it falls back to the last 24 hours.
"""
import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402

# Each shard stays under this many characters so one default Read call fits
# (character count predicts tokens far better than line count).
MAX_SHARD_CHARS = 50000
# A single memory bigger than this is split into numbered parts.
MAX_ITEM_CHARS = 40000
SETTLE_MINUTES = 10
REAL_TURN_TYPES = {"user", "assistant"}


def _text_blocks(content):
    if isinstance(content, str):
        if content:
            yield content
        return
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str) and text:
                    yield text


def _scan_session(path, start, end, max_chars, turns, sessions, skipped):
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except (OSError, UnicodeDecodeError) as e:
        skipped["unreadable_transcripts"].append(f"{path.name}: {e.__class__.__name__}")
        return
    last = max((i for i, l in enumerate(lines) if l.strip()), default=-1)
    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            # A torn final line is a session still being written: normal.
            skipped["torn_last_lines" if i == last else "torn_lines"] += 1
            continue
        if not isinstance(record, dict):
            skipped["malformed_records"] += 1
            continue
        if record.get("type") not in REAL_TURN_TYPES:
            continue
        if record.get("isMeta") is True:
            skipped["meta_records"] += 1
            continue
        ts = c.parse_iso_utc(record.get("timestamp"))
        if ts is None:
            skipped["bad_timestamps"] += 1
            continue
        if ts < start or ts >= end:
            continue
        message = record.get("message")
        if not isinstance(message, dict):
            skipped["malformed_records"] += 1
            continue
        parts = list(_text_blocks(message.get("content")))
        if not parts:
            continue
        session_id = record.get("sessionId") if isinstance(record.get("sessionId"), str) else path.stem
        sessions.add(session_id)
        text = "\n".join(parts)
        truncated = len(text) > max_chars
        if truncated:
            skipped["truncated_turns"] += 1
            text = text[:max_chars]
        turns.append((ts, {
            "sessionId": session_id,
            "timestamp": ts.isoformat().replace("+00:00", "Z"),
            "role": record.get("type"),
            "text": text,
            "truncated": truncated,
        }))


def _parse_frontmatter(text):
    """Minimal parser: flat key: value pairs plus one nested `metadata:` level."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    block = text[3:end].strip("\n")
    body = text[end + 4:].lstrip("\n")
    result, metadata, target = {}, {}, None
    for raw in block.splitlines():
        if not raw.strip() or ":" not in raw:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        key, _, value = raw.strip().partition(":")
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if indent == 0:
            if key == "metadata":
                result["metadata"] = metadata
                target = metadata
                continue
            target = None
            result[key] = value
        elif target is not None:
            target[key] = value
    return result, body


def _load_memories(mem_dir, skipped):
    """Every top-level *.md in the memory folder. MEMORY.md (the index Claude
    Code loads each session) is included, marked "index": true."""
    items, files = [], []
    if not mem_dir.is_dir():
        return items, files
    for path in sorted(mem_dir.glob("*.md")):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as e:
            skipped["unreadable_memories"].append(f"{path.name}: {e.__class__.__name__}")
            continue
        files.append(path.name)
        fm, body = _parse_frontmatter(text)
        meta = fm.get("metadata", {})
        base = {
            "file": path.name,
            # MEMORY.md, plus any MEMORY-*.md companion index (e.g. an archive).
            "index": path.name == "MEMORY.md" or path.name.startswith("MEMORY-"),
            "name": fm.get("name", ""),
            "type": meta.get("type", fm.get("type", "")),
            "description": fm.get("description", ""),
        }
        body = body.strip()
        chunks = [body[i:i + MAX_ITEM_CHARS] for i in range(0, len(body), MAX_ITEM_CHARS)] or [""]
        for n, chunk in enumerate(chunks, 1):
            item = dict(base, body=chunk)
            if len(chunks) > 1:
                item["part"] = f"{n}/{len(chunks)}"
            items.append(item)
    return items, files


def _write_shards(items, folder, label):
    if not items:
        return []
    files, batch, size, n = [], [], 2, 1

    def flush():
        p = folder / f"{label}_{n:03d}.json"
        p.write_text(json.dumps(batch, indent=2), encoding="utf-8")
        files.append(str(p))

    for item in items:
        item_size = len(json.dumps(item, indent=2)) + 2
        if batch and size + item_size > MAX_SHARD_CHARS:
            flush()
            n += 1
            batch, size = [], 2
        batch.append(item)
        size += item_size
    if batch:
        flush()
    return files


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--paths-only", action="store_true",
                        help="print the workspace paths and lock state, then exit; changes nothing")
    args = parser.parse_args()

    project_dir = c.resolve_project_dir(args.project_dir)
    ws = c.workspace(c.resolve_data_dir(args.data_dir), project_dir)
    config = c.load_config(ws)
    sess_dir = c.sessions_dir(project_dir)
    mem_dir = c.memory_dir(project_dir)

    if args.paths_only:
        print(json.dumps({
            "workspace": str(ws),
            "report_file": str(ws / c.REPORT_NAME),
            "archive_file": str(ws / c.ARCHIVE_NAME),
            "patterns_file": str(ws / c.PATTERNS_NAME),
            "config_file": str(ws / c.CONFIG_NAME),
            "memory_dir": str(mem_dir),
            "sessions_dir": str(sess_dir),
            "pass_running": c.read_lock(ws),
        }, indent=2))
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    ok, holder = c.acquire_lock(ws, run_id)
    if not ok:
        print(json.dumps({"error": "another dream-review pass is running for this project",
                          "lock": holder, "action": "stop; do not write a report"}))
        return 3

    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=SETTLE_MINUTES)
    start, source = None, None
    if args.hours is not None:
        start, source = end - timedelta(hours=args.hours), "explicit_hours"
    else:
        state = c.read_json(ws / c.STATE_NAME) or {}
        start = c.parse_iso_utc(state.get("last_successful_scan_through")) if isinstance(state, dict) else None
        source = "last_successful_run" if start else None
        if start is None:
            start, source = end - timedelta(hours=24), "default_24h_no_checkpoint"
    if start > end:
        start = end

    skipped = {"unreadable_transcripts": [], "unreadable_memories": [],
               "torn_lines": 0, "torn_last_lines": 0, "malformed_records": 0,
               "bad_timestamps": 0, "meta_records": 0, "truncated_turns": 0}
    turns, sessions, errors = [], set(), []
    if sess_dir.is_dir():
        for path in sess_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError as e:
                skipped["unreadable_transcripts"].append(f"{path.name}: {e.__class__.__name__}")
                continue
            if mtime < start:
                continue  # untouched since before the window: nothing new in it
            _scan_session(path, start, end, int(config["max_turn_chars"]), turns, sessions, skipped)
    else:
        errors.append(f"no session folder found for this project: {sess_dir}")
    turns.sort(key=lambda t: t[0])
    turns = [t[1] for t in turns]
    memories, memory_files = _load_memories(mem_dir, skipped)
    if not mem_dir.is_dir():
        errors.append(f"no auto-memory folder yet: {mem_dir}")

    # This run's shards go in their own folder, so an overlapping reader
    # never sees a half-replaced set; older runs' folders are removed.
    intake_root = ws / "intake"
    intake_root.mkdir(exist_ok=True)
    for old in intake_root.iterdir():
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            try:
                old.unlink()
            except OSError:
                pass
    intake = intake_root / run_id
    intake.mkdir()

    complete = not skipped["unreadable_transcripts"]
    manifest = {
        "run_id": run_id,
        "complete": complete,
        "project_dir": str(project_dir),
        "workspace": str(ws),
        "report_file": str(ws / c.REPORT_NAME),
        "archive_file": str(ws / c.ARCHIVE_NAME),
        "patterns_file": str(ws / c.PATTERNS_NAME),
        "memory_dir": str(mem_dir),
        "memory_files": memory_files,
        "hook_block_log": config["hook_block_log"],
        "archive_threshold_days": config["archive_threshold_days"],
        "run_started_utc": now.strftime("%Y-%m-%d %H:%M"),
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "cutoff_source": source,
        "sessions_scanned": sorted(sessions),
        "turn_count": len(turns),
        "existing_memory_count": len(memory_files),
        "turns_shard_files": _write_shards(turns, intake, "turns"),
        "memories_shard_files": _write_shards(memories, intake, "memories"),
        "skipped": skipped,
        "errors": errors,
        "warning": None if turns else "no session turns in this window (a quiet period, not an error)",
    }
    c.atomic_write(ws / c.MANIFEST_NAME, json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
