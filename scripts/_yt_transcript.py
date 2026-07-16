"""Fetch a YouTube transcript via youtube-transcript-api (works where WebFetch / transcript
sites are blocked). Prints the full transcript + uppercase ticker candidates."""
import re
import sys
from collections import Counter

vid = sys.argv[1] if len(sys.argv) > 1 else "znf0QIW8KRE"

segs = None
err = None
try:
    from youtube_transcript_api import YouTubeTranscriptApi
except Exception as e:
    print("IMPORT_FAILED:", repr(e)[:200]); sys.exit(2)

# The library changed shape across versions — try the common entrypoints.
for attempt in ("get_transcript", "instance_fetch", "list_then_fetch"):
    try:
        if attempt == "get_transcript":
            segs = YouTubeTranscriptApi.get_transcript(vid, languages=["en", "en-US"])
        elif attempt == "instance_fetch":
            api = YouTubeTranscriptApi()
            f = api.fetch(vid)
            segs = f.to_raw_data() if hasattr(f, "to_raw_data") else list(f)
        else:
            tl = YouTubeTranscriptApi.list_transcripts(vid)
            tr = tl.find_transcript(["en", "en-US"]) if hasattr(tl, "find_transcript") else None
            segs = tr.fetch() if tr else None
        if segs:
            break
    except Exception as e:
        err = e

if not segs:
    print("FAILED to fetch transcript:", repr(err)[:400]); sys.exit(1)


def _txt(s):
    return s.get("text") if isinstance(s, dict) else getattr(s, "text", "")


text = " ".join(_txt(s) for s in segs)
print("=== TRANSCRIPT (%d chars) ===" % len(text))
print(text)
print()
caps = re.findall(r"\b[A-Z]{1,5}\b", text)
common = set("THE AND FOR ARE BUT NOT YOU ALL CAN HAD HER WAS ONE OUR OUT DAY GET HAS HIM HIS "
             "HOW MAN NEW NOW OLD SEE TWO WAY WHO BOY DID ITS LET PUT SAY SHE TOO USE DAD MOM "
             "USA CEO IPO ATM PM AM EST ET OK TV US UK I A AN IS IT IN ON OF TO BE AS AT BY OR "
             "SO UP WE MY ME NO IF DO GO HI OH YEAH OKAY THAT THIS WITH JUST LIKE WHAT WHEN GOT "
             "RVOL EMA VWAP P L PR".split())
tickers = [t for t in caps if t not in common and len(t) >= 2]
print("=== UPPERCASE TICKER CANDIDATES (auto-captions may lowercase these) ===")
print(Counter(tickers).most_common(30))
