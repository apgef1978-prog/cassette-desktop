#!/usr/bin/env python3
"""Минимальная самопроверка ядра:  python test_core.py"""
import json, os, zipfile

import make_insert as M


def tpl_xml():
    with zipfile.ZipFile(M.TEMPLATE) as z:
        return z.read("word/document.xml").decode("utf-8")


def test_geom():
    xml = tpl_xml()
    presets = json.load(open(os.path.join(os.path.dirname(__file__), "cassettes.json"), encoding="utf-8"))
    # геометрия шаблона по умолчанию даёт те же значения (±1 твип/±0.03 мм)
    d = M.apply_geom(xml, presets[0])
    for v in ('w:w="5669"', 'w:w="3685"', 'w:w="1134"', '<w:trHeight w:val="624"/>', '"2170800"'):
        assert v in d, v
    # стандарт J-card реально меняет размеры
    s = M.apply_geom(xml, presets[-1])
    for v in ('w:w="5760"', 'w:val="5760"', 'w:w="1440"', '<w:trHeight w:val="720"/>', '"2174400"'):
        assert v in s, v
    assert "5670" not in s and "2171700" not in s and "655320" not in s
    assert 'w:top="1134"' in s                      # поля страницы не трогаем


def test_whole():
    g = {"height_mm": 101.6, "front_mm": 65.1, "spine_mm": 12.7, "flap_mm": 25.4}
    x = M.whole_layout(M.apply_geom(tpl_xml(), g), g, M.build_title_inner(["A - B (1990)"]),
                       M.build_cell_inner("h", "h", ["s1"], ["s2"], 18))
    assert x.count("<w:tbl>") == 1 and x.count("<w:tc>") == 3
    assert '<w:gridCol w:w="720"/><w:gridCol w:w="3691"/><w:gridCol w:w="3691"/>' in x
    # порядок: торец -> обложка -> треклист
    assert x.index("btLr") < x.index("<w:drawing>") < x.index("Side A:")
    assert "A - B (1990)" in x and "<w:sectPr" in x
    import xml.dom.minidom; xml.dom.minidom.parseString(x)


def test_fonts():
    import tempfile
    from PIL import Image
    img = tempfile.mktemp(suffix=".jpg"); Image.new("RGB", (50, 50)).save(img)
    out = tempfile.mktemp(suffix=".docx")
    M.make_docx(out, ["A - B"], "h", "h", ["s1"], ["s2"], 18, img, whole=True,
                spine_font="Arial", spine_sz=20, track_font="Georgia")
    x = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
    spine = x[:x.index("<w:drawing>")]
    tracks = x[x.index("Side A:") - 2000:]
    assert '"Arial"' in spine and 'w:sz w:val="20"' in spine and M.FONT not in spine
    assert '"Georgia"' in tracks and M.FONT not in tracks
    os.remove(img); os.remove(out)


def test_no_cover():
    import tempfile, xml.dom.minidom
    for whole in (False, True):
        out = tempfile.mktemp(suffix=".docx")
        M.make_docx(out, ["Натали - Ветер с моря дул (1998)"], "h", "h", ["s1"], ["s2"], 18, None,
                    whole=whole, spine_font="Arial")
        x = zipfile.ZipFile(out).read("word/document.xml").decode("utf-8")
        xml.dom.minidom.parseString(x)
        assert "<w:drawing>" not in x
        assert ">Натали<" in x and ">Ветер с моря дул (1998)<" in x and '"Arial"' in x
        os.remove(out)
    inner = M.front_text_inner("Натали - Ветер с моря дул (1998)", M.DEFAULT_GEOM)
    sz = int(inner.split('w:sz w:val="')[1].split('"')[0])
    assert 24 <= sz <= 30, sz       # ~13 pt: 23 символа на 61 мм


def test_distribute():
    t = [("T%d" % i, 300) for i in range(1, 13)]   # 12 x 5 мин
    d = M.distribute(t, 47)
    assert (len(d["side_a"]), len(d["side_b"])) == (9, 9)
    assert len(M.distribute(t, 46)["side_a"]) == 9 and len(M.distribute(t, 30)["side_a"]) == 6


if __name__ == "__main__":
    test_geom(); test_whole(); test_fonts(); test_no_cover(); test_distribute()
    print("ok")
