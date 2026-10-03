#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Веб-приложение: генератор вкладышей для аудиокассет (.docx).

Запуск:  ./.venv/bin/python app.py   →   http://<host>:8000/
Источник треклистов/обложек: iTunes Search API (без токена, с длительностями).
"""
import base64, io, json, os, re, subprocess, sys, tempfile
import requests
from PIL import Image
from flask import (Flask, request, jsonify, send_from_directory,
                   render_template_string, abort)

import make_insert as M
from paths import output_dir, user_copy

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = output_dir()

app = Flask(__name__)
UA = {"User-Agent": "cassette-insert/1.0 (apgef1978@gmail.com)"}

# ---------------------------------------------------------------------------
def slug(s):
    s = re.sub(r"[^\w\-]+", "_", s, flags=re.U).strip("_")
    return s or "insert"

def unique_base(base):
    """Имя без перезаписи прежних вкладышей: base, base_2, base_3, …"""
    name, n = base, 1
    while (os.path.exists(os.path.join(OUT, name + ".docx"))
           or os.path.exists(os.path.join(OUT, name + "_cover.jpg"))):
        n += 1
        name = "%s_%d" % (base, n)
    return name

def _get(url, **kw):
    kw.setdefault("headers", UA); kw.setdefault("timeout", 25)
    return requests.get(url, **kw)

def _mb_get(url, params, tries=3):
    """MusicBrainz нестабилен/медленный — короткий таймаут + ретраи (rate-limit 1/с)."""
    last = None
    for i in range(tries):
        try:
            return _get(url, params=params, timeout=15)
        except Exception as e:
            last = e
            import time as _t; _t.sleep(1.1)
    raise last

# ---- iTunes ----------------------------------------------------------------
def itunes_search(query):
    r = _get("https://itunes.apple.com/search",
             params={"term": query, "entity": "album", "limit": 8})
    out = []
    for a in r.json().get("results", []):
        out.append({"source": "itunes", "id": a.get("collectionId"),
                    "artist": a.get("artistName"), "album": a.get("collectionName"),
                    "year": (a.get("releaseDate") or "")[:4], "tracks": a.get("trackCount"),
                    "cover": (a.get("artworkUrl100") or "").replace("100x100bb", "300x300bb")})
    return out

def itunes_album(cid):
    res = _get("https://itunes.apple.com/lookup",
               params={"id": cid, "entity": "song"}).json().get("results", [])
    title, year, cover, tracks = "", "", "", []
    for it in res:
        if it.get("wrapperType") == "collection":
            year = (it.get("releaseDate") or "")[:4]
            title = "%s - %s" % (it.get("artistName", ""), it.get("collectionName", ""))
            cover = (it.get("artworkUrl100") or "").replace("100x100bb", "1000x1000bb")
        if it.get("kind") == "song":
            sec = int(it.get("trackTimeMillis", 0)) // 1000
            tracks.append({"name": it.get("trackName", ""), "dur": M.fmt_dur(sec),
                           "n": it.get("trackNumber", 0)})
    tracks.sort(key=lambda t: t["n"])
    if title and year:
        title = "%s (%s)" % (title, year)
    return {"title": title, "year": year, "cover": cover, "tracks": tracks}

# ---- Deezer ----------------------------------------------------------------
def deezer_search(query):
    r = _get("https://api.deezer.com/search/album", params={"q": query, "limit": 8})
    out = []
    for a in r.json().get("data", []):
        out.append({"source": "deezer", "id": a.get("id"),
                    "artist": a.get("artist", {}).get("name"), "album": a.get("title"),
                    "year": "", "tracks": a.get("nb_tracks"),
                    "cover": a.get("cover_medium") or a.get("cover")})
    return out

def deezer_album(aid):
    d = _get("https://api.deezer.com/album/%s" % aid).json()
    year = (d.get("release_date") or "")[:4]
    artist = d.get("artist", {}).get("name", "")
    title = "%s - %s" % (artist, d.get("title", ""))
    if year:
        title = "%s (%s)" % (title, year)
    tracks = [{"name": t.get("title"), "dur": M.fmt_dur(t.get("duration", 0)),
               "n": i} for i, t in enumerate(d.get("tracks", {}).get("data", []), 1)]
    return {"title": title, "year": year,
            "cover": d.get("cover_xl") or d.get("cover_big") or d.get("cover"),
            "tracks": tracks}

def deezer_track_dur(artist, title):
    """Длительность одного трека через Deezer (для источников без таймингов)."""
    for params in ({"q": 'artist:"%s" track:"%s"' % (artist, title)},
                   {"q": "%s %s" % (artist, title)}):
        try:
            data = _get("https://api.deezer.com/search", params=params).json().get("data", [])
        except Exception:
            data = []
        akey = artist.lower().split()[-1] if artist else ""
        for it in data:
            if akey and akey in it.get("artist", {}).get("name", "").lower():
                return it.get("duration", 0)
        if data:
            return data[0].get("duration", 0)
    return 0

# ---- MusicBrainz + Cover Art Archive --------------------------------------
def mb_search(query):
    d = _mb_get("https://musicbrainz.org/ws/2/release/",
                {"query": query, "fmt": "json", "limit": 8}).json()
    out = []
    for r in d.get("releases", []):
        ac = r.get("artist-credit") or [{}]
        out.append({"source": "mb", "id": r.get("id"),
                    "artist": ac[0].get("name", ""), "album": r.get("title"),
                    "year": (r.get("date") or "")[:4], "tracks": r.get("track-count"),
                    "cover": ""})
    return out

def mb_cover(mbid, rgid=""):
    """Обложка из Cover Art Archive: сперва у самого релиза, затем у его
    release-group (туда стекается арт всех переизданий)."""
    targets = [("release", mbid)]
    if rgid:
        targets.append(("release-group", rgid))
    for kind, gid in targets:
        for sz in ("front-500", "front"):
            try:
                r = requests.head("https://coverartarchive.org/%s/%s/%s" % (kind, gid, sz),
                                  headers=UA, timeout=20, allow_redirects=True)
                if r.status_code == 200:
                    return str(r.url)
            except Exception:
                pass
    return ""

def mb_album(mbid):
    d = _mb_get("https://musicbrainz.org/ws/2/release/%s" % mbid,
                {"inc": "recordings+artist-credits+release-groups", "fmt": "json"}).json()
    ac = d.get("artist-credit") or [{}]
    artist = ac[0].get("name", "")
    year = (d.get("date") or "")[:4]
    title = "%s - %s" % (artist, d.get("title", ""))
    if year:
        title = "%s (%s)" % (title, year)
    names = []
    for m in d.get("media", []):
        for t in m.get("tracks", []):
            ln = t.get("length")
            names.append((t.get("title", ""), (ln // 1000) if ln else None))
    # длительности, которых нет в MB, подтягиваем из Deezer по названию
    tracks = []
    for i, (name, sec) in enumerate(names, 1):
        if not sec:
            sec = deezer_track_dur(artist, name)
        tracks.append({"name": name, "dur": M.fmt_dur(sec or 0), "n": i})
    rgid = (d.get("release-group") or {}).get("id", "")
    return {"title": title, "year": year, "cover": mb_cover(mbid, rgid), "tracks": tracks}

# ---- нормализация запроса и чистка названий --------------------------------
def normalize_query(q):
    """Убрать год-число в конце запроса ('… 1993') — в базах его обычно нет
    в названии альбома, и цифра только засоряет выдачу. Год в начале/середине
    (напр. альбом «1999») не трогаем."""
    q = (q or "").strip()
    q2 = re.sub(r"[\s,;]+(?:19|20)\d{2}\.?$", "", q).strip()
    return q2 or q

def _collapse_repeats(words):
    """Свернуть подряд идущие повторы фраз из 2+ слов (без учёта регистра):
    'Somebody Dance With Me Dance With Me' -> 'Somebody Dance With Me'.
    Повторы одного слова ('Uh Uh Uh', 'Na Na Na') не трогаем — они осмысленны."""
    low = [w.lower() for w in words]
    changed = True
    while changed:
        changed = False
        n = len(words)
        for L in range(n // 2, 1, -1):          # L >= 2
            for i in range(0, n - 2 * L + 1):
                if low[i:i + L] == low[i + L:i + 2 * L]:
                    words = words[:i + L] + words[i + 2 * L:]
                    low = low[:i + L] + low[i + 2 * L:]
                    changed = True
                    break
            if changed:
                break
    return words

def _titlecase_caps(s):
    """КАПС -> нормальный регистр. Заглавная после начала строки и
    разделителей ( [ - / & . , — но не после апострофа, чтобы 'LET'S' -> 'Let's'."""
    s = s.lower()
    return re.sub(r"(^|[\s(\[\-/&.,])([a-zа-яё])",
                  lambda m: m.group(1) + m.group(2).upper(), s)

def clean_track_name(name):
    """Нормализовать название трека: схлопнуть пробелы, убрать повторы фраз,
    КАПС привести к нормальному регистру."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name:
        return name
    name = " ".join(_collapse_repeats(name.split()))
    letters = [c for c in name if c.isalpha()]
    if letters and not any(c.islower() for c in letters):   # всё капсом
        name = _titlecase_caps(name)
    return name

# ---- объединённый поиск ----------------------------------------------------
def combined_search(query):
    query = normalize_query(query)
    results, seen = [], set()
    for fn in (itunes_search, deezer_search, mb_search):
        try:
            for r in fn(query):
                key = (str(r.get("artist", "")).lower(), str(r.get("album", "")).lower())
                if key in seen:
                    continue
                seen.add(key); results.append(r)
        except Exception:
            continue
    return results

def album_by_source(source, aid):
    if source == "deezer":
        d = deezer_album(aid)
    elif source == "mb":
        d = mb_album(aid)
    else:
        d = itunes_album(aid)
    for t in d.get("tracks", []):
        t["name"] = clean_track_name(t.get("name", ""))
    return d

def parse_tracklist(text):
    """Строки 'Название | M:SS' -> [(name, sec)]. Пустые/без времени допустимы."""
    out = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, dur = line.rsplit("|", 1)
        else:
            name, dur = line, ""
        name = name.strip()
        if name:
            out.append((name, M.parse_dur(dur)))
    return out

GEOM_KEYS = ("height_mm", "front_mm", "spine_mm", "flap_mm")

def parse_cassette(c):
    """Запись пресета -> чистый dict; ValueError с понятным текстом, если битая."""
    try:
        item = {"name": str(c["name"]).strip(), "minutes": float(c["minutes"]),
                "note": str(c.get("note") or "").strip()}
        for k in GEOM_KEYS:
            item[k] = float(c[k])
    except (KeyError, TypeError, ValueError):
        raise ValueError("«%s»: заполните все поля числами" % (c.get("name") if isinstance(c, dict) else c))
    if not item["name"]:
        raise ValueError("у кассеты пустое название")
    if not 1 <= item["minutes"] <= 120:
        raise ValueError("«%s»: минуты на сторону — от 1 до 120" % item["name"])
    if not all(1 <= item[k] <= 300 for k in GEOM_KEYS):
        raise ValueError("«%s»: размеры — от 1 до 300 мм" % item["name"])
    return item

def load_cassettes():
    """Пресеты кассет из «Документы\\Кассетные вкладыши\\cassettes.json» (правится
    в программе или руками, читается при каждом запросе). Битые записи пропускаем."""
    with open(user_copy("cassettes.json"), encoding="utf-8") as fh:
        raw = json.load(fh)
    out = []
    for c in raw:
        try:
            out.append(parse_cassette(c))
        except ValueError:
            continue
    return out

# ---------------------------------------------------------------------------
@app.route("/api/cassettes")
def api_cassettes():
    try:
        return jsonify({"cassettes": load_cassettes()})
    except Exception as e:
        return jsonify({"error": "cassettes.json: %s" % e}), 500

@app.route("/api/cassettes", methods=["POST"])
def api_cassettes_save():
    """Сохранить весь список кассет из редактора. Битый список не пишем."""
    raw = (request.json or {}).get("cassettes")
    if not isinstance(raw, list):
        return jsonify({"error": "нет списка кассет"}), 400
    try:
        items = [parse_cassette(c) for c in raw]
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    names = [c["name"] for c in items]
    dup = next((n for n in names if names.count(n) > 1), None)
    if dup:
        return jsonify({"error": "две кассеты с названием «%s»" % dup}), 400
    path = user_copy("cassettes.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(items, fh, ensure_ascii=False, indent=1)
    os.replace(tmp, path)                  # атомарно: не оставим полузаписанный файл
    return jsonify({"cassettes": items})

@app.route("/api/search", methods=["POST"])
def api_search():
    q = (request.json or {}).get("query", "").strip()
    if not q:
        return jsonify({"error": "пустой запрос"}), 400
    try:
        return jsonify({"results": combined_search(q)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/album", methods=["POST"])
def api_album():
    body = request.json or {}
    aid = body.get("id"); source = body.get("source", "itunes")
    if not aid:
        return jsonify({"error": "нет id"}), 400
    try:
        return jsonify(album_by_source(source, aid))
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/folder", methods=["POST"])
def api_folder():
    """Треклист/обложка из локальной папки с музыкой (десктоп). Путь приходит из
    нативного диалога pywebview; читаем теги и длительности через mutagen."""
    folder = (request.json or {}).get("folder", "").strip()
    if not folder:
        return jsonify({"error": "не выбрана папка"}), 400
    try:
        import folder_scan
        d = folder_scan.scan_folder(folder)
    except Exception as e:
        return jsonify({"error": str(e)}), 400
    for t in d.get("tracks", []):
        t["name"] = clean_track_name(t.get("name", ""))
    return jsonify(d)

def font_arg(key):
    """Имя шрифта из формы: пусто -> None (шрифт шаблона)."""
    return request.form.get(key, "").strip()[:64] or None

def size_arg(key):
    """Размер в pt из формы -> полупункты Word; 'auto'/мусор -> None."""
    try:
        pt = float(request.form.get(key, ""))
    except ValueError:
        return None
    return int(round(pt * 2)) if 4 <= pt <= 72 else None

@app.route("/api/build", methods=["POST"])
def api_build():
    title = request.form.get("title", "").strip()
    limit = float(request.form.get("limit", "47") or 47)
    sizemode = request.form.get("size", "auto")
    tracks = parse_tracklist(request.form.get("tracklist", ""))
    cover_url = request.form.get("cover_url", "").strip()
    sideb_mode = request.form.get("sideb_mode", "same")        # same | other
    title_mode = request.form.get("title_mode", "first")       # first | both
    title2 = request.form.get("title2", "").strip()
    tracks2 = parse_tracklist(request.form.get("tracklist2", ""))
    cname = request.form.get("cassette", "")
    try:
        geom = next((c for c in load_cassettes() if c["name"] == cname), None) if cname else None
    except Exception as e:
        return jsonify({"error": "cassettes.json: %s" % e}), 400
    if not title:
        return jsonify({"error": "укажите название"}), 400
    if not tracks:
        return jsonify({"error": "пустой треклист"}), 400
    if sideb_mode == "other" and not tracks2:
        return jsonify({"error": "выбран второй альбом, но его треклист пуст"}), 400

    # обложка: загруженный файл приоритетнее url
    tmp_cover, cover_warn = None, ""
    f = request.files.get("cover_file")
    try:
        if f and f.filename:
            tmp_cover = tempfile.NamedTemporaryFile(delete=False, suffix=".img").name
            f.save(tmp_cover)
        elif cover_url.startswith("data:"):
            # обложка из папки приходит как data:image/...;base64,<...>
            b64 = cover_url.split(",", 1)[1] if "," in cover_url else ""
            tmp_cover = tempfile.NamedTemporaryFile(delete=False, suffix=".img").name
            with open(tmp_cover, "wb") as fh:
                fh.write(base64.b64decode(b64))
        elif cover_url:
            try:
                resp = requests.get(cover_url, headers=UA, timeout=20)
                resp.raise_for_status()
                tmp_cover = tempfile.NamedTemporaryFile(delete=False, suffix=".img").name
                with open(tmp_cover, "wb") as fh:
                    fh.write(resp.content)
            except requests.RequestException:
                cover_warn = "Обложка не скачалась — на лицевой стороне название альбома."
        if tmp_cover:
            try:
                with Image.open(tmp_cover) as im:
                    im.verify()
            except Exception:
                os.remove(tmp_cover); tmp_cover = None
                cover_warn = "Обложка не читается как картинка — на лицевой стороне название альбома."

        if sideb_mode == "other":
            dist = M.distribute_mix(tracks, tracks2, limit)
            header_a, header_b = title, (title2 or title)
            if title_mode == "both" and title2:
                title_lines = ["A: " + title, "B: " + title2]
            else:
                title_lines = [title]
        else:
            dist = M.distribute(tracks, limit)
            header_a = header_b = title
            title_lines = [title]
        names_a = [n for n, _ in dist["side_a"]]
        names_b = [n for n, _ in dist["side_b"]]
        # Микстейп: хвост 1-го альбома в начале Side B подписываем исполнителем
        if sideb_mode == "other":
            carry_fit = min(len(names_b), dist.get("carry", 0))
            artist1 = title.split(" - ", 1)[0].strip()
            if artist1 and carry_fit:
                names_b = [("%s - %s" % (artist1, n)) if i < carry_fit else n
                           for i, n in enumerate(names_b)]
        sz = (M.auto_sz(names_a, names_b, height_mm=geom["height_mm"] if geom else 100)
              if sizemode == "auto" else int(sizemode))

        base = unique_base(slug(title) + ("__" + slug(title2) if sideb_mode == "other" and title2 else ""))
        docx_name = base + ".docx"
        cover_name = base + "_cover.jpg"
        M.make_docx(os.path.join(OUT, docx_name), title_lines, header_a, header_b,
                    names_a, names_b, sz, tmp_cover, geom,
                    whole=request.form.get("layout") == "whole",
                    spine_font=font_arg("spine_font"), spine_sz=size_arg("spine_size"),
                    track_font=font_arg("track_font"), front_title=title,
                    front_font=font_arg("front_font"), front_sz=size_arg("front_size"))
        if tmp_cover:
            M.prepare_cover(tmp_cover, os.path.join(OUT, cover_name))
    finally:
        if tmp_cover and os.path.exists(tmp_cover):
            os.remove(tmp_cover)

    def pack(names, secs):
        return [{"n": "%02d" % i, "name": n, "dur": M.fmt_dur(s)}
                for i, (n, s) in enumerate(zip(names, secs), 1)]
    secs_a = [s for _, s in dist["side_a"]]
    secs_b = [s for _, s in dist["side_b"]]
    warn = ""
    if sideb_mode == "other" and not dist.get("album1_full", True):
        warn = "Первый альбом не уместился целиком: хвост не влез на Side B при лимите %g мин." % limit
    if sideb_mode != "other":
        left = tracks[len(dist["side_a"]) + len(dist["side_b"]):]   # не дошли ни на A, ни на B
        if left:
            warn = "Не поместились на кассету (%d): %s." % (len(left), ", ".join(n for n, _ in left))
    warn = " ".join(w for w in (warn, cover_warn) if w)
    return jsonify({
        "title": " / ".join(title_lines),
        "header_a": header_a, "header_b": header_b,
        "side_a": pack(names_a, secs_a), "side_b": pack(names_b, secs_b),
        "sec_a": dist["sec_a"], "sec_b": dist["sec_b"],
        "dur_a": M.fmt_dur(dist["sec_a"]), "dur_b": M.fmt_dur(dist["sec_b"]),
        "sz": sz, "sz_pt": sz / 2.0, "warn": warn,
        "docx": "/file/" + docx_name, "cover": ("/img/" + cover_name) if tmp_cover else "",
        "docx_path": os.path.join(OUT, docx_name), "out_dir": OUT,
    })

@app.route("/api/open_output", methods=["POST"])
def api_open_output():
    """Открыть папку с готовыми вкладышами в системном файловом менеджере."""
    try:
        if sys.platform.startswith("win"):
            os.startfile(OUT)                                   # noqa: S606
        elif sys.platform == "darwin":
            subprocess.Popen(["open", OUT])
        else:
            subprocess.Popen(["xdg-open", OUT])
        return jsonify({"ok": True, "dir": OUT})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/file/<path:name>")
def get_file(name):
    return send_from_directory(OUT, name, as_attachment=True)

@app.route("/img/<path:name>")
def get_img(name):
    return send_from_directory(OUT, name)

@app.route("/")
def index():
    return render_template_string(PAGE)

# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html><html lang="ru"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Вкладыши для аудиокассет</title>
<style>
  :root{--bg:#1f2430;--card:#2a3140;--mut:#8b93a7;--acc:#6aa9ff;--line:#3a4254}
  *{box-sizing:border-box} body{margin:0;font:15px/1.45 system-ui,Segoe UI,Roboto,sans-serif;
    background:var(--bg);color:#e6e9ef}
  .wrap{max-width:1000px;margin:0 auto;padding:24px}
  h1{font-size:22px;margin:0 0 4px} .sub{color:var(--mut);margin:0 0 20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin-bottom:18px}
  label{display:block;font-size:13px;color:var(--mut);margin:10px 0 4px}
  input,textarea,select{width:100%;background:#1b2029;border:1px solid var(--line);color:#e6e9ef;
    border-radius:8px;padding:9px 11px;font:inherit}
  textarea{min-height:240px;resize:vertical;font-family:ui-monospace,Menlo,Consolas,monospace;font-size:13px}
  button{background:var(--acc);color:#08111f;border:0;border-radius:8px;padding:10px 16px;
    font-weight:600;cursor:pointer} button.ghost{background:#374055;color:#e6e9ef}
  button:disabled{opacity:.5;cursor:default}
  .row{display:flex;gap:12px;flex-wrap:wrap} .row>*{flex:1;min-width:120px}
  .results{display:flex;flex-direction:column;gap:6px;margin-top:10px}
  .res{display:flex;gap:10px;align-items:center;padding:8px;border:1px solid var(--line);
    border-radius:8px;cursor:pointer;background:#1b2029}
  .res:hover{border-color:var(--acc)} .res img{width:44px;height:44px;border-radius:5px;object-fit:cover}
  .res small{color:var(--mut)}
  .grid{display:grid;grid-template-columns:200px 1fr;gap:18px}
  .cover{width:200px;height:200px;border-radius:8px;object-fit:cover;background:#1b2029;border:1px solid var(--line)}
  .muted{color:var(--mut);font-size:13px}
  .sides{display:flex;gap:18px;flex-wrap:wrap} .side{flex:1;min-width:240px}
  .side h3{margin:0 0 6px;font-size:15px} .side ol{margin:0;padding-left:0;list-style:none}
  .side li{padding:2px 0;border-bottom:1px dashed var(--line);display:flex;justify-content:space-between}
  .side li span:last-child{color:var(--mut)}
  .pill{display:inline-block;background:#1b2029;border:1px solid var(--line);border-radius:999px;
    padding:3px 10px;font-size:12px;color:var(--mut);margin-right:6px}
  .err{color:#ff8b8b;font-size:13px;margin-top:8px} .ok{color:#7ee0a0}
  a.dl{display:inline-block;margin-top:14px;background:#7ee0a0;color:#08220f;padding:11px 18px;
    border-radius:8px;text-decoration:none;font-weight:700}
  .spin{opacity:.6}
  table.cass{border-collapse:collapse;width:100%} table.cass th{font-size:12px;color:var(--mut);font-weight:400;text-align:left;padding:0 4px 4px}
  table.cass td{padding:2px 4px} table.cass input{padding:6px 8px;min-width:60px} table.cass td:first-child input{min-width:170px}
</style></head><body><div class="wrap">
<h1>🎙️ Вкладыши для аудиокассет</h1>
<p class="sub">Поиск треклиста и обложки, распределение по сторонам, генерация .docx по шаблону.</p>

<div class="card">
  <label>Поиск альбома (Исполнитель + название)</label>
  <div class="row" style="align-items:flex-end">
    <input id="q" placeholder="напр.: Натали Ветер с моря" style="flex:3">
    <button id="searchBtn" style="flex:0 0 auto">Найти</button>
  </div>
  <div id="results" class="results"></div>
  <div id="searchErr" class="err"></div>
  <div style="display:flex;align-items:center;gap:10px;margin:14px 0 6px;color:var(--mut);font-size:13px">
    <span style="flex:1;height:1px;background:var(--line)"></span>или<span style="flex:1;height:1px;background:var(--line)"></span>
  </div>
  <button id="folderBtn" class="ghost" style="width:100%">📁 Взять из папки с музыкой</button>
  <p class="muted" style="margin:6px 0 0">Названия, точные длительности и обложку прочитаем прямо из аудиофайлов.</p>
  <div id="folderErr" class="err"></div>
</div>

<div class="card">
  <div class="grid">
    <div>
      <img id="cover" class="cover" alt="обложка">
      <label>Своя обложка (необязательно)</label>
      <input id="coverFile" type="file" accept="image/*">
      <p class="muted">Если не выбрана — берётся найденная. Кадрируется в квадрат 1:1. Нет обложки — на лицевой стороне будет название альбома.</p>
    </div>
    <div>
      <label>Название (Исполнитель - Альбом (Год))</label>
      <input id="title" placeholder="Натали - Ветер с моря дул (1998)">
      <label>Кассета</label>
      <div class="row" style="align-items:center">
        <select id="cassette" style="flex:3"><option value="">Как в шаблоне</option></select>
        <button id="cassEditBtn" class="ghost" style="flex:0 0 auto">✎ Мои кассеты</button>
      </div>
      <div id="cassetteNote" class="muted"></div>
      <label>Макет</label>
      <select id="layout">
        <option value="parts">Раздельные части (торец, обложка, треклист — отдельно)</option>
        <option value="whole">Вкладыш целиком (торец + обложка + треклист одной полосой)</option>
      </select>
      <div class="row">
        <div><label>Лимит, мин/сторона</label><input id="limit" type="number" value="47" min="1" step="1"></div>
      </div>
      <div class="row">
        <div><label>Шрифт торца</label>
          <select id="spineFont" class="fontSel" data-def="Как в шаблоне (Monotype Corsiva)"></select>
          <input id="spineFontCustom" placeholder="Название шрифта, как в Word" style="display:none;margin-top:6px"></div>
        <div><label>Размер торца</label>
          <select id="spineSize">
            <option value="auto">Авто (14 / 12 pt)</option>
            <option>18</option><option>16</option><option>14</option><option>13</option><option>12</option>
            <option>11</option><option>10</option><option>9</option><option>8</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div><label>Шрифт треклиста</label>
          <select id="trackFont" class="fontSel" data-def="Как в шаблоне (Monotype Corsiva)"></select>
          <input id="trackFontCustom" placeholder="Название шрифта, как в Word" style="display:none;margin-top:6px"></div>
        <div><label>Размер треков</label>
          <select id="size">
            <option value="auto">Авто (по эталону)</option>
            <option value="24">12 pt</option>
            <option value="22">11 pt</option><option value="20">10 pt</option>
            <option value="18">9 pt</option><option value="16">8 pt</option>
            <option value="14">7 pt</option>
          </select>
        </div>
      </div>
      <div class="row">
        <div><label>Шрифт лицевой (если нет картинки)</label>
          <select id="frontFont" class="fontSel" data-def="Как у торца"></select>
          <input id="frontFontCustom" placeholder="Название шрифта, как в Word" style="display:none;margin-top:6px"></div>
        <div><label>Размер лицевой (если нет картинки)</label>
          <select id="frontSize">
            <option value="auto">Авто (максимальный)</option>
            <option>48</option><option>40</option><option>36</option><option>32</option><option>28</option>
            <option>24</option><option>20</option><option>18</option><option>16</option><option>14</option>
            <option>12</option><option>10</option>
          </select>
        </div>
      </div>
      <p class="muted" style="margin:4px 0 0">«Другой шрифт…» — вписать любой, установленный в Windows.</p>
      <label>Треклист — по строке на трек: <code>Название | M:SS</code></label>
      <textarea id="tracklist" placeholder="Ветер с моря дул | 3:45&#10;Посвящение друзьям | 3:37"></textarea>
    </div>
  </div>
</div>

<div id="cassEditor" class="card" style="display:none">
  <h2 style="margin:0 0 6px">Мои кассеты</h2>
  <p class="muted" style="margin:0 0 10px">Размеры в мм: высота вкладыша, ширина лицевой стороны (с обложкой), торца и клапана. Время — минут на одну сторону.</p>
  <div style="overflow-x:auto">
    <table class="cass"><thead><tr><th>Название</th><th>Мин/сторона</th><th>Высота</th><th>Лицевая</th><th>Торец</th><th>Клапан</th><th>Заметка</th><th></th></tr></thead>
    <tbody id="cassRows"></tbody></table>
  </div>
  <div class="row" style="margin-top:10px">
    <button id="cassAdd" class="ghost" style="flex:0 0 auto">+ Добавить кассету</button>
    <button id="cassSave" style="flex:0 0 auto">Сохранить</button>
    <button id="cassCancel" class="ghost" style="flex:0 0 auto">Отмена</button>
  </div>
  <div id="cassErr" class="err"></div>
</div>

<div class="card">
  <h2 style="margin:0 0 6px">Вторая сторона (Side B)</h2>
  <label style="display:flex;gap:8px;align-items:center;color:#e6e9ef">
    <input type="radio" name="sbmode" value="same" checked style="width:auto"> Добить тем же альбомом (продолжение + оборот на трек 1)</label>
  <label style="display:flex;gap:8px;align-items:center;color:#e6e9ef">
    <input type="radio" name="sbmode" value="other" style="width:auto"> Взять Side B из другого альбома</label>

  <div id="album2box" style="display:none;margin-top:12px;border-top:1px solid var(--line);padding-top:12px">
    <label>Поиск второго альбома</label>
    <div class="row" style="align-items:flex-end">
      <input id="q2" placeholder="напр.: Михаил Круг Жиган-лимон" style="flex:3">
      <button id="searchBtn2" class="ghost" style="flex:0 0 auto">Найти</button>
    </div>
    <div id="results2" class="results"></div>
    <div style="display:flex;align-items:center;gap:10px;margin:12px 0 8px;color:var(--mut);font-size:13px">
      <span style="flex:1;height:1px;background:var(--line)"></span>или<span style="flex:1;height:1px;background:var(--line)"></span>
    </div>
    <button id="folderBtn2" class="ghost" style="width:100%">📁 Взять из папки с музыкой</button>
    <p class="muted" style="margin:6px 0 0">Название (имя папки) и треклист прочитаем из аудиофайлов. Обложка всегда от первого альбома.</p>
    <div id="folderErr2" class="err"></div>
    <label>Название 2-го альбома</label>
    <input id="title2" placeholder="Михаил Круг - Жиган-лимон (1994)">
    <label>Треклист 2-го альбома — <code>Название | M:SS</code></label>
    <textarea id="tracklist2" style="min-height:160px"></textarea>
    <label style="margin-top:10px">Заголовок вкладыша (обложка всегда от первого альбома)</label>
    <label style="display:flex;gap:8px;align-items:center;color:#e6e9ef">
      <input type="radio" name="tmode" value="first" checked style="width:auto"> Оставить только первый альбом</label>
    <label style="display:flex;gap:8px;align-items:center;color:#e6e9ef">
      <input type="radio" name="tmode" value="both" style="width:auto"> Оба альбома (в две строки)</label>
  </div>
</div>

<div class="card">
  <button id="buildBtn">Сгенерировать вкладыш</button>
  <div id="buildErr" class="err"></div>
</div>

<div id="preview" class="card" style="display:none">
  <h2 style="margin:0 0 10px">Предпросмотр</h2>
  <div class="grid">
    <img id="pvCover" class="cover" alt="кроп обложки">
    <div>
      <div id="pvTitle" style="font-size:18px;font-weight:700;margin-bottom:8px"></div>
      <div id="pvMeta" style="margin-bottom:12px"></div>
      <div class="sides">
        <div class="side"><h3>Side A <span id="aDur" class="muted"></span></h3><ol id="sideA"></ol></div>
        <div class="side"><h3>Side B <span id="bDur" class="muted"></span></h3><ol id="sideB"></ol></div>
      </div>
      <button id="openDir" class="dl" style="border:0;cursor:pointer">📂 Открыть папку с вкладышем</button>
      <a id="dlDocx" class="dl" href="#" download>⬇ Скачать .docx</a>
      <div id="savedPath" class="muted" style="margin-top:10px"></div>
      <p class="muted" style="margin-top:8px">Файл сохраняется автоматически. Размер шрифта подбирается эвристикой — проверьте в Word, что вкладыш на 1 страницу.</p>
    </div>
  </div>
</div>

<script>
const $=s=>document.querySelector(s);
const SRC={itunes:"iTunes",deezer:"Deezer",mb:"MusicBrainz"};
let coverUrl="";

// Универсальный поиск: pick(album) вызывается при клике по результату.
async function doSearch(query, container, errEl, pick){
  if(!query)return;
  errEl.textContent=""; container.innerHTML="<p class='muted'>Поиск…</p>";
  try{
    const r=await fetch("/api/search",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({query})});
    const d=await r.json(); if(d.error)throw new Error(d.error);
    container.innerHTML="";
    if(!d.results.length){container.innerHTML="<p class='muted'>Ничего не найдено.</p>";return;}
    d.results.forEach(a=>{
      const el=document.createElement("div"); el.className="res";
      el.innerHTML=`<img src="${a.cover||''}"><div><div>${a.artist} — ${a.album}</div>
        <small>${a.year||'?'} · ${a.tracks||'?'} треков · <b>${SRC[a.source]||a.source}</b></small></div>`;
      el.onclick=()=>pick(a,el); container.appendChild(el);
    });
  }catch(e){container.innerHTML="";errEl.textContent=e.message;}
}

async function fetchAlbum(source,id,el){
  if(el)el.style.opacity=.5;
  try{
    const r=await fetch("/api/album",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({source,id})});
    const d=await r.json(); if(d.error)throw new Error(d.error);
    return d;
  } finally{if(el)el.style.opacity=1;}
}

// --- Альбом 1 ---
$("#searchBtn").onclick=()=>doSearch($("#q").value.trim(),$("#results"),$("#searchErr"),async(a,el)=>{
  try{
    const d=await fetchAlbum(a.source,a.id,el);
    $("#title").value=d.title; coverUrl=d.cover||""; $("#cover").src=d.cover||"";
    $("#tracklist").value=d.tracks.map(t=>`${t.name} | ${t.dur}`).join("\n");
    $("#searchErr").textContent=d.cover?"":"Обложка не найдена — загрузите свою, иначе на лицевой стороне будет название альбома.";
  }catch(e){$("#searchErr").textContent=e.message;}
});

// --- Из папки с музыкой (десктоп) ---
async function pickFolder(){
  if(window.pywebview && window.pywebview.api && window.pywebview.api.pick_folder){
    return await window.pywebview.api.pick_folder();      // нативный диалог pywebview
  }
  return (prompt("Путь к папке с музыкой:")||"").trim();   // фолбэк в браузере
}
// Чтение треклиста из папки. useCover=true — берём ещё и обложку (для альбома 1).
async function loadFromFolder(btn, err, titleEl, listEl, useCover){
  err.textContent="";
  let folder=""; try{folder=await pickFolder();}catch(e){err.textContent=e.message;return;}
  if(!folder)return;
  btn.disabled=true; btn.classList.add("spin"); const t0=btn.textContent; btn.textContent="Чтение папки…";
  try{
    const r=await fetch("/api/folder",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({folder})});
    const d=await r.json(); if(d.error)throw new Error(d.error);
    titleEl.value=d.title;
    listEl.value=d.tracks.map(t=>`${t.name} | ${t.dur}`).join("\n");
    if(useCover){coverUrl=d.cover||""; $("#cover").src=d.cover||"";
      err.textContent=d.cover?"":"Обложка в папке не найдена — загрузите свою, иначе на лицевой стороне будет название альбома.";}
  }catch(e){err.textContent=e.message;}
  finally{btn.disabled=false;btn.classList.remove("spin");btn.textContent=t0;}
}
$("#folderBtn").onclick=()=>loadFromFolder($("#folderBtn"),$("#folderErr"),$("#title"),$("#tracklist"),true);

// --- Альбом 2 (Side B) ---
$("#searchBtn2").onclick=()=>doSearch($("#q2").value.trim(),$("#results2"),$("#buildErr"),async(a,el)=>{
  try{
    const d=await fetchAlbum(a.source,a.id,el);
    $("#title2").value=d.title;
    $("#tracklist2").value=d.tracks.map(t=>`${t.name} | ${t.dur}`).join("\n");
  }catch(e){$("#buildErr").textContent=e.message;}
});
$("#folderBtn2").onclick=()=>loadFromFolder($("#folderBtn2"),$("#folderErr2"),$("#title2"),$("#tracklist2"),false);

// Переключение режима Side B
document.querySelectorAll('input[name="sbmode"]').forEach(r=>r.onchange=()=>{
  $("#album2box").style.display=(document.querySelector('input[name="sbmode"]:checked').value==="other")?"block":"none";
});

// Выбор шрифта: первый пункт — по умолчанию (пусто), «Другой шрифт…» открывает поле ввода.
const FONTS=["Monotype Corsiva","Times New Roman","Arial","Arial Narrow","Calibri","Cambria","Georgia",
  "Verdana","Tahoma","Segoe UI","Segoe Script","Courier New","Comic Sans MS","Impact","Garamond"];
document.querySelectorAll(".fontSel").forEach(sel=>{
  [["",sel.dataset.def],...FONTS.map(f=>[f,f]),["__custom","Другой шрифт…"]].forEach(([v,t])=>{
    const o=document.createElement("option");o.value=v;o.textContent=t;sel.appendChild(o);});
  sel.onchange=()=>{$("#"+sel.id+"Custom").style.display=sel.value==="__custom"?"block":"none";};
});
function fontVal(id){const v=$("#"+id).value;return v==="__custom"?$("#"+id+"Custom").value.trim():v;}

// Пресеты кассет: выбор подставляет лимит минут; размеры применит сервер.
let CASS=[];
function fillCassettes(list){
  const sel=$("#cassette"), cur=sel.value;
  CASS=list; sel.length=1;               // оставляем «Как в шаблоне»
  CASS.forEach(c=>{const o=document.createElement("option");o.value=o.textContent=c.name;sel.appendChild(o);});
  sel.value=CASS.some(c=>c.name===cur)?cur:""; sel.onchange();
}
fetch("/api/cassettes").then(r=>r.json()).then(d=>{
  if(d.error){$("#cassetteNote").textContent=d.error;return;}
  fillCassettes(d.cassettes);
});

// Редактор «Мои кассеты»: строки с полями, сохранение всего списка разом.
const CFIELDS=[["name","text"],["minutes","number"],["height_mm","number"],["front_mm","number"],["spine_mm","number"],["flap_mm","number"],["note","text"]];
function cassRow(c){
  const tr=document.createElement("tr");
  CFIELDS.forEach(([k,type])=>{
    const td=document.createElement("td"), inp=document.createElement("input");
    inp.type=type; inp.dataset.k=k; inp.value=c[k]??""; if(type==="number"){inp.step="0.1";inp.min="0";}
    td.appendChild(inp); tr.appendChild(td);
  });
  const td=document.createElement("td"), del=document.createElement("button");
  del.className="ghost"; del.textContent="✕"; del.title="Удалить"; del.onclick=()=>tr.remove();
  td.appendChild(del); tr.appendChild(td); return tr;
}
function openEditor(){
  $("#cassErr").textContent=""; const tb=$("#cassRows"); tb.innerHTML="";
  CASS.forEach(c=>tb.appendChild(cassRow(c)));
  $("#cassEditor").style.display="block"; $("#cassEditor").scrollIntoView({behavior:"smooth"});
}
$("#cassEditBtn").onclick=openEditor;
$("#cassCancel").onclick=()=>{$("#cassEditor").style.display="none";};
$("#cassAdd").onclick=()=>{
  const r=cassRow({name:"",minutes:47,height_mm:100,front_mm:65,spine_mm:11,flap_mm:20,note:""});
  $("#cassRows").appendChild(r); r.querySelector("input").focus();
};
$("#cassSave").onclick=async()=>{
  $("#cassErr").textContent="";
  const list=[...$("#cassRows").children].map(tr=>{
    const c={}; tr.querySelectorAll("input").forEach(i=>c[i.dataset.k]=i.value.trim()); return c;});
  try{
    const r=await fetch("/api/cassettes",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({cassettes:list})});
    const d=await r.json(); if(d.error)throw new Error(d.error);
    fillCassettes(d.cassettes); $("#cassEditor").style.display="none";
  }catch(e){$("#cassErr").textContent=e.message;}
};
$("#cassette").onchange=()=>{
  const c=CASS.find(c=>c.name===$("#cassette").value);
  if(!c){$("#cassetteNote").textContent="";return;}
  $("#limit").value=c.minutes;
  $("#cassetteNote").textContent=`${c.minutes} мин/сторона · вкладыш ${c.height_mm}×${c.front_mm} мм, корешок ${c.spine_mm}, клапан ${c.flap_mm}`+(c.note?` — ${c.note}`:"");
};

$("#coverFile").onchange=e=>{const f=e.target.files[0]; if(f)$("#cover").src=URL.createObjectURL(f);};

$("#openDir").onclick=async()=>{
  try{const r=await fetch("/api/open_output",{method:"POST"});const d=await r.json();
    if(d.error)throw new Error(d.error);}
  catch(e){$("#buildErr").textContent=e.message;}
};

$("#buildBtn").onclick=async()=>{
  $("#buildErr").textContent=""; const btn=$("#buildBtn");
  const sbmode=document.querySelector('input[name="sbmode"]:checked').value;
  const tmode=document.querySelector('input[name="tmode"]:checked').value;
  btn.disabled=true; btn.classList.add("spin"); btn.textContent="Генерация…";
  try{
    const fd=new FormData();
    fd.append("title",$("#title").value);
    fd.append("limit",$("#limit").value);
    fd.append("cassette",$("#cassette").value);
    fd.append("layout",$("#layout").value);
    fd.append("spine_font",fontVal("spineFont"));
    fd.append("spine_size",$("#spineSize").value);
    fd.append("track_font",fontVal("trackFont"));
    fd.append("front_font",fontVal("frontFont"));
    fd.append("front_size",$("#frontSize").value);
    fd.append("size",$("#size").value);
    fd.append("tracklist",$("#tracklist").value);
    fd.append("cover_url",coverUrl);
    fd.append("sideb_mode",sbmode);
    fd.append("title_mode",tmode);
    fd.append("title2",$("#title2").value);
    fd.append("tracklist2",$("#tracklist2").value);
    if($("#coverFile").files[0])fd.append("cover_file",$("#coverFile").files[0]);
    const r=await fetch("/api/build",{method:"POST",body:fd});
    const d=await r.json(); if(d.error)throw new Error(d.error);
    $("#pvTitle").innerHTML=d.title.split(" / ").map(s=>`<div>${s}</div>`).join("");
    $("#pvMeta").innerHTML=
      `<span class="pill">Шрифт: ${d.sz_pt} pt</span>`+
      `<span class="pill">Side A: ${d.dur_a}</span><span class="pill">Side B: ${d.dur_b}</span>`+
      (d.warn?`<div class="err" style="margin-top:8px">⚠ ${d.warn}</div>`:"");
    $("#aDur").textContent="— "+d.header_a+" ("+d.dur_a+")";
    $("#bDur").textContent="— "+d.header_b+" ("+d.dur_b+")";
    const fill=(ol,arr)=>{ol.innerHTML=arr.map(t=>`<li><span>${t.n} — ${t.name}</span><span>${t.dur}</span></li>`).join("");};
    fill($("#sideA"),d.side_a); fill($("#sideB"),d.side_b);
    $("#pvCover").style.display=d.cover?"":"none";
    if(d.cover)$("#pvCover").src=d.cover+"?t="+Date.now();
    $("#savedPath").textContent="Сохранено: "+d.docx_path;
    $("#dlDocx").href=d.docx;
    $("#preview").style.display="block";
    $("#preview").scrollIntoView({behavior:"smooth"});
  }catch(e){$("#buildErr").textContent=e.message;}
  finally{btn.disabled=false;btn.classList.remove("spin");btn.textContent="Сгенерировать вкладыш";}
};
</script>
</div></body></html>"""

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=False)
