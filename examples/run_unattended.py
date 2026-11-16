#!/usr/bin/env python3
"""Example: run the dream-review pass headless, for cron or Task Scheduler.

Runs `claude -p` in your project folder and asks for the pass. Writes a
one-line status log next to this script so you can see that it ran.

Edit PROJECT_DIR, then schedule it, e.g. cron (macOS/Linux):
    30 1 * * *  /usr/bin/python3 /path/to/run_unattended.py
or Windows Task Scheduler: Program = python, Arguments = the full path to this file.

Before relying on it, run it once by hand and check that no permission
prompt was needed (see the README's "Unattended runs" section).
"""
import datetime
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path("/path/to/your/project")  # <- change this
# Optional: a settings file holding the permission rules from the README.
# Passed with --settings, it applies even if the project folder hasn't been
# trusted yet. Set to None if the rules are already in your user settings.
SETTINGS_FILE = None  # e.g. Path("/path/to/dream-review-settings.json")
PROMPT = "Run the dream-review pass."
TIMEOUT_SECONDS = 30 * 60
LOG = Path(__file__).with_suffix(".log")


def log(line):
    stamp = datetime.datetime.now().isoformat(timespec="seconds")
    with LOG.open("a", encoding="utf-8") as f:
        f.write(f"{stamp} {line}\n")


def main():
    if not PROJECT_DIR.is_dir():
        log(f"FAILED: PROJECT_DIR not found: {PROJECT_DIR}")
        return 1
    try:
        cmd = ["claude", "-p"]
        if SETTINGS_FILE:
            cmd += ["--settings", str(SETTINGS_FILE)]
        result = subprocess.run(cmd + [PROMPT], cwd=PROJECT_DIR,
                                capture_output=True, text=True, timeout=TIMEOUT_SECONDS)
    except FileNotFoundError:
        log("FAILED: `claude` not on PATH")
        return 1
    except subprocess.TimeoutExpired:
        log(f"FAILED: timed out after {TIMEOUT_SECONDS}s")
        return 1
    # In -p mode a tool that isn't allowed may be refused rather than waited
    # on, so a missing permission can show up in the output instead of as a
    # hang. Keep the last few lines so a refusal is visible in the log.
    tail = " | ".join((result.stdout or result.stderr or "").strip().splitlines()[-5:])
    log(("OK: " if result.returncode == 0 else f"FAILED (exit {result.returncode}): ") + tail[:800])
    return result.returncode


if __name__ == "__main__":
    sys.exit(main())
