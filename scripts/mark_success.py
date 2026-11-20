#!/usr/bin/env python3
"""Finish a pass: save the checkpoint and release the pass lock.

Run it only after the report for that scan has been written. Marking early
would let a failed pass skip its material for good, so it checks: the report
must already contain this run's "Run <run_id>" line, or nothing is saved.

It reads window_end and run_id from the scan manifest, so the command line
stays the same every run (easy to allowlist), and it only acts if that run
still holds the lock (so an overlapping pass can't save someone else's
checkpoint).

It also runs the quote check itself (the same code as verify_quotes.py) on
the report as it is now, and refuses (exit 3, "gate": "quote_check") if
anything blocks: a quote not found, too short or unreadable, or a proposal
with no Evidence line. It doesn't trust an earlier quote_check.json, so a
report edited after the check, or a hand-made check file, can't get through.
The lock stays held, so the pass can fix the report and mark again.

If the scan couldn't read a transcript ("complete": false), the checkpoint
is NOT moved: the next run retries the same window. Pass --accept-incomplete
only when the user has decided to skip those files.

--abandon releases this run's lock without saving a checkpoint (use it when
the pass fails before the report is written).

--heartbeat only resets the lock's age (this run must still hold it), so a
long pass isn't mistaken for a dead one after two hours. It saves nothing.

Usage:
    python mark_success.py [--project-dir P] [--data-dir D]
                           [--accept-incomplete | --abandon | --heartbeat]
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _common as c  # noqa: E402
import verify_quotes  # noqa: E402


def quote_gate(ws, manifest, report):
    """Run the quote check on the report as it is now. Returns a refusal
    reason, or None when nothing blocks."""
    try:
        report_bytes = report.read_bytes()
    except OSError as e:
        return f"could not read {report}: {e}"
    out = verify_quotes.check_report(ws, manifest, report_bytes)
    if "error" in out:
        return f"quote check couldn't run: {out['error']}"
    if out["blocking"]:
        return (f"{out['summary']}. {out['blocking']} item(s) block the checkpoint: run "
                "verify_quotes.py, fix what it flags (copy the words exactly from the turn shards in "
                "the form session <id>, <date>T<time>Z: \"exact words\", add a missing Evidence line, "
                "or drop the proposal), then run this again")
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    c.add_common_args(parser)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--accept-incomplete", action="store_true")
    group.add_argument("--abandon", action="store_true")
    group.add_argument("--heartbeat", action="store_true")
    args = parser.parse_args()
    ws = c.workspace(c.resolve_data_dir(args.data_dir), c.resolve_project_dir(args.project_dir))

    manifest = c.read_json(ws / c.MANIFEST_NAME)
    if not isinstance(manifest, dict) or not manifest.get("window_end") or not manifest.get("run_id"):
        print(json.dumps({"error": f"no usable scan manifest in {ws}; run scan.py first"}))
        return 1
    run_id = manifest["run_id"]
    lock = c.read_json(ws / c.LOCK_NAME)
    if not isinstance(lock, dict) or lock.get("run_id") != run_id:
        print(json.dumps({"error": "this scan no longer holds the pass lock (another pass "
                                   "started, or the lock went stale); checkpoint NOT saved",
                          "run_id": run_id}))
        return 1

    if args.heartbeat:
        if not c.refresh_lock(ws, run_id):
            print(json.dumps({"error": "lock not refreshed (no longer held by this run)", "run_id": run_id}))
            return 1
        print(json.dumps({"heartbeat": run_id}))
        return 0

    if args.abandon:
        c.release_lock(ws, run_id)
        print(json.dumps({"abandoned": run_id, "checkpoint": "unchanged"}))
        return 0

    # Proof the report was written: it must hold this run's "Run <run_id>" line.
    report = ws / c.REPORT_NAME
    try:
        has_section = f"Run {run_id}" in report.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        has_section = False
    if not has_section:
        print(json.dumps({"error": f"{report} has no section for run {run_id} yet; checkpoint NOT saved. "
                                   "Write the report first and run this again, or run it with --abandon "
                                   "if the pass can't finish.", "run_id": run_id}))
        return 1

    if not manifest.get("complete", False) and not args.accept_incomplete:
        c.release_lock(ws, run_id)
        print(json.dumps({"checkpoint": "unchanged",
                          "reason": "scan was incomplete (unreadable transcripts or memory files); "
                                    "the next run retries this window",
                          "unreadable": manifest.get("skipped", {}).get("unreadable_transcripts", [])
                                        + manifest.get("skipped", {}).get("unreadable_memories", [])}))
        return 0

    reason = quote_gate(ws, manifest, report)
    if reason:
        print(json.dumps({"refused": True, "gate": "quote_check", "reason": reason,
                          "checkpoint": "unchanged", "lock": "still held by this run",
                          "run_id": run_id}))
        return 3

    state_file = ws / c.STATE_NAME
    state = {"last_successful_scan_through": manifest["window_end"], "run_id": run_id}
    if manifest.get("sources_fingerprint"):
        # So a later change of sources triggers a reconciliation window.
        state["sources_fingerprint"] = manifest["sources_fingerprint"]
    daily = manifest.get("daily_notes")
    if isinstance(daily, dict) and daily.get("daily_through_next"):
        # Daily notes' own marker: dates from here on aren't written up yet.
        state["daily_through"] = daily["daily_through_next"]
    c.atomic_write(state_file, json.dumps(state, indent=2))
    c.release_lock(ws, run_id)
    print(json.dumps({"last_successful_scan_through": manifest["window_end"],
                      "state_file": str(state_file)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
