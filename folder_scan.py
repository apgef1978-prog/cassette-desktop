#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Чтение треклиста из локальной папки с музыкой.

Достаёт из аудиофайлов теги (название/исполнитель/альбом/год/номер трека),
точные длительности и обложку (встроенную в файл или картинку рядом в папке).
Это третий источник данных наравне с iTunes/Deezer/MusicBrainz, но точнее по
таймингам — длительности берутся из самих файлов, а не из онлайн-базы.
"""
import base64
import os
import re

from mutagen import File as MutagenFile

import make_insert as M

AUDIO_EXTS = (".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma",
              ".wav", ".aiff", ".aif", ".ape", ".wv",
              ".dsf", ".dff")  # DSD: Sony DSF и Philips DSDIFF (mutagen 1.47+)
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# характерные имена файлов-обложек рядом с треками
COVER_STEMS = ("cover", "folder", "front", "albumart", "album", "обложка")


# Соответствие easy-ключей ID3-фреймам — для форматов без easy-режима (DSF/DSDIFF),
# где теги приходят «сырым» ID3 (TIT2/TPE1/...), а не как title/artist.
_ID3 = {"title": "TIT2", "artist": "TPE1", "albumartist": "TPE2", "album": "TALB",
        "composer": "TCOM", "date": "TDRC", "originaldate": "TDOR", "year": "TYER",
        "discnumber": "TPOS", "tracknumber": "TRCK"}


def _first(tag, *keys):
    """Первое непустое значение тега по одному из ключей (теги — списки)."""
    if tag is None:
        return ""
    for k in keys:
        v = tag.get(k)
        if v:
            return str(v[0] if isinstance(v, (list, tuple)) else v).strip()
    return ""


def _tag_first(easy, audio, *keys):
    """Сначала пробуем easy-теги (mp3/flac/m4a/ogg…), затем — ID3-фреймы из
    самого файла (DSF/DSDIFF и прочее без easy-режима)."""
    v = _first(easy, *keys)
    if v:
        return v
    tags = getattr(audio, "tags", None)
    if tags:
        for k in keys:
            fid = _ID3.get(k)
            frame = tags.get(fid) if fid else None
            if frame is None:
                continue
            try:
                return str(frame.text[0]).strip()
            except Exception:
                s = str(frame).strip()
                if s:
                    return s
    return ""


def _num(s):
    """'3', '03/12' -> 3 ; мусор -> 0."""
    m = re.match(r"\s*(\d+)", str(s or ""))
    return int(m.group(1)) if m else 0


def _year(s):
    m = re.search(r"(?:19|20)\d{2}", str(s or ""))
    return m.group(0) if m else ""


def _strip_leading_num(name):
    """'03 - Song', '03. Song', '3) Song', '03 Song' -> 'Song'."""
    return re.sub(r"^\s*\d{1,3}\s*[-.)\]]?\s+", "", name).strip() or name


def _natkey(s):
    """Натуральная сортировка по имени файла: 'A2' < 'A10'."""
    return [int(t) if t.isdigit() else t.lower() for t in re.split(r"(\d+)", s)]


def _embedded_cover(audio, path):
    """Байты встроенной обложки для распространённых форматов или None."""
    try:
        if os.path.splitext(path)[1].lower() == ".flac" and getattr(audio, "pictures", None):
            return audio.pictures[0].data
        tags = getattr(audio, "tags", None)
        if not tags:
            return None
        for k in tags.keys():                         # ID3: APIC, APIC:cover, ...
            if k == "APIC" or k.startswith("APIC:"):
                return tags[k].data
        if "covr" in tags:                            # MP4/M4A
            return bytes(tags["covr"][0])
        mbp = tags.get("metadata_block_picture")      # Vorbis/Opus (FLAC Picture в base64)
        if mbp:
            from mutagen.flac import Picture
            return Picture(base64.b64decode(mbp[0])).data
    except Exception:
        pass
    return None


def _folder_cover(folder):
    """Картинка-обложка рядом с треками: сперва по характерному имени, иначе любая."""
    imgs = [f for f in os.listdir(folder)
            if os.path.isfile(os.path.join(folder, f))
            and os.path.splitext(f)[1].lower() in IMG_EXTS]
    for f in sorted(imgs):
        if os.path.splitext(f)[0].lower() in COVER_STEMS:
            return os.path.join(folder, f)
    return os.path.join(folder, sorted(imgs)[0]) if imgs else None


def _cover_data_url(data):
    if not data:
        return ""
    mime = "image/png" if data[:8].startswith(b"\x89PNG") else "image/jpeg"
    return "data:%s;base64,%s" % (mime, base64.b64encode(data).decode("ascii"))


def _build_result(folder, entries, cover_bytes, album_fallback=""):
    """entries (уже отсортированы): [{'name','artist','dur'(сек)}] -> итоговый dict.

    Заголовок — имя папки. Имя трека: при одном исполнителе на всех — только
    песня; при разных — 'Исполнитель - Песня' (пустые исполнители не считаем).
    """
    if cover_bytes is None:
        cf = _folder_cover(folder)
        if cf:
            try:
                with open(cf, "rb") as fh:
                    cover_bytes = fh.read()
            except Exception:
                cover_bytes = None

    title = os.path.basename(folder) or album_fallback or "Альбом"
    distinct_artists = {e["artist"] for e in entries if e["artist"]}
    one_artist = len(distinct_artists) <= 1

    def track_name(e):
        if one_artist or not e["artist"]:
            return e["name"]
        return "%s - %s" % (e["artist"], e["name"])

    return {
        "title": title,
        "tracks": [{"name": track_name(e), "dur": M.fmt_dur(e["dur"])} for e in entries],
        "cover": _cover_data_url(cover_bytes),
    }


# ---------------------------------------------------------------------------
#  CUE-листы (треклист в текстовом файле рядом с образом FLAC/APE/WAV)
# ---------------------------------------------------------------------------
def _read_text(path):
    """CUE бывают в UTF-8 (с BOM и без) и в cp1251 (русские релизы)."""
    for enc in ("utf-8-sig", "cp1251", "utf-8"):
        try:
            with open(path, encoding=enc) as fh:
                return fh.read()
        except (UnicodeDecodeError, LookupError):
            continue
    with open(path, encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _cue_time(s):
    """'mm:ss:ff' (ff — кадры, 75/с) -> секунды (float) или None."""
    m = re.match(r"\s*(\d+):(\d+):(\d+)", str(s or ""))
    if not m:
        return None
    return int(m.group(1)) * 60 + int(m.group(2)) + int(m.group(3)) / 75.0


def _cue_value(s):
    """Значение после команды: снимаем кавычки, если есть."""
    s = s.strip()
    m = re.match(r'"(.*)"', s)
    return m.group(1).strip() if m else s


def parse_cue(path):
    """CUE -> (album_title, album_artist, [track]). До первого TRACK команды
    относятся к альбому, после — к текущему треку.
    track: {'no','title','artist','file','start'(сек)}."""
    album_title = album_artist = cur_file = ""
    tracks, cur = [], None
    for raw in _read_text(path).splitlines():
        line = raw.strip()
        if not line:
            continue
        up = line.upper()
        if up.startswith("FILE "):
            m = re.match(r'FILE\s+"?(.*?)"?\s+\w+\s*$', line, re.I)
            cur_file = m.group(1).strip() if m else ""
        elif up.startswith("TRACK "):
            parts = line.split()
            cur = {"no": _num(parts[1]) if len(parts) > 1 else len(tracks) + 1,
                   "title": "", "artist": "", "file": cur_file, "start": None}
            tracks.append(cur)
        elif up.startswith("TITLE "):
            val = _cue_value(line[6:])
            if cur is None:
                album_title = val
            else:
                cur["title"] = val
        elif up.startswith("PERFORMER "):
            val = _cue_value(line[10:])
            if cur is None:
                album_artist = val
            else:
                cur["artist"] = val
        elif up.startswith("INDEX ") and cur is not None:
            parts = line.split()
            if len(parts) >= 3:
                idx, t = _num(parts[1]), _cue_time(parts[2])
                # INDEX 01 — начало звука; INDEX 00 (pre-gap) берём лишь как запасной
                if idx == 1 or cur["start"] is None:
                    cur["start"] = t
    return album_title, album_artist, tracks


def _resolve(folder, name):
    """Имя файла из CUE -> путь в папке (с учётом регистра/обратных слешей)."""
    if not name:
        return ""
    name = name.replace("\\", "/").split("/")[-1]
    p = os.path.join(folder, name)
    if os.path.isfile(p):
        return p
    low = name.lower()
    for f in os.listdir(folder):
        if f.lower() == low:
            return os.path.join(folder, f)
    return ""


def _scan_cue(folder, cue_path):
    """Папка с CUE -> итоговый dict, либо None если разобрать не удалось."""
    album_title, album_artist, tracks = parse_cue(cue_path)
    if not tracks:
        return None

    # Длительности файлов-образов и обложка из первого существующего файла.
    file_len, cover_bytes = {}, None
    for t in tracks:
        f = t["file"]
        if f and f not in file_len:
            fpath = _resolve(folder, f)
            length = 0
            if fpath:
                try:
                    au = MutagenFile(fpath)
                    length = getattr(getattr(au, "info", None), "length", 0) or 0
                    if cover_bytes is None:
                        cover_bytes = _embedded_cover(au, fpath)
                except Exception:
                    length = 0
            file_len[f] = length

    entries, n = [], len(tracks)
    for i, t in enumerate(tracks):
        start = t["start"] or 0
        nxt = tracks[i + 1] if i + 1 < n else None
        if nxt and nxt["file"] == t["file"] and nxt["start"] is not None:
            dur = (nxt["start"] or 0) - start          # внутри одного образа
        else:
            dur = (file_len.get(t["file"], 0) or 0) - start   # последний в файле
        entries.append({
            "name": t["title"] or ("Трек %02d" % t["no"]),
            "artist": t["artist"] or album_artist,
            "dur": int(round(dur)) if dur and dur > 0 else 0,
        })
    return _build_result(folder, entries, cover_bytes, album_title)


def scan_folder(folder):
    """Папка -> {'title', 'tracks':[{'name','dur'}], 'cover': data-url}.

    Если в папке есть CUE-лист — треклист берём из него (часто рядом лежит один
    образ FLAC/APE/WAV). Иначе сканируем аудиофайлы: сортировка по (диск, № трека),
    при отсутствии номеров — натурально по имени файла. title — имя папки. При
    одном исполнителе имя трека — только песня, при разных — 'Исполнитель - Песня'.
    """
    folder = (folder or "").strip().rstrip("/\\")
    if not folder or not os.path.isdir(folder):
        raise ValueError("папка не найдена")

    cues = sorted(f for f in os.listdir(folder)
                  if os.path.isfile(os.path.join(folder, f)) and f.lower().endswith(".cue"))
    if cues:
        res = _scan_cue(folder, os.path.join(folder, cues[0]))
        if res:
            return res

    entries, artists, albums, years = [], {}, {}, {}
    cover_bytes = None

    for fn in sorted(os.listdir(folder)):
        path = os.path.join(folder, fn)
        if not os.path.isfile(path) or os.path.splitext(fn)[1].lower() not in AUDIO_EXTS:
            continue
        try:
            audio = MutagenFile(path)
            easy = MutagenFile(path, easy=True)
        except Exception:
            continue
        if audio is None:
            continue

        dur = int(round(getattr(getattr(audio, "info", None), "length", 0) or 0))
        title = _tag_first(easy, audio, "title") or _strip_leading_num(os.path.splitext(fn)[0])
        artist = _tag_first(easy, audio, "albumartist", "artist", "composer")
        album = _tag_first(easy, audio, "album")
        year = _year(_tag_first(easy, audio, "date", "originaldate", "year"))
        disc = _num(_tag_first(easy, audio, "discnumber")) or 1
        tno = _num(_tag_first(easy, audio, "tracknumber"))

        if artist:
            artists[artist] = artists.get(artist, 0) + 1
        if album:
            albums[album] = albums.get(album, 0) + 1
        if year:
            years[year] = years.get(year, 0) + 1
        if cover_bytes is None:
            cover_bytes = _embedded_cover(audio, path)

        entries.append({"disc": disc, "tno": tno, "fn": fn, "name": title,
                        "artist": artist, "dur": dur})

    if not entries:
        raise ValueError("в папке нет распознанных аудиофайлов")

    if any(e["tno"] for e in entries):
        entries.sort(key=lambda e: (e["disc"], e["tno"] or 9999, _natkey(e["fn"])))
    else:
        entries.sort(key=lambda e: _natkey(e["fn"]))

    top = lambda d: max(d, key=d.get) if d else ""
    return _build_result(folder, entries, cover_bytes, top(albums))
