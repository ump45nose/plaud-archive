from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import quote


SPEAKER = re.compile(r"(?:说话人|[Ss]peaker)\s*([A-Z]+)(?:\s*[（(][^）)\n]*[）)])?")
LABEL = re.compile(r"^(\s*)\*\*([^*\n]+?)[：:]\*\*")
HEADING = re.compile(r"(?m)^(##[ \t]+([^\r\n]+))(?:\r?\n|$)(?:[ \t]*\r?\n)*"
                     r"(?:\[音频\]\((audio/[^\r\n)]+)\)(?:\r?\n|$)(?:[ \t]*\r?\n)*)?")


def title(value): return re.sub(r"^录音[：:]\s*", "", value).strip()


def speaker_labels(markdown):
    return re.sub(r"(?m)^(\s*)(?:\*\*)?(?:说话人|[Ss]peaker)\s*([A-Z]+)"
                  r"(?:\s*[（(][^）)\n]*[）)])?\s*[：:](?:\*\*)?",
                  r"\1**Speaker \2:**", markdown)


def legacy_speaker_labels(markdown, keep_labels=()):
    """One-time display conversion; preserve existing distinctions, never infer identities."""
    sections, section = {}, ""
    for line in markdown.splitlines():
        if line.startswith("## "): section = title(line[3:])
        labels = sections.setdefault(section, {})
        match = LABEL.match(line)
        if match and (known := SPEAKER.fullmatch(match[2])): labels[match[2]] = known[1]
    output, section = [], ""
    for line in markdown.splitlines(keepends=True):
        if line.startswith("## "): section = title(line[3:])
        match = LABEL.match(line)
        if match and match[2] not in keep_labels:
            labels = sections[section]
            if match[2] not in labels:
                index = 0
                while True:
                    n, value = index + 1, ""
                    while n: n, digit = divmod(n - 1, 26); value = chr(65 + digit) + value
                    if value not in labels.values(): break
                    index += 1
                labels[match[2]] = value
            line = line[:match.start()] + f"{match[1]}**Speaker {labels[match[2]]}:**" + line[match.end():]
        output.append(line)
    return speaker_labels("".join(output))


def audio_href(day, name): return f"audio/{day}/" + quote(name, safe="")


def audio_links(markdown, day, audio):
    by_title, by_href = {}, {}
    for item in audio:
        by_title.setdefault(Path(item["name"]).stem, []).append(item)
        by_href[audio_href(day, item["name"])] = item
    active = None

    def section(match):
        nonlocal active
        candidates = by_title.get(title(match[2]), [])
        item = by_href.get(match[3]) or (candidates[0] if len(candidates) == 1 else None)
        if item:
            if active == item["id"]: return ""
            active = item["id"]
            return match[1] + "\n\n[音频](" + audio_href(day, item["name"]) + ")\n\n"
        if match[3] and match[3].startswith(f"audio/{day}/"):
            active = None
            return match[1] + "\n\n"
        return match[0]

    return HEADING.sub(section, markdown)


def format_transcript(markdown, day, audio):
    return audio_links(speaker_labels(markdown), day, audio)


def change_audio_link(markdown, day, old_name, new_name=None):
    old = audio_href(day, old_name)
    if new_name is not None: return markdown.replace("](" + old + ")", "](" + audio_href(day, new_name) + ")")
    return re.sub(r"!?\[[^\]\n]*\]\(" + re.escape(old) + r"\)", "", markdown)
