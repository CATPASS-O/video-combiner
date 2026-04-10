"""
Storytelling Channel – Video Combiner  v8.0
==========================================
NavyCat Studio  |  Built with Claude (Anthropic AI)
Optimized for: Intel Core i3 2nd Gen  +  4 GB RAM

v7 Changes
----------
• FIXED: RAM error "Unable to allocate 21.1 MiB" – silent_audio now
  returns float32 numpy array instead of python list (3x less RAM)
• FIXED: crop_fill_numpy returns uint8 (not float64)
• Console output window – real-time progress visible & copy-able
• Settings auto-save/load (navycat_settings.json next to the .py file)
• All temp files written to system temp folder, cleaned up on exit
• Temp folder shown in UI so user knows where intermediate files go
"""

import os, sys, json, random, threading, gc, tempfile, subprocess, atexit, shutil
import tkinter as tk
from tkinter import filedialog, messagebox, ttk, scrolledtext
import numpy as np

# -- Settings file (same dir as script) -------------------
SCRIPT_DIR   = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "navycat_settings.json")

# -- Temp working folder (auto-cleaned on exit) ------------
# Use Documents\NavyCat_Temp – avoids AppData permission issues
# that cause "accessible licence" errors with some MS Store apps.
def _make_tmp_root():
    """
    Choose temp folder. Priority:
    1. NavyCat_Temp next to the script  (safest, Defender-friendly)
    2. Documents/NavyCat_Temp
    Never uses AppData, system Temp, or WindowsApps paths.
    """
    candidates = [
        os.path.join(SCRIPT_DIR, "NavyCat_Temp"),
        os.path.join(os.path.expanduser("~"), "Documents", "NavyCat_Temp"),
        os.path.join(os.path.expanduser("~"), "NavyCat_Temp"),
    ]
    for path in candidates:
        try:
            os.makedirs(path, exist_ok=True)
            test = os.path.join(path, "_test.tmp")
            with open(test, "w") as f:
                f.write("ok")
            os.remove(test)
            return path
        except Exception:
            continue
    # Last resort: script dir itself
    return SCRIPT_DIR

TMP_ROOT = _make_tmp_root()

def _cleanup_tmp_all(log=None):
    """
    Wipe ALL files in TMP_ROOT after a confirmed successful render.
    Never deletes the folder itself — only its contents.
    """
    removed, failed = 0, 0
    try:
        for f in os.listdir(TMP_ROOT):
            fp = os.path.join(TMP_ROOT, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    removed += 1
                except Exception:
                    failed += 1
    except Exception:
        pass
    if log:
        if removed:
            log(f"  Temp folder cleaned  ({removed} file(s) removed)")
        if failed:
            log(f"  [{failed} file(s) could not be deleted — safe to remove manually]")

def _clear_tmp_batch_files():
    """Delete only batch_*.mp4 and kb_*.mp4 files from TMP_ROOT."""
    try:
        for f in os.listdir(TMP_ROOT):
            if (f.startswith("batch_") or f.startswith("kb_")) and f.endswith(".mp4"):
                try:
                    os.remove(os.path.join(TMP_ROOT, f))
                except Exception:
                    pass
    except Exception:
        pass

def _get_existing_batch_files() -> list:
    """Return sorted list of batch_N.mp4 paths that exist in TMP_ROOT."""
    found = []
    try:
        for f in sorted(os.listdir(TMP_ROOT)):
            if f.startswith("batch_") and f.endswith(".mp4"):
                found.append(os.path.join(TMP_ROOT, f))
    except Exception:
        pass
    return found


try:
    import imageio_ffmpeg
    FFMPEG_BIN = imageio_ffmpeg.get_ffmpeg_exe()
    os.environ["FFMPEG_BINARY"] = FFMPEG_BIN
except Exception:
    FFMPEG_BIN = "ffmpeg"

from moviepy.editor import (
    VideoFileClip, AudioFileClip,
    concatenate_videoclips, CompositeVideoClip, TextClip,
)
from moviepy.video.fx.fadein  import fadein
from moviepy.video.fx.fadeout import fadeout
import moviepy.config as mpy_cfg
try:
    mpy_cfg.change_settings({"FFMPEG_BINARY": FFMPEG_BIN})
except Exception:
    pass

PRE_ROLL = 1.0
TAIL     = 5.0

QUALITY = {
    "lowspec":  dict(res=(1280, 720),  preset="veryfast", bitrate="2500k", fps=24, threads=2),
    "balanced": dict(res=(1280, 720),  preset="fast",     bitrate="4000k", fps=30, threads=2),
    "youtube":  dict(res=(1920, 1080), preset="fast",     bitrate="6000k", fps=30, threads=2),
}

DEFAULT_SETTINGS = {
    "video_folder":   "",
    "image_folder":   "",
    "output_file":    "",
    "voiceover_file": "",
    "quality_preset": "lowspec",
    "image_dur":      4.0,
    "kb_style":       "random",
    "fade_dur":       1.0,
    "title_text":     "",
    "use_vo":         False,
    "custom_dur":     120,
    "use_subtitles":    False,
    "sub_font_size":    22,
    "sub_output_mode":  "burn",   # "burn" | "srt" | "both"
    "sub_script_file":  "",       # optional .txt script for forced alignment
    "qt_font_size":     22,
    "qt_bold":          True,
    "qt_output_mode":   "burn",
    "qt_use_script":    False,
}


# ==========================================================
#  Settings helpers
# ==========================================================

def load_settings():
    try:
        with open(SETTINGS_FILE, "r") as f:
            data = json.load(f)
        # merge with defaults so new keys are always present
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data)
        return merged
    except Exception:
        return DEFAULT_SETTINGS.copy()


def save_settings(data: dict):
    try:
        with open(SETTINGS_FILE, "w") as f:
            json.dump(data, f, indent=2)
    except Exception:
        pass


# ==========================================================
#  Helpers
# ==========================================================

def silent_audio(duration, fps=44100):
    """
    Return a silent stereo AudioClip.
    Uses float32 numpy zeros – avoids the 'Unable to allocate 21 MiB
    for float64 array' error caused by returning a Python list.
    """
    from moviepy.audio.AudioClip import AudioClip
    silence = np.zeros((max(1, int(duration * fps)), 2), dtype=np.float32)
    def make_frame(t):
        idx = np.clip((np.array(t) * fps).astype(int), 0, len(silence)-1)
        return silence[idx]
    return AudioClip(make_frame, duration=duration, fps=fps)


def probe_video(path):
    """ffprobe duration check – no frame decode."""
    ffprobe = FFMPEG_BIN.replace("ffmpeg-win-x86_64-v7.1.exe", "ffprobe.exe") \
                        .replace("ffmpeg.exe", "ffprobe.exe") \
                        .replace("ffmpeg", "ffprobe")
    try:
        r = subprocess.run(
            [ffprobe, "-v", "quiet", "-print_format", "json",
             "-show_format", path],
            capture_output=True, text=True, timeout=10
        )
        if r.returncode == 0:
            data = json.loads(r.stdout)
            dur  = float(data.get("format", {}).get("duration", 0))
            return dur if dur > 0 else None
    except Exception:
        pass
    return _probe_fallback(path)


def _probe_fallback(path):
    clip = None
    try:
        clip = VideoFileClip(path, audio=False)
        w, h = int(clip.w), int(clip.h)
        if w <= 0 or h <= 0:
            raise ValueError(f"bad size {w}x{h}")
        dur = clip.duration
        if not dur or dur <= 0:
            raise ValueError("bad duration")
        _ = clip.get_frame(0)
        return dur
    except Exception:
        return None
    finally:
        if clip:
            try: clip.close()
            except: pass
        gc.collect()


def safe_load_video(path):
    """Load + validate VideoFileClip. Returns None on any failure."""
    clip = None
    try:
        clip = VideoFileClip(path, audio=True)
        w, h = int(clip.w), int(clip.h)
        if w <= 0 or h <= 0:
            raise ValueError(f"bad size {w}x{h}")
        if not clip.duration or clip.duration <= 0:
            raise ValueError("bad duration")
        _ = clip.get_frame(0)
        return clip
    except Exception:
        if clip is not None:
            try: clip.close()
            except: pass
        gc.collect()
        return None


def crop_fill_numpy(clip, tw, th):
    """
    Scale + centre-crop to tw×th using PIL per frame.
    Returns uint8 frames – NOT float64 (avoids RAM spike).
    """
    from PIL import Image
    cw, ch = int(clip.w), int(clip.h)
    scale  = max(tw / cw, th / ch)
    new_w  = int(cw * scale)
    new_h  = int(ch * scale)
    x1     = (new_w - tw) // 2
    y1     = (new_h - th) // 2

    def process(frame):
        # ensure uint8 input
        f8  = frame.astype(np.uint8) if frame.dtype != np.uint8 else frame
        img = Image.fromarray(f8).resize((new_w, new_h), Image.BILINEAR)
        arr = np.array(img, dtype=np.uint8)
        return arr[y1:y1+th, x1:x1+tw]

    return clip.fl_image(process)


# -- Ken Burns preset list --------------------------------
# Each entry: (name, zoom_dir, x_start_frac, x_end_frac, y_start_frac, y_end_frac)
# Fractions are of the "extra" pixels available after scaling.
# x: 0.0=left edge, 0.5=centre, 1.0=right edge
# y: 0.0=top edge,  0.5=centre, 1.0=bottom edge
KB_PRESETS = [
    ("zoom_in_centre",    "in",  0.5, 0.5, 0.5, 0.5),
    ("zoom_out_centre",   "out", 0.5, 0.5, 0.5, 0.5),
    ("zoom_in_topleft",   "in",  0.0, 0.25, 0.0, 0.25),
    ("zoom_in_botright",  "in",  1.0, 0.75, 1.0, 0.75),
    ("zoom_out_topright", "out", 0.75, 1.0, 0.0, 0.25),
    ("zoom_out_botleft",  "out", 0.25, 0.0, 1.0, 0.75),
    ("pan_left_right",    "in",  0.0, 1.0, 0.5, 0.5),
    ("pan_right_left",    "in",  1.0, 0.0, 0.5, 0.5),
    ("pan_top_bottom",    "out", 0.5, 0.5, 0.0, 1.0),
    ("pan_bottom_top",    "out", 0.5, 0.5, 1.0, 0.0),
    ("pan_diag_tl_br",    "in",  0.0, 1.0, 0.0, 1.0),
    ("pan_diag_tr_bl",    "out", 1.0, 0.0, 0.0, 1.0),
]

_kb_recent = []
_KB_NO_REPEAT = 3

def _pick_kb_preset(style):
    global _kb_recent
    if style == "zoom_in":
        pool = [p for p in KB_PRESETS if p[1] == "in"]
    elif style == "zoom_out":
        pool = [p for p in KB_PRESETS if p[1] == "out"]
    else:
        pool = KB_PRESETS

    names_recent = set(_kb_recent)
    available = [p for p in pool if p[0] not in names_recent]
    if not available:
        available = pool

    chosen = random.choice(available)
    _kb_recent.append(chosen[0])
    if len(_kb_recent) > _KB_NO_REPEAT:
        _kb_recent.pop(0)
    return chosen


def ken_burns_to_file(img_path, duration, fps, style, tw, th, out_path, log):
    """
    Smooth Ken Burns by rendering individual JPEG frames with PIL,
    then encoding with ffmpeg image2 pipe.

    Approach:
    - Pre-compute crop rect for every frame in Python (precise float math)
    - Write frames as raw RGB to ffmpeg via stdin pipe
    - No ffmpeg filter expressions = no floating point jitter
    """
    from PIL import Image as PILImage
    import math

    try:
        preset = _pick_kb_preset(style)
        name, zoom_dir, px0, px1, py0, py1 = preset
        log(f"   KB: {name}")

        # -- Load & pre-scale ------------------------------
        img = PILImage.open(img_path).convert("RGB")
        iw, ih = img.size

        # Scale so image at max zoom (Z_MAX) still fills tw x th
        Z_MAX = 1.12
        base_scale = max(tw / iw, th / ih) * Z_MAX * 1.05
        nw = int(iw * base_scale)
        nh = int(ih * base_scale)
        nw += nw % 2; nh += nh % 2
        img = img.resize((nw, nh), PILImage.LANCZOS)
        arr = img.tobytes()   # raw RGB bytes, kept in memory
        del img; gc.collect()

        Z_MIN  = 1.00
        total  = int(duration * fps)
        fade_f = int(min(fps * 0.7, total // 5))

        # -- Build ffmpeg pipe process ----------------------
        cmd = [
            FFMPEG_BIN, "-y",
            "-f", "rawvideo",
            "-vcodec", "rawvideo",
            "-s", f"{tw}x{th}",
            "-pix_fmt", "rgb24",
            "-r", str(fps),
            "-i", "pipe:0",
            "-vf", (
                f"fade=t=in:st=0:d={fade_f/fps:.3f},"
                f"fade=t=out:st={(total-fade_f)/fps:.3f}:d={fade_f/fps:.3f}"
            ),
            "-t", f"{duration:.3f}",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-b:v", "2000k",
            "-pix_fmt", "yuv420p",
            "-an",
            out_path
        ]

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        # -- Write frames -----------------------------------
        from PIL import Image as PI
        src = PI.frombytes("RGB", (nw, nh), arr)

        for fi in range(total):
            # smooth ease in-out  t: 0->1
            t = fi / max(total - 1, 1)
            ease = (1 - math.cos(math.pi * t)) / 2  # smooth S-curve

            # zoom
            if zoom_dir == "in":
                zoom = Z_MIN + (Z_MAX - Z_MIN) * ease
            else:
                zoom = Z_MAX - (Z_MAX - Z_MIN) * ease

            # visible window size in the pre-scaled image
            vw = tw / zoom
            vh = th / zoom

            # extra space available for panning
            extra_x = nw - vw
            extra_y = nh - vh

            # pan position
            cx = (px0 + (px1 - px0) * ease) * extra_x
            cy = (py0 + (py1 - py0) * ease) * extra_y

            # clamp
            x1 = max(0, min(cx, nw - vw))
            y1 = max(0, min(cy, nh - vh))
            x2 = x1 + vw
            y2 = y1 + vh

            # crop then resize to tw x th
            frame = src.crop((x1, y1, x2, y2)).resize((tw, th), PI.BILINEAR)
            try:
                proc.stdin.write(frame.tobytes())
            except BrokenPipeError:
                break

        proc.stdin.close()
        _, stderr = proc.communicate(timeout=120)
        del src, arr; gc.collect()

        if proc.returncode != 0:
            log(f"   [WARN] pipe encode failed: {stderr.decode()[-200:]}")
            return _static_fallback(img_path, duration, fps, tw, th, out_path, 0.6, log)

        return True

    except Exception as e:
        log(f"   [WARN] Ken Burns error {os.path.basename(img_path)}: {e}")
        try:
            proc.stdin.close()
            proc.kill()
        except Exception:
            pass
        return _static_fallback(img_path, duration, fps, tw, th, out_path, 0.6, log)


def _static_fallback(img_path, duration, fps, tw, th, out_path, fade_dur, log):
    """No zoom – static image with fade in/out only."""
    from PIL import Image as PILImage
    tmp_png = out_path.replace(".mp4", "_fb.png")
    try:
        img = PILImage.open(img_path).convert("RGB")
        iw, ih = img.size
        scale = max(tw / iw, th / ih)
        nw = int(iw * scale); nh = int(ih * scale)
        nw += nw % 2; nh += nh % 2
        img = img.resize((nw, nh), PILImage.BILINEAR)
        x1 = (nw - tw) // 2; y1 = (nh - th) // 2
        img = img.crop((x1, y1, x1+tw, y1+th))
        img.save(tmp_png, "PNG")
        del img; gc.collect()

        vf = (
            f"fade=t=in:st=0:d={fade_dur:.3f},"
            f"fade=t=out:st={duration-fade_dur:.3f}:d={fade_dur:.3f}"
        )
        cmd = [
            FFMPEG_BIN, "-y",
            "-loop", "1", "-framerate", str(fps),
            "-i", tmp_png,
            "-vf", vf,
            "-t", f"{duration:.3f}",
            "-c:v", "libx264", "-preset", "veryfast",
            "-b:v", "2000k", "-pix_fmt", "yuv420p", "-an",
            out_path
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=60)
        try: os.remove(tmp_png)
        except: pass
        if r.returncode == 0:
            log(f"   [INFO] Static fallback: {os.path.basename(img_path)}")
            return True
        return False
    except Exception as e:
        log(f"   [WARN] Static fallback failed: {e}")
        try: os.remove(tmp_png)
        except: pass
        return False



# ==========================================================
#  Subtitle helpers
# ==========================================================

def check_whisper():
    """Return True if openai-whisper is installed."""
    try:
        import whisper
        return True
    except ImportError:
        return False


def transcribe_to_srt(audio_path, srt_path, log):
    """
    Use openai-whisper to transcribe audio and save as SRT file.
    Uses 'tiny' model — fastest, least RAM usage for low-spec PCs.
    Returns True on success.
    """
    try:
        import whisper
        log("Whisper: Loading 'tiny' model (first run downloads ~75MB)...")
        model = whisper.load_model("tiny")
        log("Whisper: Transcribing voiceover...")
        # Load audio with scipy to bypass whisper's internal ffmpeg call
        try:
            import scipy.io.wavfile as _wav
            # First convert audio to 16kHz WAV using our known ffmpeg
            wav_tmp = audio_path + "_16k.wav"
            subprocess.run([
                FFMPEG_BIN, "-y", "-i", audio_path,
                "-ar", "16000", "-ac", "1",
                "-acodec", "pcm_s16le", wav_tmp
            ], capture_output=True, timeout=60)
            sr, wav_data = _wav.read(wav_tmp)
            if wav_data.dtype == np.int16:
                audio_arr = wav_data.astype(np.float32) / 32768.0
            else:
                audio_arr = wav_data.astype(np.float32)
            if audio_arr.ndim > 1:
                audio_arr = audio_arr.mean(axis=1)
            try: os.remove(wav_tmp)
            except: pass
            result = model.transcribe(audio_arr, word_timestamps=False,
                                      verbose=False, fp16=False)
        except ImportError:
            ffmpeg_dir = os.path.dirname(FFMPEG_BIN)
            os.environ["PATH"] = ffmpeg_dir + os.pathsep + os.environ.get("PATH", "")
            result = model.transcribe(audio_path, word_timestamps=False,
                                      verbose=False, fp16=False)

        segments = result.get("segments", [])
        if not segments:
            log("Whisper: No speech detected in audio!")
            return False

        # Write SRT
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, seg in enumerate(segments, 1):
                start = _sec_to_srt_time(seg["start"] + PRE_ROLL)
                end   = _sec_to_srt_time(seg["end"]   + PRE_ROLL)
                text  = seg["text"].strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

        log(f"Whisper: {len(segments)} subtitle segments generated")
        log(f"  SRT saved: {srt_path}")
        return True

    except ImportError:
        log("ERROR: openai-whisper not installed!")
        log("  Run: pip install openai-whisper")
        return False
    except Exception as e:
        log(f"Whisper error: {e}")
        return False


def _align_to_srt(audio_path, script_path, srt_path, log):
    """
    Use stable-ts forced alignment to sync a known script to audio.
    Falls back to plain Whisper transcription if stable-ts is not installed.
    Returns True on success.
    """
    try:
        with open(script_path, "r", encoding="utf-8") as f:
            script_text = f.read().strip()
        if not script_text:
            log("  [WARN] Script file is empty – falling back to auto-transcribe")
            return transcribe_to_srt(audio_path, srt_path, log)

        # Convert audio to 16kHz WAV first
        wav_tmp = audio_path + "_align_16k.wav"
        subprocess.run([
            FFMPEG_BIN, "-y", "-i", audio_path,
            "-ar", "16000", "-ac", "1", "-acodec", "pcm_s16le", wav_tmp
        ], capture_output=True, timeout=60)

        import scipy.io.wavfile as _wav
        sr, wav_data = _wav.read(wav_tmp)
        if wav_data.dtype == np.int16:
            audio_arr = wav_data.astype(np.float32) / 32768.0
        else:
            audio_arr = wav_data.astype(np.float32)
        if audio_arr.ndim > 1:
            audio_arr = audio_arr.mean(axis=1)
        try: os.remove(wav_tmp)
        except: pass

        try:
            import stable_whisper as stable_ts
            log("  stable-ts: Loading Whisper tiny model...")
            model = stable_ts.load_model("tiny")
            log("  stable-ts: Detecting language...")
            import whisper as _wh
            _lm = _wh.load_model("tiny")
            short = audio_arr[:16000 * 30]  # first 30s for language detection
            _lr = _lm.transcribe(short, fp16=False, verbose=False)
            lang = _lr.get("language", "en") or "en"
            del _lm, _lr, short
            log(f"  Language: {lang}")
            log("  stable-ts: Aligning script to audio...")
            result = model.align(audio_arr, script_text, language=lang)
            # Write SRT manually so we can apply PRE_ROLL offset
            segs = result.segments
            with open(srt_path, "w", encoding="utf-8") as f:
                for i, seg in enumerate(segs, 1):
                    st = _sec_to_srt_time(seg.start + PRE_ROLL)
                    en = _sec_to_srt_time(seg.end   + PRE_ROLL)
                    f.write(f"{i}\n{st} --> {en}\n{seg.text.strip()}\n\n")
            log("  Alignment complete")
            return True
        except ImportError:
            log("  stable-ts not installed – falling back to plain Whisper")
            return transcribe_to_srt(audio_path, srt_path, log)

    except Exception as e:
        log(f"  Alignment error: {e} – falling back to auto-transcribe")
        return transcribe_to_srt(audio_path, srt_path, log)


def _sec_to_srt_time(seconds):
    """Convert float seconds to SRT timestamp HH:MM:SS,mmm"""
    seconds = max(0, seconds)
    h  = int(seconds // 3600)
    m  = int((seconds % 3600) // 60)
    s  = int(seconds % 60)
    ms = int(round((seconds - int(seconds)) * 1000))
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def burn_subtitles(input_video, output_video, srt_path, font_size, log, bold=True):
    """
    Burn SRT subtitles using ffmpeg subtitles filter.
    Uses Popen + stderr pipe for live progress — no timeout.
    """
    bold_val = "1" if bold else "0"

    style = (
        f"FontName=Arial,"
        f"FontSize={font_size},"
        f"Bold={bold_val},"
        f"PrimaryColour=&H00FFFFFF,"
        f"OutlineColour=&H00000000,"
        f"BackColour=&H99000000,"
        f"BorderStyle=4,"
        f"Outline=0,"
        f"Shadow=0,"
        f"MarginV=40,"
        f"Alignment=2"
    )

    srt_escaped = srt_path.replace("\\", "/").replace(":", "\\:")
    vf = f"subtitles=\'{srt_escaped}\':force_style=\'{style}\'"

    cmd = [
        FFMPEG_BIN, "-y",
        "-i", input_video,
        "-vf", vf,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-b:v", "3000k",
        "-c:a", "copy",
        "-progress", "pipe:2",   # send progress to stderr
        output_video
    ]

    log("Burning subtitles into video  (this may take several minutes)...")
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        last_time = ""
        stderr_lines = []
        for line in proc.stderr:
            line = line.strip()
            stderr_lines.append(line)
            # -progress pipe:2 emits key=value pairs; show time progress
            if line.startswith("out_time="):
                t = line.split("=", 1)[1].strip()
                if t != last_time and t not in ("N/A", ""):
                    log(f"  Burning...  {t}")
                    last_time = t
            elif line.startswith("progress=end"):
                log("  Encode complete.")

        proc.wait()
        if proc.returncode != 0:
            tail = "\n".join(stderr_lines[-10:])
            log(f"  Subtitle burn failed:\n{tail}")
            return False
        log("  Subtitles burned successfully!")
        return True
    except Exception as e:
        log(f"  Subtitle burn error: {e}")
        return False


def _write_srt(segments, srt_path, offset=0):
    """Write Whisper segments to SRT file with optional time offset."""
    with open(srt_path, "w", encoding="utf-8") as f:
        for i, seg in enumerate(segments, 1):
            st = _sec_to_srt_time(seg["start"] + offset)
            en = _sec_to_srt_time(seg["end"]   + offset)
            f.write(f"{i}\n{st} --> {en}\n{seg['text'].strip()}\n\n")



def combine_videos(
    video_folder,
    output_file,
    total_duration,
    voiceover_file  = None,
    image_folder    = None,
    image_duration  = 4.0,
    kb_style        = "random",
    quality_preset  = "lowspec",
    fade_duration   = 1.0,
    title_text      = "",
    title_duration  = 3,
    use_subtitles      = False,
    sub_font_size      = 22,
    sub_output_mode    = "burn",   # "burn" | "srt" | "both"
    sub_script_file    = None,     # optional .txt for forced alignment
    progress_callback  = None,
):
    cfg    = QUALITY.get(quality_preset, QUALITY["lowspec"])
    TW, TH = cfg["res"]
    FPS    = cfg["fps"]

    def log(msg):
        if progress_callback:
            progress_callback(msg)

    log("=" * 56)
    log(f"  NavyCat Studio – Video Combiner v8.0")
    log(f"  {TW}x{TH} @ {FPS}fps | {cfg['preset']} | {cfg['bitrate']}")
    log(f"  Temp folder: {TMP_ROOT}")
    log("=" * 56)

    # -- Collect media -------------------------------------
    v_exts = ('.mp4', '.avi', '.mov', '.mkv', '.webm')
    i_exts = ('.jpg', '.jpeg', '.png', '.bmp', '.webp')

    videos = [os.path.join(video_folder, f)
              for f in os.listdir(video_folder)
              if f.lower().endswith(v_exts)]
    images = []
    if image_folder and os.path.isdir(image_folder):
        images = [os.path.join(image_folder, f)
                  for f in os.listdir(image_folder)
                  if f.lower().endswith(i_exts)]

    if not videos and not images:
        raise Exception("No video or image files found!")
    log(f"Found: {len(videos)} clips  +  {len(images)} images")

    # -- Validate clips via ffprobe ------------------------
    log("Validating clips...")
    good, bad, dur_cache = [], [], {}
    for p in videos:
        dur = probe_video(p)
        if dur:
            dur_cache[p] = dur
            good.append(p)
        else:
            bad.append(os.path.basename(p))
            log(f"  SKIP (unreadable): {os.path.basename(p)}")
        gc.collect()

    if bad:
        log(f"  {len(bad)} file(s) skipped")
    if not good and not images:
        raise Exception("No readable files! Check FFmpeg or file codecs.")
    log(f"  {len(good)}/{len(videos)} clips OK")

    # -- Pre-render Ken Burns images -----------------------
    kb_files = []
    if images:
        total_imgs = len(images)
        log(f"Pre-rendering {total_imgs} image(s) (Ken Burns)...")
        for idx, ip in enumerate(images):
            log(f"  Image {idx+1}/{total_imgs}: {os.path.basename(ip)} ...")
            out = os.path.join(TMP_ROOT, f"kb_{idx}.mp4")
            if ken_burns_to_file(ip, image_duration, FPS,
                                  kb_style, TW, TH, out, log):
                kb_files.append((out, image_duration))
                log(f"  Image {idx+1}/{total_imgs}: done v")
            else:
                log(f"  Image {idx+1}/{total_imgs}: SKIPPED")
            gc.collect()
        log(f"  {len(kb_files)}/{total_imgs} image(s) rendered")

    # -- Build playlist ------------------------------------
    all_items = [(p, dur_cache[p]) for p in good] + list(kb_files)
    random.shuffle(all_items)

    playlist, running = [], 0.0
    pool = list(all_items)
    while running < total_duration:
        if not pool:
            pool = list(all_items)
            random.shuffle(pool)
        path, dur = pool.pop(0)
        playlist.append((path, dur))
        running += dur

    log(f"Playlist: {len(playlist)} items  ~{running:.0f}s (need {total_duration:.0f}s)")

    # -- Resume check --------------------------------------
    BATCH     = 4
    n_batches = max(1, (len(playlist) - 1) // BATCH + 1)

    existing = _get_existing_batch_files()
    resume   = False
    if existing:
        import queue as _queue
        answer_q = _queue.Queue()

        def _ask():
            sizes = sum(os.path.getsize(p) for p in existing) // (1024 * 1024)
            result = messagebox.askyesno(
                "Resume previous job?",
                f"Found {len(existing)} batch file(s) in temp folder  ({sizes} MB).\n\n"
                f"YES  —  Continue from where it stopped\n"
                f"NO   —  Delete old files and start fresh",
                icon="question"
            )
            answer_q.put(result)

        try:
            import tkinter as _tk
            _tk._default_root.after(0, _ask)
        except Exception:
            _ask()
        resume = answer_q.get()

        if resume:
            log(f"  Resuming — reusing {len(existing)} existing batch file(s)...")
        else:
            log("  Starting fresh — deleting old temp files...")
            _clear_tmp_batch_files()
            existing = []

    # Build set of already-done batch numbers from existing files
    completed = set()
    if resume:
        for bp in existing:
            try:
                bn = int(os.path.basename(bp)
                         .replace("batch_", "").replace(".mp4", ""))
                completed.add(bn)
            except Exception:
                pass

    # -- Batch processing ----------------------------------
    tmp_batch = []

    for i in range(0, len(playlist), BATCH):
        batch_num = i // BATCH + 1
        bp        = os.path.join(TMP_ROOT, f"batch_{batch_num}.mp4")

        if batch_num in completed:
            log(f"Batch {batch_num}/{n_batches}  [skipped — reusing existing]")
            tmp_batch.append(bp)
            continue

        batch = playlist[i:i + BATCH]
        log(f"Batch {batch_num}/{n_batches}...")

        clips = []
        for path, _ in batch:
            c = None
            try:
                c = safe_load_video(path)
                if c is None:
                    log(f"  SKIP: {os.path.basename(path)}")
                    continue
                if (int(c.w), int(c.h)) != (TW, TH):
                    c = crop_fill_numpy(c, TW, TH)
                c = c.set_fps(FPS)
                c = c.set_audio(silent_audio(c.duration))  # always mute – voiceover only
                clips.append(c)
            except Exception as e:
                log(f"  SKIP {os.path.basename(path)}: {e}")
                if c:
                    try: c.close()
                    except: pass
                gc.collect()

        if not clips:
            continue

        bc = concatenate_videoclips(clips, method="compose")
        bc.write_videofile(
            bp, codec="libx264", audio_codec="aac",
            temp_audiofile=os.path.join(TMP_ROOT, f"audio_{batch_num}.m4a"),
            remove_temp=True,
            threads=cfg["threads"], preset=cfg["preset"],
            fps=FPS, bitrate=cfg["bitrate"],
            verbose=False, logger=None,
        )
        tmp_batch.append(bp)
        bc.close()
        for c in clips: c.close()
        del clips, bc; gc.collect()

    if not tmp_batch:
        raise Exception("No clips processed! All files may be unreadable.")

    # -- Merge (FFmpeg concat demuxer – zero RAM) ----------
    # MoviePy's concatenate_videoclips loads ALL batch files into memory
    # simultaneously, which causes WinError 1455 (paging file too small)
    # on low-RAM machines with 20+ batches.
    # The FFmpeg concat demuxer reads files sequentially from disk instead,
    # using negligible RAM regardless of how many batches there are.
    log("Merging batches...")
    concat_list = os.path.join(TMP_ROOT, "__concat_list.txt")
    merged_raw  = os.path.join(TMP_ROOT, "__merged_raw.mp4")
    with open(concat_list, "w", encoding="utf-8") as f:
        for bp in tmp_batch:
            # FFmpeg concat list requires forward slashes and escaped apostrophes
            safe = bp.replace("\\", "/").replace("'", "\\'")
            f.write(f"file '{safe}'\n")

    concat_cmd = [
        FFMPEG_BIN, "-y",
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",          # stream-copy: no re-encode, no RAM spike
        merged_raw,
    ]
    log("  Running FFmpeg concat (stream-copy)...")
    r = subprocess.run(concat_cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise Exception(f"FFmpeg concat failed:\n{r.stderr[-400:]}")

    try: os.remove(concat_list)
    except Exception: pass

    # Load the single merged file for downstream processing (trim / VO / fade)
    merged_clip = VideoFileClip(merged_raw)
    video  = merged_clip
    video  = video.subclip(0, min(total_duration, video.duration))

    # -- Voiceover -----------------------------------------
    from moviepy.audio.AudioClip import concatenate_audioclips
    if voiceover_file and os.path.isfile(voiceover_file):
        log("Attaching voiceover...")
        vo_clip  = AudioFileClip(voiceover_file)
        vo_dur   = vo_clip.duration
        pre      = silent_audio(PRE_ROLL)
        tail_dur = max(0.01, video.duration - PRE_ROLL - vo_dur)
        tail     = silent_audio(tail_dur)
        full_au  = concatenate_audioclips([pre, vo_clip, tail])
        full_au  = full_au.subclip(0, video.duration)
        video    = video.set_audio(full_au)
    else:
        vo_clip = None
        log("No voiceover – original audio kept")

    # -- Fade ----------------------------------------------
    fd    = min(fade_duration, video.duration / 4)
    video = fadein(video, fd)
    video = fadeout(video, fd)
    log(f"Fade: {fd:.1f}s at start & end")

    # -- Title overlay -------------------------------------
    if title_text.strip():
        log(f"Adding title: \"{title_text}\"")
        try:
            txt = (
                TextClip(title_text,
                         fontsize=55, color="white", font="Arial-Bold",
                         stroke_color="black", stroke_width=2,
                         size=(TW - 120, None), method="caption")
                .set_position("center")
                .set_duration(min(title_duration, video.duration))
            )
            video = CompositeVideoClip([video, txt])
        except Exception as e:
            log(f"  Title skipped: {e}")

    # -- Write final (pre-subtitle) -----------------------
    if use_subtitles and voiceover_file and os.path.isfile(voiceover_file):
        pre_sub_file = os.path.join(TMP_ROOT, "pre_subtitle.mp4")
        log(f"Saving pre-subtitle video...")
    else:
        pre_sub_file = output_file

    log(f"Saving to: {pre_sub_file}")
    video.write_videofile(
        pre_sub_file, codec="libx264", audio_codec="aac",
        temp_audiofile=os.path.join(TMP_ROOT, "final_audio.m4a"),
        remove_temp=True,
        threads=cfg["threads"], preset=cfg["preset"],
        fps=FPS, bitrate=cfg["bitrate"],
        verbose=False, logger=None,
    )

    video.close()
    if vo_clip:
        vo_clip.close()
    try: merged_clip.close()
    except Exception: pass
    try: os.remove(merged_raw)
    except Exception: pass

    # -- Subtitles -----------------------------------------
    if use_subtitles and voiceover_file and os.path.isfile(voiceover_file):
        srt_tmp  = os.path.join(TMP_ROOT, "subtitles.srt")
        # Determine permanent SRT path (same folder & base name as output video)
        srt_perm = os.path.splitext(output_file)[0] + ".srt"

        # Choose transcription method automatically
        if sub_script_file and os.path.isfile(sub_script_file):
            log("Subtitles: Script file found – using forced alignment (stable-ts)...")
            ok = _align_to_srt(voiceover_file, sub_script_file, srt_tmp, log)
        else:
            log("Subtitles: No script file – using Whisper auto-transcribe...")
            ok = transcribe_to_srt(voiceover_file, srt_tmp, log)

        if ok:
            # Save SRT next to output video if mode is "srt" or "both"
            if sub_output_mode in ("srt", "both"):
                import shutil as _sh
                _sh.copy2(srt_tmp, srt_perm)
                log(f"  SRT saved: {srt_perm}")

            # Burn subtitles into video if mode is "burn" or "both"
            if sub_output_mode in ("burn", "both"):
                ok2 = burn_subtitles(pre_sub_file, output_file, srt_tmp, sub_font_size, log)
                if not ok2:
                    log("  Subtitle burn failed – saving without subtitles")
                    import shutil as _sh
                    _sh.copy2(pre_sub_file, output_file)
            elif sub_output_mode == "srt":
                # SRT only – just copy the pre-subtitle video as the final output
                import shutil as _sh
                _sh.copy2(pre_sub_file, output_file)
        else:
            log("  Transcription failed – saving without subtitles")
            import shutil as _sh
            _sh.copy2(pre_sub_file, output_file)
        try: os.remove(srt_tmp)
        except: pass
        if pre_sub_file != output_file:
            try: os.remove(pre_sub_file)
            except: pass

    # -- Verify output then clean up temp files ------------
    if os.path.isfile(output_file) and os.path.getsize(output_file) > 0:
        log("Output verified  ✓  — cleaning temp folder...")
        _cleanup_tmp_all(log)
    else:
        log("  [WARN] Output file not found or empty — temp files kept for inspection")

    log("=" * 56)
    log(f"  DONE!  Saved to: {output_file}")
    log("=" * 56)


# ==========================================================
#  GUI
# ==========================================================

BG     = "#1e1e2e"
PANEL  = "#2a2a3e"
ACCENT = "#7c3aed"
GREEN  = "#22c55e"
TEXT   = "#e2e8f0"
SUB    = "#94a3b8"
ENTRY  = "#0f172a"
ORANGE = "#f97316"


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        root.title("NavyCat Studio – Video Combiner v8.0")
        root.geometry("680x620")
        root.configure(bg=BG)
        root.resizable(True, True)

        # Load saved settings
        s = load_settings()

        self.video_folder   = s["video_folder"]
        self.image_folder   = s["image_folder"]
        self.output_file    = s["output_file"]
        self.voiceover_file = s["voiceover_file"]

        self.quality_preset = tk.StringVar(value=s["quality_preset"])
        self.image_dur      = tk.DoubleVar(value=s["image_dur"])
        self.kb_style       = tk.StringVar(value=s["kb_style"])
        self.fade_dur       = tk.DoubleVar(value=s["fade_dur"])
        self.title_text     = tk.StringVar(value=s["title_text"])
        self.use_vo         = tk.BooleanVar(value=s["use_vo"])
        self.custom_dur     = tk.IntVar(value=s["custom_dur"])
        self.use_subtitles   = tk.BooleanVar(value=s.get("use_subtitles", False))
        self.sub_font_size   = tk.IntVar(value=s.get("sub_font_size", 22))
        self.sub_output_mode = tk.StringVar(value=s.get("sub_output_mode", "burn"))
        self.sub_script_file = s.get("sub_script_file", "")

        self._build()
        self._restore_labels()

        # Save settings on close
        root.protocol("WM_DELETE_WINDOW", self._on_close)

    # -- Settings ------------------------------------------

    def _collect_settings(self):
        return {
            "video_folder":   self.video_folder,
            "image_folder":   self.image_folder,
            "output_file":    self.output_file,
            "voiceover_file": self.voiceover_file,
            "quality_preset": self.quality_preset.get(),
            "image_dur":      self.image_dur.get(),
            "kb_style":       self.kb_style.get(),
            "fade_dur":       self.fade_dur.get(),
            "title_text":     self.title_text.get(),
            "use_vo":         self.use_vo.get(),
            "custom_dur":     self.custom_dur.get(),
            "use_subtitles":    self.use_subtitles.get(),
            "sub_font_size":    self.sub_font_size.get(),
            "sub_output_mode":  self.sub_output_mode.get(),
            "sub_script_file":  self.sub_script_file,
        }

    def _restore_labels(self):
        def short(p): return ("..."+p[-49:]) if len(p) > 52 else p
        if self.video_folder:
            self._lbl_vid.config(text=short(self.video_folder), fg=TEXT)
        if self.image_folder:
            self._lbl_img.config(text=short(self.image_folder), fg=TEXT)
        if self.output_file:
            self._lbl_out.config(text=os.path.basename(self.output_file), fg=TEXT)
        if self.voiceover_file:
            self._lbl_vo.config(text=os.path.basename(self.voiceover_file), fg=TEXT)
        if self.sub_script_file:
            self._lbl_sub_script.config(text=os.path.basename(self.sub_script_file), fg=GREEN)
        self._toggle_vo()
        self._upd_dur()

    def _on_close(self):
        save_settings(self._collect_settings())
        self.root.destroy()

    # -- UI build ------------------------------------------

    def _build(self):
        # ── Header ──────────────────────────────────────────
        hdr = tk.Frame(self.root, bg=ACCENT, pady=8)
        hdr.pack(fill=tk.X)
        tk.Label(hdr, text="NavyCat Studio  —  Video Combiner  v8",
                 bg=ACCENT, fg="white",
                 font=("Segoe UI", 12, "bold")).pack(side=tk.LEFT, padx=14)
        tk.Label(hdr, text="Low-Spec Edition  •  i3 / 4 GB RAM",
                 bg=ACCENT, fg="#c4b5fd",
                 font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=14)

        # ── Warning bar ──────────────────────────────────────
        wb = tk.Frame(self.root, bg="#431407", pady=3)
        wb.pack(fill=tk.X)
        tk.Label(wb,
                 text=f"⚠  Close other apps before rendering   •   Temp: {TMP_ROOT}",
                 bg="#431407", fg="#fed7aa", font=("Segoe UI", 7)).pack()

        # ── Notebook (tabs) ──────────────────────────────────
        style = ttk.Style()
        style.theme_use("default")
        style.configure("NC.TNotebook",
                        background=BG, borderwidth=0, tabmargins=0)
        style.configure("NC.TNotebook.Tab",
                        background=PANEL, foreground=SUB,
                        font=("Segoe UI", 9, "bold"),
                        padding=(14, 6), borderwidth=0)
        style.map("NC.TNotebook.Tab",
                  background=[("selected", ACCENT)],
                  foreground=[("selected", "white")])

        nb = ttk.Notebook(self.root, style="NC.TNotebook")
        nb.pack(fill=tk.BOTH, expand=True, padx=0, pady=0)

        # helper – scrollable tab page
        def make_page(label):
            outer = tk.Frame(nb, bg=BG)
            nb.add(outer, text=f"  {label}  ")
            canvas = tk.Canvas(outer, bg=BG, highlightthickness=0)
            sb = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
            canvas.configure(yscrollcommand=sb.set)
            sb.pack(side=tk.RIGHT, fill=tk.Y)
            canvas.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
            page = tk.Frame(canvas, bg=BG)
            cwin = canvas.create_window((0, 0), window=page, anchor="nw")
            def _resize(e, c=canvas, w=cwin):
                c.configure(scrollregion=c.bbox("all"))
                c.itemconfig(w, width=c.winfo_width())
            page.bind("<Configure>", _resize)
            canvas.bind("<Configure>", _resize)
            canvas.bind_all("<MouseWheel>",
                lambda e, c=canvas: c.yview_scroll(int(-1*(e.delta/120)), "units"))
            return page

        # helper – card section inside a page
        def card(page, title):
            tk.Label(page, text=title, bg=BG, fg=ACCENT,
                     font=("Segoe UI", 9, "bold")).pack(
                     anchor=tk.W, padx=14, pady=(12, 2))
            f = tk.Frame(page, bg=PANEL, padx=14, pady=10)
            f.pack(fill=tk.X, padx=10, pady=(0, 2))
            return f

        # helper – file picker row
        def file_row(parent, label, hint, btn_text, cmd, attr):
            row = tk.Frame(parent, bg=PANEL)
            row.pack(fill=tk.X, pady=(4, 0))
            tk.Label(row, text=label, bg=PANEL, fg=TEXT,
                     width=18, anchor=tk.W,
                     font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT)
            lbl = tk.Label(row, text=hint, bg=ENTRY, fg=SUB,
                           anchor=tk.W, padx=6, relief=tk.FLAT,
                           font=("Segoe UI", 9))
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(4, 4))
            setattr(self, attr, lbl)
            tk.Button(row, text=btn_text, command=cmd,
                      bg=ACCENT, fg="white", relief=tk.FLAT,
                      padx=10, font=("Segoe UI", 9, "bold")).pack(side=tk.RIGHT)

        # ════════════════════════════════════════════════════
        #  TAB 1 — FILES & DURATION
        # ════════════════════════════════════════════════════
        t1 = make_page("📁  Files")

        # Files card
        c1 = card(t1, "Media Files")
        file_row(c1, "Video Clips Folder", "Not selected", "Browse",
                 self.pick_videos, "_lbl_vid")
        file_row(c1, "Images Folder", "Not selected  (optional)", "Browse",
                 self.pick_images, "_lbl_img")
        file_row(c1, "Save Output As", "Not selected", "Choose",
                 self.pick_output, "_lbl_out")

        # Duration card
        c2 = card(t1, "Duration")
        tog = tk.Frame(c2, bg=PANEL); tog.pack(anchor=tk.W, pady=(0, 6))
        tk.Radiobutton(tog, text="Set duration manually",
                       variable=self.use_vo, value=False,
                       command=self._toggle_vo,
                       bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                       activebackground=PANEL,
                       font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=(0, 20))
        tk.Radiobutton(tog, text="Match voiceover length  (recommended)",
                       variable=self.use_vo, value=True,
                       command=self._toggle_vo,
                       bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                       activebackground=PANEL,
                       font=("Segoe UI", 9)).pack(side=tk.LEFT)

        # Duration slider (shown when no VO)
        self._dur_frame = tk.Frame(c2, bg=PANEL)
        self._dur_frame.pack(fill=tk.X)
        dr = tk.Frame(self._dur_frame, bg=PANEL); dr.pack(fill=tk.X)
        tk.Label(dr, text="Length:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), width=8,
                 anchor=tk.W).pack(side=tk.LEFT)
        self._dur_scale = tk.Scale(dr, from_=10, to=3600,
                                   orient=tk.HORIZONTAL,
                                   variable=self.custom_dur,
                                   command=self._upd_dur,
                                   bg=PANEL, fg=TEXT, troughcolor=ENTRY,
                                   highlightthickness=0)
        self._dur_scale.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=6)
        self._dur_lbl = tk.Label(dr, text="2m", width=7, bg=PANEL, fg=GREEN,
                                 font=("Segoe UI", 10, "bold"))
        self._dur_lbl.pack(side=tk.RIGHT)
        qp = tk.Frame(self._dur_frame, bg=PANEL); qp.pack(anchor=tk.W, pady=(4, 0))
        tk.Label(qp, text="Quick set:", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(0, 6))
        for t, v in [("1m",60),("3m",180),("5m",300),("10m",600),("15m",900),("30m",1800)]:
            tk.Button(qp, text=t, command=lambda v=v: self.custom_dur.set(v),
                      bg=ENTRY, fg=TEXT, relief=tk.FLAT, padx=6,
                      font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=2)

        # Voiceover picker (shown when VO mode)
        self._vo_frame = tk.Frame(c2, bg=PANEL)
        file_row(self._vo_frame, "Voiceover File",
                 "Not selected", "Browse", self.pick_vo, "_lbl_vo")
        tk.Label(self._vo_frame,
                 text="  ▸  1s lead-in  →  Voiceover  →  5s tail  →  End",
                 bg=PANEL, fg=GREEN, font=("Segoe UI", 8)).pack(
                 anchor=tk.W, pady=(4, 0))

        # ════════════════════════════════════════════════════
        #  TAB 2 — VIDEO SETTINGS
        # ════════════════════════════════════════════════════
        t2 = make_page("⚙  Settings")

        # Quality card
        cq = card(t2, "Output Quality")
        qr = tk.Frame(cq, bg=PANEL); qr.pack(anchor=tk.W)
        for t, v, hint in [
            ("Low-Spec  720p 24fps",  "lowspec",  "— safest for i3 / 4 GB"),
            ("Balanced  720p 30fps",  "balanced", "— faster machines"),
            ("YouTube   1080p 30fps", "youtube",  "— high quality upload"),
        ]:
            rb = tk.Frame(qr, bg=PANEL); rb.pack(anchor=tk.W, pady=2)
            tk.Radiobutton(rb, text=t, variable=self.quality_preset, value=v,
                           bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                           activebackground=PANEL,
                           font=("Segoe UI", 9)).pack(side=tk.LEFT)
            tk.Label(rb, text=hint, bg=PANEL, fg=SUB,
                     font=("Segoe UI", 8)).pack(side=tk.LEFT, padx=(4, 0))

        # Images / Ken Burns card
        ck = card(t2, "Images  (optional)")
        ir = tk.Frame(ck, bg=PANEL); ir.pack(anchor=tk.W, pady=(0, 6))
        tk.Label(ir, text="Each image shows for", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        tk.Spinbox(ir, from_=2, to=20, increment=0.5, textvariable=self.image_dur,
                   width=4, bg=ENTRY, fg=TEXT, relief=tk.FLAT,
                   font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        tk.Label(ir, text="seconds", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT)
        kr = tk.Frame(ck, bg=PANEL); kr.pack(anchor=tk.W)
        tk.Label(kr, text="Ken Burns effect:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(side=tk.LEFT, padx=(0, 10))
        for t, v in [("Random", "random"), ("Zoom In", "zoom_in"), ("Zoom Out", "zoom_out")]:
            tk.Radiobutton(kr, text=t, variable=self.kb_style, value=v,
                           bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                           activebackground=PANEL,
                           font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)

        # Polish card (fade + title)
        cp = card(t2, "Polish")
        fr = tk.Frame(cp, bg=PANEL); fr.pack(anchor=tk.W, pady=(0, 6))
        tk.Label(fr, text="Fade in/out:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), width=14,
                 anchor=tk.W).pack(side=tk.LEFT)
        tk.Spinbox(fr, from_=0.1, to=3.0, increment=0.1,
                   textvariable=self.fade_dur,
                   width=4, bg=ENTRY, fg=TEXT, relief=tk.FLAT,
                   font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        tk.Label(fr, text="seconds  (applied at video start & end)",
                 bg=PANEL, fg=SUB, font=("Segoe UI", 8)).pack(side=tk.LEFT)
        tr = tk.Frame(cp, bg=PANEL); tr.pack(anchor=tk.W)
        tk.Label(tr, text="Title overlay:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), width=14,
                 anchor=tk.W).pack(side=tk.LEFT)
        tk.Entry(tr, textvariable=self.title_text, bg=ENTRY, fg=TEXT,
                 insertbackground=TEXT, relief=tk.FLAT, width=32,
                 font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        tk.Label(tr, text="optional", bg=PANEL, fg=SUB,
                 font=("Segoe UI", 8)).pack(side=tk.LEFT)

        # ════════════════════════════════════════════════════
        #  TAB 3 — SUBTITLES
        # ════════════════════════════════════════════════════
        t3 = make_page("💬  Subtitles")

        cs = card(t3, "Whisper AI Subtitles")

        # Enable toggle
        en_row = tk.Frame(cs, bg=PANEL); en_row.pack(fill=tk.X, pady=(0, 8))
        tk.Checkbutton(en_row,
                       text="Enable subtitles",
                       variable=self.use_subtitles,
                       command=self._toggle_sub,
                       bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                       activebackground=PANEL,
                       font=("Segoe UI", 10, "bold")).pack(side=tk.LEFT)
        self._whisper_status = tk.Label(en_row, text="", bg=PANEL,
                                        font=("Segoe UI", 8))
        self._whisper_status.pack(side=tk.LEFT, padx=12)
        self._check_whisper_installed()

        # Divider
        tk.Frame(cs, bg=ENTRY, height=1).pack(fill=tk.X, pady=(0, 8))

        # Output mode
        om_row = tk.Frame(cs, bg=PANEL); om_row.pack(anchor=tk.W, pady=(0, 6))
        tk.Label(om_row, text="Output mode:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), width=14,
                 anchor=tk.W).pack(side=tk.LEFT)
        for t, v in [("Burn into video", "burn"),
                     ("SRT file only",   "srt"),
                     ("Both",            "both")]:
            tk.Radiobutton(om_row, text=t, variable=self.sub_output_mode, value=v,
                           bg=PANEL, fg=TEXT, selectcolor=ENTRY,
                           activebackground=PANEL,
                           font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=8)

        # Font size
        fs_row = tk.Frame(cs, bg=PANEL); fs_row.pack(anchor=tk.W, pady=(0, 6))
        tk.Label(fs_row, text="Font size:", bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold"), width=14,
                 anchor=tk.W).pack(side=tk.LEFT)
        tk.Spinbox(fs_row, from_=12, to=48, textvariable=self.sub_font_size,
                   width=4, bg=ENTRY, fg=TEXT, relief=tk.FLAT,
                   font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=6)
        tk.Label(fs_row, text="px  •  white bold text, dark box, bottom centre",
                 bg=PANEL, fg=SUB, font=("Segoe UI", 8)).pack(side=tk.LEFT)

        # Script file
        tk.Frame(cs, bg=ENTRY, height=1).pack(fill=tk.X, pady=(4, 8))
        tk.Label(cs, text="Script file  (optional — improves accuracy)",
                 bg=PANEL, fg=TEXT,
                 font=("Segoe UI", 9, "bold")).pack(anchor=tk.W, pady=(0, 4))
        tk.Label(cs,
                 text="No script → Whisper auto-transcribes\n"
                      "Script selected → stable-ts forced alignment (correct spelling)",
                 bg=PANEL, fg=SUB, font=("Segoe UI", 8),
                 justify=tk.LEFT).pack(anchor=tk.W, pady=(0, 6))
        srow = tk.Frame(cs, bg=PANEL); srow.pack(fill=tk.X)
        self._lbl_sub_script = tk.Label(srow,
                                        text="No script  —  auto-transcribe",
                                        bg=ENTRY, fg=SUB, anchor=tk.W,
                                        padx=6, relief=tk.FLAT,
                                        font=("Segoe UI", 9))
        self._lbl_sub_script.pack(side=tk.LEFT, fill=tk.X, expand=True)
        tk.Button(srow, text="Browse", command=self.pick_sub_script,
                  bg=ACCENT, fg="white", relief=tk.FLAT, padx=10,
                  font=("Segoe UI", 9, "bold")).pack(side=tk.RIGHT, padx=(4, 0))
        tk.Button(srow, text="Clear", command=self.clear_sub_script,
                  bg=ENTRY, fg=SUB, relief=tk.FLAT, padx=8,
                  font=("Segoe UI", 8)).pack(side=tk.RIGHT, padx=(4, 0))

        # ════════════════════════════════════════════════════
        #  TAB 4 — LOG
        # ════════════════════════════════════════════════════
        t4 = make_page("📋  Log")
        lf = tk.Frame(t4, bg=PANEL, padx=10, pady=10)
        lf.pack(fill=tk.BOTH, expand=True, padx=10, pady=10)
        self._console = scrolledtext.ScrolledText(
            lf, wrap=tk.WORD, state=tk.DISABLED,
            bg="#060d14", fg="#86efac", relief=tk.FLAT,
            font=("Consolas", 9), insertbackground="#86efac"
        )
        self._console.pack(fill=tk.BOTH, expand=True)
        tk.Button(lf, text="Copy log  ⧉",
                  command=self._copy_log,
                  bg=PANEL, fg=SUB, relief=tk.FLAT,
                  font=("Segoe UI", 8), pady=4).pack(
                  anchor=tk.E, pady=(6, 0))

        # ── Bottom action bar (always visible) ───────────────
        bar = tk.Frame(self.root, bg="#0f172a", pady=8)
        bar.pack(fill=tk.X, side=tk.BOTTOM)

        tk.Button(bar, text="ℹ",
                  command=self.show_about,
                  bg="#0f172a", fg=SUB,
                  font=("Segoe UI", 12), relief=tk.FLAT,
                  width=3).pack(side=tk.RIGHT, padx=(0, 8))

        tk.Button(bar, text="🔤  Subtitle Tool",
                  command=self.show_subtitle_tool,
                  bg="#1d4ed8", fg="white",
                  font=("Segoe UI", 10, "bold"), relief=tk.FLAT,
                  padx=14, pady=6).pack(side=tk.RIGHT, padx=(0, 6))

        self._start_btn = tk.Button(
            bar, text="▶   START",
            command=self.start,
            bg=GREEN, fg="#052e16",
            font=("Segoe UI", 12, "bold"), relief=tk.FLAT,
            padx=28, pady=6,
        )
        self._start_btn.pack(side=tk.LEFT, padx=10)

    # -- Helpers ------------------------------------------

    def _toggle_sub(self):
        # just visual feedback — no widget hide needed
        pass

    def _check_whisper_installed(self):
        if check_whisper():
            self._whisper_status.config(
                text="[OK] openai-whisper installed", fg=GREEN)
        else:
            self._whisper_status.config(
                text="[ERR] Not installed – run: pip install openai-whisper",
                fg=ORANGE)

    def _toggle_vo(self):
        if self.use_vo.get():
            self._dur_frame.pack_forget()
            self._vo_frame.pack(fill=tk.X)
        else:
            self._vo_frame.pack_forget()
            self._dur_frame.pack(fill=tk.X)

    def _upd_dur(self, *_):
        s = self.custom_dur.get()
        if s >= 3600:
            h = s//3600; m = (s%3600)//60; txt = f"{h}h {m}m"
        elif s >= 60:
            m, sec = divmod(s, 60); txt = f"{m}m" + (f" {sec}s" if sec else "")
        else:
            txt = f"{s}s"
        self._dur_lbl.config(text=txt)

    def _copy_log(self):
        self._console.config(state=tk.NORMAL)
        content = self._console.get("1.0", tk.END)
        self._console.config(state=tk.DISABLED)
        self.root.clipboard_clear()
        self.root.clipboard_append(content)
        messagebox.showinfo("Copied", "Console log copied to clipboard!")

    # -- File pickers -------------------------------------

    def pick_videos(self):
        f = filedialog.askdirectory(title="Select Video Clips Folder")
        if f:
            self.video_folder = f
            self._lbl_vid.config(
                text=("..."+f[-49:]) if len(f)>52 else f, fg=TEXT)

    def pick_images(self):
        f = filedialog.askdirectory(title="Select Images Folder")
        if f:
            self.image_folder = f
            self._lbl_img.config(
                text=("..."+f[-49:]) if len(f)>52 else f, fg=TEXT)

    def pick_output(self):
        f = filedialog.asksaveasfilename(
            title="Save As", defaultextension=".mp4",
            filetypes=[("MP4","*.mp4"),("All","*.*")])
        if f:
            self.output_file = f
            self._lbl_out.config(text=os.path.basename(f), fg=TEXT)

    def pick_vo(self):
        f = filedialog.askopenfilename(
            title="Select Voiceover",
            filetypes=[("Audio","*.mp3 *.wav *.m4a *.aac *.ogg"),("All","*.*")])
        if f:
            self.voiceover_file = f
            try:
                with AudioFileClip(f) as a:
                    vo_d  = a.duration
                    total = PRE_ROLL + vo_d + TAIL
                    m, s  = divmod(int(total), 60)
                    self._lbl_vo.config(
                        text=f"{os.path.basename(f)}  ->  VO {vo_d:.1f}s | "
                             f"Video {m}m {s}s",
                        fg=GREEN)
            except Exception:
                self._lbl_vo.config(text=os.path.basename(f), fg=TEXT)

    def pick_sub_script(self):
        f = filedialog.askopenfilename(
            title="Select Script Text File",
            filetypes=[("Text files","*.txt"),("All","*.*")])
        if f:
            self.sub_script_file = f
            self._lbl_sub_script.config(text=os.path.basename(f), fg=GREEN)

    def clear_sub_script(self):
        self.sub_script_file = ""
        self._lbl_sub_script.config(
            text="Not selected  (auto-transcribe)", fg=SUB)



    def _log(self, msg):
        self._console.config(state=tk.NORMAL)
        self._console.insert(tk.END, msg + "\n")
        self._console.see(tk.END)
        self._console.config(state=tk.DISABLED)
        self.root.update_idletasks()
        # Also print to system console (if launched from terminal)
        print(msg)

    # -- Start ---------------------------------------------

    def start(self):
        if not self.video_folder:
            messagebox.showerror("Error", "Please select a video clips folder!")
            return

        if self.use_vo.get():
            if not self.voiceover_file:
                messagebox.showerror("Error", "Please select a voiceover file!")
                return
            try:
                with AudioFileClip(self.voiceover_file) as a:
                    total_dur = PRE_ROLL + a.duration + TAIL
            except Exception as e:
                messagebox.showerror("Error", f"Cannot read voiceover:\n{e}")
                return
        else:
            total_dur = float(self.custom_dur.get())

        if not self.output_file:
            self.output_file = os.path.join(
                os.path.dirname(self.video_folder), "storytelling_output.mp4")
            self._lbl_out.config(text="storytelling_output.mp4", fg=TEXT)

        # Save settings before starting
        save_settings(self._collect_settings())

        self._start_btn.config(state=tk.DISABLED, text="[WAIT]  Processing...")
        self._console.config(state=tk.NORMAL)
        self._console.delete("1.0", tk.END)
        self._console.config(state=tk.DISABLED)

        def run():
            try:
                combine_videos(
                    video_folder    = self.video_folder,
                    output_file     = self.output_file,
                    total_duration  = total_dur,
                    voiceover_file  = self.voiceover_file if self.use_vo.get() else None,
                    image_folder    = self.image_folder or None,
                    image_duration  = self.image_dur.get(),
                    kb_style        = self.kb_style.get(),
                    quality_preset  = self.quality_preset.get(),
                    fade_duration   = self.fade_dur.get(),
                    title_text      = self.title_text.get(),
                    title_duration  = 3,
                    use_subtitles      = self.use_subtitles.get(),
                    sub_font_size      = self.sub_font_size.get(),
                    sub_output_mode    = self.sub_output_mode.get(),
                    sub_script_file    = self.sub_script_file or None,
                    progress_callback  = self._log,
                )
                messagebox.showinfo("[OK] Done!", f"Saved to:\n{self.output_file}")
            except Exception as e:
                self._log(f"ERROR: {e}")
                messagebox.showerror("[ERR] Error", str(e))
            finally:
                self._start_btn.config(state=tk.NORMAL, text=">  START")

        threading.Thread(target=run, daemon=True).start()

    # -- About ---------------------------------------------

    def show_subtitle_tool(self):
        """
        Quick Subtitle Tool:
        - Extract audio from video
        - Transcribe with Whisper (or align with stable-ts if script provided)
        - Output: burned-in video / SRT only / both
        """
        win = tk.Toplevel(self.root)
        win.title("Quick Subtitle Tool – NavyCat Studio")
        win.geometry("580x580")
        win.configure(bg=BG)
        win.resizable(True, True)

        _video_path   = tk.StringVar()
        _script_path  = tk.StringVar()
        _output_path  = tk.StringVar()
        # Load saved quick-tool settings
        _qs = load_settings()
        _font_size    = tk.IntVar(value=_qs.get("qt_font_size",   22))
        _bold         = tk.BooleanVar(value=_qs.get("qt_bold",    True))
        _use_script   = tk.BooleanVar(value=_qs.get("qt_use_script", False))
        _output_mode  = tk.StringVar(value=_qs.get("qt_output_mode", "burn"))

        # Header
        hdr = tk.Frame(win, bg="#1d4ed8", pady=10); hdr.pack(fill=tk.X)
        tk.Label(hdr, text="Quick Subtitle Tool",
                 bg="#1d4ed8", fg="white",
                 font=("Segoe UI",13,"bold")).pack()
        tk.Label(hdr,
                 text="Whisper AI  +  stable-ts forced alignment  |  NavyCat Studio",
                 bg="#1d4ed8", fg="#bfdbfe", font=("Segoe UI",8)).pack()

        body = tk.Frame(win, bg=BG, padx=16, pady=10)
        body.pack(fill=tk.BOTH, expand=True)

        def file_row(parent, caption, btn_text, cmd, note=""):
            tk.Label(parent, text=caption, bg=BG, fg=TEXT,
                     font=("Segoe UI",9,"bold")).pack(anchor=tk.W, pady=(8,0))
            if note:
                tk.Label(parent, text=note, bg=BG, fg=SUB,
                         font=("Segoe UI",8)).pack(anchor=tk.W)
            r = tk.Frame(parent, bg=BG); r.pack(fill=tk.X, pady=2)
            lbl = tk.Label(r, text="Not selected", bg=ENTRY, fg=SUB,
                           anchor=tk.W, padx=6, relief=tk.FLAT, font=("Segoe UI",9))
            lbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
            tk.Button(r, text=btn_text, command=cmd,
                      bg=ACCENT, fg="white", relief=tk.FLAT, padx=8,
                      font=("Segoe UI",9,"bold")).pack(side=tk.RIGHT, padx=(4,0))
            return lbl

        # Video file
        def pick_video():
            p = filedialog.askopenfilename(
                parent=win, title="Select Video",
                filetypes=[("Video","*.mp4 *.mkv *.avi *.mov"),("All","*.*")])
            if p:
                _video_path.set(p)
                vlbl.config(text=os.path.basename(p), fg=TEXT)
                base = os.path.splitext(p)[0]
                _output_path.set(base + "_subtitled.mp4")
                outlbl.config(text=os.path.basename(base+"_subtitled.mp4"), fg=TEXT)

        vlbl = file_row(body, "Video File:", "Browse", pick_video)

        # Script file (optional)
        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=(10,4))

        sc_hdr = tk.Frame(body, bg=BG); sc_hdr.pack(fill=tk.X)
        tk.Checkbutton(
            sc_hdr,
            text="Use script file for accurate timing  (stable-ts forced alignment)",
            variable=_use_script, command=lambda: toggle_script(),
            bg=BG, fg=GREEN, selectcolor=ENTRY, activebackground=BG,
            font=("Segoe UI",9,"bold")
        ).pack(side=tk.LEFT)

        script_row = tk.Frame(body, bg=BG)
        slbl = tk.Label(script_row, text="Not selected", bg=ENTRY, fg=SUB,
                        anchor=tk.W, padx=6, relief=tk.FLAT, font=("Segoe UI",9))
        slbl.pack(side=tk.LEFT, fill=tk.X, expand=True)
        def pick_script():
            p = filedialog.askopenfilename(
                parent=win, title="Select Script TXT",
                filetypes=[("Text","*.txt"),("All","*.*")])
            if p:
                _script_path.set(p)
                slbl.config(text=os.path.basename(p), fg=TEXT)
        tk.Button(script_row, text="Browse", command=pick_script,
                  bg=ACCENT, fg="white", relief=tk.FLAT, padx=8,
                  font=("Segoe UI",9,"bold")).pack(side=tk.RIGHT, padx=(4,0))

        def toggle_script():
            if _use_script.get():
                script_row.pack(fill=tk.X, pady=2)
            else:
                script_row.pack_forget()

        # Restore saved toggle state
        toggle_script()

        # Output file
        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=(10,0))
        def pick_output():
            p = filedialog.asksaveasfilename(
                parent=win, defaultextension=".mp4",
                filetypes=[("MP4","*.mp4"),("All","*.*")])
            if p:
                _output_path.set(p)
                outlbl.config(text=os.path.basename(p), fg=TEXT)
        outlbl = file_row(body, "Save Output As:", "Choose", pick_output)

        # Output mode + Style
        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=(10,6))

        # Output mode row
        om_row = tk.Frame(body, bg=BG); om_row.pack(anchor=tk.W)
        tk.Label(om_row, text="Output mode:", bg=BG, fg=TEXT,
                 font=("Segoe UI",9,"bold")).pack(side=tk.LEFT, padx=(0,10))

        def on_mode_change(*_):
            """Update output path extension when mode changes."""
            cur = _output_path.get()
            if not cur:
                return
            base = os.path.splitext(cur)[0]
            # Remove _subtitled suffix to get clean base
            if base.endswith("_subtitled"):
                base = base[:-len("_subtitled")]
            if _output_mode.get() == "srt":
                new_path = base + "_subtitled.srt"
            else:
                new_path = base + "_subtitled.mp4"
            _output_path.set(new_path)
            outlbl.config(text=os.path.basename(new_path), fg=TEXT)

        _output_mode.trace("w", on_mode_change)

        for t, v in [("Burned-in video","burn"),("SRT file only","srt"),("Both","both")]:
            tk.Radiobutton(om_row, text=t, variable=_output_mode, value=v,
                           bg=BG, fg=TEXT, selectcolor=ENTRY, activebackground=BG,
                           font=("Segoe UI",9)).pack(side=tk.LEFT, padx=6)

        # Style row
        st_row = tk.Frame(body, bg=BG); st_row.pack(anchor=tk.W, pady=(6,0))
        tk.Label(st_row, text="Font size:", bg=BG, fg=TEXT,
                 font=("Segoe UI",9,"bold")).pack(side=tk.LEFT)
        tk.Spinbox(st_row, from_=12, to=48, textvariable=_font_size,
                   width=4, bg=ENTRY, fg=TEXT, relief=tk.FLAT,
                   font=("Segoe UI",9)).pack(side=tk.LEFT, padx=6)
        tk.Checkbutton(st_row, text="Bold", variable=_bold,
                       bg=BG, fg=TEXT, selectcolor=ENTRY,
                       activebackground=BG, font=("Segoe UI",9)).pack(side=tk.LEFT, padx=8)
        tk.Label(st_row, text="White text on dark box  (bottom centre)",
                 bg=BG, fg=SUB, font=("Segoe UI",8)).pack(side=tk.LEFT)

        # Library status
        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=(8,4))
        lib_lines = []
        try:
            import stable_whisper; lib_lines.append("[OK] stable-ts installed  (forced alignment ready)")
        except ImportError:
            lib_lines.append("[WARN] stable-ts not installed:  pip install stable-ts")
        try:
            import whisper; lib_lines.append("[OK] openai-whisper installed")
        except ImportError:
            lib_lines.append("[ERR] openai-whisper not installed:  pip install openai-whisper")
        tk.Label(body, text="\n".join(lib_lines), bg=BG, fg=SUB,
                 font=("Segoe UI",8), justify=tk.LEFT).pack(anchor=tk.W)

        # Progress log
        tk.Label(body, text="Progress:", bg=BG, fg=SUB,
                 font=("Segoe UI",9)).pack(anchor=tk.W, pady=(8,0))
        log_box = scrolledtext.ScrolledText(
            body, height=4, wrap=tk.WORD, state=tk.DISABLED,
            bg="#0a0a14", fg="#a0f0a0", relief=tk.FLAT, font=("Consolas",9))
        log_box.pack(fill=tk.BOTH, expand=True, pady=(2,0))

        def sub_log(msg):
            log_box.config(state=tk.NORMAL)
            log_box.insert(tk.END, msg+"\n")
            log_box.see(tk.END)
            log_box.config(state=tk.DISABLED)
            win.update_idletasks()
            print(msg)

        start_btn = tk.Button(body, text="> Generate Subtitles",
                              bg=GREEN, fg="#0f172a",
                              font=("Segoe UI",11,"bold"),
                              relief=tk.FLAT, pady=8)
        start_btn.pack(fill=tk.X, pady=(10,0))

        def run_subtitle():
            v = _video_path.get()
            o = _output_path.get()
            s = _script_path.get() if _use_script.get() else None
            mode = _output_mode.get()

            if not v or not os.path.isfile(v):
                messagebox.showerror("Error","Please select a video file!",parent=win)
                return
            if s and not os.path.isfile(s):
                messagebox.showerror("Error","Script file not found!",parent=win)
                return
            if not o:
                base = os.path.splitext(v)[0]
                o = base + "_subtitled.mp4"
                _output_path.set(o)
                outlbl.config(text=os.path.basename(o), fg=TEXT)
            try:
                import whisper
            except ImportError:
                messagebox.showerror("Error",
                    "openai-whisper not installed!\nRun: pip install openai-whisper",
                    parent=win)
                return

            start_btn.config(state=tk.DISABLED, text="Processing...")
            log_box.config(state=tk.NORMAL)
            log_box.delete("1.0", tk.END)
            log_box.config(state=tk.DISABLED)

            def worker():
                try:
                    # Save quick-tool settings
                    _cur = load_settings()
                    _cur["qt_font_size"]   = _font_size.get()
                    _cur["qt_bold"]        = _bold.get()
                    _cur["qt_use_script"]  = _use_script.get()
                    _cur["qt_output_mode"] = _output_mode.get()
                    save_settings(_cur)

                    srt_path = os.path.join(TMP_ROOT, "qs_subs.srt")

                    # Step 1: Extract audio
                    sub_log("Step 1/3: Extracting audio from video...")
                    audio_tmp = os.path.join(TMP_ROOT, "qs_audio.wav")
                    r = subprocess.run([
                        FFMPEG_BIN, "-y", "-i", v,
                        "-vn", "-acodec", "pcm_s16le",
                        "-ar", "16000", "-ac", "1", audio_tmp
                    ], capture_output=True, text=True, timeout=120)
                    if r.returncode != 0:
                        sub_log(f"  ERROR: {r.stderr[-200:]}")
                        return
                    sub_log("  Audio extracted (16kHz mono)")

                    # Load as numpy array via scipy
                    import scipy.io.wavfile as _wav
                    sr, wav_data = _wav.read(audio_tmp)
                    if wav_data.dtype == np.int16:
                        audio_arr = wav_data.astype(np.float32) / 32768.0
                    else:
                        audio_arr = wav_data.astype(np.float32)
                    if audio_arr.ndim > 1:
                        audio_arr = audio_arr.mean(axis=1)
                    try: os.remove(audio_tmp)
                    except: pass

                    # Step 2: Transcribe / align
                    if s:
                        sub_log("Step 2/3: Aligning script (stable-ts)...")
                        try:
                            import stable_whisper
                            model = stable_whisper.load_model("tiny")
                            with open(s, "r", encoding="utf-8") as tf:
                                script_text = tf.read().strip()
                            # Detect language
                            import whisper as _wl
                            _lm = _wl.load_model("tiny")
                            short = audio_arr[:16000*20] if len(audio_arr) > 16000*20 else audio_arr
                            _lr = _lm.transcribe(short, fp16=False, verbose=False)
                            lang = _lr.get("language","en") or "en"
                            del _lm, _lr, short
                            sub_log(f"  Language: {lang}")
                            result = model.align(audio_arr, script_text, language=lang)
                            result.to_srt_vtt(srt_path, word_level=False)
                            sub_log("  Alignment complete")
                        except ImportError:
                            sub_log("  stable-ts not found – using plain Whisper")
                            import whisper as _w
                            model = _w.load_model("tiny")
                            res = model.transcribe(audio_arr, fp16=False, verbose=False)
                            _write_srt(res["segments"], srt_path, offset=0)
                            sub_log(f"  {len(res['segments'])} segments")
                    else:
                        sub_log("Step 2/3: Transcribing with Whisper AI...")
                        import whisper as _w
                        model = _w.load_model("tiny")
                        res = model.transcribe(audio_arr, fp16=False, verbose=False)
                        segs = res.get("segments", [])
                        if not segs:
                            sub_log("ERROR: No speech detected!")
                            return
                        _write_srt(segs, srt_path, offset=0)
                        sub_log(f"  {len(segs)} segments generated")

                    # Step 3: Output
                    sub_log("Step 3/3: Generating output...")

                    # Determine SRT output path (always same folder as video)
                    vid_base = os.path.splitext(v)[0]
                    srt_out  = vid_base + "_subtitled.srt"

                    # Save SRT if requested
                    if mode in ("srt", "both"):
                        import shutil as _sh
                        _sh.copy2(srt_path, srt_out)
                        sub_log(f"  SRT saved: {srt_out}")

                    # Burn subtitles if requested
                    if mode in ("burn", "both"):
                        ok = burn_subtitles(v, o, srt_path,
                                            _font_size.get(), sub_log,
                                            bold=_bold.get())
                        if ok:
                            sub_log(f"  Video saved: {o}")
                        else:
                            sub_log("  Burn failed – check log above")
                            return

                    # Success message
                    if mode == "srt":
                        done_msg = f"SRT file saved to:\n{srt_out}"
                    elif mode == "both":
                        done_msg = f"Video saved to:\n{o}\n\nSRT saved to:\n{srt_out}"
                    else:
                        done_msg = f"Video saved to:\n{o}"
                    messagebox.showinfo("Done!", done_msg, parent=win)

                except Exception as e:
                    sub_log(f"ERROR: {e}")
                    import traceback
                    sub_log(traceback.format_exc())
                    messagebox.showerror("Error", str(e), parent=win)
                finally:
                    start_btn.config(state=tk.NORMAL, text="> Generate Subtitles")

            threading.Thread(target=worker, daemon=True).start()

        start_btn.config(command=run_subtitle)


    def show_about(self):
        win = tk.Toplevel(self.root)
        win.title("About – v8.0")
        win.geometry("460x420")
        win.configure(bg=BG)
        win.resizable(False, False)

        hdr = tk.Frame(win, bg=ACCENT, pady=14); hdr.pack(fill=tk.X)
        tk.Label(hdr, text="[VIDEO]  Video Combiner",
                 bg=ACCENT, fg="white",
                 font=("Segoe UI",14,"bold")).pack()
        tk.Label(hdr, text="v8.0  –  Low-Spec Edition",
                 bg=ACCENT, fg="#c4b5fd", font=("Segoe UI",9)).pack()

        body = tk.Frame(win, bg=BG, padx=24, pady=16)
        body.pack(fill=tk.BOTH, expand=True)

        def row(lbl, val, vc=TEXT):
            f = tk.Frame(body, bg=BG); f.pack(fill=tk.X, pady=2)
            tk.Label(f, text=lbl, bg=BG, fg=SUB, font=("Segoe UI",9),
                     width=20, anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(f, text=val, bg=BG, fg=vc,
                     font=("Segoe UI",9,"bold"), anchor=tk.W).pack(side=tk.LEFT)

        row("Version",    "8.0  –  Subtitles Edition", GREEN)
        row("Output",     "720p default  |  1080p optional")
        row("Preset",     "x264 veryfast  (i3-safe)")
        row("Batch size", "3  (4 GB RAM-safe)")
        row("Temp folder", TMP_ROOT[:40]+"...")

        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=10)
        tk.Label(body, text="[COMPANY]  Developed by", bg=BG, fg=SUB,
                 font=("Segoe UI",9)).pack(anchor=tk.W)
        tk.Label(body, text="NavyCat Studio",
                 bg=BG, fg=ACCENT,
                 font=("Segoe UI",15,"bold")).pack(anchor=tk.W, pady=(2,10))

        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=(0,10))
        tk.Label(body, text="[DEV]  Credits", bg=BG, fg=SUB,
                 font=("Segoe UI",9)).pack(anchor=tk.W)

        for lbl, val in [
            ("Program & Architecture", "Claude  (Anthropic AI)"),
            ("Concept & Direction",     "NavyCat Studio"),
            ("Libraries",               "MoviePy  •  Pillow  •  NumPy  •  FFmpeg"),
        ]:
            f = tk.Frame(body, bg=BG); f.pack(fill=tk.X, pady=2)
            tk.Label(f, text=f"  {lbl}:", bg=BG, fg=SUB, font=("Segoe UI",9),
                     width=24, anchor=tk.W).pack(side=tk.LEFT)
            tk.Label(f, text=val, bg=BG, fg=TEXT,
                     font=("Segoe UI",9), anchor=tk.W).pack(side=tk.LEFT)

        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=10)

        # Release notes tab
        tk.Label(body, text="[LOG]  Release Notes", bg=BG, fg=SUB,
                 font=("Segoe UI",9)).pack(anchor=tk.W)

        notes_frame = tk.Frame(body, bg=ENTRY, padx=10, pady=8)
        notes_frame.pack(fill=tk.X, pady=(4,0))

        RELEASE_NOTES = [
            ("v8.0", [
                "Whisper AI auto-transcribe voiceover -> SRT subtitles",
                "Burned-in subtitles: dark box + white bold text (YouTube style)",
                "Font size configurable (12–48px)",
                "Whisper install check shown in UI",
                "Subtitles offset by PRE_ROLL (1s) automatically",
            ]),
            ("v7.1", [
                "Ken Burns: smooth ease in-out zoom, no shake, no pan",
                "Images: fade in/out applied per image clip",
                "Temp folder moved to Documents\\NavyCat_Temp (no AppData)",
                "Image render console progress per image",
            ]),
            ("v7.0", [
                "RAM fix: silent_audio uses float32 (was float64 – 21MB spike)",
                "crop_fill returns uint8 frames",
                "Console output window with Copy log button",
                "Settings auto-save/load (navycat_settings.json)",
                "Temp files in system temp folder, auto-cleaned on exit",
            ]),
            ("v6.1", [
                "probe_video() via ffprobe – fast pre-validation, no decode",
                "safe_load_video() validates w/h/duration/first frame",
                "Batch loop uses safe_load_video to avoid broken clip objects",
            ]),
            ("v6.0", [
                "list index out of range fix: crop_fill rewritten with numpy",
                "imageio-ffmpeg used for reliable FFmpeg path",
                "Voiceover optional – custom duration slider added",
                "Simplified UI (sections reduced)",
            ]),
            ("v5.0", [
                "Batch size 3 (4 GB RAM safe)",
                "x264 veryfast preset (i3 2nd Gen optimized)",
                "Ken Burns pre-rendered to temp file (saves RAM)",
                "PIL BILINEAR resize (faster than LANCZOS)",
            ]),
        ]

        notes_text = scrolledtext.ScrolledText(
            notes_frame, height=10, wrap=tk.WORD, state=tk.NORMAL,
            bg=ENTRY, fg=TEXT, relief=tk.FLAT, font=("Consolas", 8),
        )
        notes_text.pack(fill=tk.BOTH, expand=True)
        for ver, items in RELEASE_NOTES:
            notes_text.insert(tk.END, f"[ {ver} ]\n", "ver")
            for item in items:
                notes_text.insert(tk.END, f"  • {item}\n")
            notes_text.insert(tk.END, "\n")
        notes_text.tag_config("ver", foreground=GREEN, font=("Consolas", 8, "bold"))
        notes_text.config(state=tk.DISABLED)

        tk.Frame(body, bg=PANEL, height=1).pack(fill=tk.X, pady=10)
        tk.Label(body, text="© 2025 NavyCat Studio  –  All rights reserved",
                 bg=BG, fg=SUB, font=("Segoe UI",8)).pack()

        tk.Button(win, text="Close", command=win.destroy,
                  bg=PANEL, fg=TEXT, relief=tk.FLAT,
                  font=("Segoe UI",9), pady=6).pack(
                  fill=tk.X, padx=24, pady=(0,16))


# ==========================================================
if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()
