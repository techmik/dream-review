"""Bounded JSON shard writer shared by scan.py and find.py (stdlib only).

Every shard file, as written, stays under BOTH limits so one default Read
call fits, with room under read-size gates as low as 40 KB. Characters
predict tokens; bytes are what a size gate measures (non-ASCII text takes
2-4 bytes per character in UTF-8). An item too big for one shard on its own
is split into numbered parts ("part": "2/3") on its text field.
"""
import json

MAX_SHARD_CHARS = 30000
MAX_SHARD_BYTES = 36000


def dump(obj):
    # ensure_ascii=False: non-ASCII text stays as itself (UTF-8), not \uXXXX
    # escapes that make a shard up to six times longer than its content.
    return json.dumps(obj, indent=2, ensure_ascii=False)


def _entry_size(item):
    """(chars, bytes) one item adds inside a shard's JSON list: its own
    serialization, every line indented two more spaces."""
    s = dump(item)
    extra = 2 * (s.count("\n") + 1)
    return len(s) + extra, len(s.encode("utf-8")) + extra


def _fits(sizes):
    """Whether json.dumps(list, indent=2) of items of these sizes fits:
    "[\\n" + items joined by ",\\n" + "\\n]"."""
    overhead = 4 + 2 * (len(sizes) - 1)
    chars = sum(s[0] for s in sizes) + overhead
    nbytes = sum(s[1] for s in sizes) + overhead
    return chars <= MAX_SHARD_CHARS and nbytes <= MAX_SHARD_BYTES


def split_item(item, field):
    """Split item[field] into numbered parts that each fit a shard alone."""
    if _fits([_entry_size(item)]):
        return [item]
    text, pieces, pos = item[field], [], 0
    while pos < len(text):
        # Largest slice from pos that fits (binary search); the "part" label
        # is sized at its widest so relabelling below can't overflow.
        lo, hi = 1, len(text) - pos
        while lo < hi:
            mid = (lo + hi + 1) // 2
            probe = dict(item, **{field: text[pos:pos + mid]}, part="99999/99999")
            if _fits([_entry_size(probe)]):
                lo = mid
            else:
                hi = mid - 1
        pieces.append(text[pos:pos + lo])
        pos += lo
    total = len(pieces)
    return [dict(item, **{field: p}, part=f"{n}/{total}") for n, p in enumerate(pieces, 1)]


def write_shards(items, folder, label, field):
    """Write items as JSON-list shards <label>_NNN.json that each stay under
    both limits. Returns the list of file paths (as strings)."""
    pieces = [p for item in items for p in split_item(item, field)]
    files, batch, sizes = [], [], []

    def flush():
        p = folder / f"{label}_{len(files) + 1:03d}.json"
        # Bytes, not write_text: on Windows text mode turns every "\n" into
        # "\r\n", which would push a full shard past its limits.
        p.write_bytes(dump(batch).encode("utf-8"))
        files.append(str(p))

    for piece in pieces:
        size = _entry_size(piece)
        if batch and not _fits(sizes + [size]):
            flush()
            batch, sizes = [], []
        batch.append(piece)
        sizes.append(size)
    if batch:
        flush()
    return files
