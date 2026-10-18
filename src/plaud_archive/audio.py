from __future__ import annotations

import json
import os
import struct
import subprocess
from pathlib import Path


def probe(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=format_name,duration", "-of", "json", str(path)], capture_output=True, text=True)
    if p.returncode: raise ValueError("audio cannot be decoded: " + p.stderr.strip()[-300:])
    result = json.loads(p.stdout)["format"]
    return result["format_name"], float(result["duration"])


def _crc_table():
    table = []
    for i in range(256):
        n = i << 24
        for _ in range(8): n = ((n << 1) ^ (0x04C11DB7 if n & 0x80000000 else 0)) & 0xFFFFFFFF
        table.append(n)
    return table


CRC = _crc_table()


def ogg_page(packets, sequence, granule, flags):
    laces = [x for p in packets for x in [*([255] * (len(p) // 255)), len(p) % 255]]
    data = b"OggS" + bytes((0, flags)) + struct.pack("<QII", granule, 1, sequence) + b"\0\0\0\0" + bytes((len(laces),)) + bytes(laces) + b"".join(packets)
    crc = 0
    for value in data: crc = ((crc << 8) & 0xFFFFFFFF) ^ CRC[((crc >> 24) ^ value) & 255]
    return data[:22] + struct.pack("<I", crc) + data[26:]


def playable(source: Path, *, raw_opus=False, output: Path | None = None) -> tuple[Path, str, float]:
    """Keep playable originals; wrap the device's packet stream without re-encoding."""
    try:
        fmt, duration = probe(source)
    except ValueError:
        if not raw_opus: raise
        target = output or source.with_suffix(".opus")
        target.parent.mkdir(parents=True, exist_ok=True)
        with source.open("rb") as inp, target.open("wb") as out:
            out.write(ogg_page([b"OpusHead" + bytes((1, 1)) + struct.pack("<H", 312) + struct.pack("<I", 48000) + b"\0\0\0"], 0, 0, 2))
            out.write(ogg_page([b"OpusTags" + struct.pack("<I", 13) + b"plaud-archive" + struct.pack("<I", 0)], 1, 0, 0))
            seq, granule = 2, 0
            while chunk := inp.read(80 * 128):
                if len(chunk) % 80: raise ValueError("incomplete device Opus packet")
                packets = [chunk[i:i+80] for i in range(0, len(chunk), 80)]
                granule += len(packets) * 960
                out.write(ogg_page(packets, seq, granule, 4 if inp.tell() == source.stat().st_size else 0)); seq += 1
            out.flush(); os.fsync(out.fileno())
        fmt, duration = probe(target)
        return target, ".opus", duration
    suffix = ".mp3" if "mp3" in fmt else ".opus" if "ogg" in fmt else ".wav" if "wav" in fmt else ".m4a" if "mov" in fmt else source.suffix
    return source, suffix, duration


def cut(source, target, offset, seconds):
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_suffix(".part")
    p = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-y", "-threads", "1", "-ss", str(offset), "-i", str(source),
                        "-t", str(seconds), "-ac", "1", "-ar", "16000", "-b:a", "32k", "-f", "mp3", str(part)], capture_output=True)
    if p.returncode: raise ValueError("audio decode failed: " + p.stderr.decode(errors="replace")[-300:])
    os.replace(part, target)
