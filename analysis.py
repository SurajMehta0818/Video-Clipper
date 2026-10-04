"""
analysis.py - understand a long video and choose its most important moments.

For every possible clip (any length between your Min and Max, always starting and ending on
a sentence boundary) the code measures:
  topic    - how central the clip is to what the whole video is about (key terms)
  energy   - how loud / lively the voice is compared with the rest of the video
  hook     - does it open with a question, a number or a bold statement
  complete - does it start a fresh sentence and end on a finished one
  pace     - speaking speed (dead air and music score low)
  pause    - does it begin and end at natural pauses
  power    - emotional / strong words (English only)
  and subtracts filler words ("um", "uh").
The best non-overlapping clips win. Works offline and is free.
If speech recognition is not available, it falls back to audio energy only.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass

import numpy as np


@dataclass
class Seg:
    start: float
    end: float
    text: str


POWER_WORDS = set("""
secret mistake mistakes never always nobody everyone truth lie lies money rich million
billion free stop warning shocking crazy insane best worst biggest hack trick easy fast
simple why imagine actually honestly literally love hate fear afraid scared dead die died
killed failed failure success win won lost important problem solution proof reason lesson
regret struggle quit fired rejected story changed incredible amazing powerful dangerous
risk wrong remember key must exam tip trick mistake
""".split())

HOOK_PHRASES = [
    "here's", "here is", "the truth", "nobody", "no one", "never", "stop", "don't",
    "do not", "why", "how to", "how i", "what if", "imagine", "listen", "the secret",
    "biggest", "number one", "most people", "the reason", "let me tell", "you need to",
    "you should", "the first thing", "the problem", "important", "remember this",
]

CONNECTORS = {"and", "but", "so", "because", "which", "that", "or", "then", "also"}
FILLERS = {"um", "uh", "umm", "uhh", "erm"}
END_PUNCT = (".", "?", "!", "…", "।", "。", "？", "！")

# how much each signal matters
W_EN = dict(hook=.15, power=.10, topic=.20, energy=.15, pace=.10, questions=.05,
            complete=.20, pause=.05)
W_OTHER = dict(hook=.05, power=0., topic=.30, energy=.25, pace=.10, questions=.05,
               complete=.20, pause=.05)          # non-English: English word lists don't apply
W_AUDIO = dict(energy=.85, pause=.15)             # no speech recognition available


def ts(sec: float) -> str:
    return f"{int(sec // 60):02d}:{int(sec % 60):02d}"


def _tokens(text: str):
    return re.findall(r"\w+", text.lower())


def _prefix(values):
    return np.concatenate([[0.0], np.cumsum(values, dtype=np.float64)])


# ----------------------------------------------------------------------
#                       AUDIO + SPEECH RECOGNITION
# ----------------------------------------------------------------------
def rms_per_second(audio, sr: int = 16000):
    """Loudness for every second of the video."""
    secs = len(audio) // sr
    if secs == 0:
        return None
    res = []
    for k in range(0, secs, 600):  # blocks of 10 minutes to save memory
        blk = audio[k * sr: min(k + 600, secs) * sr].astype(np.float32).reshape(-1, sr)
        res.append(np.sqrt((blk ** 2).mean(axis=1)))
    return np.concatenate(res)


_MODELS = {}


def transcribe(audio, model_size, language, on_progress, check_cancel, log, sr: int = 16000):
    """Speech -> sentences with timestamps. Returns (segments, detected_language)."""
    from faster_whisper import WhisperModel

    if model_size not in _MODELS:
        log("   loading speech model (downloads on first use)...")
        _MODELS[model_size] = WhisperModel(model_size, device="cpu", compute_type="int8")
    model = _MODELS[model_size]

    wave = audio.astype(np.float32) / 32768.0
    kwargs = dict(vad_filter=True, beam_size=1, condition_on_previous_text=False)
    if language and language != "auto":
        kwargs["language"] = language
    segments, info = model.transcribe(wave, **kwargs)

    total = max(len(audio) / sr, 1.0)
    segs, recent = [], []
    for s in segments:
        check_cancel()
        text = s.text.strip()
        if text:
            if not (len(recent) >= 2 and text == recent[-1] == recent[-2]):  # skip loops
                segs.append(Seg(float(s.start), float(s.end), text))
            recent = (recent + [text])[-3:]
        on_progress(min(s.end / total, 1.0))
    return segs, info.language


def grid_segments(total: float, step: float = 5.0):
    """Stand-in 'sentences' (every few seconds) when speech recognition is unavailable."""
    out, t = [], 0.0
    while t < total:
        out.append(Seg(t, min(t + step, total), ""))
        t += step
    return out


# ----------------------------------------------------------------------
#                              SCORING
# ----------------------------------------------------------------------
def topic_weights(toks):
    """Words that define what this video is about: frequent, but not in every sentence."""
    n = len(toks)
    if n < 8:
        return {}
    tf, df = Counter(), Counter()
    for t in toks:
        tf.update(t)
        df.update(set(t))
    cand = {}
    for w, f in tf.items():
        if len(w) < 3 or w.isdigit() or f < 3:
            continue
        d = df[w]
        if d > 0.12 * n:  # appears everywhere = filler word, not a topic
            continue
        cand[w] = math.log(1 + f) * math.log(n / d) ** 2
    top = sorted(cand.items(), key=lambda x: -x[1])[:200]
    if not top:
        return {}
    m = top[0][1]
    return {w: v / m for w, v in top}


def _hook(segs, i, en):
    first = segs[i].text.strip()
    nxt = segs[i + 1].text.strip() if i + 1 < len(segs) else ""
    low = (first + " " + nxt).lower()[:220]
    s = 0.0
    if "?" in first:
        s += 0.35
    if en and any(p in low for p in HOOK_PHRASES):
        s += 0.35
    if re.search(r"\d", low):
        s += 0.1
    if 4 <= len(first.split()) <= 18:
        s += 0.2
    return min(s, 1.0)


def _starts_clean(segs, i):
    prev_ok = i == 0 or segs[i - 1].text.rstrip().endswith(END_PUNCT)
    toks = _tokens(segs[i].text)
    return 1.0 if (prev_ok and (toks[0] if toks else "") not in CONNECTORS) else 0.0


def _ends_clean(segs, j):
    return 1.0 if segs[j].text.rstrip().endswith(END_PUNCT) else 0.0


def _energy(rms, p90, start, end):
    if rms is None or len(rms) == 0:
        return 0.5
    w = rms[int(start): int(end) + 1]
    if len(w) == 0:
        return 0.5
    return 0.6 * min(float(w.mean()) / p90, 1.0) + 0.4 * min(float(w.std()) / (0.5 * p90), 1.0)


def build_candidates(segs, rms, cfg, lang, has_text, total, skip):
    n = len(segs)
    if n == 0:
        return []
    lo = cfg["min_len"]
    hi = cfg["max_len"] - cfg.get("pad_start", 0.25) - cfg.get("pad_end", 0.4)
    en = lang == "en"

    if has_text:
        toks = [_tokens(s.text) for s in segs]
        W = _prefix([len(t) for t in toks])
        C = _prefix([len(re.sub(r"\W", "", s.text)) for s in segs])
        P = _prefix([sum(w in POWER_WORDS for w in t) for t in toks])
        F = _prefix([sum(w in FILLERS for w in t) for t in toks])
        Q = _prefix([s.text.count("?") for s in segs])
        tw = topic_weights(toks)
        T = _prefix([sum(tw.get(w, 0.0) for w in t) for t in toks])

    p90 = float(np.percentile(rms, 90)) if rms is not None and len(rms) else 0.0
    p90 = p90 if p90 > 0 else 1.0
    p30 = float(np.percentile(rms, 30)) if rms is not None and len(rms) else 0.0

    def quiet(t):
        return 1.0 if (rms is not None and int(t) < len(rms) and rms[int(t)] <= p30) else 0.0

    out = []
    for i in range(n):
        start = segs[i].start
        if start < skip:
            continue
        gap_before = (start - segs[i - 1].end) if i > 0 else 1.0
        if has_text:
            hook = _hook(segs, i, en)
            clean_start = _starts_clean(segs, i)
        for j in range(i, n):
            end = segs[j].end
            dur = end - start
            if dur > hi or end > total - skip:
                break
            if dur < lo:
                continue
            gap_after = (segs[j + 1].start - end) if j + 1 < n else 1.0
            f = {"energy": _energy(rms, p90, start, end)}
            if has_text:
                words = max(float(W[j + 1] - W[i]), float(C[j + 1] - C[i]) / 5.5)
                wps = words / dur
                if wps < 0.8:  # mostly silence or music
                    continue
                f["topic_raw"] = float(T[j + 1] - T[i]) / max(words, 1.0)
                f["power"] = min((float(P[j + 1] - P[i]) / words * 100) / 6.0, 1.0) if en else 0.0
                f["questions"] = min(float(Q[j + 1] - Q[i]) / 3.0, 1.0)
                f["pace"] = 1.0 - min(abs(wps - 2.7) / 1.8, 1.0)
                f["fillers"] = min((float(F[j + 1] - F[i]) / words * 100) / 4.0, 1.0)
                f["complete"] = 0.5 * clean_start + 0.5 * _ends_clean(segs, j)
                f["pause"] = 0.5 * min(gap_before / 0.8, 1.0) + 0.5 * min(gap_after / 0.8, 1.0)
                f["hook"] = hook
                text = " ".join(s.text for s in segs[i:j + 1])
            else:
                f["pause"] = 0.5 * quiet(start) + 0.5 * quiet(end)
                text = ""
            out.append({"start": start, "end": end, "f": f, "text": text})

    # normalise topic across all candidates, then compute final scores
    raws = [c["f"].get("topic_raw", 0.0) for c in out]
    top90 = float(np.percentile(raws, 90)) if raws else 0.0
    top90 = top90 if top90 > 0 else 1.0
    weights = W_AUDIO if not has_text else (W_EN if en else W_OTHER)
    for c in out:
        f = c["f"]
        f["topic"] = min(f.get("topic_raw", 0.0) / top90, 1.0)
        score = sum(weights[k] * f.get(k, 0.0) for k in weights) - 0.10 * f.get("fillers", 0.0)
        c["score"] = round(max(score, 0.0) * 100, 1)
        why = []
        if f["topic"] > 0.6:
            why.append("key topic")
        if f["energy"] > 0.6:
            why.append("high energy")
        if f.get("hook", 0) > 0.5:
            why.append("strong opening")
        if f.get("questions", 0) > 0.3:
            why.append("asks questions")
        if f.get("power", 0) > 0.5:
            why.append("strong words")
        c["reason"] = ", ".join(why) or "clear, well-paced speech"
        c["title"] = " ".join(c["text"].split()[:8]) or f"Clip at {ts(c['start'])}"
    return out


def _overlaps(c, others, gap):
    return any(c["start"] < o[1] + gap and o[0] < c["end"] + gap for o in others)


def pick_clips(segs, rms, cfg, lang, has_text, total, exclude=()):
    """Best non-overlapping clips, best first. `exclude` = time ranges used before."""
    cands = build_candidates(segs, rms, cfg, lang, has_text, total, cfg["skip"])
    if not cands:  # video too short to skip its start/end
        cands = build_candidates(segs, rms, cfg, lang, has_text, total, 0)
    cands.sort(key=lambda c: c["score"], reverse=True)

    chosen, taken = [], []
    for c in cands:
        if len(chosen) >= cfg["num_clips"]:
            break
        if _overlaps(c, taken, 3.0) or _overlaps(c, [tuple(r) for r in exclude], 1.0):
            continue
        chosen.append(c)
        taken.append((c["start"], c["end"]))
    return chosen
