import io, sys, json, os, time
sys.stdout=io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
from youtube_transcript_api import YouTubeTranscriptApi
D='project_ws/_ross_playlist'
vids=json.load(open(D+'/_index.json',encoding='utf-8'))
api=YouTubeTranscriptApi()
ok=fail=skip=0
for v in vids:
    vid=v['id']; fn=f"{D}/{v['i']:02d}_{vid}.txt"
    if os.path.exists(fn) and os.path.getsize(fn)>200: skip+=1; continue
    try:
        try: ft=api.fetch(vid); snips=ft.snippets if hasattr(ft,'snippets') else ft
        except Exception:
            tl=api.list(vid); t=tl.find_transcript(['en','en-US','en-GB']); snips=t.fetch()
        text=' '.join((s.text if hasattr(s,'text') else s.get('text','')) for s in snips)
        if len(text)<200: raise ValueError('too short')
        open(fn,'w',encoding='utf-8').write(f"# {v['i']}. {v['title']}\n# id={vid} len={len(text)}\n\n{text}")
        ok+=1; print(f"OK {v['i']:02d} {vid} ({len(text)} chars) {v['title'][:50]}")
    except Exception as ex:
        fail+=1; print(f"FAIL {v['i']:02d} {vid}: {str(ex)[:60]}")
    time.sleep(0.4)
print(f"\nDONE: ok={ok} skip={skip} fail={fail} / {len(vids)}")
