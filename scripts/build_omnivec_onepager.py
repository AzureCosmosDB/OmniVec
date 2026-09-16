"""Generate the OmniVec value-focused one-pager PDF.

Audience: field engineers pitching OmniVec. Emphasis on what it offers.

Run: python scripts/build_omnivec_onepager.py
Output: OmniVec-OnePager.pdf (repo root)
"""
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch
from reportlab.lib.colors import Color
from reportlab.pdfgen import canvas
from reportlab.pdfbase.pdfmetrics import stringWidth

NAVY   = Color(0.043, 0.145, 0.271)
NAVY2  = Color(0.075, 0.200, 0.361)
AZURE  = Color(0.169, 0.498, 1.0)
CYAN   = Color(0.133, 0.765, 0.902)
TEAL   = Color(0.106, 0.788, 0.647)
AMBER  = Color(0.961, 0.651, 0.137)
ROSE   = Color(0.878, 0.416, 0.416)
INK    = Color(0.078, 0.106, 0.180)
SLATE  = Color(0.326, 0.376, 0.463)
LIGHT  = Color(0.957, 0.969, 0.988)
LIGHT2 = Color(0.906, 0.933, 0.973)
MUTE   = Color(0.604, 0.651, 0.722)
WHITE  = Color(1, 1, 1)

PW, PH = letter
M = 0.5 * inch
c = canvas.Canvas("OmniVec-OnePager.pdf", pagesize=letter)


def wrap(text, font, size, maxw):
    words = text.split()
    lines, cur = [], ""
    for w in words:
        t = (cur + " " + w).strip()
        if stringWidth(t, font, size) <= maxw:
            cur = t
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def para(x, y, text, font="Helvetica", size=8.6, color=SLATE, maxw=100, leading=10.6):
    c.setFillColor(color)
    c.setFont(font, size)
    for ln in wrap(text, font, size, maxw):
        c.drawString(x, y, ln)
        y -= leading
    return y


def bullet(x, y, lead, rest, maxw, size=8.6, acc=AZURE, leading=10.6, marker="\u2013"):
    c.setFillColor(acc)
    c.setFont("Helvetica-Bold", size)
    c.drawString(x, y, marker)
    tx = x + 10
    full = lead + rest
    boldwords = lead.split()
    words = full.split()
    cur, curparts, lines = "", [], []
    for i, w in enumerate(words):
        t = (cur + " " + w).strip()
        if stringWidth(t, "Helvetica", size) <= maxw:
            cur = t
            curparts.append((w, i < len(boldwords)))
        else:
            lines.append(curparts)
            cur = w
            curparts = [(w, i < len(boldwords))]
    if curparts:
        lines.append(curparts)
    for parts in lines:
        cx = tx
        for w, isbold in parts:
            c.setFont("Helvetica-Bold" if isbold else "Helvetica", size)
            c.setFillColor(INK if isbold else SLATE)
            c.drawString(cx, y, w)
            cx += stringWidth(w + " ", "Helvetica-Bold" if isbold else "Helvetica", size)
        y -= leading
    return y - 1.5


def section(x, y, title, acc):
    c.setFillColor(acc)
    c.rect(x, y - 1, 3, 11, fill=1, stroke=0)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 10.5)
    c.drawString(x + 9, y, title.upper())
    return y - 16


# ---------------------------------------------------------------- header band
c.setFillColor(NAVY)
c.rect(0, PH - 1.55 * inch, PW, 1.55 * inch, fill=1, stroke=0)
c.setFillColor(AZURE)
c.rect(0, PH - 0.09 * inch, PW, 0.09 * inch, fill=1, stroke=0)
cols = [AZURE, CYAN, TEAL]
for i in range(5):
    for j in range(3):
        c.setFillColor(cols[(i + j) % 3])
        c.circle(PW - M - 6 - i * 15, PH - 0.5 * inch - j * 15, 4.2, fill=1, stroke=0)
c.setFillColor(CYAN)
c.setFont("Helvetica-Bold", 9)
c.drawString(M, PH - 0.5 * inch, "AZURE COSMOS DB")
c.setFillColor(WHITE)
c.setFont("Helvetica-Bold", 30)
c.drawString(M, PH - 0.92 * inch, "OmniVec")
c.setFillColor(Color(0.725, 0.804, 0.925))
c.setFont("Helvetica", 13)
c.drawString(M + 133, PH - 0.9 * inch, "Universal Vector Ingestion Platform")
c.setFillColor(TEAL)
c.rect(M, PH - 1.06 * inch, 44, 3, fill=1, stroke=0)
c.setFillColor(WHITE)
c.setFont("Helvetica", 11)
c.drawString(M, PH - 1.34 * inch, "Turn any data into AI-ready vector search \u2014 in minutes, on Azure. No pipeline to build.")

# ---------------------------------------------------------------- columns
top = PH - 1.85 * inch
colw = (PW - 2 * M - 0.3 * inch) / 2
lx = M
rx = M + colw + 0.3 * inch

# ---- LEFT COLUMN ----
y = top
y = section(lx, y, "What it offers", AZURE)
for lead, rest in [
    ("Minutes, not months ", "\u2014 one command stands up the platform; vectors searchable the same day."),
    ("Zero glue code ", "\u2014 configure sources, models & destinations; the rest is handled."),
    ("Works with your stack ", "\u2014 any source, any vector store, your model. No lock-in."),
    ("Production-grade ", "\u2014 identity, RBAC, autoscaling, metrics \u2014 secure from day one."),
]:
    y = bullet(lx, y, lead, rest, colw - 10, acc=AZURE)
y -= 6

y = section(lx, y, "What your customers can build", TEAL)
for lead, rest in [
    ("RAG & copilots ", "\u2014 ground LLMs and chatbots in enterprise data."),
    ("Semantic search ", "\u2014 search by meaning across docs, tickets, wikis."),
    ("Multi-modal doc search ", "\u2014 PDFs, scans & images via OCR/vision."),
    ("Always-fresh index ", "\u2014 change feed re-embeds as data changes."),
    ("Similarity & recs ", "\u2014 find related items, dedupe, cluster."),
    ("Vectorize what you have ", "\u2014 backfill databases & blobs into an index."),
]:
    y = bullet(lx, y, lead, rest, colw - 10, acc=TEAL)
y -= 6

y = section(lx, y, "What's in the box", CYAN)
for lead, rest in [
    ("Universal connectors ", "\u2014 Blob, Cosmos DB, PostgreSQL, MSSQL."),
    ("Bring-your-own model ", "\u2014 Azure OpenAI or self-hosted GPU."),
    ("Smart chunking ", "\u2014 truncate or overlap-chunk, per pipeline."),
    ("Real-time sync ", "\u2014 change feed & Event Grid keep vectors current."),
    ("Three interfaces ", "\u2014 Web UI, CLI and REST API."),
    ("Elastic & cost-aware ", "\u2014 autoscaling; GPU scale-to-zero."),
]:
    y = bullet(lx, y, lead, rest, colw - 10, acc=CYAN)

# ---- RIGHT COLUMN ----
y = top
# build vs omnivec panel
cmp_h = 118
half_w = (colw - 8) / 2
c.setFillColor(INK)
c.setFont("Helvetica-Bold", 10.5)
c.setFillColor(AMBER)
c.rect(rx, y - 1, 3, 11, fill=1, stroke=0)
c.setFillColor(INK)
c.drawString(rx + 9, y, "WHY OMNIVEC")
y -= 16
# left mini-panel: build yourself
c.setFillColor(LIGHT2)
c.roundRect(rx, y - cmp_h, half_w, cmp_h, 5, fill=1, stroke=0)
c.setFillColor(MUTE)
c.roundRect(rx, y - 16, half_w, 16, 5, fill=1, stroke=0)
c.rect(rx, y - 16, half_w, 8, fill=1, stroke=0)
c.setFillColor(WHITE)
c.setFont("Helvetica-Bold", 7.6)
c.drawCentredString(rx + half_w / 2, y - 11.5, "BUILD IT YOURSELF")
yy = y - 30
for line in ["Weeks of glue code", "Connector per source", "Manual re-indexing",
             "DIY GPU & scaling", "You own security", "Slow to stand up"]:
    c.setFillColor(ROSE); c.setFont("Helvetica-Bold", 8)
    c.drawString(rx + 7, yy, "\u2715")
    c.setFillColor(INK); c.setFont("Helvetica", 7.8)
    c.drawString(rx + 17, yy, line)
    yy -= 13.5
# right mini-panel: omnivec
rx2 = rx + half_w + 8
c.setFillColor(NAVY)
c.roundRect(rx2, y - cmp_h, half_w, cmp_h, 5, fill=1, stroke=0)
c.setFillColor(AZURE)
c.roundRect(rx2, y - 16, half_w, 16, 5, fill=1, stroke=0)
c.rect(rx2, y - 16, half_w, 8, fill=1, stroke=0)
c.setFillColor(WHITE)
c.setFont("Helvetica-Bold", 7.6)
c.drawCentredString(rx2 + half_w / 2, y - 11.5, "WITH OMNIVEC")
yy = y - 30
for line in ["Deploy in ~20 min", "Universal connectors", "Real-time auto-sync",
             "Managed GPUs", "Secure by default", "Live in ~20 min"]:
    c.setFillColor(TEAL); c.setFont("Helvetica-Bold", 8)
    c.drawString(rx2 + 7, yy, "\u2713")
    c.setFillColor(WHITE); c.setFont("Helvetica", 7.8)
    c.drawString(rx2 + 17, yy, line)
    yy -= 13.5
y = y - cmp_h - 12

y = section(rx, y, "Works with your data", AZURE)
# mini flow
box_h = 50
c.setFillColor(LIGHT)
c.roundRect(rx, y - box_h, colw, box_h, 6, fill=1, stroke=0)
stages = [("SOURCES", AZURE), ("EMBED", CYAN), ("VECTORS", TEAL)]
sw = colw / 3
for i, (t, ac) in enumerate(stages):
    cxx = rx + i * sw
    c.setFillColor(ac); c.rect(cxx + 6, y - 13, sw - 12, 3, fill=1, stroke=0)
    c.setFillColor(ac); c.setFont("Helvetica-Bold", 8)
    c.drawCentredString(cxx + sw / 2, y - 25, t)
    if i < 2:
        c.setFillColor(SLATE); c.setFont("Helvetica-Bold", 12)
        c.drawCentredString(cxx + sw, y - 25, "\u203a")
c.setFillColor(SLATE); c.setFont("Helvetica", 7.4)
c.drawCentredString(rx + colw / 2, y - 40, "Blob \u00b7 Cosmos \u00b7 Postgres \u00b7 SQL   \u2192   AOAI / GPU   \u2192   Cosmos \u00b7 pgvector \u00b7 SQL")
y = y - box_h - 12

for lead, rest in [
    ("One-command deploy ", "\u2014 azd up provisions AKS, Cosmos DB, Key Vault & more (~15\u201325 min)."),
    ("Multi-modal ", "\u2014 text, images and PDFs (vision + OCR), truncate or chunk."),
    ("Data stays yours ", "\u2014 self-host models so content never leaves your tenant."),
    ("Observable & elastic ", "\u2014 health, per-pipeline metrics; GPU scale-to-zero."),
]:
    y = bullet(rx, y, lead, rest, colw - 10, acc=AZURE)
y -= 6

# ---------------------------------------------------------------- quick-facts band
band_top = 2.62 * inch
bh = 0.98 * inch
gap = 0.16 * inch
n = 4
cw2 = (PW - 2 * M - (n - 1) * gap) / n
htext = "WHY IT WINS"
c.setFont("Helvetica-Bold", 9.5)
hw = stringWidth(htext, "Helvetica-Bold", 9.5)
hy = band_top + 16
c.setFillColor(LIGHT2)
c.rect(M, hy + 3, (PW - 2 * M - hw) / 2 - 10, 2, fill=1, stroke=0)
c.rect(PW - M - (PW - 2 * M - hw) / 2 + 10, hy + 3, (PW - 2 * M - hw) / 2 - 10, 2, fill=1, stroke=0)
c.setFillColor(SLATE)
c.drawCentredString(PW / 2, hy, htext)
stats = [
    ("~20", "min to deploy", "full platform, end to end", AZURE),
    ("$5\u201310", "per day", "default footprint, no GPU", TEAL),
    ("0", "lines of glue", "configuration, not code", CYAN),
    ("3", "ways to drive", "Web UI \u00b7 CLI \u00b7 REST API", AMBER),
]
for i, (num, lab, sub, ac) in enumerate(stats):
    x = M + i * (cw2 + gap)
    c.setFillColor(LIGHT)
    c.roundRect(x, band_top - bh, cw2, bh, 6, fill=1, stroke=0)
    c.setFillColor(ac)
    c.rect(x, band_top - 6, cw2, 4, fill=1, stroke=0)
    c.setFillColor(ac)
    c.setFont("Helvetica-Bold", 24)
    c.drawString(x + 13, band_top - 33, num)
    c.setFillColor(INK)
    c.setFont("Helvetica-Bold", 9.6)
    c.drawString(x + 13, band_top - 49, lab)
    c.setFillColor(SLATE)
    c.setFont("Helvetica", 7.5)
    for k, ln in enumerate(wrap(sub, "Helvetica", 7.5, cw2 - 26)):
        c.drawString(x + 13, band_top - 61 - k * 9, ln)

# ---------------------------------------------------------------- footer
c.setFillColor(NAVY)
c.rect(0, 0, PW, 0.42 * inch, fill=1, stroke=0)
c.setFillColor(TEAL)
c.rect(0, 0.42 * inch, PW, 2.5, fill=1, stroke=0)
c.setFillColor(WHITE)
c.setFont("Helvetica-Bold", 8.5)
c.drawString(M, 0.16 * inch, "Get started:")
c.setFillColor(CYAN)
c.drawString(M + 58, 0.16 * inch, "github.com/AzureCosmosDB/OmniVec  \u00b7  azd up")
c.setFillColor(Color(0.6, 0.7, 0.85))
c.setFont("Helvetica-Oblique", 8)
c.drawRightString(PW - M, 0.16 * inch, "Public preview \u00b7 Azure Cosmos DB")

c.showPage()
c.save()
print("Saved OmniVec-OnePager.pdf")
