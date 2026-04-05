#!/usr/bin/env python3
import argparse
import re
import json
import os
import subprocess
import sys
import tempfile
import shutil
import random
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
HISTORY_FILE = SCRIPT_DIR / ".shorts_history.json"

# Find yt-dlp: check explicit venv, then local venv, then system PATH
def _find_ytdlp(venv_path=None):
    if venv_path:
        venv_path = Path(venv_path).expanduser().resolve()
        candidate = venv_path / "bin" / "yt-dlp"
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
        print(f"❌  No yt-dlp found in venv: {venv_path}")
        print(    "    Make sure yt-dlp is installed there: pip install yt-dlp")
        sys.exit(1)
    # Auto-discover: local venv next to script
    local = SCRIPT_DIR / "venv" / "bin" / "yt-dlp"
    if local.exists() and os.access(local, os.X_OK):
        return str(local)
    # Fall back to system PATH
    found = shutil.which("yt-dlp")
    if found:
        return found
    return None  # Resolved at runtime so install command still works

YTDLP = None  # resolved in main()

# ─── Encoder detection ───────────────────────────────────────────────────────

def _probe_encoder(enc, global_flags=None, enc_flags=None):
    """Quick smoke-test: can ffmpeg actually use this encoder?"""
    cmd = (
        ["ffmpeg", "-hide_banner"]
        + (global_flags or [])
        + ["-f", "lavfi", "-i", "nullsrc=s=16x16:d=0.1",
           "-c:v", enc]
        + (enc_flags or [])
        + ["-f", "null", "-"]
    )
    try:
        r = subprocess.run(cmd, capture_output=True)
        return r.returncode == 0
    except FileNotFoundError:
        return False  # ffmpeg not on PATH


def detect_encoder(preferred="auto"):
    """
    Return (encoder, global_flags, enc_flags) for the best available H.264 encoder.
    - global_flags: inserted before -i inputs (e.g. -vaapi_device)
    - enc_flags:    inserted after -c:v <encoder>

    Priority: NVENC → VAAPI → libx264 (ultrafast)
    Pass preferred='nvenc'|'vaapi'|'software' to override auto-detection.
    """
    if preferred in ("nvenc", "auto"):
        nvenc_flags = ["-preset", "p2", "-rc", "vbr", "-cq", "23"]
        if _probe_encoder("h264_nvenc", enc_flags=nvenc_flags):
            return "h264_nvenc", [], nvenc_flags
        if preferred == "nvenc":
            print("❌  h264_nvenc not available on this system.")
            sys.exit(1)

    if preferred in ("vaapi", "auto"):
        # -vaapi_device must be a global option (before inputs)
        vaapi_global = ["-vaapi_device", "/dev/dri/renderD128"]
        if _probe_encoder("h264_vaapi", global_flags=vaapi_global):
            return "h264_vaapi", vaapi_global, []
        if preferred == "vaapi":
            print("❌  h264_vaapi not available on this system.")
            sys.exit(1)

    # Software fallback
    return "libx264", [], ["-preset", "ultrafast"]

# Estimated MB per minute at each quality
BITRATE_ESTIMATES = {
    "best": 8.0,
    "1080": 6.0,
    "720":  2.5,
    "480":  1.2,
    "360":  0.7,
}


# ─── Helpers ────────────────────────────────────────────────────────────────

def parse_duration(s):
    """Parse '30m', '1h', '1h30m', '90s' → seconds."""
    try:
        s = s.lower().strip()
        total = 0
        if 'h' in s:
            h, s = s.split('h', 1)
            total += int(h) * 3600
        if 'm' in s:
            m, s = s.split('m', 1)
            total += int(m) * 60
        if 's' in s:
            sec, _ = s.split('s', 1)
            total += int(sec)
        elif s.isdigit():
            total += int(s)
        return total
    except (ValueError, AttributeError):
        return 0


def fmt_duration(seconds):
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {s}s"
    if m:
        return f"{m}m {s}s"
    return f"{s}s"


def estimate_size_mb(duration_seconds, quality):
    mbpm = BITRATE_ESTIMATES.get(quality, BITRATE_ESTIMATES["best"])
    return mbpm * (duration_seconds / 60)


def load_history():
    if HISTORY_FILE.exists():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            # Ensure every value is a list of strings; drop anything malformed
            return {
                k: [x for x in v if isinstance(x, str)]
                for k, v in data.items()
                if isinstance(v, list)
            }
        except (json.JSONDecodeError, OSError, ValueError):
            print("⚠️   History file was unreadable and has been reset.")
            return {}
    return {}


def save_history(history):
    # Write atomically: temp file in same dir → rename, so a Ctrl+C mid-write can't corrupt history
    # Using NamedTemporaryFile avoids a predictable .tmp filename on shared systems
    fd, tmp_path = tempfile.mkstemp(dir=SCRIPT_DIR, prefix=".shorts_history_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(history, f, indent=2)
        os.replace(tmp_path, HISTORY_FILE)
    except Exception:
        # Clean up the temp file if something went wrong before the rename
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def channel_url(channel):
    handle = channel if channel.startswith("@") else f"@{channel}"
    # Validate: only allow characters YouTube actually uses in handles
    if not re.match(r'^@[A-Za-z0-9_.\-]{1,100}$', handle):
        print(f"❌  Invalid channel handle: {channel!r}")
        print("    Handles may only contain letters, numbers, underscores, hyphens, and dots.")
        sys.exit(1)
    return f"https://www.youtube.com/{handle}/shorts"


# ─── yt-dlp ─────────────────────────────────────────────────────────────────

def _parse_yt_lines(stdout):
    """Parse yt-dlp --flat-playlist --print output lines into short dicts."""
    _yt_id = re.compile(r'^[A-Za-z0-9_-]{5,15}$')
    shorts = []
    for line in stdout.strip().splitlines():
        if not line:
            continue
        parts = line.split("\t")
        video_id = parts[0]
        if not _yt_id.match(video_id):
            continue
        try:
            duration = int(float(parts[1])) if len(parts) > 1 and parts[1] != "NA" else None
        except (ValueError, IndexError):
            duration = None
        title = parts[3] if len(parts) > 3 else video_id
        shorts.append({
            "id": video_id,
            "duration": duration,
            "title": title,
            "url": f"https://www.youtube.com/shorts/{video_id}",
        })
    return shorts


def fetch_shorts_list(url, after=None, before=None, order="newest",
                      needed=None, seen_ids=None):
    """
    Fetch shorts from any yt-dlp-compatible URL.

    If `needed` is set (int), fetches in expanding batches until we have
    at least `needed` unseen entries, then stops — avoiding a full playlist
    scan on channels with thousands of shorts.

    If `needed` is None (duration-only mode), fetches the whole playlist.
    `seen_ids` is the set of already-downloaded IDs for smart batching.
    """
    seen_ids = seen_ids or set()

    base_cmd = [
        YTDLP,
        "--flat-playlist",
        "--print", "%(id)s\t%(duration)s\t%(upload_date)s\t%(title)s",
        "--no-warnings",
    ]
    if after:
        base_cmd += ["--dateafter", after]
    if before:
        base_cmd += ["--datebefore", before]
    if order == "oldest":
        base_cmd += ["--playlist-reverse"]

    # ── No count limit: fetch everything in one shot ──────────────────────────
    if needed is None:
        cmd = base_cmd + [url]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")
        shorts = _parse_yt_lines(result.stdout)
        if order == "random":
            random.shuffle(shorts)
        return shorts

    # ── Count mode: expand in batches until we have `needed` unseen entries ──
    # Batch sizes: 20, 40, 80, 160, 320, 500, 500, ... (doubles each round, capped at 500)
    BATCH_CAP   = 500
    fetched_end = 0       # how far into the playlist we've gone so far
    all_shorts  = []      # deduplicated ordered list of everything fetched
    seen_fetch  = set()   # IDs we've already received (dedup guard)
    batch_num   = 0       # round counter for clean doubling

    while True:
        batch_size = min(max(needed, 10) * (2 ** batch_num), BATCH_CAP)
        new_end = fetched_end + batch_size
        cmd = base_cmd + ["--playlist-start", str(fetched_end + 1),
                           "--playlist-end",   str(new_end), url]
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding="utf-8", errors="replace")

        # Count raw non-empty lines to detect exhaustion — _parse_yt_lines may
        # silently drop malformed lines, which would cause a false "exhausted" signal.
        raw_line_count = sum(1 for l in result.stdout.splitlines() if l.strip())
        batch = _parse_yt_lines(result.stdout)

        new_items = [s for s in batch if s["id"] not in seen_fetch]
        for s in new_items:
            seen_fetch.add(s["id"])
        all_shorts.extend(new_items)

        fetched_end = new_end
        batch_num += 1
        playlist_exhausted = raw_line_count < batch_size

        unseen_count = sum(1 for s in all_shorts if s["id"] not in seen_ids)

        if unseen_count >= needed or playlist_exhausted:
            break

        print(f"    ↻  {unseen_count}/{needed} new shorts found so far "
              f"(searched {fetched_end}), expanding search...")

    if order == "random":
        random.shuffle(all_shorts)

    return all_shorts


def download_short(video_id, url, quality, output_dir):
    if quality == "best":
        fmt = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]"
    else:
        fmt = f"bestvideo[height<={quality}][ext=mp4]+bestaudio[ext=m4a]/best[height<={quality}][ext=mp4]"

    out = os.path.join(output_dir, f"{video_id}.mp4")
    cmd = [
        YTDLP,
        "-f", fmt,
        "-o", out,
        "--merge-output-format", "mp4",
        "--no-warnings",
        url,
    ]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    return out if r.returncode == 0 and os.path.exists(out) else None


# ─── ffprobe / ffmpeg ────────────────────────────────────────────────────────

def get_video_duration(path):
    cmd = ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", path]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        if r.returncode == 0:
            data = json.loads(r.stdout)
            for stream in data.get("streams", []):
                if stream.get("codec_type") == "video":
                    dur = float(stream.get("duration", 0))
                    return dur if dur > 0 else None
    except (json.JSONDecodeError, ValueError):
        pass
    return None


NORMALIZE_FILTER = "scale=1080:1920:force_original_aspect_ratio=decrease,pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1"

BAR_WIDTH = 40


def _render_bar(current_s, total_s):
    """Print an in-place progress bar: [####-------] - 46%"""
    pct = min(current_s / total_s, 1.0) if total_s > 0 else 0.0
    filled = int(pct * BAR_WIDTH)
    bar = "#" * filled + "-" * (BAR_WIDTH - filled)
    print(f"\r    [{bar}] {pct * 100:.0f}%", end="", flush=True)


def _parse_out_time(line):
    """Parse 'out_time=HH:MM:SS.ffffff' → seconds, or None."""
    if not line.startswith("out_time="):
        return None
    time_str = line[len("out_time="):].strip()
    try:
        h, m, s = time_str.split(":")
        return int(h) * 3600 + int(m) * 60 + float(s)
    except (ValueError, AttributeError):
        return None


def run_ffmpeg_progress(cmd, total_seconds):
    """
    Run an ffmpeg command (which must include -progress pipe:1) and display
    a live hashtag progress bar.  Returns True on success.
    """
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        )
    except FileNotFoundError:
        print("❌  ffmpeg not found. Is it installed and on your PATH?")
        sys.exit(1)
    _render_bar(0, total_seconds)
    try:
        for line in proc.stdout:
            secs = _parse_out_time(line.strip())
            if secs is not None:
                _render_bar(secs, total_seconds)
    finally:
        proc.stdout.close()
        proc.wait()
    _render_bar(total_seconds, total_seconds)   # pin to 100% on finish
    print()                                      # move to next line
    return proc.returncode == 0


def stitch_videos(video_paths, output_path, scroll=False, encoder_tuple=None):
    """
    encoder_tuple: (enc, global_flags, enc_flags) from detect_encoder().
    If None, detect_encoder("auto") is called internally.
    """
    if encoder_tuple is None:
        encoder_tuple = detect_encoder("auto")
    enc, global_flags, enc_flags = encoder_tuple

    # VAAPI requires pixel format conversion uploaded to GPU inside the filtergraph.
    # For all other encoders this suffix is empty.
    hw_upload = ",format=nv12,hwupload" if enc == "h264_vaapi" else ""

    if not video_paths:
        return False

    if len(video_paths) == 1:
        shutil.copy(video_paths[0], output_path)
        return True

    if scroll:
        # xfade requires identical resolution — normalize every input first
        inputs = []
        for p in video_paths:
            inputs += ["-i", p]

        durations = [get_video_duration(p) or 0.0 for p in video_paths]
        n = len(video_paths)

        filter_parts = []

        # Scale/pad each input to 1080x1920 (+ optional hw upload for VAAPI)
        for i in range(n):
            filter_parts.append(f"[{i}:v]{NORMALIZE_FILTER}{hw_upload}[sv{i}]")

        # Chain xfade transitions
        offset = 0.0
        current = "[sv0]"
        for i in range(1, n):
            offset += max(durations[i - 1] - 0.5, 0)
            nxt = f"[sv{i}]"
            out_label = f"[xv{i}]" if i < n - 1 else "[vout]"
            filter_parts.append(
                f"{current}{nxt}xfade=transition=slideup:duration=0.5:offset={offset:.3f}{out_label}"
            )
            current = f"[xv{i}]"

        audio_in = "".join(f"[{i}:a]" for i in range(n))
        filter_parts.append(f"{audio_in}concat=n={n}:v=0:a=1[aout]")

        filtergraph = ";".join(filter_parts)
        # Total output duration is shorter than raw sum due to xfade overlaps
        total_out_secs = max(sum(durations) - (n - 1) * 0.5, 0.1)
        cmd = (
            ["ffmpeg"]
            + global_flags
            + inputs
            + ["-filter_complex", filtergraph,
               "-map", "[vout]",
               "-map", "[aout]",
               "-c:v", enc]
            + enc_flags
            + ["-c:a", "aac",
               "-shortest",     # xfade shortens video vs audio; trim to the shorter stream
               "-progress", "pipe:1",
               "-nostats",
               "-y", output_path]
        )
        return run_ffmpeg_progress(cmd, total_out_secs)

    else:
        # Try fast stream-copy concat first
        with tempfile.NamedTemporaryFile(mode="w", suffix=".txt", delete=False) as f:
            for p in video_paths:
                # ffmpeg concat demuxer format: escape single quotes, strip newlines
                safe_p = os.path.abspath(p).replace("\n", "").replace("\r", "").replace("'", "'\\''")
                f.write(f"file '{safe_p}'\n")
            list_file = f.name

        cmd = [
            "ffmpeg", "-fflags", "+genpts",  # Add this line
            "-f", "concat", "-safe", "0",
            "-i", list_file, "-c", "copy", "-y", output_path,
        ]
        try:
            r = subprocess.run(cmd, stdout=subprocess.DEVNULL)
        finally:
            os.unlink(list_file)

        if r.returncode == 0:
            return True

        # Fallback: re-encode with normalization (handles codec/resolution mismatches)
        print("    ⚠️  Stream copy failed, re-encoding (this takes longer)...")
        inputs = []
        for p in video_paths:
            inputs += ["-i", p]

        durations = [get_video_duration(p) or 0.0 for p in video_paths]
        filter_parts = [f"[{i}:v]{NORMALIZE_FILTER}{hw_upload}[sv{i}]" for i in range(len(video_paths))]
        n = len(video_paths)
        video_in = "".join(f"[sv{i}]" for i in range(n))
        audio_in = "".join(f"[{i}:a]" for i in range(n))
        filter_parts.append(f"{video_in}concat=n={n}:v=1:a=0[vout]")
        filter_parts.append(f"{audio_in}concat=n={n}:v=0:a=1[aout]")
        filtergraph = ";".join(filter_parts)

        total_out_secs = max(sum(durations), 0.1)
        cmd = (
            ["ffmpeg", "-fflags", "+genpts"]  # ← ADD THIS LINE (changes this line)
            + global_flags
            + inputs
            + ["-filter_complex", filtergraph,
               "-map", "[vout]", "-map", "[aout]",
               "-c:v", enc]
            + enc_flags
            + ["-c:a", "aac",
               "-progress", "pipe:1",
               "-nostats",
               "-y", output_path]
        )
        return run_ffmpeg_progress(cmd, total_out_secs)


# ─── Main ────────────────────────────────────────────────────────────────────

HELP_TEXT = """
╔══════════════════════════════════════════════════════════════╗
║                        shortdl.py                           ║
║          Download & stitch YouTube Shorts into one video     ║
╚══════════════════════════════════════════════════════════════╝

USAGE:
  python3 shortdl.py --channel CHANNEL (--duration TIME | --count N) --name NAME [options]
  python3 shortdl.py --hashtag TAG     (--duration TIME | --count N) --name NAME [options]
  python3 shortdl.py install
  python3 shortdl.py help

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 COMMANDS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  install
    Install all required dependencies (yt-dlp, ffmpeg, ffprobe).
    Will ask if you want yt-dlp in a local venv or installed globally.
    Structure:  python3 shortdl.py install
    Example:    python3 shortdl.py install

  help
    Show this help message and exit.
    Structure:  python3 shortdl.py help
    Example:    python3 shortdl.py help

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 SOURCE FLAGS (use one)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --channel @handle [...]
    Pull shorts from one or more YouTube channels. Use the @ handle.
    Pass multiple handles space-separated to mix channels together —
    shorts will be interleaved evenly in the output.
    Structure:  --channel <@handle> [<@handle> ...]
    Examples:   --channel @mkbhd
                --channel @mkbhd @linustechtips @veritasium

  --hashtag TAG
    Pull shorts from a YouTube hashtag page instead of a channel.
    The # is optional — both #fyp and fyp work.
    Structure:  --hashtag <tag>
    Examples:   --hashtag cooking
                --hashtag #funny

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 LIMIT FLAGS (use at least one; both can be combined)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --duration TIME
    Stop downloading once the total stitched duration reaches this.
    Accepts hours, minutes, seconds or a combo.
    Structure:  --duration <Xh><Xm><Xs>  (mix and match)
    Examples:   --duration 30m
                --duration 1h
                --duration 1h30m
                --duration 90s

  --count N
    Stop after downloading exactly N shorts, regardless of duration.
    Can be combined with --duration — whichever limit is hit first wins.
    Structure:  --count <integer>
    Examples:   --count 10
                --count 25

  --name FILENAME
    What to call the output file (no extension needed).
    Only letters, numbers, hyphens and underscores allowed.
    Structure:  --name <filename>
    Example:    --name mkbhd_compilation

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 OPTIONAL FLAGS
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  --channels-per-short N
    When using multiple --channel handles, cap how many shorts are
    taken from each channel per run. Useful to keep the mix balanced
    when one channel has way more shorts than another.
    Structure:  --channels-per-short <integer>
    Example:    --channel @mkbhd @linustechtips --count 20 --channels-per-short 10

  --quality QUALITY
    Resolution to download. Defaults to best available.
    Choices:    best | 1080 | 720 | 480 | 360
    Structure:  --quality <choice>
    Example:    --quality 720

  --order ORDER
    Order to select and stitch shorts in. Defaults to newest first.
    Choices:    newest | oldest | random
    Structure:  --order <choice>
    Example:    --order random

  --after YYYYMMDD
    Only include shorts uploaded on or after this date.
    Structure:  --after <YYYYMMDD>
    Example:    --after 20240101   (Jan 1st 2024 onwards)

  --before YYYYMMDD
    Only include shorts uploaded on or before this date.
    Can be combined with --after to set a date range.
    Structure:  --before <YYYYMMDD>
    Example:    --before 20241231  (up to Dec 31st 2024)

  --scroll
    Adds a smooth upward scroll animation between each short,
    like naturally scrolling through the app. Off by default.
    Structure:  --scroll  (no value needed, just the flag)
    Example:    python3 shortdl.py --channel @mkbhd --count 10 --name test --scroll

  -o / --output PATH
    Directory to save the final video to.
    Defaults to the same folder as this script.
    Structure:  -o <path>  or  --output <path>
    Examples:   -o ~/Videos
                --output /mnt/usb/shorts

  --no-confirm
    Skips the file size estimate and "Continue? [Y/n]" prompt.
    Useful for automated or unattended runs.
    Structure:  --no-confirm  (no value needed, just the flag)
    Example:    python3 shortdl.py --channel @mkbhd --duration 1h --name test --no-confirm

  --no-history
    Ignores the history file for this run — all shorts are eligible,
    including ones you've already downloaded before.
    The history file is NOT updated after the run either, so existing
    entries are preserved and nothing new gets recorded.
    Useful for re-downloading a channel from scratch, or making a
    second compilation with overlapping content.
    Structure:  --no-history  (no value needed, just the flag)
    Example:    python3 shortdl.py --channel @mkbhd --count 20 --name mkbhd_redux --no-history

  --venv PATH
    Point to a specific Python venv that has yt-dlp installed.
    By default the script checks for a ./venv folder next to itself,
    then falls back to whatever yt-dlp is on your system PATH.
    Use this if your yt-dlp lives in a different venv.
    Structure:  --venv <path/to/venv>
    Examples:   --venv ~/envs/media
                --venv /home/mack/projects/yttools/venv

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FULL EXAMPLES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  # 30 mins of the newest @mkbhd shorts, saved next to the script
  python3 shortdl.py --channel @mkbhd --duration 30m --name mkbhd_shorts

  # Exactly 15 shorts from @mkbhd, random order, scroll transitions
  python3 shortdl.py --channel @mkbhd --count 15 --order random --scroll --name mkbhd_mix

  # Mix 3 channels — 30 shorts total, max 10 per channel
  python3 shortdl.py --channel @mkbhd @linustechtips @veritasium --count 30 --channels-per-short 10 --name mixed_tech

  # 1 hour of mixed content from 2 channels, 720p, saved to ~/Videos
  python3 shortdl.py --channel @mkbhd @linustechtips --duration 1h --quality 720 --name tech_mix -o ~/Videos

  # 10 shorts tagged #cooking, newest first
  python3 shortdl.py --hashtag cooking --count 10 --name cooking_shorts

  # 30 mins of #funny shorts from 2024, random order, skip confirmation
  python3 shortdl.py --hashtag funny --duration 30m --after 20240101 --before 20241231 --order random --name funny_2024 --no-confirm

  # Only channel shorts from 2024, skip the confirmation prompt
  python3 shortdl.py --channel @mkbhd --duration 45m --after 20240101 --before 20241231 --name mkbhd_2024 --no-confirm

  # Use a specific venv for yt-dlp
  python3 shortdl.py --channel @mkbhd --count 10 --name test --venv ~/envs/media

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 FINDING YOUR YT-DLP VENV
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  Not sure where your yt-dlp is installed? Run this:

    which yt-dlp
    # e.g. output: /home/mack/venv/bin/yt-dlp
    # your venv path would be:  /home/mack/venv

  If yt-dlp isn't on your PATH (e.g. it's inside a venv you haven't
  activated), you can search for it:

    find ~ -name yt-dlp 2>/dev/null
    # e.g. output: /home/mack/projects/media/venv/bin/yt-dlp
    # your venv path would be:  /home/mack/projects/media/venv

  Then pass that venv to the script:
    python3 shortdl.py --channel @mkbhd --duration 30m --name test --venv /home/mack/projects/media/venv

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 MANAGING HISTORY
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  History is stored in .shorts_history.json next to this script.
  It maps each channel (without @) to a list of downloaded video IDs:

    {
      "mkbhd": ["dQw4w9WgXcQ", "abc123xyz", ...],
      "linustechtips": ["xyz789abc"]
    }

  TO IGNORE HISTORY FOR ONE RUN (don't reset it, just bypass it):
    Add --no-history to your command. The file won't be read or
    written, so existing entries are untouched.
    Example:  python3 shortdl.py --channel @mkbhd --count 10 --name test --no-history

  TO RESET A SINGLE CHANNEL (re-download everything for @mkbhd):
    Open .shorts_history.json and delete the "mkbhd" key, or run:
      python3 -c "
      import json; f='.shorts_history.json'
      d=json.load(open(f)); d.pop('mkbhd', None); json.dump(d, open(f,'w'), indent=2)
      "

  TO RESET ALL HISTORY (start fresh for every channel):
    Just delete the file:
      rm .shorts_history.json

  TO SEE WHAT'S IN HISTORY:
      cat .shorts_history.json
    Or for a count per channel:
      python3 -c "
      import json
      d=json.load(open('.shorts_history.json'))
      [print(f'  @{k}: {len(v)} shorts seen') for k,v in d.items()]
      "

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 NOTES
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━

  • Already-downloaded shorts are tracked in .shorts_history.json
    next to this script, so re-running won't re-download them.
  • Requires: yt-dlp, ffmpeg, ffprobe
  • First time setup:  python3 shortdl.py install
"""


# ─── Install ─────────────────────────────────────────────────────────────────

def check_installed(cmd):
    return shutil.which(cmd) is not None


def run_install():
    print("\n📦  shortdl dependency installer\n")

    # ── ffmpeg / ffprobe (system package, use apt) ──
    ffmpeg_ok  = check_installed("ffmpeg")
    ffprobe_ok = check_installed("ffprobe")

    if ffmpeg_ok and ffprobe_ok:
        print("✅  ffmpeg & ffprobe already installed")
    else:
        missing = []
        if not ffmpeg_ok:  missing.append("ffmpeg")
        if not ffprobe_ok: missing.append("ffprobe")
        print(f"⚙️   Need to install: {' '.join(missing)}")
        ans = input("    This requires running 'sudo apt install ffmpeg'. Continue? [Y/n] ").strip().lower()
        if ans == "n":
            print("    Skipping. Install manually: sudo apt install ffmpeg")
            sys.exit(1)
        print(f"⚙️   Installing via apt...")
        r = subprocess.run(["sudo", "apt", "install", "-y", "ffmpeg"], text=True)
        if r.returncode == 0:
            print("✅  ffmpeg & ffprobe installed")
        else:
            print("❌  apt install failed. Try manually: sudo apt install ffmpeg")
            sys.exit(1)

    print()

    # ── yt-dlp (pip package) ──
    ytdlp_ok = check_installed("yt-dlp")
    if ytdlp_ok:
        print("✅  yt-dlp already installed")
        print("\n🎉  All dependencies satisfied. You're good to go!")
        print("    Run  python3 shortdl.py help  to get started.\n")
        sys.exit(0)

    print("🐍  yt-dlp needs to be installed via pip.")
    print("    It's best practice to use a virtual environment (venv)")
    print("    so it doesn't interfere with your system Python.\n")
    ans = input("    Create a venv in this folder? [Y/n] ").strip().lower()

    if ans == "n":
        print("\n⚙️   Installing yt-dlp globally...")
        r = subprocess.run([sys.executable, "-m", "pip", "install", "yt-dlp"], text=True)
        if r.returncode == 0:
            print("✅  yt-dlp installed globally")
        else:
            print("❌  pip install failed. Try manually: pip install yt-dlp")
            sys.exit(1)
    else:
        venv_dir = SCRIPT_DIR / "venv"

        if venv_dir.exists():
            print(f"\n📁  venv already exists at {venv_dir}")
        else:
            print(f"\n⚙️   Creating venv at {venv_dir}...")
            r = subprocess.run([sys.executable, "-m", "venv", str(venv_dir)], text=True)
            if r.returncode != 0:
                print("❌  Failed to create venv. Is python3-venv installed?")
                print("    Try: sudo apt install python3-venv")
                sys.exit(1)
            print("✅  venv created")

        pip = venv_dir / "bin" / "pip"
        if not pip.exists():
            print("❌  pip not found inside venv — venv may be corrupted.")
            print(f"    Try deleting {venv_dir} and running install again.")
            sys.exit(1)
        print("⚙️   Installing yt-dlp into venv...")
        r = subprocess.run([str(pip), "install", "yt-dlp"], text=True)
        if r.returncode == 0:
            print("✅  yt-dlp installed into venv")
        else:
            print("❌  pip install failed inside venv.")
            sys.exit(1)

        print(f"""
⚠️   yt-dlp is inside the venv, so always run shortdl.py with the venv active:

    source {venv_dir}/bin/activate
    python3 shortdl.py --channel @mkbhd --duration 30m --name test

    Or permanently activate it in your shell:
    echo 'source {venv_dir}/bin/activate' >> ~/.bashrc && source ~/.bashrc
""")

    print("🎉  All dependencies installed. Run  python3 shortdl.py help  to get started.\n")
    sys.exit(0)


def main():
    if len(sys.argv) == 2 and sys.argv[1] == "help":
        print(HELP_TEXT)
        sys.exit(0)

    if len(sys.argv) == 2 and sys.argv[1] == "install":
        run_install()

    parser = argparse.ArgumentParser(
        description="Download YouTube Shorts and stitch them into one video."
    )
    parser.add_argument("--channel",   nargs="+", help="One or more channel handles, e.g. --channel @mkbhd @linustechtips")
    parser.add_argument("--hashtag",   help="Hashtag to search, e.g. cooking or #funny")
    parser.add_argument("--channels-per-short", type=int, default=None, metavar="N",
                        help="Max shorts taken from each channel per run (multi-channel only)")
    parser.add_argument("--duration",  help="Target duration, e.g. 30m / 1h / 1h30m")
    parser.add_argument("--count",     type=int, help="Max number of shorts to download, e.g. 10")
    parser.add_argument("--name",      required=True,  help="Output filename (no extension), e.g. mkbhd_shorts")
    parser.add_argument("--quality",   default="best", choices=["best", "1080", "720", "480", "360"])
    parser.add_argument("--after",     help="Only shorts after YYYYMMDD")
    parser.add_argument("--before",    help="Only shorts before YYYYMMDD")
    parser.add_argument("--order",     default="newest", choices=["newest", "oldest", "random"])
    parser.add_argument("--scroll",    action="store_true", help="Add slideup scroll transition between shorts")
    parser.add_argument("--encoder",   default="auto", choices=["auto", "nvenc", "vaapi", "software"],
                        help="Video encoder (default: auto-detect best available)")
    parser.add_argument("--output",    "-o", help="Output directory (default: same folder as script)")
    parser.add_argument("--no-confirm", action="store_true", help="Skip size estimate confirmation")
    parser.add_argument("--no-history", action="store_true", help="Ignore history and re-download already-seen shorts")
    parser.add_argument("--venv",      help="Path to a venv containing yt-dlp, e.g. --venv ~/my_venv")
    args = parser.parse_args()

    # ── Validate source ───────────────────────────────────────────────────────
    if not args.channel and not args.hashtag:
        print("❌  You must provide either --channel @handle(s) or --hashtag TAG.")
        sys.exit(1)
    if args.channel and args.hashtag:
        print("❌  Use either --channel or --hashtag, not both.")
        sys.exit(1)
    if args.hashtag and args.channels_per_short is not None:
        print("❌  --channels-per-short only applies when using --channel with multiple channels.")
        sys.exit(1)
    if args.channels_per_short is not None and args.channels_per_short < 1:
        print("❌  --channels-per-short must be a positive integer.")
        sys.exit(1)

    # ── Validate limits ───────────────────────────────────────────────────────
    if args.count is not None and args.count < 1:
        print("❌  --count must be a positive integer (got 0 or negative).")
        sys.exit(1)
    if not args.duration and not args.count:
        print("❌  You must provide at least one of --duration or --count.")
        sys.exit(1)

    global YTDLP
    YTDLP = _find_ytdlp(args.venv)
    if YTDLP is None:
        print("❌  yt-dlp not found. Run  python3 shortdl.py install  to set it up.")
        sys.exit(1)
    if args.venv:
        print(f"🐍  Using yt-dlp from venv: {Path(args.venv).expanduser().resolve()}")

    # Validate date filters
    date_re = re.compile(r'^\d{8}$')
    for flag, val in [("--after", args.after), ("--before", args.before)]:
        if val and not date_re.match(val):
            print(f"❌  {flag} must be in YYYYMMDD format (e.g. 20240101), got: {val!r}")
            sys.exit(1)

    # Resolve output path
    out_dir = Path(args.output) if args.output else SCRIPT_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    # Sanitize filename — only allow alphanumeric, underscores, hyphens, max 200 chars
    safe_name = re.sub(r"[^\w\-]", "_", args.name)[:200]
    if safe_name != args.name:
        print(f"⚠️   Name sanitized to: {safe_name}")
    output_path = out_dir / f"{safe_name}.mp4"

    # Warn if output file already exists (ffmpeg would silently overwrite it)
    if output_path.exists():
        ans = input(f"⚠️   {output_path.name} already exists. Overwrite? [Y/n] ").strip().lower()
        if ans == "n":
            print("Aborted.")
            sys.exit(0)

    target_seconds = parse_duration(args.duration) if args.duration else None
    if args.duration and not target_seconds:
        print("❌  Could not parse --duration. Use formats like 30m, 1h, 1h30m.")
        sys.exit(1)

    target_count = args.count  # may be None

    # Size estimate + confirmation
    if not args.no_confirm:
        if target_seconds:
            est = estimate_size_mb(target_seconds, args.quality)
            limit_str = f"Duration: {fmt_duration(target_seconds)}"
            if target_count:
                limit_str += f"  |  Max shorts: {target_count}"
            print(f"\n📊  Estimated size: ~{est:.0f} MB  |  {limit_str}  |  Quality: {args.quality}")
        else:
            print(f"\n📊  Count limit: {target_count} short(s)  |  Quality: {args.quality}")
            print("    (Size estimate unavailable for count-only mode)")
        ans = input("    Continue? [Y/n] ").strip().lower()
        if ans == "n":
            print("Aborted.")
            sys.exit(0)

    # Load history early so we can pass seen IDs into the smart fetcher
    history = load_history()

    # ── Build list of (source_url, history_key) pairs ────────────────────────
    if args.hashtag:
        tag = args.hashtag.lstrip("#")
        if not re.match(r'^[A-Za-z0-9_\-]{1,100}$', tag):
            print(f"❌  Invalid hashtag: {args.hashtag!r}")
            print("    Hashtags may only contain letters, numbers, underscores, and hyphens.")
            sys.exit(1)
        sources = [(f"https://www.youtube.com/hashtag/{tag}/shorts", f"#{tag}")]
    else:
        sources = []
        for ch in args.channel:
            url = channel_url(ch)   # validates and exits on bad handle
            key = ch.lstrip("@")
            sources.append((url, key))

    # ── Fetch candidate shorts from each source ───────────────────────────────
    # For multi-channel we fetch per-source, respecting --channels-per-short,
    # then interleave so the final list alternates between channels.
    per_source_needed = None
    if target_count:
        if len(sources) == 1:
            per_source_needed = target_count
        else:
            # With a per-channel cap, use that; otherwise spread evenly with headroom
            if args.channels_per_short:
                per_source_needed = args.channels_per_short
            else:
                # Fetch enough from each to comfortably fill the total
                per_source_needed = max(target_count, 10)

    all_candidates = []   # flat interleaved list of shorts across sources
    source_pools  = {}    # history_key → [shorts from that source]

    for source_url, history_key in sources:
        seen_this = set(history.get(history_key, []))
        label = f"#{history_key}" if history_key.startswith("#") else f"@{history_key}"
        print(f"\n🔍  Fetching shorts from {label}...")
        pool = fetch_shorts_list(
            source_url,
            after=args.after,
            before=args.before,
            order=args.order,
            needed=per_source_needed,
            seen_ids=seen_this if not args.no_history else set(),
        )
        if not pool:
            print(f"    ⚠️  No shorts found for {label}, skipping.")
            continue
        print(f"    Found {len(pool)} shorts")
        source_pools[history_key] = pool

    if not source_pools:
        print("❌  No shorts found from any source. Check handles / hashtag and date filters.")
        sys.exit(1)

    # Interleave: round-robin across sources so the mix is even
    if len(source_pools) == 1:
        all_candidates = list(source_pools.values())[0]
    else:
        pools = list(source_pools.values())
        max_len = max(len(p) for p in pools)
        for i in range(max_len):
            for pool in pools:
                if i < len(pool):
                    all_candidates.append(pool[i])

    # Apply global history filter
    if args.no_history:
        print("\n    ⚠️  History ignored — all shorts eligible for download")
        new_shorts = all_candidates
    else:
        all_seen = set()
        for history_key in source_pools:
            all_seen.update(history.get(history_key, []))
        new_shorts = [s for s in all_candidates if s["id"] not in all_seen]
        skipped = len(all_candidates) - len(new_shorts)
        if skipped:
            print(f"\n    Skipping {skipped} already-downloaded short(s)")

    if not new_shorts:
        print("⚠️   No new shorts to download.")
        sys.exit(0)

    # ── Download loop ─────────────────────────────────────────────────────────
    # Track per-source download counts for --channels-per-short enforcement
    per_source_counts: dict[str, int] = {k: 0 for k in source_pools}
    # Map video ID → history_key so we know which source to credit
    id_to_source: dict[str, str] = {}
    for hk, pool in source_pools.items():
        for s in pool:
            id_to_source[s["id"]] = hk
    # Track seen per source for history saving
    seen_per_source: dict[str, set] = {
        hk: set(history.get(hk, [])) for hk in source_pools
    }

    with tempfile.TemporaryDirectory() as tmpdir:
        downloaded = []
        total_seconds = 0.0

        for short in new_shorts:
            # Global limits
            if target_seconds is not None and total_seconds >= target_seconds:
                break
            if target_count is not None and len(downloaded) >= target_count:
                break

            # Per-channel cap
            src_key = id_to_source.get(short["id"])
            if (args.channels_per_short is not None and src_key is not None
                    and per_source_counts.get(src_key, 0) >= args.channels_per_short):
                continue  # skip — this channel has hit its cap, keep looking

            title_preview = short["title"][:55]
            print(f"⬇️   {title_preview}...")
            path = download_short(short["id"], short["url"], args.quality, tmpdir)

            if path:
                dur = get_video_duration(path)
                if not dur:
                    print(f"    ✗ Could not read duration, skipping.")
                    continue
                downloaded.append(path)
                total_seconds += dur
                if src_key:
                    per_source_counts[src_key] = per_source_counts.get(src_key, 0) + 1
                    seen_per_source[src_key].add(short["id"])
                # Build progress label
                if target_seconds and target_count:
                    progress_suffix = f"{fmt_duration(total_seconds)} / {fmt_duration(target_seconds)}  |  {len(downloaded)}/{target_count} shorts"
                elif target_seconds:
                    progress_suffix = f"{fmt_duration(total_seconds)} / {fmt_duration(target_seconds)}"
                else:
                    progress_suffix = f"{len(downloaded)}/{target_count} shorts"
                print(f"    ✓ +{fmt_duration(dur)}  →  {progress_suffix}")
            else:
                print(f"    ✗ Failed to download, skipping.")

        if not downloaded:
            print("❌  No shorts were downloaded successfully.")
            sys.exit(1)

        def _save_all_history():
            if args.no_history:
                return
            for hk, seen_set in seen_per_source.items():
                history[hk] = list(seen_set)
            save_history(history)

        # Not enough content? (only relevant when --duration is set)
        if target_seconds and total_seconds < target_seconds:
            print(f"\n⚠️   Only {fmt_duration(total_seconds)} of content available (wanted {fmt_duration(target_seconds)}).")
            _save_all_history()
            ans = input("    Stitch what we have anyway? [Y/n] ").strip().lower()
            if ans == "n":
                print("Aborted. Progress saved — those shorts won't be re-downloaded.")
                sys.exit(0)

        # Stitch
        enc_tuple = detect_encoder(args.encoder)
        enc_label = enc_tuple[0]
        print(f"\n✂️   Stitching {len(downloaded)} short(s){' with scroll transitions' if args.scroll else ''}  [{enc_label}]...")
        ok = stitch_videos(downloaded, str(output_path), scroll=args.scroll, encoder_tuple=enc_tuple)

        if ok:
            size_mb = os.path.getsize(output_path) / (1024 * 1024)
            print(f"\n✅  Done!  →  {output_path}  ({size_mb:.1f} MB, {fmt_duration(total_seconds)}, {len(downloaded)} shorts)")
            _save_all_history()
        else:
            print("❌  Stitching failed. Check that ffmpeg is installed.")
            sys.exit(1)


if __name__ == "__main__":
    main()
