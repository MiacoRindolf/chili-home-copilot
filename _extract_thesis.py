import re
try:
    from pypdf import PdfReader
except Exception:
    from PyPDF2 import PdfReader

path = r"C:\Users\rindo\.claude\projects\D--dev-chili-home-copilot\7bfcb528-0730-479f-9964-ed482446908a\tool-results\webfetch-1782262303004-0ramai.pdf"
r = PdfReader(path)
out = []
out.append(f"PAGES {len(r.pages)}")
txt = []
for p in r.pages:
    txt.append(p.extract_text() or "")
full = "\n".join(txt)

out.append("==== FIRST 1800 CHARS (title page) ====")
out.append(full[:1800])

# Search for key conclusion terms
for kw in ["weak", "conclus", "Conclus", "Gao", "Zhang", "SBB", "Sinch", "predictab", "R2", "R-squared", "significan", "OMXS30", "abstract", "Abstract"]:
    idxs = [m.start() for m in re.finditer(re.escape(kw), full)]
    out.append(f"\n#### KW '{kw}' occurrences: {len(idxs)}")
    for i in idxs[:5]:
        seg = full[max(0,i-260):i+320].replace("\n"," ")
        out.append("  ... " + seg + " ...")

with open(r"D:\dev\chili-home-copilot\_thesis_out.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(out))
print("WROTE _thesis_out.txt", len(full), "chars total")
