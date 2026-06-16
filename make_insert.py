#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Генератор вкладыша для аудиокассеты из чистого шаблона.
Не меняет размеры таблиц/ячеек/бокса картинки. Подгоняет только шрифт треков.
"""
import os, re, shutil, subprocess, sys, tempfile, zipfile
from PIL import Image, ImageOps

from paths import resource_path

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = resource_path("Шаблон_вкладыш_ЧИСТЫЙ.docx")

def esc(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))

# ---------------------------------------------------------------------------
#  Длительности и распределение треков по сторонам
# ---------------------------------------------------------------------------
def parse_dur(s):
    """'3:45' / '63' / '1:02:03' -> секунды (int). Пустое -> 0."""
    s = (s or "").strip()
    if not s:
        return 0
    parts = [p for p in re.split(r"[:.]", s) if p != ""]
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for n in nums:
        sec = sec * 60 + n
    return sec

def fmt_dur(sec):
    sec = int(round(sec))
    return "%d:%02d" % (sec // 60, sec % 60)

def fill_forward(tracks, limit_sec):
    """С первого трека, пока сумма <= лимита (невлезающий пропускается, стоп)."""
    res, acc = [], 0
    for name, sec in tracks:
        if acc + sec > limit_sec:
            break
        res.append((name, sec)); acc += sec
    return res, acc

def fill_wrap(tracks, start, limit_sec):
    """С позиции start, затем оборот на трек 1, пока есть место (не больше круга)."""
    res, acc, n = [], 0, len(tracks)
    if not n:
        return res, 0
    i, steps = start % n, 0
    while steps < n:
        name, sec = tracks[i]
        if acc + sec > limit_sec:
            break
        res.append((name, sec)); acc += sec
        i = (i + 1) % n; steps += 1
    return res, acc

def distribute(tracks, limit_min=47):
    """Один альбом: Side A — с первого трека; Side B — продолжение с места
    остановки A, затем оборот на трек 1. Нумерация B всегда с 01."""
    limit = int(limit_min * 60)
    side_a, sec_a = fill_forward(tracks, limit)
    side_b, sec_b = fill_wrap(tracks, len(side_a), limit)
    return {"side_a": side_a, "side_b": side_b, "sec_a": sec_a, "sec_b": sec_b,
            "total": sum(s for _, s in tracks)}

def distribute_mix(tracks_a, tracks_b, limit_min=47):
    """Микстейп: альбом 1 записывается полностью — что влезло на Side A, остаток
    переходит в начало Side B; затем Side B продолжает альбом 2. Нумерация B с 01."""
    limit = int(limit_min * 60)
    side_a, sec_a = fill_forward(tracks_a, limit)
    remainder = tracks_a[len(side_a):]                 # хвост альбома 1
    side_b, sec_b = fill_forward(remainder + tracks_b, limit)
    carry = len(remainder)
    album1_full = len(side_b) >= carry                 # весь хвост влез на Side B?
    return {"side_a": side_a, "side_b": side_b, "sec_a": sec_a, "sec_b": sec_b,
            "carry": carry, "album1_full": album1_full,
            "total_a": sum(s for _, s in tracks_a), "total_b": sum(s for _, s in tracks_b)}

def auto_sz(side_a, side_b, sizes=(22, 20, 18, 16, 14)):
    """Эвристика выбора sz (LibreOffice-рендер недоступен). Калибровка по эталону
    Bon Jovi: 22 строки треков + 2 заголовка(12pt) помещаются при 8pt.
    Ёмкость ~ 192 pt высоты под треки. Возвращает крупнейший sz из списка, влезающий."""
    lines = len(side_a) + len(side_b)
    if lines == 0:
        return sizes[0]
    max_pt = 192.0 / lines
    for sz in sizes:               # от крупного к мелкому
        if sz / 2.0 <= max_pt:
            return sz
    return sizes[-1]

def header_para(side_label, title):
    """Side A:/Side B: header — sz=24 (12pt), Monotype Corsiva, bold, 111111."""
    rpr_title = ('<w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva" '
                 'w:cs="Tahoma"/><w:b/><w:bCs/><w:color w:val="111111"/><w:sz w:val="24"/>'
                 '<w:szCs w:val="24"/><w:lang w:val="en-US"/></w:rPr>')
    ppr = ('<w:pPr>' + rpr_title + '</w:pPr>')
    return ('<w:p>' + ppr
            + '<w:r><w:rPr><w:lang w:val="en-US"/></w:rPr><w:t>' + esc(side_label) + '</w:t></w:r>'
            + '<w:r><w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva"/>'
              '<w:lang w:val="en-US"/></w:rPr><w:t xml:space="preserve"> </w:t></w:r>'
            + '<w:r>' + rpr_title + '<w:t xml:space="preserve">' + esc(title) + '</w:t></w:r>'
            + '</w:p>')

def track_para(text, sz):
    """Трек — Monotype Corsiva, bold, 444543, заданный sz (half-points)."""
    rpr = ('<w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva" '
           'w:cs="Tahoma"/><w:b/><w:bCs/><w:color w:val="444543"/><w:sz w:val="%d"/>'
           '<w:szCs w:val="%d"/><w:shd w:val="clear" w:color="auto" w:fill="F7F7F9"/>'
           '<w:lang w:val="en-US"/></w:rPr>' % (sz, sz))
    return ('<w:p><w:pPr>' + rpr + '</w:pPr><w:r>' + rpr
            + '<w:t xml:space="preserve">' + esc(text) + '</w:t></w:r></w:p>')

def empty_para():
    return '<w:p/>'

def title_para(text, sz=28):
    """Заголовок (Table 0) — Monotype Corsiva, bold, 111111, заданный sz."""
    rpr = ('<w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva" '
           'w:cs="Tahoma"/><w:b/><w:bCs/><w:color w:val="111111"/><w:sz w:val="%d"/>'
           '<w:szCs w:val="%d"/><w:lang w:val="en-US"/></w:rPr>' % (sz, sz))
    return ('<w:p><w:pPr>' + rpr + '</w:pPr><w:r>' + rpr
            + '<w:t xml:space="preserve">' + esc(text) + '</w:t></w:r></w:p>')

def build_title_inner(lines):
    """Одна или несколько строк заголовка. Две строки делаем чуть мельче (sz=24),
    чтобы сохранить высоту ячейки."""
    sz = 28 if len(lines) <= 1 else 24
    return "".join(title_para(l, sz) for l in lines)

def build_cell_inner(header_a, header_b, side_a, side_b, sz):
    parts = [header_para("Side A:", header_a)]
    parts += [track_para("%02d - %s" % (i, t), sz) for i, t in enumerate(side_a, 1)]
    parts.append(empty_para())
    parts.append(header_para("Side B:", header_b))
    parts += [track_para("%02d - %s" % (i, t), sz) for i, t in enumerate(side_b, 1)]
    parts.append(empty_para())
    return "".join(parts)

def prepare_cover(src, dst, box=600):
    """Кадрирует обложку в квадрат 1:1 (по центру) под фиксированный бокс."""
    im = Image.open(src)
    im = ImageOps.exif_transpose(im).convert("RGB")
    sq = ImageOps.fit(im, (box, box), Image.LANCZOS, centering=(0.5, 0.5))
    sq.save(dst, "JPEG", quality=92)

def _replace_cell_inner(xml, marker, inner):
    """Заменяет абзацы ячейки, содержащей текст marker, сохраняя её tcPr (размер)."""
    i = xml.find(marker)
    if i < 0:
        return xml
    tc_start = max(xml.rfind("<w:tc>", 0, i), xml.rfind("<w:tc ", 0, i))
    tc_end = xml.find("</w:tc>", i) + len("</w:tc>")
    cell = xml[tc_start:tc_end]
    tcpr_end = cell.find("</w:tcPr>") + len("</w:tcPr>")
    new_cell = cell[:tcpr_end] + inner + "</w:tc>"
    return xml[:tc_start] + new_cell + xml[tc_end:]

def make_docx(out_path, title_lines, header_a, header_b, side_a, side_b, sz, cover_src):
    """title_lines — список строк заголовка (1 — один альбом, 2 — микстейп).
    header_a/header_b — текст после 'Side A:'/'Side B:'. side_a/side_b — имена треков."""
    if isinstance(title_lines, str):
        title_lines = [title_lines]
    work = tempfile.mkdtemp(prefix="cassette_build_")
    with zipfile.ZipFile(TEMPLATE) as z:
        z.extractall(work)
    docxml_path = os.path.join(work, "word", "document.xml")
    xml = open(docxml_path, encoding="utf-8").read()

    # 1) Заголовок (Table 0) — одна или две строки
    xml = _replace_cell_inner(xml, "ИСПОЛНИТЕЛЬ - НАЗВАНИЕ АЛЬБОМА (ГОД)",
                              build_title_inner(title_lines))
    # 2) Левая ячейка Table 2 — треклист
    xml = _replace_cell_inner(xml, "Side A:",
                              build_cell_inner(header_a, header_b, side_a, side_b, sz))

    open(docxml_path, "w", encoding="utf-8").write(xml)

    # 3) Обложка
    prepare_cover(cover_src, os.path.join(work, "word", "media", "image1.jpg"))

    # 4) Пересобрать .docx
    if os.path.exists(out_path):
        os.remove(out_path)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        for root, _, files in os.walk(work):
            for f in files:
                fp = os.path.join(root, f)
                arc = os.path.relpath(fp, work)
                z.write(fp, arc)
    shutil.rmtree(work)

def pdf_page_count(docx_path):
    outdir = os.path.join(HERE, "_pdf")
    os.makedirs(outdir, exist_ok=True)
    subprocess.run(["libreoffice", "--headless", "--convert-to", "pdf",
                    "--outdir", outdir, docx_path],
                   check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    pdf = os.path.join(outdir, os.path.splitext(os.path.basename(docx_path))[0] + ".pdf")
    data = open(pdf, "rb").read()
    return len(re.findall(rb"/Type\s*/Page[^s]", data)), pdf

def autosize(out_path, title, side_a, side_b, cover_src, sizes=(22, 20, 18, 16, 14)):
    chosen = None
    for sz in sizes:
        make_docx(out_path, title, side_a, side_b, sz, cover_src)
        pages, _ = pdf_page_count(out_path)
        print("  sz=%2d (%.0fpt) -> %d стр." % (sz, sz/2, pages))
        if pages <= 1:
            chosen = sz
            break
    if chosen is None:
        chosen = sizes[-1]
        make_docx(out_path, title, side_a, side_b, chosen, cover_src)
        print("  ВНИМАНИЕ: не влезает даже при sz=%d — взят минимальный." % chosen)
    return chosen
