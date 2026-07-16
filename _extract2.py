try:
    from pypdf import PdfReader
except Exception:
    from PyPDF2 import PdfReader

path = r"C:\Users\rindo\.claude\projects\D--dev-chili-home-copilot\7bfcb528-0730-479f-9964-ed482446908a\tool-results\webfetch-1782262303004-0ramai.pdf"
r = PdfReader(path)
txt = [p.extract_text() or "" for p in r.pages]
full = "\n".join(txt)

i = full.rfind("9. Conclusion")
seg = full[i:i+3200] if i >= 0 else "NOT FOUND"
with open(r"D:\dev\chili-home-copilot\_concl_out.txt", "w", encoding="utf-8") as f:
    f.write("==== CONCLUSION SECTION ====\n")
    f.write(seg)
print("done", i)
