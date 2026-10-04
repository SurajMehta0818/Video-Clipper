"""
Shorts Maker
------------
1. Downloads long YouTube videos (or uses local video files)
2. Cuts them into vertical 9:16 clips for YouTube Shorts / Instagram Reels
3. Packs ALL clips into one ZIP file you can download

HOW TO USE IN PYCHARM
    1. Put your YouTube links in SOURCES below
    2. Right-click this file -> Run 'main'
    3. Collect  shorts_clips.zip  from the project folder

You can also run from a terminal:
    python main.py https://www.youtube.com/watch?v=XXXX https://youtu.be/YYYY
"""

import random
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import yt_dlp

# =====================================================================
#                            SETTINGS
# =====================================================================

# YouTube links and/or paths to local video files
SOURCES = [
    "https://www.youtube.com/watch?v=XXXXXXXXXXX",
    # "https://youtu.be/YYYYYYYYYYY",
    # r"C:\Videos\my_long_video.mp4",
]

NUM_CLIPS = 12          # random clips per video
MIN_LEN = 50            # shortest clip (seconds)
MAX_LEN = 55            # longest clip (seconds)
SKIP_EDGES = 20         # seconds to skip at start and end (intro / outro)

MODE = "blur"           # "blur" = full video over blurred background
                        # "crop" = zoom + center-crop to fill the screen
WIDTH, HEIGHT = 1080, 1920
FPS = 30
MAX_DOWNLOAD_HEIGHT = 1080   # max quality to download from YouTube

ZIP_NAME = "shorts_clips.zip"
KEEP_ORIGINAL_DOWNLOADS = False

# =====================================================================

BASE_DIR = Path(__file__).resolve().parent
WORK_DIR = BASE_DIR / "work"
DOWNLOAD_DIR = WORK_DIR / "downloads"
CLIPS_DIR = WORK_DIR / "clips"


def get_ffmpeg() -> str:
    """Use system ffmpeg if installed, otherwise the one bundled via pip."""
    exe = shutil.which("ffmpeg")
    if exe:
        return exe
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found. Run: pip install -r requirements.txt")


FFMPEG = get_ffmpeg()


def safe_name(text: str) -> str:
    text = re.sub(r'[\\/:*?"<>|]+', "", text).strip()
    text = re.sub(r"\s+", "_", text)
    return text[:60] or "video"


def get_duration(path: Path) -> float:
    """Read video duration by parsing ffmpeg's output."""
    result = subprocess.run([FFMPEG, "-i", str(path)], capture_output=True, text=True)
    m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
    if not m:
        raise RuntimeError(f"Could not read duration of {path.name}")
    h, mi, s = m.groups()
    return int(h) * 3600 + int(mi) * 60 + float(s)


def download(url: str):
    """Download a YouTube video. Returns (file_path, title)."""
    DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
    opts = {
        "format": f"bv*[height<={MAX_DOWNLOAD_HEIGHT}]+ba/b[height<={MAX_DOWNLOAD_HEIGHT}]/best",
        "outtmpl": str(DOWNLOAD_DIR / "%(id)s.%(ext)s"),
        "merge_output_format": "mp4",
        "ffmpeg_location": FFMPEG,
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "progress_hooks": [_progress],
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        path = Path(ydl.prepare_filename(info))
    mp4 = path.with_suffix(".mp4")
    if mp4.exists():
        path = mp4
    return path, info.get("title", path.stem)


def _progress(d):
    if d["status"] == "downloading":
        pct = d.get("_percent_str", "").strip()
        print(f"\r   downloading... {pct}", end="", flush=True)
    elif d["status"] == "finished":
        print("\r   download finished.          ")


def build_filter() -> str:
    if MODE == "crop":
        return (
            f"scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
            f"crop={WIDTH}:{HEIGHT},setsar=1"
        )
    return (
        f"split=2[bg][fg];"
        f"[bg]scale={WIDTH}:{HEIGHT}:force_original_aspect_ratio=increase,"
        f"crop={WIDTH}:{HEIGHT},boxblur=25:5[bgb];"
        f"[fg]scale={WIDTH}:-2[fgs];"
        f"[bgb][fgs]overlay=(W-w)/2:(H-h)/2,setsar=1"
    )


def cut_clip(src: Path, dst: Path, start: float, length: float, vf: str):
    cmd = [
        FFMPEG, "-y", "-loglevel", "error",
        "-ss", f"{start:.3f}", "-i", str(src), "-t", f"{length:.3f}",
        "-vf", vf, "-r", str(FPS),
        "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(dst),
    ]
    subprocess.run(cmd, check=True)


def plan_random(total: float):
    """Pick random, non-overlapping (start, length) spots across the whole video."""
    start0, end0 = float(SKIP_EDGES), total - SKIP_EDGES
    if end0 - start0 < MIN_LEN:
        start0, end0 = 0.0, total
    usable = end0 - start0
    if usable < MIN_LEN:
        return []
    n = min(NUM_CLIPS, int(usable // MAX_LEN))
    if n == 0:
        return [(start0, min(usable, MAX_LEN))]
    if n < NUM_CLIPS:
        print(f"   video is short - only {n} clips fit")
    lengths = [random.uniform(MIN_LEN, MAX_LEN) for _ in range(n)]
    free = usable - sum(lengths)
    cuts = sorted(random.uniform(0, free) for _ in range(n))
    plan, used = [], 0.0
    for cut, ln in zip(cuts, lengths):
        plan.append((start0 + cut + used, ln))
        used += ln
    return plan


def make_clips(video: Path, title: str) -> int:
    total = get_duration(video)
    plan = plan_random(total)
    if not plan:
        raise RuntimeError("video is too short")

    folder = CLIPS_DIR / safe_name(title)
    folder.mkdir(parents=True, exist_ok=True)
    vf = build_filter()

    print(f"   {total / 60:.1f} min video -> {len(plan)} random clips")
    for i, (start, length) in enumerate(plan, 1):
        at = f"{int(start // 60)}m{int(start % 60):02d}s"
        dst = folder / f"{safe_name(title)}_clip{i:02d}_{at}.mp4"
        print(f"   [{i}/{len(plan)}] from {at}, {length:.0f}s")
        cut_clip(video, dst, start, length, vf)
    return len(plan)


def zip_clips() -> Path:
    zip_path = BASE_DIR / ZIP_NAME
    files = sorted(CLIPS_DIR.rglob("*.mp4"))
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_STORED) as z:  # mp4 is already compressed
        for f in files:
            z.write(f, f.relative_to(CLIPS_DIR))
    return zip_path


def main():
    sources = sys.argv[1:] or SOURCES
    sources = [s for s in sources if "XXXXXXXX" not in s]
    if not sources:
        sys.exit("Add at least one YouTube link to SOURCES in main.py")

    # fresh run
    if CLIPS_DIR.exists():
        shutil.rmtree(CLIPS_DIR)
    CLIPS_DIR.mkdir(parents=True)

    total_clips = 0
    for n, src in enumerate(sources, 1):
        print(f"\n({n}/{len(sources)}) {src}")
        try:
            if src.lower().startswith(("http://", "https://")):
                video, title = download(src)
            else:
                video = Path(src)
                if not video.is_file():
                    print("   file not found, skipping")
                    continue
                title = video.stem
            total_clips += make_clips(video, title)
            if src.lower().startswith("http") and not KEEP_ORIGINAL_DOWNLOADS:
                video.unlink(missing_ok=True)
        except Exception as e:  # keep going if one link fails
            print(f"   FAILED: {e}")

    if total_clips == 0:
        sys.exit("\nNo clips were created.")

    zip_path = zip_clips()
    print(f"\nDone! {total_clips} clips saved.")
    print(f"Download this file: {zip_path}")


if __name__ == "__main__":
    main()