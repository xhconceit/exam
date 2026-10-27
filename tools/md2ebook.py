#!/usr/bin/env python3
"""把课程 Markdown 讲义打包成 EPUB3 + 可打印 HTML（再交给 headless Chrome 出 PDF）。

用法:
    python3 tools/md2ebook.py <input.md> <out_dir> --title "..." --subtitle "..." \
        --footer "..." --cover-kicker "03344"

只依赖标准库 + Pillow（用于封面），不联网。
"""
from __future__ import annotations

import argparse
import datetime as _dt
import html
import os
import re
import shutil
import subprocess
import tempfile
import sys
import uuid
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# ---------------------------------------------------------------- markdown ---

CJK = r"\u3000-\u303f\u4e00-\u9fff\uff00-\uffef"


def slug(text: str) -> str:
    s = re.sub(r"[^\w" + CJK + r"]+", "-", text.strip().lower())
    return s.strip("-") or "sec"


def _link(m: re.Match) -> str:
    label, href = m.group(1), html.unescape(m.group(2))
    return f'<a href="{html.escape(href, quote=True)}">{label}</a>'


def inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", _link, out)
    out = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"(?<![*\w])\*(?!\s)(.+?)(?<!\s)\*(?![*\w])", r"<em>\1</em>", out)
    return out


def render_table(rows: list[str]) -> str:
    cells = []
    for r in rows:
        raw = r.strip().strip("|")
        cells.append([c.strip() for c in raw.split("|")])
    if len(cells) >= 2 and all(re.fullmatch(r":?-{2,}:?", c) for c in cells[1]):
        head, body = cells[0], cells[2:]
    else:
        head, body = None, cells
    html_rows = []
    if head:
        html_rows.append("<tr>" + "".join(f"<th>{inline(c)}</th>" for c in head) + "</tr>")
    for r in body:
        html_rows.append("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>")
    return '<table class="tbl">\n<tbody>\n' + "\n".join(html_rows) + "\n</tbody>\n</table>"


BLOCK_START = re.compile(r"^(#{1,6}\s|```|\||>|-\s|\*\s|\+\s|\d+\.\s)")


def md_to_html(md: str) -> str:
    lines = md.split("\n")
    out: list[str] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        s = line.strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            i += 1
            buf = []
            while i < n and not lines[i].strip().startswith("```"):
                buf.append(lines[i])
                i += 1
            i += 1
            out.append("<pre><code>" + html.escape("\n".join(buf)) + "</code></pre>")
            continue
        if re.fullmatch(r"-{3,}", s):
            out.append("<hr/>")
            i += 1
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            lvl = len(m.group(1))
            txt = m.group(2).strip()
            out.append(f'<h{lvl} id="{slug(txt)}">{inline(txt)}</h{lvl}>')
            i += 1
            continue
        if s.startswith("|"):
            rows = []
            while i < n and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip())
                i += 1
            out.append(render_table(rows))
            continue
        if s.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(re.sub(r"^\s*>\s?", "", lines[i]))
                i += 1
            out.append("<blockquote>\n" + md_to_html("\n".join(buf)) + "\n</blockquote>")
            continue
        if re.match(r"^[-*+]\s+", s):
            items = []
            while i < n and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            out.append("<ul>\n" + "\n".join(f"<li>{inline(x)}</li>" for x in items) + "\n</ul>")
            continue
        if re.match(r"^\d+\.\s+", s):
            items = []
            while i < n and re.match(r"^\s*\d+\.\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+\.\s+", "", lines[i]).strip())
                i += 1
            out.append("<ol>\n" + "\n".join(f"<li>{inline(x)}</li>" for x in items) + "\n</ol>")
            continue
        buf = []
        while i < n:
            t = lines[i].strip()
            if not t or BLOCK_START.match(t) or re.fullmatch(r"-{3,}", t):
                break
            buf.append(t)
            i += 1
        out.append("<p>" + inline("".join(buf)) + "</p>")
    return "\n".join(out)


# ---------------------------------------------------------------- structure ---


def split_sections(md: str):
    """-> (title, lead_html, [(heading, body_md), ...])"""
    lines = md.split("\n")
    title = None
    for idx, line in enumerate(lines):
        if line.startswith("# "):
            title = line[2:].strip()
            lines = lines[:idx] + lines[idx + 1 :]
            break
    chapters, cur_head, cur = [], None, []
    for line in lines:
        if re.match(r"^##\s+", line):
            if cur_head is not None:
                chapters.append((cur_head, "\n".join(cur)))
            elif cur:
                chapters.append((None, "\n".join(cur)))
            cur_head, cur = re.sub(r"^##\s+", "", line).strip(), []
        else:
            cur.append(line)
    if cur_head is not None:
        chapters.append((cur_head, "\n".join(cur)))
    elif cur:
        chapters.append((None, "\n".join(cur)))
    return title or "讲义", chapters


# ---------------------------------------------------------------- cover ------


def make_cover(path: Path, kicker: str, title: str, subtitle: str, footer: str, w=1240, h=1754):
    img = Image.new("RGB", (w, h), "#0b1a2a")
    d = ImageDraw.Draw(img)
    top, bot = (11, 26, 42), (18, 58, 84)
    for y in range(h):
        t = y / (h - 1)
        c = tuple(int(top[k] + (bot[k] - top[k]) * t) for k in range(3))
        d.line([(0, y), (w, y)], fill=c)

    accent = (86, 190, 220)
    d.rectangle([90, 0, 96, h], fill=accent)

    def font(size, index=1):
        for p in (
            "/System/Library/Fonts/Hiragino Sans GB.ttc",
            "/System/Library/Fonts/STHeiti Medium.ttc",
            "/Library/Fonts/Arial Unicode.ttf",
        ):
            if os.path.exists(p):
                try:
                    return ImageFont.truetype(p, size, index=index)
                except OSError:
                    try:
                        return ImageFont.truetype(p, size)
                    except OSError:
                        continue
        return ImageFont.load_default()

    if kicker and title.startswith(kicker):
        title = title[len(kicker):].strip()

    f_kick = font(160, 0)
    f_title = font(84, 1)
    f_sub = font(38, 0)
    f_foot = font(28, 0)

    d.text((150, 200), kicker, font=f_kick, fill=accent)
    d.rectangle([150, 400, 150 + 120, 408], fill=accent)

    # 主标题按宽度折行
    max_w = w - 300
    lines, cur = [], ""
    for ch in title:
        if d.textlength(cur + ch, font=f_title) > max_w and cur:
            lines.append(cur)
            cur = ch
        else:
            cur += ch
    if cur:
        lines.append(cur)

    y = 500
    for ln in lines:
        d.text((150, y), ln, font=f_title, fill="#ffffff")
        y += 120
    y += 40
    for ln in subtitle.split("\n"):
        d.text((152, y), ln, font=f_sub, fill=(186, 214, 232))
        y += 62

    d.text((150, h - 150), footer, font=f_foot, fill=(140, 172, 194))
    d.rectangle([0, h - 20, w, h], fill=accent)
    img.save(path, "PNG", optimize=True)


# ---------------------------------------------------------------- css -------

EPUB_CSS = """
html, body { margin: 0; padding: 0; }
body { font-family: "Songti SC", "Source Han Serif SC", serif; line-height: 1.75;
       font-size: 1em; color: #16202b; text-align: justify; }
h1, h2, h3, h4 { font-family: "PingFang SC", "Hiragino Sans GB", sans-serif;
       color: #0d3552; line-height: 1.35; page-break-after: avoid; }
h1 { font-size: 1.7em; margin: 0 0 0.6em; }
h2 { font-size: 1.4em; margin: 1.6em 0 0.6em; border-bottom: 2px solid #cfe3ee;
     padding-bottom: 0.25em; }
h3 { font-size: 1.15em; margin: 1.3em 0 0.45em; color: #12496d; }
p { margin: 0.55em 0; }
ul, ol { margin: 0.5em 0 0.7em 1.2em; padding: 0; }
li { margin: 0.3em 0; }
strong { color: #8c2f0d; }
code { font-family: "SF Mono", Menlo, monospace; font-size: 0.88em;
       background: #eef3f7; padding: 0.05em 0.3em; border-radius: 3px; }
pre { background: #f4f7fa; border-left: 4px solid #9dc4d8; padding: 0.7em 0.9em;
      overflow-x: auto; font-size: 0.85em; }
pre code { background: none; padding: 0; }
blockquote { margin: 0.8em 0; padding: 0.6em 0.9em; background: #f3f8fb;
             border-left: 4px solid #4b93b8; color: #2b3d4d; font-size: 0.94em; }
blockquote p { margin: 0.35em 0; }
hr { border: none; border-top: 1px solid #d5e2ea; margin: 1.6em 0; }
a { color: #0f6ea8; text-decoration: none; }
table.tbl { width: 100%; border-collapse: collapse; margin: 0.8em 0; font-size: 0.86em;
            font-family: "PingFang SC", "Hiragino Sans GB", sans-serif; }
table.tbl th { background: #e8f1f6; color: #0d3552; font-weight: 600; }
table.tbl th, table.tbl td { border: 1px solid #c8dae4; padding: 0.35em 0.5em;
                             vertical-align: top; text-align: left; }
table.tbl tr:nth-child(even) td { background: #fafcfd; }
.cover { margin: 0; padding: 0; text-align: center; }
.cover img { max-width: 100%; }
.titlepage { padding-top: 15%; text-align: center; }
.titlepage h1 { border: none; font-size: 2em; }
.titlepage .sub { color: #55707f; font-size: 0.95em; }
"""

PRINT_CSS = """
@page { size: A4; margin: 18mm 16mm 20mm; }
@page :first { margin: 0; }
body { font-size: 10.5pt; }
h2 { page-break-before: always; }
h1, h2, h3 { page-break-after: avoid; }
table.tbl { page-break-inside: auto; }
tr { page-break-inside: avoid; }
.toc-page { page-break-after: always; }
.toc-page h2 { page-break-before: avoid; }
.toc-page ol { list-style: none; margin-left: 0.4em; }
.toc-page li { margin: 0.45em 0; font-size: 11pt; }
.toc-page li .n { color: #4b93b8; font-weight: 600; margin-right: 0.5em; }
.pdf-cover { page-break-after: always; margin: 0; padding: 0; }
.pdf-cover img { display: block; width: 210mm; height: 297mm; }
"""


def xhtml_page(body: str, title: str, css_href: str = "style.css") -> str:
    return f"""<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"
      lang="zh-CN" xml:lang="zh-CN">
<head>
<meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<link rel="stylesheet" type="text/css" href="{css_href}"/>
</head>
<body>
{body}
</body>
</html>
"""


# ---------------------------------------------------------------- epub ------


def build_epub(out: Path, title: str, subtitle: str, author: str, chapters, lead_html, cover: Path):
    uid = "urn:uuid:" + str(uuid.uuid5(uuid.NAMESPACE_URL, title + subtitle))
    modified = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    files: dict[str, bytes] = {}
    files["style.css"] = EPUB_CSS.encode()
    files["cover.png"] = cover.read_bytes()

    cover_page = (
        '<div class="cover"><img src="cover.png" alt="封面"/></div>'
    )
    files["cover.xhtml"] = xhtml_page(cover_page, "封面").encode()

    title_page = (
        f'<div class="titlepage"><h1>{html.escape(title)}</h1>'
        f'<p class="sub">{html.escape(subtitle)}</p>'
        f'<p class="sub">{html.escape(author)}</p></div>'
    )
    if lead_html:
        title_page += lead_html
    files["title.xhtml"] = xhtml_page(title_page, title).encode()

    items = [("cover", "cover.xhtml", "封面"), ("title", "title.xhtml", title)]
    nav_lis = []
    for idx, (head, body) in enumerate(chapters, 1):
        cid = f"ch{idx:02d}"
        fname = f"{cid}.xhtml"
        body_html = md_to_html(body)
        if head:
            body_html = f"<h1>{inline(head)}</h1>\n" + body_html
        files[fname] = xhtml_page(body_html, head or title).encode()
        items.append((cid, fname, head or f"第{idx}节"))
        nav_lis.append(
            f'<li><a href="{fname}">{html.escape(head or f"第{idx}节")}</a></li>'
        )

    nav = (
        '<nav epub:type="toc" id="toc"><h1>目录</h1><ol>'
        + "".join(f'<li><a href="{i[1]}">{html.escape(i[2])}</a></li>' for i in items)
        + "</ol></nav>"
    )
    files["nav.xhtml"] = xhtml_page(nav, "目录").encode()

    manifest = "\n".join(
        f'<item id="{i[0]}" href="{i[1]}" media-type="application/xhtml+xml"/>' for i in items
    )
    manifest += '\n<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>'
    manifest += '\n<item id="css" href="style.css" media-type="text/css"/>'
    manifest += '\n<item id="cover-img" href="cover.png" media-type="image/png" properties="cover-image"/>'
    spine = "\n".join(f'<itemref idref="{i[0]}"/>' for i in items)

    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="pid"
         xml:lang="zh-CN">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="pid">{uid}</dc:identifier>
    <dc:title>{html.escape(title)}</dc:title>
    <dc:creator>{html.escape(author)}</dc:creator>
    <dc:language>zh-CN</dc:language>
    <dc:description>{html.escape(subtitle)}</dc:description>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
{manifest}
  </manifest>
  <spine>
{spine}
  </spine>
</package>
"""
    files["content.opf"] = opf.encode()

    container = """<?xml version="1.0" encoding="utf-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""
    with zipfile.ZipFile(out, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip", zipfile.ZIP_STORED)
        z.writestr("META-INF/container.xml", container, zipfile.ZIP_DEFLATED)
        for name, data in files.items():
            z.writestr(f"OEBPS/{name}", data, zipfile.ZIP_DEFLATED)


# ---------------------------------------------------------------- print -----


def build_print_html(out: Path, title: str, subtitle: str, author: str, chapters, lead_html, cover: Path):
    cover_rel = cover.name
    parts = [
        f'<div class="pdf-cover"><img src="{cover_rel}" alt="封面"/></div>',
    ]
    toc = ['<div class="toc-page"><h1>目录</h1><ol>']
    for i, (head, _) in enumerate(chapters, 1):
        toc.append(f'<li>{html.escape(head or "")}</li>')
    toc.append("</ol></div>")
    parts.append("".join(toc))
    if lead_html:
        parts.append(f'<section class="lead-page">{lead_html}</section>')
    for i, (head, body) in enumerate(chapters, 1):
        body_html = md_to_html(body)
        head_html = f"<h2>{inline(head)}</h2>" if head else ""
        parts.append(f"<section>{head_html}\n{body_html}</section>")
    doc = f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8"/>
<title>{html.escape(title)}</title>
<style>{EPUB_CSS}
{PRINT_CSS}
</style></head>
<body>
{''.join(parts)}
</body></html>
"""
    out.write_text(doc, encoding="utf-8")


def chrome_pdf(html_path: Path, pdf_path: Path) -> bool:
    html_path, pdf_path = html_path.resolve(), pdf_path.resolve()
    for app in (
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ):
        if not os.path.exists(app):
            continue
        profile = tempfile.mkdtemp(prefix="dsh-chrome-")
        base = [
            app, "--headless", "--disable-gpu", "--no-sandbox",
            "--disable-dev-shm-usage", f"--user-data-dir={profile}",
            "--virtual-time-budget=15000", "--run-all-compositor-stages-before-draw",
        ]
        for flag in ("--no-pdf-header-footer", "--print-to-pdf-no-header"):
            cmd = base + [flag, f"--print-to-pdf={pdf_path}", html_path.as_uri()]
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
            if pdf_path.exists() and pdf_path.stat().st_size > 2048:
                return True
            sys.stderr.write(r.stderr[-800:] + "\n")
    return False


# ---------------------------------------------------------------- main ------


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("md")
    ap.add_argument("outdir")
    ap.add_argument("--title", required=True)
    ap.add_argument("--subtitle", default="")
    ap.add_argument("--author", default="")
    ap.add_argument("--footer", default="")
    ap.add_argument("--kicker", default="")
    ap.add_argument("--stem", default=None)
    ap.add_argument("--no-pdf", action="store_true")
    args = ap.parse_args()

    md = Path(args.md).read_text(encoding="utf-8")
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = args.stem or Path(args.md).stem

    title, chapters = split_sections(md)
    title = args.title or title
    lead_md = chapters[0][1] if chapters and chapters[0][0] is None else ""
    if lead_md:
        chapters = chapters[1:]
    lead_html = md_to_html(lead_md) if lead_md.strip() else ""
    chapters = [c for c in chapters if (c[0] or "").strip() != "目录"]

    cover = outdir / "cover.png"
    make_cover(cover, args.kicker, args.title, args.subtitle, args.footer)

    epub_path = outdir / f"{stem}.epub"
    build_epub(epub_path, title, args.subtitle, args.author, chapters, lead_html, cover)
    print(f"epub  {epub_path}  {epub_path.stat().st_size/1024:.0f} KB  ({len(chapters)} 节)")

    if not args.no_pdf:
        html_path = outdir / f"{stem}.print.html"
        build_print_html(html_path, title, args.subtitle, args.author, chapters, lead_html, cover)
        pdf_path = outdir / f"{stem}.pdf"
        if chrome_pdf(html_path, pdf_path):
            print(f"pdf   {pdf_path}  {pdf_path.stat().st_size/1024:.0f} KB")
            html_path.unlink(missing_ok=True)
        else:
            print(f"pdf   失败，保留可打印 HTML: {html_path}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
