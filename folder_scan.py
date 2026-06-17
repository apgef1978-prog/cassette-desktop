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
              ".wav", ".aiff", ".aif", ".ape", ".wv")
IMG_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")
# характерные имена файлов-обложек рядом с треками
COVER_STEMS = ("cover", "folder", "front", "albumart", "album", "обложка")


def _first(tag, *keys):
    """Первое непустое значение тега по одному из ключей (теги — списки)."""
    if tag is None:
        return ""
    for k in keys:
        v = tag.get(k)
        if v:
            return str(v[0] if isinstance(v, (list, tuple)) else v).strip()
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


def scan_folder(folder):
    """Папка -> {'title', 'tracks':[{'name','dur'}], 'cover': data-url}.

    Треки сортируются по (диск, № трека); если номеров нет — натурально по имени
    файла. title собирается как 'Исполнитель - Альбом (Год)' по самым частым тегам.
    """
    folder = (folder or "").strip().rstrip("/\\")
    if not folder or not os.path.isdir(folder):
        raise ValueError("папка не найдена")

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
        tag = easy if easy is not None else audio

        dur = int(round(getattr(getattr(audio, "info", None), "length", 0) or 0))
        title = _first(tag, "title") or _strip_leading_num(os.path.splitext(fn)[0])
        artist = _first(tag, "albumartist", "artist", "composer")
        album = _first(tag, "album")
        year = _year(_first(tag, "date", "originaldate", "year"))
        disc = _num(_first(tag, "discnumber")) or 1
        tno = _num(_first(tag, "tracknumber"))

        if artist:
            artists[artist] = artists.get(artist, 0) + 1
        if album:
            albums[album] = albums.get(album, 0) + 1
        if year:
            years[year] = years.get(year, 0) + 1
        if cover_bytes is None:
            cover_bytes = _embedded_cover(audio, path)

        entries.append({"disc": disc, "tno": tno, "fn": fn, "name": title, "dur": dur})

    if not entries:
        raise ValueError("в папке нет распознанных аудиофайлов")

    if any(e["tno"] for e in entries):
        entries.sort(key=lambda e: (e["disc"], e["tno"] or 9999, _natkey(e["fn"])))
    else:
        entries.sort(key=lambda e: _natkey(e["fn"]))

    if cover_bytes is None:
        cf = _folder_cover(folder)
        if cf:
            try:
                with open(cf, "rb") as fh:
                    cover_bytes = fh.read()
            except Exception:
                cover_bytes = None

    top = lambda d: max(d, key=d.get) if d else ""
    artist = top(artists)
    album = top(albums) or os.path.basename(folder) or "Альбом"
    year = top(years)
    title = ("%s - %s" % (artist, album)) if artist else album
    if year:
        title = "%s (%s)" % (title, year)

    return {
        "title": title,
        "tracks": [{"name": e["name"], "dur": M.fmt_dur(e["dur"])} for e in entries],
        "cover": _cover_data_url(cover_bytes),
    }
