"""Fetch + Whisper-transcribe fresh Ross recap videos into the evidence tree.

Thin reuse of the proven ``project_ws/_ross_playlist/_whisper_fetch.py`` loop
(yt_dlp bestaudio, no ffmpeg — PyAV decodes; faster-whisper base.en cpu/int8 with
VAD), but writing the TIMESTAMPED ``transcript_ts.txt`` format the recap-evidence
dirs use (``[MM:SS.ss] text`` lines) so the downstream trade-extraction pass
(EXTRACTION_CHECKLIST.md) can cite timestamps. Resumable — skips ids whose
transcript already exists (>200 bytes). Audio is deleted after transcription.

    python scripts/fetch_ross_recap.py --ids "p73Vmwgg64c,-QZ8buLplck" \
        --out-root project_ws/AgentOps/ross_video_evidence

Output per id: <out-root>/<id>/transcript_ts.txt + <id>/video_meta.json (title,
uploader, upload_date, duration) + dl.log.
"""
from __future__ import annotations

import argparse
import glob
import io
import json
import os
import sys
import time

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")


def fmt_ts(seconds: float) -> str:
    m = int(seconds // 60)
    s = seconds - m * 60
    return f"{m:02d}:{s:05.2f}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--ids", required=True,
                    help="comma-separated YouTube video ids (leading '-' is fine here)")
    ap.add_argument("--out-root", default=os.path.join(
        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
        "AgentOps", "ross_video_evidence"))
    args = ap.parse_args()
    ids = [v.strip() for v in args.ids.split(",") if v.strip()]

    import yt_dlp
    from faster_whisper import WhisperModel

    todo = []
    for vid in ids:
        d = os.path.join(args.out_root, vid)
        t = os.path.join(d, "transcript_ts.txt")
        if os.path.exists(t) and os.path.getsize(t) > 200:
            print(f"SKIP {vid} (transcript exists)", flush=True)
            continue
        todo.append(vid)
    print(f"to fetch+transcribe: {len(todo)}/{len(ids)}", flush=True)
    if not todo:
        return 0

    print("loading faster-whisper base.en (cpu/int8)...", flush=True)
    model = WhisperModel("base.en", device="cpu", compute_type="int8")
    print("model loaded", flush=True)

    ok = 0
    for vid in todo:
        d = os.path.join(args.out_root, vid)
        os.makedirs(d, exist_ok=True)
        audio = os.path.join(d, "_audio")
        try:
            opts = {"quiet": True, "no_warnings": True, "format": "bestaudio/best",
                    "outtmpl": audio + ".%(ext)s", "ignoreerrors": True}
            t0 = time.time()
            with yt_dlp.YoutubeDL(opts) as y:
                info = y.extract_info(f"https://www.youtube.com/watch?v={vid}", download=True)
            meta = {k: (info or {}).get(k) for k in
                    ("id", "title", "uploader", "upload_date", "duration", "view_count")}
            with open(os.path.join(d, "video_meta.json"), "w", encoding="utf-8") as fh:
                json.dump(meta, fh, indent=1)
            files = glob.glob(audio + ".*")
            if not files:
                print(f"FAIL {vid} no-audio", flush=True)
                continue
            dl_s = time.time() - t0
            t0 = time.time()
            segments, _ = model.transcribe(files[0], language="en", vad_filter=True)
            lines = [f"[{fmt_ts(s.start)}] {s.text.strip()}" for s in segments]
            text = "\n".join(lines)
            if len(text) < 200:
                print(f"FAIL {vid} short-transcript", flush=True)
            else:
                with open(os.path.join(d, "transcript_ts.txt"), "w", encoding="utf-8") as fh:
                    fh.write(f"# {meta.get('title')}\n# id={vid} upload_date={meta.get('upload_date')}"
                             f" (whisper base.en ts)\n\n{text}\n")
                ok += 1
                print(f"OK {vid} '{(meta.get('title') or '')[:60]}' "
                      f"({len(lines)} segs, dl {dl_s:.0f}s, whisper {time.time() - t0:.0f}s)",
                      flush=True)
        except Exception as ex:
            print(f"FAIL {vid} {str(ex)[:100]}", flush=True)
        finally:
            for f in glob.glob(audio + ".*"):
                try:
                    os.remove(f)
                except OSError:
                    pass
    print(f"\ndone: {ok}/{len(todo)}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
