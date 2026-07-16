"""Complete the Ross playlist research: transcribe the 36 caption-disabled videos via
audio->Whisper (faster-whisper base.en, CPU int8). Resumable — skips any transcript that
already exists (>200 bytes). Downloads raw bestaudio (no ffmpeg conversion; PyAV decodes
m4a/webm directly), transcribes, writes NN_<id>.txt, deletes the audio.
"""
import glob
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

import yt_dlp  # noqa: E402
from faster_whisper import WhisperModel  # noqa: E402

D = os.path.dirname(os.path.abspath(__file__))
vids = json.load(open(os.path.join(D, "_index.json"), encoding="utf-8"))

missing = [
    v for v in vids
    if not (os.path.exists(os.path.join(D, f"{v['i']:02d}_{v['id']}.txt"))
            and os.path.getsize(os.path.join(D, f"{v['i']:02d}_{v['id']}.txt")) > 200)
]
print(f"to transcribe: {len(missing)}", flush=True)

print("loading faster-whisper base.en (cpu/int8)...", flush=True)
model = WhisperModel("base.en", device="cpu", compute_type="int8")
print("model loaded", flush=True)

ok = 0
for v in missing:
    fn = os.path.join(D, f"{v['i']:02d}_{v['id']}.txt")
    audio = os.path.join(D, f"_audio_{v['id']}")
    try:
        # raw bestaudio, no post-processing (no ffmpeg) -> PyAV decodes it.
        opts = {
            "quiet": True, "no_warnings": True, "format": "bestaudio/best",
            "outtmpl": audio + ".%(ext)s", "ignoreerrors": True,
        }
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([f"https://www.youtube.com/watch?v={v['id']}"])
        files = [f for f in glob.glob(audio + ".*")]
        if not files:
            print(f"FAIL {v['i']} no-audio", flush=True)
            continue
        af = files[0]
        t0 = time.time()
        segments, info = model.transcribe(af, language="en", vad_filter=True)
        text = " ".join(s.text.strip() for s in segments)
        if len(text) < 200:
            print(f"FAIL {v['i']} short-transcript", flush=True)
        else:
            with open(fn, "w", encoding="utf-8") as fh:
                fh.write(f"# {v['i']}. {v['title']}\n# id={v['id']} (whisper base.en)\n\n{text}")
            ok += 1
            print(f"OK {v['i']} {v['id']} ({len(text)} chars, {time.time()-t0:.0f}s transcribe)", flush=True)
        for f in glob.glob(audio + ".*"):
            try:
                os.remove(f)
            except OSError:
                pass
    except Exception as ex:
        print(f"FAIL {v['i']} {str(ex)[:60]}", flush=True)
        for f in glob.glob(audio + ".*"):
            try:
                os.remove(f)
            except OSError:
                pass

print(f"\nwhisper transcribed: {ok}/{len(missing)}", flush=True)
print(f"total transcripts now: {len([f for f in glob.glob(os.path.join(D,'*.txt')) if '_index' not in f])}/75", flush=True)
