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

def auto_sz(side_a, side_b, sizes=(22, 20, 18, 16, 14), height_mm=100):
    """Эвристика выбора sz (LibreOffice-рендер недоступен). Калибровка по эталону
    Bon Jovi: 22 строки треков + 2 заголовка(12pt) помещаются при 8pt.
    Ёмкость ~ 192 pt высоты под треки при высоте вкладыша 100 мм (масштабируется).
    Возвращает крупнейший sz из списка, влезающий."""
    lines = len(side_a) + len(side_b)
    if lines == 0:
        return sizes[0]
    max_pt = 192.0 * height_mm / 100 / lines
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

def _track_rpr(sz, bold):
    """rPr трека — Monotype Corsiva, 444543, заданный sz; жирность по флагу."""
    b = "<w:b/><w:bCs/>" if bold else ""
    return ('<w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva" '
            'w:cs="Tahoma"/>%s<w:color w:val="444543"/><w:sz w:val="%d"/>'
            '<w:szCs w:val="%d"/><w:shd w:val="clear" w:color="auto" w:fill="F7F7F9"/>'
            '<w:lang w:val="en-US"/></w:rPr>' % (b, sz, sz))

def _run(text, rpr):
    return '<w:r>' + rpr + '<w:t xml:space="preserve">' + esc(text) + '</w:t></w:r>'

def track_para(num, name, sz):
    """Строка трека 'NN - [Артист - ]Песня'. Если у трека есть исполнитель
    (часть до первого ' - '), его выводим жирным, песню — обычным шрифтом;
    без исполнителя вся песня обычным. Номер всегда обычным."""
    reg = _track_rpr(sz, False)
    artist, sep, song = name.partition(" - ")
    runs = [_run("%s - " % num, reg)]
    if sep:                                            # 'Артист - Песня'
        runs.append(_run(artist, _track_rpr(sz, True)))
        runs.append(_run(" - " + song, reg))
    else:
        runs.append(_run(name, reg))
    return '<w:p><w:pPr>' + reg + '</w:pPr>' + "".join(runs) + '</w:p>'

def empty_para():
    return '<w:p/>'

def title_para(text, sz=28):
    """Заголовок (Table 0) — Monotype Corsiva, bold, 111111, заданный sz."""
    rpr = ('<w:rPr><w:rFonts w:ascii="Monotype Corsiva" w:hAnsi="Monotype Corsiva" '
           'w:cs="Tahoma"/><w:b/><w:bCs/><w:color w:val="111111"/><w:sz w:val="%d"/>'
           '<w:szCs w:val="%d"/><w:lang w:val="en-US"/></w:rPr>' % (sz, sz))
    return ('<w:p><w:pPr>' + rpr + '</w:pPr><w:r>' + rpr
            + '<w:t xml:space="preserve">' + esc(text) + '</w:t></w:r></w:p>')

def build_title_inner(lines, sz=None):
    """Одна или несколько строк заголовка. По умолчанию две строки делаем чуть
    мельче (sz=24), чтобы сохранить высоту ячейки; sz задаёт размер явно."""
    sz = sz or (28 if len(lines) <= 1 else 24)
    return "".join(title_para(l, sz) for l in lines)

def build_cell_inner(header_a, header_b, side_a, side_b, sz):
    parts = [header_para("Side A:", header_a)]
    parts += [track_para("%02d" % i, t, sz) for i, t in enumerate(side_a, 1)]
    parts.append(empty_para())
    parts.append(header_para("Side B:", header_b))
    parts += [track_para("%02d" % i, t, sz) for i, t in enumerate(side_b, 1)]
    parts.append(empty_para())
    return "".join(parts)

def prepare_cover(src, dst, box=600):
    """Кадрирует обложку в квадрат 1:1 (по центру) под фиксированный бокс."""
    im = Image.open(src)
    im = ImageOps.exif_transpose(im).convert("RGB")
    sq = ImageOps.fit(im, (box, box), Image.LANCZOS, centering=(0.5, 0.5))
    sq.save(dst, "JPEG", quality=92)

def _replace_cell_inner(xml, marker, inner, tcpr_extra=""):
    """Заменяет абзацы ячейки, содержащей текст marker, сохраняя её tcPr (размер).
    tcpr_extra дописывается в конец tcPr (напр. вертикальное выравнивание)."""
    i = xml.find(marker)
    if i < 0:
        return xml
    tc_start = max(xml.rfind("<w:tc>", 0, i), xml.rfind("<w:tc ", 0, i))
    tc_end = xml.find("</w:tc>", i) + len("</w:tc>")
    cell = xml[tc_start:tc_end]
    tcpr_end = cell.find("</w:tcPr>") + len("</w:tcPr>")
    new_cell = (cell[:tcpr_end - len("</w:tcPr>")] + tcpr_extra + "</w:tcPr>"
                + inner + "</w:tc>")
    return xml[:tc_start] + new_cell + xml[tc_end:]

def apply_geom(xml, g):
    """Размеры развёртки под конкретную кассету (мм) -> значения в шаблоне.
    Шаблон: корешок = полоса заголовка (длина height x ширина spine), лицевая
    панель и оборот = front x height, клапан = flap. Квадрат обложки = панель
    минус 4,7 мм, по центру по вертикали (как в исходном шаблоне)."""
    tw = lambda mm: str(int(round(mm * 1440 / 25.4)))
    emu = lambda mm: str(int(round(mm * 36000)))
    h, w = g["height_mm"], g["front_mm"]
    side = min(w, h) - 4.7
    for old, new in (('w:w="5670"', 'w:w="%s"' % tw(h)),
                     ('w:val="5670"', 'w:val="%s"' % tw(h)),
                     ('w:w="3686"', 'w:w="%s"' % tw(w)),
                     ('w:w="1134"', 'w:w="%s"' % tw(g["flap_mm"])),
                     ('<w:trHeight w:val="624"/>', '<w:trHeight w:val="%s"/>' % tw(g["spine_mm"])),
                     ('"2171700"', '"%s"' % emu(side)),
                     ('>655320<', '>%s<' % emu((h - side) / 2 - 1.65))):
        xml = xml.replace(old, new)
    return xml

FONT = "Monotype Corsiva"   # шрифт шаблона; свой подставляется заменой в make_docx
DEFAULT_GEOM = {"height_mm": 100, "front_mm": 65, "spine_mm": 11, "flap_mm": 20}

VCENTER = '<w:vAlign w:val="center"/>'

def front_text_inner(title, g, font=None, sz=None):
    """Лицевая сторона без картинки: 'Исполнитель' / 'Альбом (Год)' в две строки
    по центру, одним размером — sz (полупункты) или максимальным, при котором
    длинная строка влезает в ширину панели."""
    artist, sep, album = title.partition(" - ")
    lines = [artist.strip(), album.strip()] if sep else [title.strip()]
    width_pt = (g["front_mm"] - 4) * 72 / 25.4          # минус поля ячейки
    height_pt = (g["height_mm"] - 10) * 72 / 25.4
    # ponytail: метрик шрифта нет — средняя ширина жирной буквы ~0.55 em;
    # широкий шрифт (Arial Black и т.п.) может перенестись — тогда задать мельче.
    pt = min(width_pt / (max(len(l) for l in lines) * 0.55),
             height_pt / (len(lines) * 1.3), 48)
    inner = "".join(title_para(l, sz or max(16, int(pt * 2))) for l in lines)
    inner = inner.replace("<w:pPr>", '<w:pPr><w:jc w:val="center"/>')
    return inner.replace('"%s"' % FONT, '"%s"' % esc(font)) if font else inner

def whole_layout(xml, g, title_inner, tracks_inner, front_inner=None):
    """Вкладыш одной полосой: [торец | обложка | треклист], общая высота.
    Берёт рамки и картинку из шаблона (после apply_geom), остальные таблицы
    шаблона заменяет одной. Текст торца повёрнут (снизу вверх)."""
    tw = lambda mm: int(round(mm * 1440 / 25.4))
    b0 = xml.find("<w:body>") + len("<w:body>")
    b1 = xml.rfind("<w:sectPr")
    tables = re.findall(r"<w:tbl>.*?</w:tbl>", xml[b0:b1], re.S)
    tblpr = re.search(r"<w:tblPr>.*?</w:tblPr>", tables[2], re.S).group(0)
    cover = re.search(r"</w:tcPr>(.*)</w:tc>", tables[1], re.S).group(1)
    w, s = tw(g["front_mm"]), tw(g["spine_mm"])
    front = cover if front_inner is None else front_inner
    cell = lambda width, inner, extra="": (
        '<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s</w:tcPr>%s</w:tc>' % (width, extra, inner))
    spine = title_inner.replace("<w:pPr>", '<w:pPr><w:jc w:val="center"/>')
    tbl = ('<w:tbl>' + tblpr
           + '<w:tblGrid><w:gridCol w:w="%d"/><w:gridCol w:w="%d"/><w:gridCol w:w="%d"/></w:tblGrid>' % (s, w, w)
           + '<w:tr><w:trPr><w:trHeight w:val="%d"/></w:trPr>' % tw(g["height_mm"])
           + cell(s, spine, '<w:textDirection w:val="btLr"/><w:vAlign w:val="center"/>')
           + cell(w, front, "" if front_inner is None else VCENTER)
           + cell(w, tracks_inner)
           + '</w:tr></w:tbl><w:p/>')
    return xml[:b0] + tbl + xml[b1:]

def make_docx(out_path, title_lines, header_a, header_b, side_a, side_b, sz, cover_src,
              geom=None, whole=False, spine_font=None, spine_sz=None, track_font=None,
              front_title=None, front_font=None, front_sz=None):
    """title_lines — список строк заголовка (1 — один альбом, 2 — микстейп).
    header_a/header_b — текст после 'Side A:'/'Side B:'. side_a/side_b — имена треков.
    geom — размеры кассеты в мм (см. apply_geom); None — как в шаблоне.
    whole — вкладыш одной полосой (торец + обложка + треклист).
    spine_font/spine_sz — шрифт и размер (полупункты) торца-заголовка;
    track_font — шрифт треклиста (заголовки сторон и треки). None — как в шаблоне.
    cover_src=None — вместо картинки на лицевой стороне название front_title
    (по умолчанию первая строка заголовка) шрифтом front_font (иначе шрифт торца)
    и размером front_sz (полупункты; иначе максимальный влезающий)."""
    if isinstance(title_lines, str):
        title_lines = [title_lines]
    work = tempfile.mkdtemp(prefix="cassette_build_")
    with zipfile.ZipFile(TEMPLATE) as z:
        z.extractall(work)
    docxml_path = os.path.join(work, "word", "document.xml")
    xml = open(docxml_path, encoding="utf-8").read()

    title_inner = build_title_inner(title_lines, spine_sz)
    tracks_inner = build_cell_inner(header_a, header_b, side_a, side_b, sz)
    if spine_font:
        title_inner = title_inner.replace('"%s"' % FONT, '"%s"' % esc(spine_font))
    if track_font:
        tracks_inner = tracks_inner.replace('"%s"' % FONT, '"%s"' % esc(track_font))
    front_inner = None
    if not cover_src:
        front_inner = front_text_inner(front_title or title_lines[0],
                                       geom or DEFAULT_GEOM, front_font or spine_font, front_sz)
    if whole:
        geom = geom or DEFAULT_GEOM
        xml = whole_layout(apply_geom(xml, geom), geom, title_inner, tracks_inner, front_inner)
    else:
        if front_inner is not None:          # картинку в ячейке обложки -> текст
            xml = _replace_cell_inner(xml, "<w:drawing>", front_inner, VCENTER)
        # 1) Заголовок (Table 0) — одна или две строки
        xml = _replace_cell_inner(xml, "ИСПОЛНИТЕЛЬ - НАЗВАНИЕ АЛЬБОМА (ГОД)", title_inner)
        # 2) Левая ячейка Table 2 — треклист
        xml = _replace_cell_inner(xml, "Side A:", tracks_inner)
        if geom:
            xml = apply_geom(xml, geom)

    open(docxml_path, "w", encoding="utf-8").write(xml)

    # 3) Обложка
    if cover_src:
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
