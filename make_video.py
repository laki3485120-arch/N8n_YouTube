#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_video.py - Hindi Shorts video generator (runs on GitHub Actions, no YouTube secrets)

Usage : python make_video.py payload.json [--no-release]
Input : payload.json = {"tag":"short-2026...","scenes":[...],"title":"...", ...}
Output: final_short.mp4 (720x1280) published as a GitHub Release asset under <tag>.
        n8n polls that release, downloads the video and uploads it to YouTube.

Env:
  GH_TOKEN             (auto: ${{ github.token }})  used by the `gh` CLI
  GITHUB_REPOSITORY    (auto in GitHub Actions)
  POLLINATIONS_API_KEY (optional) only if the anonymous Pollinations tier rate-limits you
"""
import asyncio
import json
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
W, H = 720, 1280
FPS = 25
WORK = "/tmp/shorts_work"
OUT = os.path.abspath(os.environ.get("OUT_FILE", "final_short.mp4"))
FONT = "Noto Sans Devanagari"
FONT_SIZE = 76
WORDS_PER_CHUNK = 3
PAD = 0.25
STYLE = ("3D Pixar style cartoon character, vertical 9:16 composition, "
         "cinematic lighting, highly detailed, vibrant colors")

EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
    "\U00002190-\U000021FF\U00002300-\U000023FF\U0000FE0F]+",
    flags=re.UNICODE,
)


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
    if isinstance(data, list):  # plain scenes array
        data = {"scenes": data}
    if "output" in data and "scenes" not in data:  # raw Gemini output
        data = data["output"]
    if not data.get("scenes"):
        sys.exit("No scenes found in payload")
    return data


def clean_text(t):
    t = EMOJI_RE.sub("", t or "")
    return re.sub(r"\s+", " ", t).strip()


# ----------------------------- IMAGE ----------------------------------------
def valid_image(path):
    try:
        run(["ffprobe", "-v", "error", "-show_entries", "stream=width", "-of", "csv=p=0", path])
        return os.path.getsize(path) > 5000
    except Exception:
        return False


def fetch_image(prompt, path, seed):
    q = urllib.parse.quote(prompt[:900])
    key = os.environ.get("POLLINATIONS_API_KEY", "").strip()
    urls = [
        ("https://image.pollinations.ai/prompt/%s?width=%d&height=%d&model=flux&seed=%d&nologo=true&private=true" % (q, W, H, seed)),
        ("https://gen.pollinations.ai/image/%s?width=%d&height=%d&model=flux&seed=%d&nologo=true" % (q, W, H, seed)),
    ]
    for attempt in range(1, 6):
        for u in urls:
            try:
                headers = {"User-Agent": "Mozilla/5.0"}
                if key and "gen.pollinations.ai" in u:
                    headers["Authorization"] = "Bearer " + key
                req = urllib.request.Request(u, headers=headers)
                with urllib.request.urlopen(req, timeout=150) as r:
                    data = r.read()
                    ctype = r.headers.get("Content-Type", "")
                if "image" in ctype and len(data) > 5000:
                    with open(path, "wb") as f:
                        f.write(data)
                    if valid_image(path):
                        return True
            except Exception as e:
                log("  image attempt %d failed: %s" % (attempt, e))
        time.sleep(10 * attempt)
    return False


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
    if len(words) < max(1, len(clean_words) // 2):
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


def ass_events(words, offset, scene_len):
    lines = []
    chunks = [words[i:i + WORDS_PER_CHUNK] for i in range(0, len(words), WORDS_PER_CHUNK)]
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
            parts.append("{\\kf%d}%s" % (cs, w))
        lines.append("Dialogue: 0,%s,%s,Karaoke,,0,0,0,,%s" % (
            ass_time(offset + start), ass_time(offset + end), " ".join(parts)))
    return lines


def write_ass(path, event_lines):
    header = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: %d\nPlayResY: %d\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Karaoke,%s,%d,&H0000E6FF,&H00FFFFFF,&H00000000,&H64000000,-1,0,0,0,"
        "100,100,0,0,1,6,2,5,40,40,0,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    ) % (W, H, FONT, FONT_SIZE)
    with open(path, "w", encoding="utf-8") as f:
        f.write(header + "\n".join(event_lines) + "\n")


# ----------------------------- VIDEO ----------------------------------------
def make_clip(idx, img, mp3, dur, out):
    n = int(round(dur * FPS))
    if idx % 2 == 0:
        z = "min(zoom+0.0008,1.2)"
    else:
        z = "if(eq(on,0),1.15,max(zoom-0.0008,1.0))"
    vf = ("scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
          "zoompan=z='%s':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=%d:s=%dx%d:fps=%d,"
          "format=yuv420p") % (z, n, W, H, FPS)
    run(["ffmpeg", "-y", "-i", img, "-i", mp3, "-vf", vf,
         "-af", "apad=pad_dur=%s" % PAD, "-t", "%.3f" % dur,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-r", str(FPS),
         "-c:a", "aac", "-b:a", "128k", "-ar", "44100", "-ac", "2", out])


def render(scenes):
    if os.path.exists(OUT):
        os.remove(OUT)
    shutil.rmtree(WORK, ignore_errors=True)
    os.makedirs(WORK)

    if "devanagari" not in subprocess.run(["fc-list"], capture_output=True, text=True).stdout.lower():
        log("WARNING: Devanagari font missing (apt install fonts-noto-core)")

    seed = random.randint(1, 999999)
    clips, ass_lines, offset, prev_img = [], [], 0.0, None

    for i, sc in enumerate(scenes):
        text = clean_text(sc.get("dialogue", ""))
        if not text:
            continue
        log("[scene %d] %s" % (i + 1, text))
        img = os.path.join(WORK, "img_%02d.jpg" % i)
        mp3 = os.path.join(WORK, "voice_%02d.mp3" % i)
        clip = os.path.join(WORK, "clip_%02d.mp4" % i)

        prompt = "%s. Background: %s. %s" % (sc.get("image_prompt", ""), sc.get("background", ""), STYLE)
        if not fetch_image(prompt, img, seed):
            log("  image failed, using fallback")
            if prev_img:
                shutil.copy(prev_img, img)
            else:
                fallback_image(img)
        prev_img = img

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
        words = build_word_timings(text, events, audio_len)
        make_clip(i, img, mp3, audio_len + PAD, clip)
        scene_len = media_duration(clip)

        ass_lines += ass_events(words, offset, scene_len)
        offset += scene_len
        clips.append(clip)
        time.sleep(2)

    if not clips:
        raise RuntimeError("No clips were generated")

    concat_txt = os.path.join(WORK, "concat.txt")
    with open(concat_txt, "w") as f:
        for c in clips:
            f.write("file '%s'\n" % c)
    merged = os.path.join(WORK, "merged.mp4")
    run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt, "-c", "copy", merged])

    ass_path = os.path.join(WORK, "subs.ass")
    write_ass(ass_path, ass_lines)
    run(["ffmpeg", "-y", "-i", merged, "-vf", "ass=filename=%s" % ass_path,
         "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
         "-c:a", "copy", "-movflags", "+faststart", OUT])

    total = media_duration(OUT)
    log("Rendered %s (%.1fs, %d scenes)" % (OUT, total, len(clips)))
    if total > 59:
        log("WARNING: video is %.1fs, longer than a 60s Short" % total)
    shutil.rmtree(WORK, ignore_errors=True)
    return total


# ----------------------------- GITHUB RELEASE -------------------------------
def publish_release(path, tag, title):
    """Upload the finished video as a GitHub Release asset (n8n downloads it from there)."""
    if not re.fullmatch(r"short-[0-9A-Za-z_-]{1,60}", tag or ""):
        tag = "short-run-%s" % os.environ.get("GITHUB_RUN_ID", int(time.time()))
        log("Invalid/missing tag in payload, using %s" % tag)
    notes = (title or "Hindi Short").strip()[:200]
    run(["gh", "release", "create", tag, path,
         "--title", tag, "--notes", notes])
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
