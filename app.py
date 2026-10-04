"""
Shorts Maker (GUI)
------------------
Paste YouTube links and/or add video files from your computer, click Start, and get ONE zip
file with the most important moments of each video as vertical 9:16 clips (any length between
your Min and Max), ready for Shorts / Reels. Videos you already clipped are detected and skipped.

PyCharm: right-click this file -> Run 'app'
"""

import csv
import importlib.util
import json
import os
import queue
import random
import re
import shutil
import socket
import subprocess
import sys
import sysconfig
import tempfile
import threading
import time
import urllib.request
import tkinter as tk
import zipfile
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

LANGUAGES = {"Auto-detect": "auto", "English": "en", "Hindi": "hi", "Nepali": "ne"}
BASE_DIR = Path(__file__).resolve().parent
DEFAULT_OUT = BASE_DIR / "output"
COOKIE_PATH = BASE_DIR / "youtube_cookies.txt"  # saved YouTube login (keep private!)
HISTORY_PATH = BASE_DIR / "used_videos.json"    # videos already clipped (duplicate check)


def _pip(*packages, upgrade=False):
    cmd = [sys.executable, "-m", "pip", "install", "-q"] + (["-U"] if upgrade else []) + list(packages)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    return r.returncode == 0, r.stderr[-300:]


def _bootstrap():
    """Installs what is missing and keeps the YouTube downloader fresh (YouTube changes often).
    Done through pip, automatically. Runs BEFORE yt_dlp is imported."""
    try:
        # one time: speech analysis (understands what is said, to find the important parts)
        if (importlib.util.find_spec("faster_whisper") is None
                or importlib.util.find_spec("numpy") is None):
            print("Installing speech analysis (one time, can take a few minutes)...")
            ok, err = _pip("faster-whisper", "numpy")
            print("Speech analysis installed." if ok else f"Could not install it (continuing): {err}")

        # YouTube downloader + JavaScript runtime: at most once every 12 hours
        stamp = BASE_DIR / ".tools_checked"
        if stamp.exists() and time.time() - stamp.stat().st_mtime < 12 * 3600:
            return
        print("Checking YouTube downloader updates (first time takes a minute)...")
        ok, err = _pip("yt-dlp[default]", "deno", upgrade=True)
        if ok:
            stamp.write_text(time.strftime("%Y-%m-%d %H:%M:%S"))
            print("Downloader is up to date.")
        else:
            print("Could not update downloader (continuing):", err)
    except Exception as e:
        print("Setup step skipped (continuing):", e)


_bootstrap()
import yt_dlp  # noqa: E402  (must come after _bootstrap)

try:
    import numpy as np  # noqa: E402
    import analysis  # noqa: E402
    import history  # noqa: E402
except ImportError as _e:
    sys.exit(f"Missing package ({_e}). Run:  pip install -r requirements.txt")


def find_js_runtime():
    """Locate the Deno runtime installed by pip (or any Deno/Node on this PC)."""
    exe = "deno.exe" if sys.platform.startswith("win") else "deno"
    for folder in (Path(sys.executable).parent, Path(sysconfig.get_path("scripts"))):
        if (folder / exe).exists():
            return {"deno": {"path": str(folder / exe)}}
    if shutil.which("deno"):
        return {"deno": {}}
    node = shutil.which("node")
    if node:
        return {"node": {"path": node}}
    return None


# Different ways of asking YouTube for the video. If one gets blocked, the next is tried.
CLIENT_ATTEMPTS = [None, ["tv"], ["android_vr"], ["web_safari"], ["mweb"]]
BLOCK_HINTS = ("not a bot", "sign in", "po token", "403", "forbidden", "format is not available")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)  # hides black console on Windows


# ----------------------------------------------------------------------
#                 YOUTUBE BROWSER (sign in + pick videos)
# ----------------------------------------------------------------------
def _cdp(ws_url: str, method: str, params=None, timeout: float = 15):
    """Send one command to Chrome/Edge through its debugging port and return the result."""
    from websockets.sync.client import connect  # installed together with yt-dlp[default]
    with connect(ws_url, max_size=None, open_timeout=timeout) as ws:
        ws.send(json.dumps({"id": 1, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(ws.recv(timeout=timeout))
            if msg.get("id") == 1:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "browser error"))
                return msg.get("result", {})


def write_netscape(cookies, path: Path):
    """Save cookies in the cookies.txt format that yt-dlp understands."""
    lines = ["# Netscape HTTP Cookie File", "# Made by Shorts Maker - keep this file private", ""]
    for c in cookies:
        domain = c["domain"]
        expires = c.get("expires", -1)
        expires = int(expires) if expires and expires > 0 else 0
        lines.append("\t".join([
            ("#HttpOnly_" if c.get("httpOnly") else "") + domain,
            "TRUE" if domain.startswith(".") else "FALSE",
            c.get("path", "/"),
            "TRUE" if c.get("secure") else "FALSE",
            str(expires), c["name"], c["value"]]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


class YouTubeBrowser:
    """Opens a real Chrome/Edge window (fresh temporary profile) where you sign in to
    YouTube and open the videos you want. Afterwards the app reads your open tabs and your
    login through the browser's debugging port, saves it, and closes the window."""

    SIGNED_IN = {"SID", "__Secure-1PSID", "__Secure-3PSID", "SAPISID", "LOGIN_INFO"}

    def __init__(self):
        self.port = None
        self.profile = None

    @staticmethod
    def find_browser():
        cands = []
        if sys.platform.startswith("win"):
            for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
                base = os.environ.get(env)
                if base:
                    cands += [Path(base) / "Google/Chrome/Application/chrome.exe",
                              Path(base) / "Microsoft/Edge/Application/msedge.exe",
                              Path(base) / "BraveSoftware/Brave-Browser/Application/brave.exe"]
        elif sys.platform == "darwin":
            cands += [Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
                      Path("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]
        for n in ("google-chrome", "chrome", "chromium", "chromium-browser",
                  "microsoft-edge", "msedge", "brave-browser"):
            w = shutil.which(n)
            if w:
                cands.append(Path(w))
        for c in cands:
            if c.exists():
                return str(c)
        return None

    def start(self):
        exe = self.find_browser()
        if not exe:
            raise RuntimeError("Chrome or Edge was not found on this PC.")
        with socket.socket() as sck:
            sck.bind(("127.0.0.1", 0))
            self.port = sck.getsockname()[1]
        self.profile = tempfile.mkdtemp(prefix="shorts_yt_")
        subprocess.Popen([exe, f"--remote-debugging-port={self.port}",
                          f"--user-data-dir={self.profile}", "--remote-allow-origins=*",
                          "--no-first-run", "--no-default-browser-check",
                          "https://www.youtube.com/"])

    def _get(self, path: str, timeout: float = 4):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def is_running(self) -> bool:
        if not self.port:
            return False
        try:
            self._get("/json/version", 1.5)
            return True
        except Exception:
            return False

    def pages(self):
        return [t for t in self._get("/json/list") if t.get("type") == "page"]

    def watch_urls(self):
        """Links of every YouTube video open in a tab."""
        out = []
        for t in self.pages():
            u = t.get("url", "")
            if re.search(r"youtube\.com/(watch\?|shorts/|live/)|youtu\.be/", u) and u not in out:
                out.append(u)
        return out

    def export_cookies(self):
        """Returns (cookies, signed_in)."""
        pages = [t for t in self.pages() if t.get("webSocketDebuggerUrl")]
        if not pages:
            raise RuntimeError("No browser tab found.")
        ws = pages[0]["webSocketDebuggerUrl"]
        # Show a plain page before exporting: YouTube then has no reason to change the login
        # cookies while we copy them (recommended by yt-dlp's cookie guide).
        try:
            _cdp(ws, "Page.navigate", {"url": "https://www.youtube.com/robots.txt"})
            time.sleep(3)
        except Exception:
            pass
        try:
            cookies = _cdp(ws, "Network.getAllCookies")["cookies"]
        except Exception:
            cookies = _cdp(ws, "Storage.getCookies")["cookies"]
        keep = [c for c in cookies if "youtube." in c["domain"] or "google." in c["domain"]]
        return keep, any(c["name"] in self.SIGNED_IN for c in keep)

    def close(self):
        try:
            _cdp(self._get("/json/version")["webSocketDebuggerUrl"], "Browser.close", timeout=5)
        except Exception:
            pass
        if self.profile:
            time.sleep(2)
            shutil.rmtree(self.profile, ignore_errors=True)


# ----------------------------------------------------------------------
#                              ENGINE
# ----------------------------------------------------------------------
class Cancelled(Exception):
    pass


def get_ffmpeg() -> str:
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        raise RuntimeError("ffmpeg not found. Run: pip install -r requirements.txt")


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:60] or "video"


DEFAULTS = {
    "num_clips": 12,          # up to this many clips per video
    "min_len": 30,            # flexible length: every clip is between min and max seconds
    "max_len": 75,
    "skip": 15,               # ignore the first/last seconds (intro / outro)
    "mode": "blur",           # "blur" or "crop"
    "quality": "1080",
    "whisper_model": "base",  # tiny / base / small
    "language": "auto",
    "skip_dupes": True,
    "pad_start": 0.25,        # small breathing room around each clip
    "pad_end": 0.40,
    "cookie_browser": "none",
    "cookie_file": "",
}


class Duplicate(Exception):
    """The video (or its spoken content) was already clipped before."""


class Engine:
    SR = 16000  # audio sample rate used for analysis

    def __init__(self, cfg, log, progress):
        self.cfg = {**DEFAULTS, **cfg}
        self.log = log
        self.progress = progress  # progress(0..1)
        self.cancel = threading.Event()
        self.proc = None
        self.ffmpeg = get_ffmpeg()
        self.good_attempt = 0  # index in CLIENT_ATTEMPTS that worked last time
        self.history = history.History(HISTORY_PATH)
        self.cache_dir = None

    # ---- helpers ----
    def pause(self, seconds: float):
        """Cancellable wait (being gentle with YouTube avoids blocks)."""
        end = time.time() + seconds
        while time.time() < end:
            self.check_cancel()
            time.sleep(0.25)

    def check_cancel(self):
        if self.cancel.is_set():
            raise Cancelled()

    def stop(self):
        self.cancel.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def duration(self, path: Path) -> float:
        r = subprocess.run([self.ffmpeg, "-i", str(path)], capture_output=True,
                           text=True, creationflags=NO_WINDOW)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", r.stderr)
        if not m:
            raise RuntimeError("Could not read video duration")
        h, mi, s = m.groups()
        return int(h) * 3600 + int(mi) * 60 + float(s)

    def vfilter(self) -> str:
        w, h = 1080, 1920
        if self.cfg["mode"] == "crop":
            return (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                    f"crop={w}:{h},setsar=1")
        return (f"split=2[bg][fg];"
                f"[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},boxblur=25:5[bgb];"
                f"[fg]scale={w}:-2[fgs];"
                f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1")

    # ---- steps ----
    def download(self, url: str, folder: Path, on_progress):
        q = self.cfg["quality"]

        def hook(d):
            self.check_cancel()
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate")
                if total:
                    on_progress(d["downloaded_bytes"] / total)

        base = {
            "format": f"bv*[height<={q}]+ba/b[height<={q}]/best",
            "outtmpl": str(folder / "%(id)s.%(ext)s"),
            "merge_output_format": "mp4",
            "ffmpeg_location": self.ffmpeg,
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "retries": 5,
            "progress_hooks": [hook],
        }
        runtime = find_js_runtime()
        if runtime:
            base["js_runtimes"] = runtime
        # optional: logged-in browser / cookies.txt (only if you choose it in the window)
        cookie_file = self.cfg.get("cookie_file", "").strip()
        browser = self.cfg.get("cookie_browser", "none")
        if cookie_file and Path(cookie_file).is_file():
            base["cookiefile"] = cookie_file
        elif browser and browser != "none":
            base["cookiesfrombrowser"] = (browser,)

        # try the method that worked last time first, then the others
        order = [self.good_attempt] + [i for i in range(len(CLIENT_ATTEMPTS))
                                       if i != self.good_attempt]
        last_error = None
        for n, i in enumerate(order):
            self.check_cancel()
            clients = CLIENT_ATTEMPTS[i]
            opts = dict(base)
            if clients:
                opts["extractor_args"] = {"youtube": {"player_client": clients}}
            if n > 0:
                self.log(f"   blocked - trying another method ({clients[0] if clients else 'default'})...")
            try:
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info(url, download=True)
                    path = Path(ydl.prepare_filename(info))
                self.good_attempt = i
                if path.with_suffix(".mp4").exists():
                    path = path.with_suffix(".mp4")
                return path, info.get("title", path.stem), info.get("id")
            except Exception as e:
                self.check_cancel()
                last_error = e
                if not any(k in str(e).lower() for k in BLOCK_HINTS):
                    raise  # a different problem (private video, no internet...) - no point retrying
        raise last_error

    def cut(self, src: Path, dst: Path, start: float, length: float):
        cmd = [self.ffmpeg, "-y", "-loglevel", "error",
               "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
               "-vf", self.vfilter(), "-r", "30",
               "-c:v", "libx264", "-preset", "medium", "-crf", "20",
               "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "160k",
               "-movflags", "+faststart", str(dst)]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        _, err = self.proc.communicate()
        self.check_cancel()
        if self.proc.returncode != 0:
            raise RuntimeError(err.decode(errors="ignore")[-300:])

    def load_audio(self, video: Path):
        """Decode the audio once with ffmpeg (mono, 16 kHz, int16)."""
        self.proc = subprocess.Popen(
            [self.ffmpeg, "-v", "error", "-i", str(video), "-vn", "-ac", "1",
             "-ar", str(self.SR), "-f", "s16le", "-"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        out, err = self.proc.communicate()
        self.check_cancel()
        if self.proc.returncode != 0:
            raise RuntimeError(err.decode(errors="ignore")[-300:])
        audio = np.frombuffer(out, dtype=np.int16)
        if len(audio) == 0:
            raise RuntimeError("this video has no audio track")
        return audio

    def get_transcript(self, audio, key: str, dur: float, on_progress):
        """Returns (segments, language, has_text). Falls back to audio-only analysis."""
        model, lang_cfg = self.cfg["whisper_model"], self.cfg["language"]
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache = self.cache_dir / f"{safe_name(key)}_{model}_{lang_cfg}.json"
        if cache.exists():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                self.log("   using saved transcript")
                on_progress(1.0)
                return [analysis.Seg(*x) for x in data["segments"]], data["language"], True
            except Exception:
                pass
        try:
            self.log("   transcribing speech (a 1-hour video can take several minutes)...")
            segs, lang = analysis.transcribe(audio, model, lang_cfg, on_progress,
                                             self.check_cancel, self.log, self.SR)
        except Cancelled:
            raise
        except Exception as e:
            self.log(f"   speech analysis unavailable ({str(e)[:120]}) - using audio energy only")
            return analysis.grid_segments(dur), "", False
        if not segs:
            self.log("   no speech found - using audio energy only")
            return analysis.grid_segments(dur), "", False
        cache.write_text(json.dumps({"language": lang,
                                     "segments": [[s.start, s.end, s.text] for s in segs]},
                                    ensure_ascii=False), encoding="utf-8")
        self.log(f"   language: {lang}")
        return segs, lang, True

    def _check_duplicate(self, entry, why: str):
        if entry is None:
            return
        label = f"{why}: '{entry.get('title', '?')[:50]}' ({entry.get('date', '')})"
        if self.cfg["skip_dupes"]:
            raise Duplicate(label)
        self.log(f"   note: {label} - using only parts not clipped before")

    def process_source(self, src: str, idx: int, count: int, dl_dir: Path, clips_dir: Path) -> int:
        """Analyse one video and cut its most important moments. Returns number of clips."""
        def phase(a, b):
            return lambda f: self.progress((idx + a + f * (b - a)) / count)

        cfg = self.cfg
        is_url = src.lower().startswith(("http://", "https://"))
        vid = fp = entry = None

        # 1) duplicate check BEFORE downloading (saves time)
        if is_url:
            vid = history.youtube_id(src)
            entry = self.history.lookup(vid=vid) if vid else None
        else:
            fp = history.file_fingerprint(Path(src))
            entry = self.history.lookup(fp=fp)
        self._check_duplicate(entry, "already clipped before")

        if is_url:
            self.log("   downloading...")
            video, title, real_id = self.download(src, dl_dir, phase(0.0, 0.15))
            vid = vid or real_id
            if entry is None and vid:
                entry = self.history.lookup(vid=vid)
                self._check_duplicate(entry, "already clipped before")
        else:
            video, title = Path(src), Path(src).stem

        try:
            dur = self.duration(video)
            self.log(f"   {title[:70]}  ({dur / 60:.1f} min)")

            # 2) understand the video: audio loudness + what is said
            self.log("   reading audio...")
            audio = self.load_audio(video)
            rms = analysis.rms_per_second(audio, self.SR)
            segs, lang, has_text = self.get_transcript(audio, vid or fp or safe_name(title),
                                                       dur, phase(0.15, 0.6))
            del audio

            # 3) duplicate check by CONTENT (re-uploads, copies of a video used before)
            sketch = history.text_sketch(" ".join(s.text for s in segs)) if has_text else []
            if entry is None and sketch:
                entry = self.history.similar(sketch)
                self._check_duplicate(entry, "same spoken content as")

            # 4) choose the important parts (never the parts clipped before)
            exclude = [tuple(r) for r in entry.get("clips", [])] if entry else []
            self.log("   finding the most important moments...")
            picks = analysis.pick_clips(segs, rms, cfg, lang, has_text, dur, exclude)
            if not picks:
                raise RuntimeError("no suitable moments found (video too short, no clear speech, "
                                   "or everything was already used)")
            if len(picks) < cfg["num_clips"]:
                self.log(f"   only {len(picks)} good clips found in this video")

            # 5) cut
            name = safe_name(title)
            folder = clips_dir / name
            k = 2
            while folder.exists():
                folder = clips_dir / f"{name}_{k}"
                k += 1
            folder.mkdir(parents=True)

            rows, used = [], []
            for rank, c in enumerate(picks, 1):
                self.check_cancel()
                start = max(0.0, c["start"] - cfg["pad_start"])
                end = min(dur, c["end"] + cfg["pad_end"])
                at = f"{int(start // 60)}m{int(start % 60):02d}s"
                fname = f"{folder.name}_clip{rank:02d}_{at}.mp4"
                self.log(f"   clip {rank}/{len(picks)}  {analysis.ts(start)}-{analysis.ts(end)}"
                         f"  ({end - start:.0f}s)  score {c['score']:.0f}  {c['reason']}")
                self.cut(video, folder / fname, start, end - start)
                phase(0.6, 1.0)(rank / len(picks))
                used.append((start, end))
                rows.append([rank, fname, analysis.ts(start), analysis.ts(end),
                             round(end - start, 1), c["score"], c["reason"], c["text"]])

            with open(folder / "clips_info.csv", "w", newline="", encoding="utf-8-sig") as f:
                w = csv.writer(f)
                w.writerow(["rank", "file", "start", "end", "seconds", "score", "why",
                            "transcript"])
                w.writerows(rows)

            self.history.record(entry, vid, fp, title, src, dur, sketch, used, lang)
            return len(picks)
        finally:
            if is_url:
                video.unlink(missing_ok=True)  # only delete our own temporary download

    # ---- main run ----
    def run(self, links, out_dir: Path):
        run_id = time.strftime("%Y%m%d_%H%M%S")
        out_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir = out_dir / "_cache"
        work = out_dir / f"_work_{run_id}"
        dl_dir, clips_dir = work / "downloads", work / "clips"
        dl_dir.mkdir(parents=True)
        clips_dir.mkdir(parents=True)

        total_clips, failed, skipped = 0, [], []
        try:
            for idx, url in enumerate(links):
                self.check_cancel()
                is_url = url.lower().startswith(("http://", "https://"))
                if idx > 0 and is_url:
                    self.pause(random.uniform(4, 9))  # short break between YouTube downloads
                self.log(f"\n[{idx + 1}/{len(links)}] {url}")
                try:
                    total_clips += self.process_source(url, idx, len(links), dl_dir, clips_dir)
                except Cancelled:
                    raise
                except Duplicate as e:
                    skipped.append(url)
                    self.log(f"   SKIPPED (duplicate) - {e}")
                except Exception as e:
                    failed.append(url)
                    msg = str(e)
                    self.log(f"   FAILED: {msg[:200]}")
                    low = msg.lower()
                    if "not a bot" in low or "sign in" in low:
                        self.log("   -> YouTube is blocking this internet connection. Try: a phone "
                                 "hotspot, wait an hour, or use 'Open YouTube browser' to sign in "
                                 "(a saved login may have expired - sign in again).")
                    elif "cookie" in low or "dpapi" in low:
                        self.log("   -> Could not read browser cookies. Close that browser and retry, "
                                 "or use Firefox, or select a cookies.txt file.")

            if total_clips == 0:
                if skipped and not failed:
                    raise RuntimeError("All videos were already clipped before (duplicates). "
                                       "Untick 'Skip videos I already clipped' to cut NEW parts "
                                       "from them.")
                raise RuntimeError("No clips were created.")

            self.log("\nCreating ZIP file...")
            zip_path = out_dir / f"shorts_{run_id}.zip"
            with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:
                for f in sorted(clips_dir.rglob("*")):
                    if f.is_file():
                        z.write(f, f.relative_to(clips_dir))
            self.progress(1.0)
            return zip_path, total_clips, failed, skipped
        finally:
            shutil.rmtree(work, ignore_errors=True)


# ----------------------------------------------------------------------
#                                GUI
# ----------------------------------------------------------------------
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Shorts Maker")
        self.geometry("940x900")
        self.minsize(640, 560)
        self.q = queue.Queue()
        self.engine = None
        self.last_zip = None
        self.out_var = tk.StringVar(value=str(DEFAULT_OUT))
        self.num_var = tk.IntVar(value=12)
        self.min_var = tk.IntVar(value=30)
        self.max_var = tk.IntVar(value=75)
        self.skip_var = tk.IntVar(value=15)
        self.model_var = tk.StringVar(value="base")
        self.lang_var = tk.StringVar(value="Auto-detect")
        self.dupes_var = tk.BooleanVar(value=True)
        self.mode_var = tk.StringVar(value="blur")
        self.quality_var = tk.StringVar(value="1080")
        self.cookie_browser_var = tk.StringVar(value="none")
        self.cookie_file_var = tk.StringVar(value=str(COOKIE_PATH) if COOKIE_PATH.exists() else "")
        self.browser = None
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.build_ui()
        self.refresh_history()
        self.after(100, self.poll)

    def build_ui(self):
        pad = {"padx": 10, "pady": 4}
        ttk.Label(self, text="Paste YouTube links and/or add video files from your computer (one per line):",
                  font=("TkDefaultFont", 10, "bold")).pack(anchor="w", **pad)
        self.links_box = tk.Text(self, height=8, wrap="none")
        self.links_box.pack(fill="x", **pad)

        # settings
        s = ttk.LabelFrame(self, text="Clip settings")
        s.pack(fill="x", **pad)
        ttk.Label(s, text="Clips per video (up to):").grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Spinbox(s, from_=1, to=40, width=6, textvariable=self.num_var).grid(row=0, column=1)
        ttk.Label(s, text="Min length (s):").grid(row=0, column=2, padx=6, sticky="w")
        ttk.Spinbox(s, from_=10, to=170, width=6, textvariable=self.min_var).grid(row=0, column=3)
        ttk.Label(s, text="Max length (s):").grid(row=0, column=4, padx=6, sticky="w")
        ttk.Spinbox(s, from_=15, to=180, width=6, textvariable=self.max_var).grid(row=0, column=5)
        ttk.Label(s, text="Style:").grid(row=0, column=6, padx=6, sticky="w")
        ttk.Combobox(s, values=["blur", "crop"], width=7, state="readonly",
                     textvariable=self.mode_var).grid(row=0, column=7)
        ttk.Label(s, text="Download quality:").grid(row=1, column=0, padx=6, pady=6, sticky="w")
        ttk.Combobox(s, values=["480", "720", "1080", "1440", "2160"], width=6,
                     state="readonly", textvariable=self.quality_var).grid(row=1, column=1)
        ttk.Label(s, text="Skip start/end (s):").grid(row=1, column=2, padx=6, sticky="w")
        ttk.Spinbox(s, from_=0, to=300, width=6, textvariable=self.skip_var).grid(row=1, column=3)
        ttk.Label(s, text="Speech model:").grid(row=1, column=4, padx=6, sticky="w")
        ttk.Combobox(s, values=["tiny", "base", "small"], width=6, state="readonly",
                     textvariable=self.model_var).grid(row=1, column=5)
        ttk.Label(s, text="Language:").grid(row=1, column=6, padx=6, sticky="w")
        ttk.Combobox(s, values=list(LANGUAGES), width=10, state="readonly",
                     textvariable=self.lang_var).grid(row=1, column=7)
        ttk.Label(s, foreground="gray", wraplength=860, justify="left",
                  text="The app listens to each video and picks the most important moments: key "
                       "topics, lively delivery, strong openings. Every clip starts and ends on a "
                       "full sentence, so its length is flexible (between Min and Max). Clip 01 is "
                       "the best one. For Hindi/Nepali or other non-English videos choose the "
                       "language and the 'small' speech model (slower, more accurate)."
                  ).grid(row=2, column=0, columnspan=8, padx=6, pady=(0, 6), sticky="w")

        # duplicates
        d = ttk.LabelFrame(self, text="Duplicate videos")
        d.pack(fill="x", **pad)
        ttk.Checkbutton(d, variable=self.dupes_var,
                        text="Skip videos I already clipped (also catches re-uploads and copies)"
                        ).grid(row=0, column=0, padx=6, pady=6, sticky="w")
        ttk.Button(d, text="Clear history", command=self.clear_history).grid(row=0, column=1, padx=6)
        self.hist_label = ttk.Label(d, foreground="gray")
        self.hist_label.grid(row=0, column=2, padx=6, sticky="w")
        ttk.Label(d, foreground="gray", text="Untick to re-use a video: it will only produce NEW "
                  "parts, never the moments clipped before.").grid(row=1, column=0, columnspan=3,
                                                                  padx=6, pady=(0, 6), sticky="w")

        # youtube sign-in (fixes "Sign in to confirm you're not a bot")
        c = ttk.LabelFrame(self, text="YouTube sign-in (use this if downloads get blocked)")
        c.pack(fill="x", **pad)
        self.open_browser_btn = ttk.Button(c, text="1) Open YouTube browser",
                                           command=self.open_youtube_browser)
        self.open_browser_btn.grid(row=0, column=0, padx=6, pady=6)
        self.import_btn = ttk.Button(c, text="2) Finish & import videos",
                                     command=self.finish_browser)
        self.import_btn.grid(row=0, column=1, padx=6)
        ttk.Label(c, foreground="gray", wraplength=520, justify="left",
                  text="Step 1: a browser opens - sign in (or create an account), then open each "
                       "video you want in its own tab.  Step 2: come back here and click Finish: "
                       "the videos are added to the list and your login is saved."
                  ).grid(row=0, column=2, padx=6, sticky="w")
        ttk.Label(c, text="Advanced - browser login:").grid(row=1, column=0, padx=6, pady=4, sticky="w")
        ttk.Combobox(c, values=["none", "firefox", "chrome", "edge", "brave"], width=9,
                     state="readonly", textvariable=self.cookie_browser_var).grid(row=1, column=1)
        ttk.Label(c, text="cookies.txt:").grid(row=1, column=2, padx=6, sticky="w")
        ttk.Entry(c, textvariable=self.cookie_file_var, width=40).grid(row=1, column=2, padx=(80, 6),
                                                                      sticky="w")
        ttk.Button(c, text="Browse...", command=self.browse_cookies).grid(row=1, column=3, padx=6)

        # output folder
        o = ttk.Frame(self)
        o.pack(fill="x", **pad)
        ttk.Label(o, text="Save ZIP in:").pack(side="left")
        ttk.Entry(o, textvariable=self.out_var).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(o, text="Browse...", command=self.browse).pack(side="left")

        # buttons
        b = ttk.Frame(self)
        b.pack(fill="x", **pad)
        self.start_btn = ttk.Button(b, text="Start", command=self.start)
        self.start_btn.pack(side="left")
        self.stop_btn = ttk.Button(b, text="Cancel", command=self.cancel, state="disabled")
        self.stop_btn.pack(side="left", padx=6)
        ttk.Button(b, text="Add video files...", command=self.add_files).pack(side="left")
        ttk.Button(b, text="Clear list", command=lambda: self.links_box.delete("1.0", "end")
                   ).pack(side="left", padx=6)
        self.open_btn = ttk.Button(b, text="Open output folder", command=self.open_folder)
        self.open_btn.pack(side="right")

        self.bar = ttk.Progressbar(self, maximum=100)
        self.bar.pack(fill="x", **pad)
        self.log_box = ScrolledText(self, height=14, state="disabled")
        self.log_box.pack(fill="both", expand=True, **pad)

    # ---- actions ----
    def browse(self):
        d = filedialog.askdirectory(initialdir=self.out_var.get())
        if d:
            self.out_var.set(d)

    def refresh_history(self):
        n = history.History(HISTORY_PATH).count()
        self.hist_label.configure(text=f"{n} video(s) in history")

    def clear_history(self):
        if messagebox.askyesno("Clear history", "Forget all videos used so far?\n"
                               "They could then be clipped again."):
            history.History(HISTORY_PATH).clear()
            self.refresh_history()

    def open_youtube_browser(self):
        if self.browser and self.browser.is_running():
            messagebox.showinfo("Browser already open", "The YouTube browser is already open.\n"
                                "Sign in, open your videos, then click 'Finish & import'.")
            return
        self.browser = YouTubeBrowser()
        try:
            self.browser.start()
        except Exception as e:
            self.browser = None
            messagebox.showerror("Browser", str(e))
            return
        self.write_log("YouTube browser opened.\n"
                       "  1. Sign in (or create an account)\n"
                       "  2. Open each video you want to clip - one video per tab\n"
                       "  3. Come back here and click '2) Finish & import videos'")

    def finish_browser(self):
        if not self.browser:
            messagebox.showinfo("Browser", "Click '1) Open YouTube browser' first.")
            return
        self.import_btn.configure(state="disabled")
        self.write_log("Reading your browser session (takes a few seconds)...")
        threading.Thread(target=self._finish_worker, daemon=True).start()

    def _finish_worker(self):
        b = self.browser
        try:
            deadline = time.time() + 20
            while not b.is_running():
                if time.time() > deadline:
                    raise RuntimeError("The browser is not open. Keep it open until you click "
                                       "Finish - click '1) Open YouTube browser' to try again.")
                time.sleep(0.5)
            urls = b.watch_urls()
            cookies, signed_in = b.export_cookies()
            if signed_in:
                write_netscape(cookies, COOKIE_PATH)
            b.close()
            self.q.put(("browser_done", (urls, signed_in)))
        except Exception as e:
            self.q.put(("browser_error", str(e)))

    def on_close(self):
        try:
            if self.browser and self.browser.is_running():
                self.browser.close()
        except Exception:
            pass
        self.destroy()

    def add_files(self):
        files = filedialog.askopenfilenames(
            title="Choose video files",
            filetypes=[("Video files", "*.mp4 *.mkv *.mov *.avi *.webm *.m4v *.flv *.wmv"),
                       ("All files", "*.*")])
        for f in files:
            self.links_box.insert("end", f + "\n")

    def browse_cookies(self):
        f = filedialog.askopenfilename(title="Select cookies.txt",
                                       filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if f:
            self.cookie_file_var.set(f)

    def open_folder(self):
        path = Path(self.out_var.get())
        path.mkdir(parents=True, exist_ok=True)
        if sys.platform.startswith("win"):
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)])
        else:
            subprocess.run(["xdg-open", str(path)])

    def write_log(self, msg):
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def start(self):
        if self.browser and self.browser.is_running():
            messagebox.showinfo("Browser still open", "Click '2) Finish & import videos' first, "
                                "so your login is saved and the browser is closed.")
            return
        links, unknown = [], []
        for line in self.links_box.get("1.0", "end").splitlines():
            line = line.strip().strip('"').strip("'")
            if not line:
                continue
            urls = re.findall(r"https?://[^\s,;]+", line)
            if urls:
                links.extend(urls)
            elif Path(line).is_file():
                links.append(str(Path(line)))
            else:
                unknown.append(line)
        links = list(dict.fromkeys(links))  # remove duplicates, keep order
        seen, unique = set(), []
        for item in links:  # the same video in different link forms (youtu.be / watch?v=)
            vid = history.youtube_id(item) if item.lower().startswith("http") else None
            if vid and vid in seen:
                continue
            if vid:
                seen.add(vid)
            unique.append(item)
        links = unique
        if unknown:
            messagebox.showwarning("Not recognised", "These lines are not a link or an existing "
                                   "file and will be skipped:\n\n" + "\n".join(unknown[:8]))
        if not links:
            messagebox.showwarning("Nothing to do", "Paste a YouTube link or click "
                                                    "'Add video files...'.")
            return
        try:
            num, mn, mx, skip = (int(self.num_var.get()), int(self.min_var.get()),
                                 int(self.max_var.get()), int(self.skip_var.get()))
        except tk.TclError:
            messagebox.showwarning("Settings", "All settings must be numbers.")
            return
        if num < 1 or mn < 10 or mx < mn + 5 or skip < 0:
            messagebox.showwarning("Settings", "Check settings: clips 1+, min length 10+, "
                                               "max length at least 5 seconds more than min.")
            return

        cfg = {"num_clips": num, "min_len": mn, "max_len": mx, "skip": skip,
               "mode": self.mode_var.get(), "quality": self.quality_var.get(),
               "whisper_model": self.model_var.get(),
               "language": LANGUAGES.get(self.lang_var.get(), "auto"),
               "skip_dupes": self.dupes_var.get(),
               "cookie_browser": self.cookie_browser_var.get(),
               "cookie_file": self.cookie_file_var.get()}
        try:
            self.engine = Engine(cfg, lambda m: self.q.put(("log", m)),
                                 lambda p: self.q.put(("progress", p)))
        except RuntimeError as e:
            messagebox.showerror("ffmpeg", str(e))
            return

        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.bar["value"] = 0
        self.start_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.write_log(f"Processing {len(links)} video(s)...")
        out_dir = Path(self.out_var.get())
        threading.Thread(target=self.worker, args=(links, out_dir), daemon=True).start()

    def worker(self, links, out_dir):
        try:
            result = self.engine.run(links, out_dir)
            self.q.put(("done", result))
        except Cancelled:
            self.q.put(("cancelled", None))
        except Exception as e:
            self.q.put(("error", str(e)))

    def cancel(self):
        if self.engine:
            self.engine.stop()
            self.write_log("Cancelling...")

    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                if kind == "log":
                    self.write_log(data)
                elif kind == "progress":
                    self.bar["value"] = data * 100
                elif kind == "done":
                    zip_path, count, failed, skipped = data
                    self.write_log(f"\nDONE! {count} clips created.")
                    self.write_log(f"Your file: {zip_path}")
                    if skipped:
                        self.write_log("Skipped (already clipped before):\n  " + "\n  ".join(skipped))
                    if failed:
                        self.write_log("Failed links:\n  " + "\n  ".join(failed))
                    self.refresh_history()
                    self.finish()
                    note = f"\n\n{len(skipped)} duplicate video(s) skipped." if skipped else ""
                    messagebox.showinfo("Done", f"{count} clips saved in:\n{zip_path}{note}")
                elif kind == "browser_done":
                    urls, signed_in = data
                    existing = self.links_box.get("1.0", "end")
                    added = [u for u in urls if u not in existing]
                    for u in added:
                        self.links_box.insert("end", u + "\n")
                    self.import_btn.configure(state="normal")
                    if signed_in:
                        self.cookie_file_var.set(str(COOKIE_PATH))
                        self.cookie_browser_var.set("none")
                        self.write_log(f"Signed in - login saved. {len(added)} video(s) added to "
                                       f"the list.")
                        if not urls:
                            self.write_log("No open video tabs were found. Paste links yourself, "
                                           "or open the browser again and open a video tab.")
                        else:
                            self.write_log("Now click Start.")
                    else:
                        self.write_log(f"{len(added)} video(s) added, but you were NOT signed in, "
                                       "so no login was saved. Click '1) Open YouTube browser' "
                                       "again and sign in first.")
                elif kind == "browser_error":
                    self.write_log(f"\nBrowser problem: {data}")
                    self.import_btn.configure(state="normal")
                elif kind == "cancelled":
                    self.write_log("\nCancelled.")
                    self.finish()
                elif kind == "error":
                    self.write_log(f"\nERROR: {data}")
                    self.refresh_history()
                    self.finish()
        except queue.Empty:
            pass
        self.after(100, self.poll)

    def finish(self):
        self.start_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")


if __name__ == "__main__":
    App().mainloop()