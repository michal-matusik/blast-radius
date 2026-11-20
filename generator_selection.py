"""Narrow generator body edits using calls observed on the pinned revision."""

import re

HUNK = re.compile(rb"@@ -(\d+)(?:,(\d+))? \+\d+(?:,(\d+))? @@(?: .*)?")


def generator_groups(lines, entry):
    """Return touched coverage groups or None for an unsupported patch."""
    source = entry["source"].encode().split(b"\n")
    touched = set()
    result = []
    cursor = old = 0
    old_left = new_left = 0
    in_hunk = False
    removal_spans = []
    for line in lines:
        if line.startswith(b"@@"):
            match = HUNK.fullmatch(line)
            if not match or old_left or new_left:
                return None
            old_start, old_count, new_count = match.groups()
            old_left = int(old_count or 1)
            new_left = int(new_count or 1)
            old = int(old_start) - bool(old_left)
            if not cursor <= old < len(source):
                return None
            result.extend(source[cursor:old])
            cursor = old
            removal_spans.clear()
            in_hunk = True
            continue
        if not in_hunk:
            if not line.startswith((b"index ", b"--- a/", b"+++ b/")):
                return None
            continue
        marker, content = line[:1], line[1:]
        if marker not in (b" ", b"+", b"-"):
            return None
        if marker != b"+" and (old >= len(source) or source[old] != content):
            return None
        if marker != b" ":
            # Body insertions may precede the first statement. A replacement
            # at the end of a body can use the immediately preceding removal.
            position = old + 1
            spans = [
                span for span in entry["spans"]
                if span[0] <= position <= span[1]
            ]
            if marker == b"+" and removal_spans:
                # When a replacement touches multiple scopes, use file fallback.
                if len(removal_spans) != 1:
                    return None
                spans = removal_spans
            if len(spans) != 1:
                return None
            span = spans[0]
            if marker == b"-" and span not in removal_spans:
                removal_spans.append(span)
            if marker == b"+" and content.strip():
                indent = len(content) - len(content.lstrip(b" "))
                if indent < span[2]:
                    return None
            touched.add(span[3])
        else:
            removal_spans.clear()
        if marker != b"+":
            old += 1
            cursor = old
            old_left -= 1
        if marker != b"-":
            result.append(content)
            new_left -= 1
        if min(old_left, new_left) < 0:
            return None
    if old_left or new_left or not touched:
        return None
    result.extend(source[cursor:])
    # A syntax error affects module importers even when they never call the method.
    try:
        compile(b"\n".join(result), "<generator patch>", "exec")
    except (SyntaxError, ValueError):
        return None
    return touched
