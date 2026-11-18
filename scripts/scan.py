#!/usr/bin/env python3
"""dream-review scanner: mechanical extraction only.

Reads this project's recent Claude Code session transcripts and its existing
auto-memory files and writes them as JSON shards the dream-review skill can
Read. It drafts no proposals, calls no model and never touches a memory file.
It writes only inside its own workspace (<data-dir>/projects/<slug>/): the
pass lock, the manifest, this run's intake folder, and it removes earlier
runs' intake folders once they're over a day old. With --paths-only it
writes nothing at all.

What it keeps from a transcript: the visible text of "user" and "assistant"
turns. Tool calls, tool results, images, thinking blocks and meta records
(text Claude Code injects, such as loaded skill bodies) are dropped on
purpose. Subagent transcripts (in per-session subfolders) are not read.
Recognisable secrets (keys, tokens, "password=" values) in turn text and
native memories are replaced with [REDACTED:<kind>] (see _redact.py); vault
and daily-note text is passed through as is, since edits are applied to it.

Nothing is skipped silently: unreadable files, malformed records, torn lines
and cut-off turns are all counted in the manifest's "skipped" block. If a
transcript or a memory file couldn't be read, the manifest says
"complete": false and
mark_success.py refuses to move the checkpoint past it.

The window ends 10 minutes before "now", so turns still being written (the
running pass's own session, for one) are left for the next run instead of
being cut off mid-line and then skipped for good.

Usage:
    python scan.py [--project-dir P] [--data-dir D] [--hours N] [--paths-only]

Without --hours the window starts at the last successful run's checkpoint
(written by mark_success.py), so a missed night is picked up by the next run
that succeeds. With no checkpoint yet (the first pass) it reads back
`first_run_days` (config, default 7; 0 = all history).
"""
import argparse
import hashlib
import json
import shutil
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402
import _vault as v  # noqa: E402
from _redact import redact  # noqa: E402
from _shards import write_shards  # noqa: E402

# Shard size limits and splitting live in _shards.py.
# Intake folders from earlier runs are removed once older than this; a newer
# one may belong to a pass whose lock was taken over as stale but which is
# still reading its shards.
INTAKE_KEEP_HOURS = 24
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


def _scan_session(path, start, end, max_chars, turns, sessions, skipped, redactions):
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
        # Redact before the cut, so a key straddling it can't survive as a
        # partial match.
        text = redact("\n".join(parts), redactions)
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


def _load_memories(mem_dir, skipped, redactions):
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
            "description": redact(fm.get("description", ""), redactions),
        }
        items.append(dict(base, body=redact(body.strip(), redactions)))
    return items, files


_write_shards = write_shards


def _source_summary(src):
    if src["type"] == "native":
        return {"id": src["id"], "type": "native"}
    return {"id": src["id"], "type": "vault", "root": str(src["root"]),
            "core": src["core"], "exclude": v.DEFAULT_EXCLUDES + src["exclude"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    parser.add_argument("--hours", type=float, default=None)
    parser.add_argument("--paths-only", action="store_true",
                        help="print the workspace paths and lock state, then exit; changes nothing")
    parser.add_argument("--suggest-setup", action="store_true",
                        help="look at the memory setup and print a draft sources config; changes nothing")
    args = parser.parse_args()

    project_dir = c.resolve_project_dir(args.project_dir)
    data_dir = c.resolve_data_dir(args.data_dir)
    read_only = args.paths_only or args.suggest_setup
    # --paths-only and --suggest-setup create nothing, not even the workspace folder.
    ws = c.workspace_path(data_dir, project_dir) if read_only else c.workspace(data_dir, project_dir)
    config, problems = v.read_config(ws)
    sources, source_problems = v.resolve_sources(config, ws)
    problems += source_problems
    daily, daily_problems = v.resolve_daily(config, sources) if not source_problems else (None, [])
    problems += daily_problems
    sess_dir = c.sessions_dir(project_dir)
    mem_dir = c.memory_dir(project_dir)

    if args.paths_only:
        print(json.dumps({
            "workspace": str(ws),
            "report_file": str(ws / c.REPORT_NAME),
            "archive_file": str(ws / c.ARCHIVE_NAME),
            "patterns_file": str(ws / c.PATTERNS_NAME),
            "config_file": str(ws / c.CONFIG_NAME),
            "backups_dir": str(ws / "backups"),
            "memory_dir": str(mem_dir),
            "sessions_dir": str(sess_dir),
            "sources": [_source_summary(s) for s in sources],
            "rules_note": config["rules_note"],
            "templates": config["templates"],
            "routing": config["routing"],
            "config_problems": problems,
            "pass_running": c.read_lock(ws),
        }, indent=2))
        return 0

    if args.suggest_setup:
        out = v.suggest_setup(project_dir, mem_dir)
        out["config_file"] = str(ws / c.CONFIG_NAME)
        out["current_config_problems"] = problems
        print(json.dumps(out, indent=2))
        return 0

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:6]
    ok, holder = c.acquire_lock(ws, run_id)
    if not ok:
        print(json.dumps({"error": "another dream-review pass is running for this project",
                          "lock": holder, "action": "stop; do not write a report"}))
        return 3

    if problems:
        # Never fall back to native-only without saying so: stop, report.
        c.release_lock(ws, run_id)
        print(json.dumps({"error": "config_problem", "config_problems": problems,
                          "config_file": str(ws / c.CONFIG_NAME),
                          "report_file": str(ws / c.REPORT_NAME),
                          "action": "write a Config problem section (see SKILL.md); no checkpoint"}, indent=2))
        return 4

    fp = v.fingerprint(sources)
    if daily:
        # Turning daily notes on (or changing them) re-reads the first-run
        # window too; with it off the fingerprint is the same as in 0.2.0.
        fp += "-d" + hashlib.sha1(json.dumps(config["daily_notes"], sort_keys=True).encode("utf-8")).hexdigest()[:6]
    now = datetime.now(timezone.utc)
    end = now - timedelta(minutes=SETTLE_MINUTES)
    start, source = None, None
    if args.hours is not None:
        start, source = end - timedelta(hours=args.hours), "explicit_hours"
    else:
        state = c.read_json(ws / c.STATE_NAME) or {}
        start = c.parse_iso_utc(state.get("last_successful_scan_through")) if isinstance(state, dict) else None
        source = "last_successful_run" if start else None
        # Sources changed since the checkpoint (a checkpoint from before
        # 0.2.0 counts as the native default): compare the first-run window
        # again against the new sources.
        if start is not None and state.get("sources_fingerprint", v.DEFAULT_FINGERPRINT) != fp:
            start, source = None, "sources_changed"
        if start is None:
            days = c.first_run_days(config)
            prefix = "sources_changed" if source == "sources_changed" else "first_run"
            if days == 0:
                start, source = datetime(1970, 1, 1, tzinfo=timezone.utc), f"{prefix}_all_history"
            else:
                start, source = end - timedelta(days=days), f"{prefix}_{days:g}d"
    if start > end:
        start = end

    # Daily notes keep their own progress marker: a date is only written up
    # once the window has passed that date's local midnight, so a pass during
    # the day (or one cut off just before midnight) leaves the date open and
    # the next pass reads back to the marker. Turns read only for that reason
    # are flagged daily_only: memory proposals ignore them.
    read_start = start
    if daily and source == "last_successful_run":
        state = c.read_json(ws / c.STATE_NAME) or {}
        marker = c.parse_iso_utc(state.get("daily_through")) if isinstance(state, dict) else None
        if marker is not None and marker < start:
            read_start = marker

    skipped = {"unreadable_transcripts": [], "unreadable_memories": [], "unreadable_daily_notes": [],
               "torn_lines": 0, "torn_last_lines": 0, "malformed_records": 0,
               "bad_timestamps": 0, "meta_records": 0, "truncated_turns": 0}
    turns, sessions, errors, redactions = [], set(), [], {}
    if sess_dir.is_dir():
        for path in sess_dir.glob("*.jsonl"):
            try:
                mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            except OSError as e:
                skipped["unreadable_transcripts"].append(f"{path.name}: {e.__class__.__name__}")
                continue
            if mtime < read_start:
                continue  # untouched since before the window: nothing new in it
            _scan_session(path, read_start, end, int(config["max_turn_chars"]), turns, sessions, skipped,
                          redactions)
    else:
        errors.append(f"no session folder found for this project: {sess_dir}")
    turns.sort(key=lambda t: t[0])
    daily_only = 0
    for ts, item in turns:
        if ts < start:
            item["daily_only"] = True
            daily_only += 1
    # Sessions seen only through daily-only turns aren't new for memory.
    sessions = {item["sessionId"] for ts, item in turns if ts >= start}

    # Daily notes (0.3.0): which local date each turn falls on, and for every
    # settled past date (never today: a live session may still be writing
    # that note) a copy of the day's note, or "missing", plus the dream-review
    # entry ids already in it so nothing is proposed twice.
    daily_info, daily_items = {"enabled": False}, []
    if daily:
        today = v.local_date(now, daily["tz"])
        # Dates before the window end's local date are complete: every turn
        # of theirs is at or before window_end. Later dates wait.
        settled_before = v.local_date(end, daily["tz"])
        midnight = datetime(settled_before.year, settled_before.month, settled_before.day,
                            tzinfo=daily["tz"]) if daily["tz"] else \
            datetime(settled_before.year, settled_before.month, settled_before.day).astimezone()
        daily_through_next = midnight.astimezone(timezone.utc)
        by_date = {}
        for ts, item in turns:
            d = v.local_date(ts, daily["tz"])
            item["local_date"] = d.isoformat()
            item["local_time"] = (ts.astimezone(daily["tz"]) if daily["tz"] else ts.astimezone()).strftime("%H:%M")
            if d < settled_before:
                by_date.setdefault(d, []).append(item)
        dates = []
        for d in sorted(by_date):
            rel = v.daily_rel_path(daily["pattern"], d)
            path = v.note_path(daily["source"], rel)
            # Not "sessions": that name is the pass's own set of sessions.
            day_sessions = sorted({t["sessionId"] for t in by_date[d]})
            entry = {"date": d.isoformat(), "path": rel, "sessions": day_sessions,
                     "first_turn": by_date[d][0]["local_time"], "last_turn": by_date[d][-1]["local_time"]}
            if path is None:
                # Not "missing": never proposed, only reported.
                entry.update(exists=None, blocked="path not allowed (excluded or outside the vault)")
            elif path.is_file():
                text, sha, err = v.read_note(path)
                if err:
                    entry.update(exists=True, unreadable=err)
                    skipped["unreadable_daily_notes"].append(f"{rel}: {err}")
                else:
                    entry.update(exists=True, sha=sha,
                                 already_logged=sorted(i for i in v.logged_ids(text) if i.endswith(d.isoformat())))
                    daily_items.append({"date": d.isoformat(), "path": rel, "sha": sha, "text": text})
            else:
                entry.update(exists=False)
            dates.append(entry)
        if daily["create_missing"] and any(e["exists"] is False for e in dates):
            tsid, trel = daily["template"]
            tsrc = next(s for s in sources if s["id"] == tsid)
            ttext, tsha, terr = v.read_note(v.note_path(tsrc, trel))
            if terr:
                # Counted, so the scan is incomplete and the dates are retried.
                skipped["unreadable_daily_notes"].append(f"template {tsid}:{trel}: {terr}")
            else:
                daily_items.append({"template": f"{tsid}:{trel}", "sha": tsha, "text": ttext})
        daily_info = {"enabled": True, "source": daily["source"]["id"], "timezone": daily["tz_name"],
                      "today_local": today.isoformat(), "settled_before": settled_before.isoformat(),
                      "daily_through_next": daily_through_next.isoformat(),
                      "daily_only_turns": daily_only, "create_missing": daily["create_missing"],
                      "template": f"{daily['template'][0]}:{daily['template'][1]}" if daily["template"] else None,
                      "dates": dates}

    turns = [t[1] for t in turns]
    skipped.update({"unreadable_core_notes": [], "unreadable_notes": []})

    # Native memory: compared unless MEMORY.md is a redirect and the user
    # hasn't configured sources yet (then proposing native memories would
    # rebuild the layer they emptied: the report asks for setup instead).
    redirect = v.detect_redirect(mem_dir)
    has_native = any(s["type"] == "native" for s in sources)
    memories, memory_files = [], []
    if not has_native:
        native_comparison = "not_configured"
    elif redirect["status"] != "none" and config["sources"] is None:
        native_comparison = "suppressed_redirect"
    else:
        native_comparison = "full"
        memories, memory_files = _load_memories(mem_dir, skipped, redactions)
        if not mem_dir.is_dir():
            errors.append(f"no auto-memory folder yet: {mem_dir}")
    setup_needed = native_comparison == "suppressed_redirect"

    # Vault sources: a compact index of every included note, core notes in
    # full. Nothing else is copied; find.py searches and fetches on demand.
    index_items, core_items, source_info = [], [], []
    for src in sources:
        if src["type"] != "vault":
            source_info.append({"id": src["id"], "type": "native", "memory_dir": str(mem_dir),
                                "comparison": native_comparison})
            continue
        core = set(src["core"])
        count = 0
        for rel, path in v.iter_notes(src, ws):
            text, sha, err = v.read_note(path)
            if err:
                key = "unreadable_core_notes" if rel in core else "unreadable_notes"
                skipped[key].append(f"{src['id']}:{rel}: {err}")
                continue
            count += 1
            index_items.append({"source": src["id"], "path": rel, "headings": v.headings(text),
                                "bytes": len(text.encode("utf-8")), "sha": sha})
            if rel in core:
                core_items.append({"source": src["id"], "path": rel, "sha": sha, "text": text})
        source_info.append({"id": src["id"], "type": "vault", "root": str(src["root"]),
                            "core": src["core"], "exclude": v.DEFAULT_EXCLUDES + src["exclude"],
                            "notes_indexed": count})

    # This run's shards go in their own folder, so an overlapping reader
    # never sees a half-replaced set. Earlier runs' folders are removed only
    # once older than INTAKE_KEEP_HOURS: a pass whose lock this run took over
    # as stale may still be reading its own.
    intake_root = ws / "intake"
    intake_root.mkdir(exist_ok=True)
    keep_after = now.timestamp() - INTAKE_KEEP_HOURS * 3600
    for old in intake_root.iterdir():
        try:
            if old.stat().st_mtime >= keep_after:
                continue
        except OSError:
            continue
        if old.is_dir():
            shutil.rmtree(old, ignore_errors=True)
        else:
            try:
                old.unlink()
            except OSError:
                pass
    intake = intake_root / run_id
    intake.mkdir()

    # Unreadable memories make the comparison incomplete too: the checkpoint
    # holds, so the window is compared again once they can be read.
    # Unreadable core notes too (a configured source that can't be read at
    # all already stopped the pass as a config problem). Other unreadable
    # vault notes are listed, not blocking: one bad file in a big vault
    # mustn't freeze the checkpoint forever.
    complete = not (skipped["unreadable_transcripts"] or skipped["unreadable_memories"]
                    or skipped["unreadable_core_notes"] or skipped["unreadable_daily_notes"])
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
        "turns_shard_files": _write_shards(turns, intake, "turns", "text"),
        "memories_shard_files": _write_shards(memories, intake, "memories", "body"),
        "sources": source_info,
        "sources_fingerprint": fp,
        "native_status": redirect,
        "native_comparison": native_comparison,
        "setup_needed": setup_needed,
        "suggested_setup": v.suggest_setup(project_dir, mem_dir) if setup_needed else None,
        "core_shard_files": _write_shards(core_items, intake, "core", "text"),
        "vault_index_shard_files": _write_shards(index_items, intake, "index", "path"),
        "find_limits": {"max_searches": config["max_searches"], "max_fetches": config["max_fetches"]},
        "daily_notes": daily_info,
        "daily_shard_files": _write_shards(daily_items, intake, "daily", "text"),
        "routing": config["routing"],
        "skipped": skipped,
        # Secrets replaced in turns and native memories, by kind (no values).
        "redactions": redactions,
        "errors": errors,
        "warning": None if turns else "no session turns in this window (a quiet period, not an error)",
    }
    c.atomic_write(ws / c.MANIFEST_NAME, json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
