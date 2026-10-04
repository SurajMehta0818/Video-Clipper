# Shorts Maker

Turn long YouTube videos into vertical 9:16 clips for YouTube Shorts / Instagram Reels.
All clips are packed into one ZIP file.

## Files
- app.py            - Desktop window: paste links, click Start (recommended)
- main.py           - Script version: edit SOURCES at the top, then run
- requirements.txt  - Python packages needed

## Setup (PyCharm)
1. File -> Open -> select this folder
2. Create a virtual environment (Python 3.9+) when PyCharm asks
3. Click "Install requirements" (or run: pip install -r requirements.txt)
4. Right-click app.py -> Run 'app'

## Notes
- ffmpeg is installed automatically through imageio-ffmpeg (no manual install).
- Linux only: sudo apt install python3-tk
- If downloads stop working: pip install -U yt-dlp
- Only use videos you own or have permission to reuse.
