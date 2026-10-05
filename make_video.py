#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_video.py - Hindi Shorts generator for GitHub Actions
1080x1920 (9:16) Full HD, 30 FPS, smooth Ken-Burns camera motion, karaoke subtitles.

Usage : python make_video.py payload.json [--no-release]
Input : payload.json = {"tag":"short-2026...","scenes":[...],"title":"...", ...}
Output: /tmp/shorts_render/final_short.mp4 (+ copy at ./final_short.mp4),
        published as a GitHub Release asset under <tag> (n8n downloads it from there).

Per scene : Pollinations flux image -> edge-tts Madhur voice -> FFmpeg zoompan + karaoke ASS
Join      : video clips stream-copied; audio rebuilt sample-exact (no drift) and encoded once.

Optional env: POLLINATIONS_API_KEY (only if anonymous Pollinations gets rate-limited)
"""
import asyncio
import json
import math
import os
import random
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

# ----------------------------- CONFIG ---------------------------------------
VOICE = "hi-IN-MadhurNeural"
RATE = "+8%"
W, H = 1080, 1920
FPS = 30
CRF = "18"
PRESET = "fast"
AUDIO_RATE = 44100                 # 44100 / 30 fps = 1470 samples per frame (exact)
OUT_DIR = "/tmp/shorts_render"
WORK = os.path.join(OUT_DIR, "work")
OUT = os.path.join(OUT_DIR, "final_short.mp4")

FONT = "Noto Sans Devanagari"
FONT_SIZE = 112
MAX_WORDS_PER_CAPTION = 4
MAX_CHARS_PER_CAPTION = 18         # keeps every caption to max 2 big lines
PAD = 0.25                         # silence after each spoken line (seconds)
SUPERSAMPLE = 3                    # zoompan works on a 3x image => sub-pixel smooth motion
PAN_ZOOM = 1.25                    # constant zoom while panning (room to move)
IMG_BUDGET_SCENE = 110             # max seconds of image retries per scene
IMG_BUDGET_TOTAL = 12 * 60         # max seconds of image retries for the whole video

STYLE = ("3D Pixar style cartoon character, 8k cinematic lighting, ultra detailed, "
         "sharp focus, vibrant colors, depth of field, vertical 9:16 composition")

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U0000FE0F]+",
    flags=re.UNICODE,
)
T0 = time.time()


def log(*a):
    print(*a, file=sys.stderr, flush=True)


def run(cmd):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError("Command failed: %s\n%s" % (" ".join(cmd[:4]), p.stderr[-1500:]))
    return p.stdout


# ----------------------------- INPUT ----------------------------------------
def load_payload(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if isinstance(data, list):
        data = {"scenes": data}
    if "output" in data and "scenes" not in data:
        data = data["output"]
    if not data.get("scenes"):
        sys.exit("No scenes found in payload")
    return data


def clean_text(t):
    t = EMOJI_RE.sub("", t or "")
    return re.sub(r"\s+", " ", t).strip()


# ----------------------------- IMAGE ----------------------------------------
def image_size(path):
    try:
        out = run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                   "-show_entries", "stream=width,height", "-of", "csv=p=0", path]).strip()
        w, h = out.split(",")[:2]
        return int(w), int(h)
    except Exception:
        return None


def fetch_image(prompt, path, seed):
    """Pollinations flux 1080x1920. Last retries use 720x1280 (upscaled later) if HD keeps failing."""
    q = urllib.parse.quote(prompt[:900])
    key = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    deadline = min(time.time() + IMG_BUDGET_SCENE, T0 + IMG_BUDGET_TOTAL)
    attempt = 0
    while True:
        attempt += 1
        w, h = (W, H) if attempt <= 3 else (720, 1280)
        urls = [
            "https://image.pollinations.ai/prompt/%s?width=%d&height=%d&model=flux&seed=%d&nologo=true&private=true" % (q, w, h, seed),
            "https://gen.pollinations.ai/image/%s?width=%d&height=%d&model=flux&seed=%d&nologo=true" % (q, w, h, seed),
        ]
        for u in urls:
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                if key and "gen.pollinations.ai" in u:
                    headers["Authorization"] = "Bearer " + key
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=90) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type", "")
                if "image" in ctype and len(data) > 5000:
                    with open(path, "wb") as f:
                        f.write(data)
                    size = image_size(path)
                    if size:
                        log("  image ok %dx%d (requested %dx%d)" % (size[0], size[1], w, h))
                        return True
            except Exception as e:
                log("  image attempt %d (%dx%d) failed: %s" % (attempt, w, h, e))
        if time.time() >= deadline:
            return False
        time.sleep(min(8 * attempt, max(0, deadline - time.time())))


def fallback_image(path):
    run(["ffmpeg", "-y", "-f", "lavfi", "-i", "color=c=0x1b1b3a:s=%dx%d" % (W, H),
         "-frames:v", "1", path])


# ----------------------------- VOICE ----------------------------------------
async def tts(text, mp3_path):
    import edge_tts
    try:
        comm = edge_tts.Communicate(text, VOICE, rate=RATE, boundary="WordBoundary")
    except TypeError:
        comm = edge_tts.Communicate(text, VOICE, rate=RATE)
    events = []
    with open(mp3_path, "wb") as f:
        async for chunk in comm.stream():
            if chunk["type"] == "audio":
                f.write(chunk["data"])
            elif chunk["type"] in ("WordBoundary", "SentenceBoundary"):
                events.append((chunk["text"], chunk["offset"] / 1e7, chunk["duration"] / 1e7))
    return events


def media_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path])
    return float(out.strip())


def build_word_timings(text, events, total):
    words = []
    for t, off, dur in events:
        parts = t.split()
        if not parts:
            continue
        tot = sum(len(p) for p in parts)
        cur = off
        for p in parts:
            d = dur * len(p) / tot
            words.append([p, cur, d])
            cur += d
    clean_words = text.split()
    if len(words) < max(1, len(clean_words) // 2):      # no usable boundaries -> estimate
        tot = sum(len(w) for w in clean_words) or 1
        span = max(total - 0.15, 0.5)
        cur = 0.0
        words = []
        for w in clean_words:
            d = span * len(w) / tot
            words.append([w, cur, d])
            cur += d
    for w in words:
        w[1] = min(w[1], total)
    return words


# ----------------------------- SUBTITLES ------------------------------------
def ass_time(t):
    cs = int(round(max(t, 0) * 100))
    h, m, s, c = cs // 360000, cs % 360000 // 6000, cs % 6000 // 100, cs % 100
    return "%d:%02d:%02d.%02d" % (h, m, s, c)


def split_captions(words):
    """Group words so each caption has <= MAX_WORDS words and <= MAX_CHARS characters."""
    caps, cur, chars = [], [], 0
    for w in words:
        add = len(w[0]) + (1 if cur else 0)
        if cur and (len(cur) >= MAX_WORDS_PER_CAPTION or chars + add > MAX_CHARS_PER_CAPTION):
            caps.append(cur)
            cur, chars, add = [], 0, len(w[0])
        cur.append(w)
        chars += add
    if cur:
        caps.append(cur)
    return caps


def ass_events(words, scene_len):
    """Karaoke lines for ONE scene (times start at 0 for that scene)."""
    lines = []
    chunks = split_captions(words)
    for ci, ch in enumerate(chunks):
        start = ch[0][1]
        if ci + 1 < len(chunks):
            end = chunks[ci + 1][0][1]
        else:
            end = min(ch[-1][1] + ch[-1][2] + 0.2, scene_len)
        end = max(end, start + 0.2)
        parts = []
        for wi, (w, ws, _wd) in enumerate(ch):
            nxt = ch[wi + 1][1] if wi + 1 < len(ch) else end
            cs = max(1, int(round((nxt - ws) * 100)))
            parts.append("{\\kf%d}%s" % (cs, w))       # \kf = smooth fill: white -> yellow
        lines.append("Dialogue: 0,%s,%s,Karaoke,,0,0,0,,%s" % (
            ass_time(start), ass_time(end), " ".join(parts)))
    return lines


def write_ass(path, event_lines):
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        # Primary = YELLOW (already spoken), Secondary = WHITE (coming up), thick black outline
        "Style: Karaoke,%s,%d,&H0000E6FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,"
        "100,100,0,0,1,8,3,5,60,60,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (W, H, FONT, FONT_SIZE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(event_lines) + "\n")


# ----------------------------- CAMERA MOTION --------------------------------
def camera_expr(motion, n):
    """(z, x, y) zoompan expressions. Eased (soft start/stop) but never fully static."""
    t = "(on/%d)" % max(n - 1, 1)
    e = "(0.4*%s+0.6*pow(%s,2)*(3-2*%s))" % (t, t, t)   # 0 -> 1
    cx = "iw/2-(iw/zoom/2)"
    cy = "ih/2-(ih/zoom/2)"
    pz = "%.2f" % PAN_ZOOM
    moves = [
        ("1.0+0.22*%s" % e, cx, cy),                      # 0: zoom-in
        (pz, "(iw-iw/zoom)*%s" % e, cy),                  # 1: pan left -> right
        ("1.22-0.22*%s" % e, cx, cy),                     # 2: zoom-out
        (pz, cx, "(ih-ih/zoom)*(1-%s)" % e),              # 3: pan bottom -> top
        (pz, "(iw-iw/zoom)*(1-%s)" % e, cy),              # 4: pan right -> left
        ("1.0+0.22*%s" % e, cx, "(ih-ih/zoom)*0.35"),     # 5: zoom-in toward upper area
    ]
    return moves[motion % len(moves)]


# ----------------------------- VIDEO / AUDIO --------------------------------
def make_video_clip(motion, img, ass_path, n, out):
    """Silent video clip with exactly n frames."""
    z, x, y = camera_expr(motion, n)
    sw, sh = W * SUPERSAMPLE, H * SUPERSAMPLE
    vf = ("scale=%d:%d:force_original_aspect_ratio=increase:flags=lanczos,crop=%d:%d,"
          "zoompan=z='%s':x='%s':y='%s':d=%d:s=%dx%d:fps=%d,"
          "ass=filename=%s,"
          "scale=out_color_matrix=bt709:out_range=tv,format=yuv420p"
          ) % (sw, sh, sw, sh, z, x, y, n, W, H, FPS, ass_path)
    run(["ffmpeg", "-y", "-i", img, "-vf", vf, "-an", "-frames:v", str(n),
         "-c:v", "libx264", "-preset", PRESET, "-crf", CRF, "-pix_fmt", "yuv420p", "-r", str(FPS),
         "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
         "-color_range", "tv", out])


def make_audio_wav(mp3, n, out):
    """Voice padded/cut to EXACTLY n frames of time (n/FPS s) -> sample-exact A/V sync."""
    d = n / FPS
    run(["ffmpeg", "-y", "-i", mp3,
         "-af", "aresample=%d,aformat=sample_fmts=s16:channel_layouts=stereo,"
                "apad=whole_dur=%.6f,atrim=end=%.6f" % (AUDIO_RATE, d, d),
         "-ar", str(AUDIO_RATE), "-ac", "2", "-c:a", "pcm_s16le", out])


def concat(files, out, list_path):
    with open(list_path, "w") as f:
        for c in files:
            f.write("file '%s'\n" % c)
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_path, "-c", "copy", out])


def render(scenes):
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)
    if os.path.exists(OUT):
        os.remove(OUT)

    fams = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True).stdout
    if FONT.lower() not in fams.lower():
        log("WARNING: font '%s' missing -> Hindi subtitles may look wrong (apt install fonts-noto-core)" % FONT)

    seed = random.randint(1, 999999)       # same seed => more consistent character across scenes
    vclips, aclips, prev_img, motion = [], [], None, 0

    for i, sc in enumerate(scenes):
        text = clean_text(sc.get("dialogue", ""))
        if not text:
            continue
        log("[scene %d] %s" % (i + 1, text))
        img = os.path.join(WORK, "img_%02d.jpg" % i)
        mp3 = os.path.join(WORK, "voice_%02d.mp3" % i)
        ass = os.path.join(WORK, "subs_%02d.ass" % i)
        vclip = os.path.join(WORK, "v_%02d.mp4" % i)
        wav = os.path.join(WORK, "a_%02d.wav" % i)

        # 1) image
        prompt = "%s. Background: %s. %s" % (sc.get("image_prompt", ""), sc.get("background", ""), STYLE)
        if not fetch_image(prompt, img, seed):
            log("  image failed, using fallback")
            if prev_img:
                shutil.copy(prev_img, img)
            else:
                fallback_image(img)
        prev_img = img

        # 2) voice
        events = None
        for attempt in range(3):
            try:
                events = asyncio.run(tts(text, mp3))
                break
            except Exception as e:
                log("  tts attempt %d failed: %s" % (attempt + 1, e))
                time.sleep(4)
        if events is None:
            raise RuntimeError("edge-tts failed for scene %d" % (i + 1))
        audio_len = media_duration(mp3)

        # 3) frames from exact audio length, subtitles, camera move, sample-exact audio
        n = int(math.ceil((audio_len + PAD) * FPS))
        words = build_word_timings(text, events, audio_len)
        write_ass(ass, ass_events(words, n / FPS))
        make_video_clip(motion, img, ass, n, vclip)
        make_audio_wav(mp3, n, wav)
        vclips.append(vclip)
        aclips.append(wav)
        motion += 1
        time.sleep(2)

    if not vclips:
        raise RuntimeError("No clips were generated")

    # 4) join: video stream-copied, audio concatenated losslessly then encoded ONCE
    video_all = os.path.join(WORK, "video_all.mp4")
    audio_all = os.path.join(WORK, "audio_all.wav")
    concat(vclips, video_all, os.path.join(WORK, "v.txt"))
    concat(aclips, audio_all, os.path.join(WORK, "a.txt"))
    run(["ffmpeg", "-y", "-i", video_all, "-i", audio_all, "-map", "0:v:0", "-map", "1:a:0",
         "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", OUT])

    total = media_duration(OUT)
    log("Rendered %s (%.1fs, %d scenes, %dx%d @ %dfps)" % (OUT, total, len(vclips), W, H, FPS))
    if total > 59:
        log("WARNING: video is %.1fs, longer than a 60s Short" % total)
    shutil.rmtree(WORK, ignore_errors=True)
    try:
        shutil.copy(OUT, "final_short.mp4")   # for the workflow's backup-artifact step
    except Exception as e:
        log("copy to cwd skipped: %s" % e)
    return total


# ----------------------------- GITHUB RELEASE -------------------------------
def publish_release(path, tag, title):
    """Upload the finished video as a GitHub Release asset (n8n downloads it from there)."""
    if not re.fullmatch(r"short-[0-9A-Za-z_-]{1,60}", tag or ""):
        tag = "short-run-%s" % os.environ.get("GITHUB_RUN_ID", int(time.time()))
        log("Invalid/missing tag in payload, using %s" % tag)
    notes = (title or "Hindi Short").strip()[:200]
    try:
        run(["gh", "release", "create", tag, path, "--title", tag, "--notes", notes])
    except RuntimeError as e:                    # e.g. re-run of the same tag
        log("release create failed (%s) -> trying upload --clobber" % str(e)[:200])
        run(["gh", "release", "upload", tag, path, "--clobber"])
    log("Release published: %s" % tag)
    return tag


# ----------------------------- MAIN -----------------------------------------
def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        sys.exit("Usage: make_video.py payload.json [--no-release]")
    data = load_payload(args[0])

    total = render(data["scenes"])

    result = {"ok": True, "file": OUT, "duration_sec": round(total, 1)}
    if "--no-release" not in sys.argv:
        result["release_tag"] = publish_release(OUT, data.get("tag"), data.get("title"))
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log("FATAL: %s" % e)
        sys.exit(1)
