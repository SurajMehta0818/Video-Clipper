"""
history.py - remembers which videos were already clipped (duplicate detection).

A video counts as "already used" if ANY of these match an earlier run:
  1. Same YouTube video ID          (also catches youtu.be / watch / shorts link variants)
  2. Same local file                (size + start/end bytes fingerprint)
  3. Same spoken content            (re-uploads, re-encodes, or a downloaded copy of a
                                     video you used as a link before - found by comparing
                                     fingerprints of the transcripts)
The history also remembers which time ranges were already clipped, so a video you choose
to re-use only produces NEW parts.
"""

import hashlib
import json
import re
import time
import zlib
from pathlib import Path
from typing import Optional

SKETCH_SIZE = 128        # how many numbers represent one video's spoken content
SIMILAR_AT = 0.45        # transcript similarity (0-1) at which two videos count as the same

_ID = r"([A-Za-z0-9_-]{11})"


def youtube_id(url: str) -> Optional[str]:
    """11-character YouTube video ID from any common link form, or None."""
    for pat in (r"youtu\.be/" + _ID,
                r"[?&]v=" + _ID,
                r"youtube\.com/(?:shorts|live|embed|v)/" + _ID):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return None


def file_fingerprint(path: Path) -> str:
    """Fast fingerprint of a big video file: size + first MB + last MB."""
    size = path.stat().st_size
    h = hashlib.sha1(str(size).encode())
    with open(path, "rb") as f:
        h.update(f.read(1 << 20))
        if size > (2 << 20):
            f.seek(-(1 << 20), 2)
            h.update(f.read(1 << 20))
    return h.hexdigest()


def text_sketch(text: str, n: int = 4) -> list:
    """Compact fingerprint of what is SAID in a video (bottom-k sketch of word 4-grams)."""
    words = re.findall(r"\w+", text.lower())
    if len(words) < n + 5:
        return []
    shingles = {zlib.crc32(" ".join(words[i:i + n]).encode("utf-8"))
                for i in range(len(words) - n + 1)}
    return sorted(shingles)[:SKETCH_SIZE]


def similarity(a: list, b: list) -> float:
    """Estimated overlap (Jaccard) between two sketches, 0..1."""
    if not a or not b:
        return 0.0
    sa, sb = set(a), set(b)
    union = sorted(sa | sb)[:SKETCH_SIZE]
    return sum(1 for x in union if x in sa and x in sb) / len(union)


class History:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.videos = []
        try:
            self.videos = json.loads(self.path.read_text(encoding="utf-8")).get("videos", [])
        except Exception:
            self.videos = []

    def count(self) -> int:
        return len(self.videos)

    def lookup(self, vid: Optional[str] = None, fp: Optional[str] = None):
        for v in self.videos:
            if (vid and v.get("id") == vid) or (fp and v.get("fingerprint") == fp):
                return v
        return None

    def similar(self, sketch: list):
        best, best_sim = None, 0.0
        for v in self.videos:
            s = similarity(sketch, v.get("sketch", []))
            if s > best_sim:
                best, best_sim = v, s
        return best if best_sim >= SIMILAR_AT else None

    def record(self, entry, vid, fp, title, source, duration, sketch, clips, language):
        """Remember a processed video. If `entry` exists, just add the new clip ranges."""
        clips = [[round(a, 1), round(b, 1)] for a, b in clips]
        if entry is None:
            entry = {"clips": []}
            self.videos.append(entry)
        entry["id"] = vid or entry.get("id")
        entry["fingerprint"] = fp or entry.get("fingerprint")
        entry["title"] = title
        entry["source"] = source
        entry["duration"] = round(duration, 1)
        entry["sketch"] = sketch or entry.get("sketch", [])
        entry["language"] = language
        entry["date"] = time.strftime("%Y-%m-%d %H:%M")
        entry["clips"] = entry.get("clips", []) + clips
        self.save()

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"videos": self.videos}, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def clear(self):
        self.videos = []
        self.save()
