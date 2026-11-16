# dream-review

A "dreaming" pass for Claude Code's auto-memory that **proposes changes and never applies them on its own.**

Each run reads the project's recent sessions, compares them with your existing memory files, and writes a report of proposals:

- **New memories:** things you said or decided that will matter later but aren't in memory yet.
- **Stale memories:** memories a recent session contradicts or supersedes.
- **Duplicates and conflicts:** memories that overlap or disagree.
- **Skill candidates:** the same kind of work coming up three or more times across nights, with no skill covering it.
- **Rule candidates** (optional): one of your own blocking hooks firing three or more times in two weeks on the same thing, with a fix that would stop the attempt earlier.

Every proposal quotes its evidence: the session, the timestamp and the exact words. Nothing touches your memory until you review the report and approve.

## Why review-first

Recent Claude Code builds appear to include an automatic background memory consolidation option. It isn't documented yet and is switched on from Anthropic's side, so check your own `/memory` screen to see whether you have it. As far as we can tell, that kind of option updates memory itself.

dream-review is for people who want to see each change, and the reason for it, before it lands. Every proposal comes with the quote that prompted it, you approve or decline each one, and the dated report keeps a record of what was proposed and what you decided.

The two can coexist. If both run, memory may change between a dream-review pass and your review of it, so the review re-checks every proposal against the current files before applying it.

## Install

```
/plugin marketplace add techmik/dream-review
/plugin install dream-review@dream-review
```

Needs Python 3.8+ on your PATH as `python3` or `python` (standard library only). On Windows, Claude Code should be using Git Bash for the start-of-session hook.

## Use

- **Run a pass:** "run dream-review" (or "run the dream pass"). Takes a minute or two; ends with counts per category and a line confirming no memory file changed.
- **Review:** "review my dreams". Claude walks you through the unreviewed proposals, re-checks each one, takes a git snapshot of the memory folder if it's a repo, applies only what you approve, and marks the section Reviewed.
- **Reminder:** at the start of each session the plugin names any unreviewed nights for that project. Silent when there's nothing pending.

The first pass in a project looks back 24 hours. After that each pass starts where the last successful one ended, so a missed night is picked up by the next run. The last 10 minutes are always left for the next pass, so a session that's still being written isn't read half-finished.

## Where things live

All working files are kept per project in the plugin's data folder, so a plugin update never wipes them:

```
~/.claude/plugins/data/<plugin-id>/projects/<project>/
  dream_report.md          the report (newest night first)
  dream_report_archive.md  reviewed sections older than 7 days
  skill_patterns.json      tags per night, for skill candidates
  dream_state.json         checkpoint
  scan_manifest.json, intake/   the last scan's output
  config.json              optional settings (below)
```

Ask "where is my dream report?" and Claude will print the exact paths. The scanner finds memory the way Claude Code does: `autoMemoryDirectory` if you set one, otherwise the project's default memory folder.

These files contain text from your sessions. Keep them out of any repo you share.

## Settings (optional)

Create `config.json` in the project's workspace folder:

```json
{
  "hook_block_log": "~/.claude/hook_blocks.log",
  "archive_threshold_days": 7,
  "max_turn_chars": 4000
}
```

- `hook_block_log`: turns on rule candidates. A tab-separated file with one line per block: `<utc-iso-time> <hook> <tool> <reason-label> <session-id>`. `examples/log_hook_block.sh` shows a helper your hooks can call.
- `archive_threshold_days`: how long a reviewed section stays in the live report.
- `max_turn_chars`: longer turns are cut to this length. The report says how many were cut.

## Unattended runs

A pass can run on a schedule, but only if it never stops at a permission prompt. Three ways:

1. **On request only:** no setup.
2. **Claude Desktop scheduled task:** create a daily task in the project with the prompt `Run the dream-review pass.`
3. **Headless `claude -p` from cron or Task Scheduler:** see `examples/run_unattended.py`.

The skill pre-approves its own script commands. For the file reads and writes, add rules like these to the project's `.claude/settings.json` (or your user settings):

```json
{
  "permissions": {
    "allow": [
      "Read(~/.claude/plugins/data/dream-review*/**)",
      "Write(~/.claude/plugins/data/dream-review*/**)",
      "Edit(~/.claude/plugins/data/dream-review*/**)",
      "Read(~/.claude/skills/**)",
      "Read(.claude/skills/**)"
    ]
  }
}
```

The folder name after `data/` depends on how the plugin was installed; "where is my dream report?" prints the exact path, and you can use that instead of the wildcard. Add `Read(...)` for your `hook_block_log` if you use one. Then run the pass once by hand in a fresh session and check that it finished without asking you anything.

Only one pass runs per project at a time. A second one that starts while the first is still going stops without writing anything. If a pass dies partway, its lock clears itself after two hours.

## What the pass never does

- Write, edit or delete a memory file or `MEMORY.md`.
- Edit CLAUDE.md or settings, or build a skill. Those come back as proposals with exact text.
- Read thinking blocks, tool results or subagent transcripts. Only the visible text of your turns and Claude's replies is scanned.
- Drop anything silently. Unreadable files, malformed lines and cut-off turns are counted in the report. If a transcript can't be read, the checkpoint stays put and the next run tries again.
- Follow instructions it finds in your sessions or memory. Those are treated as data.

## Undo

If your memory folder is a git repo, the review takes a snapshot commit before applying anything. To make it one:

```
cd <your memory folder>
git init
git add -A
git commit -m "start"
```

The plugin never runs `git init`, adds a remote or pushes.

## Credits

By techmik & Claude. Inspired by Anthropic's "dreaming" idea for memory. MIT license.
