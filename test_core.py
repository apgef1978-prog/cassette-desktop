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


def test_distribute():
    t = [("T%d" % i, 300) for i in range(1, 13)]   # 12 x 5 мин
    d = M.distribute(t, 47)
    assert (len(d["side_a"]), len(d["side_b"])) == (9, 9)
    assert len(M.distribute(t, 46)["side_a"]) == 9 and len(M.distribute(t, 30)["side_a"]) == 6


if __name__ == "__main__":
    test_geom(); test_distribute()
    print("ok")
