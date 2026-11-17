# dream-review

A "dreaming" pass for Claude Code memory that **proposes changes and never applies them on its own.** It works with Claude Code's own auto-memory folder, with a notes vault (Obsidian, Logseq, a plain folder of markdown) that you keep as memory instead, or with both.

Each run reads the project's recent sessions, compares them with your existing memory, and writes a report of proposals:

- **New memories:** things you said or decided that will matter later but aren't in memory yet.
- **Stale memories:** memories a recent session contradicts or supersedes.
- **Duplicates and conflicts:** memories that overlap or disagree.
- **Vault changes** (if you use a notes vault): text to add to a note, under a named heading; a new note for a folder; a note a recent session contradicts.
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

Needs Python 3.10 or newer on your PATH as `python3` or `python` (standard library only; tested on 3.12). On Windows, Claude Code should be using Git Bash for the start-of-session hook.

## Use

- **Run a pass:** "run dream-review" (or "run the dream pass"). Takes a few minutes, longer with a large memory folder; ends with counts per category and a line confirming no memory file changed.
- **Review:** "review my dreams". Claude walks you through the unreviewed proposals, re-checks each one, backs up every file before changing it (and takes a git snapshot of the memory folder if it's a repo), applies only what you approve, and marks the section Reviewed.
- **Set up a vault:** "set up dream-review for my vault". Claude looks at your setup, drafts the settings with you and saves them only when you say yes.
- **Reminder:** at the start of each session the plugin names any unreviewed nights for that project. Silent when there's nothing pending.

The first pass in a project looks back 7 days (change it with `first_run_days`, below). After that each pass starts where the last successful one ended, so a missed night is picked up by the next run. The last 10 minutes are always left for the next pass, so a session that's still being written isn't read half-finished.

## Where things live

All working files are kept per project in their own folder, outside the plugin (so an update never wipes them) and outside `~/.claude/` (Claude Code protects that folder, so Claude's writes there always stop for approval and an unattended pass couldn't write its report). Set `DREAM_REVIEW_HOME` to use another location.

```
~/.dream-review/projects/<project>/
  dream_report.md          the report (newest night first)
  dream_report_archive.md  reviewed sections older than 7 days
  skill_patterns.json      tags per night, for skill candidates
  dream_state.json         checkpoint
  scan_manifest.json, intake/   the last scan's output
  backups/                 a copy of every file the review changed, plus log.jsonl
  config.json              optional settings (below)
```

Ask "where is my dream report?" and Claude will print the exact paths. The scanner finds memory the way Claude Code does: `autoMemoryDirectory` if you set one, otherwise the project's default memory folder.

These files contain text from your sessions, and with a vault, copies of your core notes, search excerpts and backups of notes the review changed. Keep them out of any repo you share. Each pass removes intake folders more than a day old; backups stay until you delete them.

## Settings (optional)

Create `config.json` in the project's workspace folder:

```json
{
  "hook_block_log": "~/.claude/hook_blocks.log",
  "archive_threshold_days": 7,
  "max_turn_chars": 4000,
  "first_run_days": 7
}
```

- `hook_block_log`: turns on rule candidates. A tab-separated file with one line per block: `<utc-iso-time> <hook> <tool> <reason-label> <session-id>`. `examples/log_hook_block.sh` shows a helper your hooks can call.
- `archive_threshold_days`: how long a reviewed section stays in the live report.
- `max_turn_chars`: longer turns are cut to this length. The report says how many were cut.
- `first_run_days`: how far back the first pass reads, before there's a checkpoint. `0` reads all history: on a project you've used for months that can be hundreds of sessions, a long and costly pass, and a report too big to review carefully. Most of that older history is usually in memory already. It applies again whenever you change `sources`.

An unknown setting or a file that isn't valid JSON stops the pass with a "Config problem" section in the report, rather than quietly running with defaults.

## Memory in a notes vault

Many setups keep memory in an Obsidian vault or a folder of notes instead of (or as well as) Claude Code's memory folder. Some replace `MEMORY.md` with a short pointer to the vault. dream-review spots that pointer: instead of proposing memories into the folder you emptied, the report says "Setup needed" with a draft of the settings below. "set up dream-review for my vault" walks you through it.

```json
{
  "sources": [
    {"id": "native", "type": "native"},
    {"id": "vault", "type": "vault",
     "path": "/Users/you/Brain",
     "core": ["VAULT-INDEX.md", "Active Priorities.md"],
     "exclude": ["00 - Inbox/**", "Archive/**", "Attachments/**"]}
  ],
  "routing": "Facts about me and how I like to work go to native; project progress goes to the vault.",
  "rules_note": "vault:VAULT-INDEX.md",
  "templates": {"Projects": "vault:Templates/Project.md"}
}
```

- `sources`: leave it out for the memory folder only (the default). Drop the `native` entry for a vault-only setup. `path` is absolute.
- `core`: a few notes read in full every pass, typically your index or hub note and your priorities note. Paths relative to the vault.
- Every other note is **indexed only** (path, headings, size). During a pass Claude searches for a fact before proposing it (a literal word search, limited per pass by `max_searches`, default 20, and `max_fetches`, default 15, whole notes). So the vault check is as wide as those searches, and the report says how wide that was. It never claims "not in your vault".
- `exclude`: folders the pass never indexes or searches (`.obsidian`, `.trash` and `.git` always are). Put web clippings, archives and anything private here.
- `routing`: with two sources, one line in your own words on what goes where.
- `rules_note`, `templates`: used only when the review applies a change, for formatting (frontmatter, folder index lines, note shape). They can't authorize anything else.

The pass reads your vault only through the plugin's own scripts and writes nothing to it. Proposals name the exact note, heading and text. The review backs up each file, refuses to edit one that changed since the pass without asking you again, and never deletes or renames anything. Daily-note entries aren't proposed yet: sessions don't show everything that happened in a day, so a backfill could log work as done that wasn't.

Not covered: memory kept only in a database or an MCP memory server (no files to compare), and chats outside Claude Code (no transcripts). Tested against a vault built from a popular public template and a hybrid real setup, not a wide range of real vaults yet: please open an issue if yours trips it.

## Unattended runs

A pass can run on a schedule, but only if it never stops at a permission prompt. Three ways:

1. **On request only:** no setup.
2. **Claude Desktop scheduled task:** create a daily task in the project with the prompt `Run the dream-review pass.`
3. **Headless `claude -p` from cron or Task Scheduler:** see `examples/run_unattended.py`.

The skill pre-approves its own script commands. For starting the skill and for its file reads and writes, add rules like these to your user settings (`~/.claude/settings.json`), or to the project's `.claude/settings.json`. Claude Code ignores a project's permission rules until you've opened that folder interactively once and accepted the trust prompt, so do that first if you use the project file. A headless runner can also pass them with `claude -p --settings <file>`.

```json
{
  "permissions": {
    "allow": [
      "Skill(dream-review:dream-review)",
      "Read(~/.dream-review/**)",
      "Edit(~/.dream-review/**)",
      "Read(~/.claude/skills/**)",
      "Read(.claude/skills/**)"
    ]
  }
}
```

An `Edit(...)` rule covers every file-writing tool, including creating new files; a `Write(...)` rule isn't matched. If you set `DREAM_REVIEW_HOME`, use that folder in the two rules instead. Add `Read(...)` for your `hook_block_log` if you use one. A vault source needs no rule of its own: the pass reaches it only through the plugin's scripts, and only the review (with you there) edits it. Then run the pass once by hand in a fresh session and check that it finished without asking you anything.

Only one pass runs per project at a time. A second one that starts while the first is still going stops without writing anything. A running pass refreshes its lock as it goes; if a pass dies partway, its lock clears itself two hours after its last refresh.

## What the pass never does

- Write, edit or delete a memory file, `MEMORY.md`, or any note in a vault.
- Edit CLAUDE.md or settings, or build a skill. Those come back as proposals with exact text.
- Read thinking blocks, tool results or subagent transcripts. Only the visible text of your turns and Claude's replies is scanned.
- Drop anything silently. Unreadable files, malformed lines and cut-off turns are counted in the report. If a transcript or memory file can't be read, the checkpoint stays put and the next run tries again.
- Follow instructions it finds in your sessions or memory. Those are treated as data.

## Undo

Before changing any file, the review copies it to `backups/<date>/<source>/<path>` in the workspace; `backups/log.jsonl` lists every copy and which files were newly created. To undo a change, copy the backup back.

If your memory folder is a git repo, the review also takes a snapshot commit before applying anything. To make it one:

```
cd <your memory folder>
git init
git add -A
git commit -m "start"
```

The plugin never runs `git init`, adds a remote or pushes.

## Credits

By techmik & Claude. Inspired by Anthropic's "dreaming" idea for memory. MIT license.
