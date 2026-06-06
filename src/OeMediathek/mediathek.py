# -*- coding: utf-8 -*-
# mediathek.py
# Holt Sendungslisten über MediathekViewWeb-API (aggregiert alle ÖR-Sender)

import json
import io
import os
import threading
import time

from urllib.request import urlopen, Request

try:
    import ssl
    _ssl_context = ssl._create_unverified_context()
except Exception:
    _ssl_context = None

LOG_FILE = "/tmp/oemediathek.log"
FAVORITES_FILE = "/etc/enigma2/oemediathek_favorites.json"
EPISODE_FAVORITES_FILE = "/etc/enigma2/oemediathek_episode_favorites.json"
WATCHED_FILE = "/etc/enigma2/oemediathek_watched.json"
SEARCH_HISTORY_FILE = "/etc/enigma2/oemediathek_search_history.json"
SEARCH_HISTORY_MAX = 10
DEBUG = False

# Bekannte Sendernamen fuer die Favoriten-Bereinigung (Duplikat zu CHANNEL_MAP in plugin.py,
# aber mediathek.py soll ohne plugin.py lauffaehig bleiben).
_KNOWN_CHANNELS = {
    "ARD", "ZDF", "ARTE", "3Sat", "NDR", "WDR", "BR", "MDR", "HR", "SWR",
    "RBB", "SR", "ZDFinfo", "ZDFneo", "KiKA", "PHOENIX",
    "Radio Bremen TV", "Funk.net", "ARD-alpha", "ONE", "tagesschau24", "DW",
}

# Generische Film/Doku-Container: topic ist nur ein Genre-Label, Titel direkt als Gruppe nutzen.
_FILM_TOPICS = {
    # Spielfilme
    "film", "filme", "spielfilm", "spielfilme", "kinofilm", "kino",
    "tv-film", "fernsehfilm", "fernsehfilme", "maerchenfilm", "kurzfilm",
    "film-highlights", "film-klassiker",
    # Dokumentationen & Reportagen
    "dokumentation", "dokumentationen", "dokumentarfilm", "doku", "dokis",
    "reportage", "reportagen", "feature",
}


def _decode_bytes(data):
    """Decode API bytes robustly. MVW has sent Latin-1/CP1252 JSON despite UTF-8 headers."""
    if data is None:
        return ""
    if not isinstance(data, bytes):
        return str(data)
    for enc in ("utf-8", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            pass
        except Exception:
            pass
    return data.decode("utf-8", "replace")


def _repair_mojibake(text):
    """Repair common UTF-8-as-Latin-1 mojibake such as 'MÃ¼nchen'."""
    if not text:
        return ""
    markers = ("Ã", "Â", "â€", "â€™", "â€œ", "â€ž", "â€“", "â€”")
    if not any(m in text for m in markers):
        return text
    fixed = None
    for enc in ("latin-1", "cp1252"):
        try:
            fixed = text.encode(enc).decode("utf-8")
            break
        except Exception:
            pass
    if fixed is None:
        return text
    old_hits = sum(text.count(m) for m in markers)
    new_hits = sum(fixed.count(m) for m in markers)
    if new_hits < old_hits and "�" not in fixed:
        return fixed
    return text


def _s(val):
    """Gibt val als nativen Text-String zurück (Python 3 / Enigma2)."""
    if val is None:
        return ""
    if isinstance(val, bytes):
        return _decode_bytes(val)
    return _repair_mojibake(str(val))


def _valid_stream_url(url):
    """Return a playable URL or an empty string for API placeholders such as 'Offline'."""
    url = _s(url).strip()
    if not url:
        return ""
    if url.lower() in ("offline", "null", "none", "false", "n/a", "-", ""):
        return ""
    if not url.startswith(("http://", "https://", "rtmp://", "rtsp://", "file://")):
        return ""
    return url


def _log(msg):
    if not DEBUG:
        return
    line = "[OeMediathek] " + str(msg)
    print(line)
    try:
        with io.open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ------------------------------------------------------------------
# MediathekViewWeb-API
# POST https://mediathekviewweb.de/api/query
# ------------------------------------------------------------------
def _mvw_query(channel=None, size=100, offset=0, search_term=None, min_duration=0, sort_by="timestamp", search_fields=None):
    """
    Fragt die MediathekViewWeb-API ab.
    search_fields: Liste der Felder fuer die Suche, Standard ["title", "topic"]
    sort_by: "timestamp" | "duration" | "topic" (alle API-seitig)
    """
    url = "https://mediathekviewweb.de/api/query"

    queries = []
    if channel:
        queries.append({"fields": ["channel"], "query": channel})

    if search_term:
        fields = search_fields if search_fields else ["title", "topic"]
        queries.append({"fields": fields, "query": search_term})

    if sort_by in ("topic", "title"):
        api_sort, sort_order = sort_by, "asc"
    elif sort_by in ("timestamp", "duration"):
        api_sort, sort_order = sort_by, "desc"
    else:
        api_sort, sort_order = "timestamp", "desc"

    body_dict = {
        "queries": queries,
        "sortBy": api_sort,
        "sortOrder": sort_order,
        "future": False,
        "offset": offset,
        "size": size,
    }
    if min_duration > 0:
        body_dict["duration_min"] = min_duration

    body = json.dumps(body_dict)

    if isinstance(body, str):
        body = body.encode("utf-8")

    req = Request(url, data=body)
    req.add_header("Content-Type", "application/json")

    _log("MVW Abruf channel=%s offset=%d size=%d search=%s" % (channel, offset, size, search_term))
    try:
        resp = urlopen(req, timeout=15, context=_ssl_context) if _ssl_context else urlopen(req, timeout=15)
        try:
            _log("MVW HTTP %s" % resp.getcode())
            payload = _decode_bytes(resp.read()).lstrip("﻿")
            data = json.loads(payload)
        finally:
            try:
                resp.close()
            except Exception:
                pass
    except Exception as e:
        _log("MVW Fehler: " + str(e))
        raise

    results_raw = data.get("result", {}).get("results", [])
    total_results = data.get("result", {}).get("queryInfo", {}).get("totalResults", 0)
    try:
        total_results = int(total_results)
    except Exception:
        total_results = 0
    raw_count = len(results_raw)
    _log("MVW %d Ergebnisse (gesamt: %d)" % (raw_count, total_results))

    # Sender die in DE nicht verfuegbar sind ausblenden (nur bei "Alle Mediatheken").
    # Hinweis: Die API hat keinen Exclude-Parameter. Wenn eine Seite viele geblockte
    # Eintraege enthaelt, kann die zurueckgegebene Liste kuerzer als "size" sein.
    # Das ist ein bekanntes, akzeptiertes Verhalten ohne saubere Gegenmassnahme.
    blocked = {"ORF", "SRF"} if not channel else set()

    items = []
    for entry in results_raw:
        ch = _s(entry.get("channel", ""))
        topic = _s(entry.get("topic", ""))
        title = _s(entry.get("title", ""))
        timestamp = entry.get("timestamp", 0)

        if ch in blocked:
            continue

        # Arte: nur deutschsprachige Inhalte (ARTE.DE) behalten.
        # Die API liefert auch ARTE.FR, ARTE.IT etc. bei channel="ARTE"-Abfragen.
        if ch.upper().startswith("ARTE") and ch.upper() != "ARTE.DE":
            continue

        # HD und SD getrennt auslesen
        # ORF: Q8C (HD) seit 2026-06 auf "video_not_available"-Tafel umgeleitet — kein HD anbieten
        url_hd     = "" if ch.upper() == "ORF" else (entry.get("url_video_hd") or "")
        url_sd = _valid_stream_url(entry.get("url_video") or "")

        desc = _s(entry.get("description", ""))
        duration = entry.get("duration", 0)

        # API kuerzt Beschreibungen mit "\n....." — nur das API-Artefakt entfernen,
        # echte Satzpunkte aber in Ruhe lassen.
        if desc:
            desc = desc.rstrip()
            # "\n....." am Ende ist ein API-Kuerzel fuer abgeschnittenen Text
            while desc.endswith("\n.") or desc.endswith("\n..") or desc.endswith("\n...") \
                    or desc.endswith("\n....") or desc.endswith("\n....."):
                desc = desc.rsplit("\n", 1)[0].rstrip()
            if len(_s(entry.get("description", ""))) >= 400:
                desc = desc + " ..."

        # Überspringen, wenn gar kein Stream vorhanden ist
        if not title or (not url_hd and not url_sd):
            continue

        # Audiodeskriptions- und Gebaerdensprach-Fassungen ausblenden
        if title.endswith("(Audiodeskription)") or title.endswith("(Gebärdensprache)") or title.endswith("(ÖGS)"):
            continue

        try:
            duration = int(duration)
        except Exception:
            duration = 0

        if duration > 0:
            m, s = divmod(duration, 60)
            h, m = divmod(m, 60)
            if h > 0:
                duration_str = "%d:%02d Std." % (h, m)
            else:
                duration_str = "%d Min." % m
        else:
            duration_str = "Unbekannt"

        if not desc:
            desc = "Keine Beschreibung verfügbar."

        # Generische Film-Container aufbrechen: topic ist ein Sammelbehaelter
        # (z.B. "Filme", "Spielfilm") → Filmtitel direkt als Gruppenname verwenden.
        topic_lower = topic.lower() if topic else ""
        is_film_container = topic_lower in _FILM_TOPICS

        if is_film_container:
            group_key = title if channel else ch + ": " + title
        elif topic and topic != title:
            group_key = topic if channel else ch + ": " + topic
        else:
            group_key = title if channel else ch + ": " + title

        _log("URL [%s] HD=%s SD=%s" % (ch, url_hd if url_hd else "-", url_sd if url_sd else "-"))
        try:
            ts = int(timestamp)
        except Exception:
            ts = 0

        items.append({
            "title": _s(title),
            "group": _s(group_key),
            "channel": _s(ch),
            "stream_url_hd": _s(url_hd),
            "stream_url_sd": _s(url_sd),
            "description": _s(desc),
            "duration": _s(duration_str),
            "timestamp": ts,
        })

    _log("MVW %d Sendungen verarbeitet" % len(items))
    return items, total_results, raw_count


# ------------------------------------------------------------------
# Topics-Endpunkt
# ------------------------------------------------------------------
def get_topics(channel=None):
    """
    Gibt alle Topics (Sendungsnamen) zurueck.
    Optional gefiltert nach Sender (channel="ARD" etc.).
    Liefert eine alphabetisch sortierte Liste von Unicode-Strings.
    """
    url = "https://mediathekviewweb.de/api/topics"
    if channel:
        url += "?channel=" + channel
    try:
        req = Request(url)
        req.add_header("User-Agent", "Mozilla/5.0")
        resp = urlopen(req, timeout=15, context=_ssl_context) if _ssl_context else urlopen(req, timeout=15)
        try:
            data = json.loads(_decode_bytes(resp.read()).lstrip("﻿"))
        finally:
            try:
                resp.close()
            except Exception:
                pass
        topics = data.get("topics", [])
        _log("get_topics channel=%s -> %d Topics" % (channel, len(topics)))
        return topics
    except Exception as e:
        _log("get_topics Fehler: " + str(e))
        return []


# ------------------------------------------------------------------
# Sender-spezifische Funktionen
# ------------------------------------------------------------------
def get_ard_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ARD", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_zdf_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ZDF", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_arte_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ARTE", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_3sat_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("3Sat", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_ndr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("NDR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_wdr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("WDR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_br_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("BR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_mdr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("MDR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_hr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("HR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_swr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("SWR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_rbb_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("RBB", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_sr_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("SR", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_zdfinfo_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ZDFinfo", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_zdfneo_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ZDFneo", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_kika_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("KiKA", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_phoenix_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("PHOENIX", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_radio_bremen_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("Radio Bremen TV", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_funk_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("Funk.net", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_ard_alpha_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ARD-alpha", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_one_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ONE", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_tagesschau24_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("tagesschau24", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_dw_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("DW", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_orf_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("ORF", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_srf_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query("SRF", size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


def get_all_highlights(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    return _mvw_query(channel=None, size=size, offset=offset, search_term=search_term, min_duration=min_duration, sort_by=sort_by)


# ------------------------------------------------------------------
# Favoritenverwaltung
# Gespeichert als JSON: Liste von {"group": "...", "channel": "..."}
# ------------------------------------------------------------------
_SV_SN_NAMES = {">> Sendung verpasst?", ">> Demn\u00e4chst"}


def _load_favorites_raw():
    try:
        if os.path.exists(FAVORITES_FILE):
            with io.open(FAVORITES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    # Sondereintraege (SV/SN) bereinigen falls versehentlich gespeichert
                    cleaned = [e for e in data if e.get("group") not in _SV_SN_NAMES]
                    if len(cleaned) != len(data):
                        save_favorites(cleaned)
                    return cleaned
    except Exception:
        pass
    return []


def save_favorites(favorites_raw):
    """favorites_raw: list of {"group": str, "channel": str}."""
    try:
        with io.open(FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(favorites_raw, f, ensure_ascii=False)
    except Exception as e:
        _log("Favoriten speichern Fehler: " + str(e))


def reorder_favorites(group_values):
    """Speichert Favoriten in neuer Reihenfolge."""
    favs_raw = _load_favorites_raw()
    name_to_fav = {}
    for f in favs_raw:
        name_to_fav[f.get("group", "")] = f
    reordered = []
    for gb in group_values:
        g = _s(gb)
        if g in name_to_fav:
            reordered.append(name_to_fav[g])
    save_favorites(reordered)


def add_favorite(group_value, channel_value):
    """Fuegt eine Gruppe zu den Favoriten hinzu (Duplikate werden ignoriert)."""
    group = _s(group_value)
    channel = _s(channel_value)

    favs = _load_favorites_raw()
    for f in favs:
        if f.get("group") == group:
            return  # bereits vorhanden
    favs.append({"group": group, "channel": channel})
    save_favorites(favs)
    _log("Favorit hinzugefuegt: " + group)


def remove_favorite(group_value):
    """Entfernt eine Gruppe aus den Favoriten."""
    group = _s(group_value)

    favs = _load_favorites_raw()
    favs = [f for f in favs if f.get("group") != group]
    save_favorites(favs)
    _log("Favorit entfernt: " + group)


def is_favorite(group_value):
    group = _s(group_value)
    return any(f.get("group") == group for f in _load_favorites_raw())


def _load_watched():
    try:
        if os.path.exists(WATCHED_FILE):
            with io.open(WATCHED_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return set(data)
    except Exception:
        pass
    return set()


def _save_watched(watched_set):
    try:
        with io.open(WATCHED_FILE, "w", encoding="utf-8") as f:
            json.dump(list(watched_set), f, ensure_ascii=False)
    except Exception as e:
        _log("Watched speichern Fehler: " + str(e))


def is_watched(url_value):
    url = _s(url_value)
    return url in _load_watched()


def toggle_watched(url_value):
    url = _s(url_value)
    watched = _load_watched()
    if url in watched:
        watched.discard(url)
        _log("Watched entfernt: " + url)
    else:
        watched.add(url)
        _log("Watched markiert: " + url)
    _save_watched(watched)


_episode_favorites_cache = None


def _load_episode_favorites():
    global _episode_favorites_cache
    if _episode_favorites_cache is not None:
        return _episode_favorites_cache
    try:
        if os.path.exists(EPISODE_FAVORITES_FILE):
            with io.open(EPISODE_FAVORITES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    _episode_favorites_cache = data
                    return _episode_favorites_cache
    except Exception:
        pass
    _episode_favorites_cache = []
    return _episode_favorites_cache


def _save_episode_favorites(items):
    global _episode_favorites_cache
    _episode_favorites_cache = items
    try:
        with io.open(EPISODE_FAVORITES_FILE, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False)
    except Exception as e:
        _log("Episode-Favoriten speichern Fehler: " + str(e))


def _item_to_text(item):
    """Convert all bytes values in an item dict to text for JSON."""
    result = {}
    for k, v in item.items():
        if isinstance(v, bytes):
            result[_s(k)] = _s(v)
        else:
            result[_s(k)] = v
    return result


def is_episode_favorite(url_value):
    url = _s(url_value)
    return any(e.get("stream_url_hd") == url or e.get("stream_url_sd") == url
               for e in _load_episode_favorites())


def add_episode_favorite(item):
    """Save one episode favorite as JSON-safe text."""
    item_u = _item_to_text(item)
    url = item_u.get("stream_url_hd") or item_u.get("stream_url_sd") or ""
    if not url:
        return
    favs = _load_episode_favorites()
    if any(e.get("stream_url_hd") == url or e.get("stream_url_sd") == url for e in favs):
        return
    favs.insert(0, item_u)
    _save_episode_favorites(favs)
    _log("Episode-Favorit hinzugefuegt: " + url)


def remove_episode_favorite(url_value):
    url = _s(url_value)
    favs = _load_episode_favorites()
    favs = [e for e in favs if e.get("stream_url_hd") != url and e.get("stream_url_sd") != url]
    _save_episode_favorites(favs)
    _log("Episode-Favorit entfernt: " + url)


def get_episode_favorites():
    """Return episode favorites as native Python 3 text dicts."""
    return [_item_to_text(e) for e in _load_episode_favorites()]


def get_favorites(offset=0, size=100, search_term=None, min_duration=0, sort_by="timestamp"):
    """
    Laedt alle Favoriten-Gruppen frisch aus der API.
    Nutzt die serverseitige Suche, um alle Folgen einer Serie zuverlaessig zu finden.
    """
    favs = _load_favorites_raw()
    if not favs:
        return [], 0, 0

    results = [None] * len(favs)
    _sem = threading.Semaphore(3)

    def _fetch_one(idx, fav):
        channel = fav.get("channel") or None
        group = fav.get("group", "")

        # Den reinen Sendungsnamen extrahieren fuer die API Suche.
        # Nur entfernen wenn der Teil vor ": " wirklich ein bekannter Sendername ist —
        # sonst wuerde z.B. "Tatort: Borowski..." faelschlich zu "Borowski..." gekuerzt.
        pure_topic = group
        if channel and group.startswith(channel + ": "):
            pure_topic = group[len(channel) + 2:]
        elif ": " in group:
            prefix = group.split(": ", 1)[0]
            if prefix in _KNOWN_CHANNELS:
                pure_topic = group.split(": ", 1)[1]

        matched = []
        with _sem:
            try:
                # Hole gezielt bis zu 100 Folgen genau dieser Serie.
                # ["title", "topic"] statt nur ["topic"], damit Film-Favoriten gefunden
                # werden: bei Film-Containern (topic="Spielfilm" o.ae.) ist der
                # group_key der Filmtitel, der nur im title-Feld der API steht.
                # Das lokale Exakt-Filter (item_group_str == group) verhindert Fehlzuordnungen.
                items, _, _rc = _mvw_query(
                    channel=channel,
                    size=100,
                    offset=0,
                    search_term=pure_topic,
                    min_duration=min_duration,
                    sort_by=sort_by,
                    search_fields=["title", "topic"],
                )
                # Lokal auf die exakte Gruppe filtern, um unscharfen Beifang auszublenden.
                # Vergleich normalisiert: "BR: Schnittgut" gespeichert von "Alle" passt auch
                # auf group_key "Schnittgut" aus der channel-spezifischen Abfrage.
                for item in items:
                    item_group = item.get("group", "")
                    item_group_str = _s(item_group)
                    # Direkte Übereinstimmung
                    if item_group_str == group:
                        matched.append(item)
                        continue
                    # Fallback: gespeicherte Gruppe hat Sender-Prefix, item_group nicht
                    # z.B. group="BR: Schnittgut", item_group_str="Schnittgut"
                    if ": " in group:
                        group_suffix = group.split(": ", 1)[1]
                        if item_group_str == group_suffix:
                            # group-Feld auf den gespeicherten Namen normalisieren,
                            # damit is_favorite(gname) in der Favoriten-Ansicht korrekt matcht
                            item = dict(item)
                            item["group"] = group
                            matched.append(item)
            except Exception as e:
                _log("Favorit laden Fehler (%s): %s" % (group, str(e)))
        results[idx] = matched

    threads = [threading.Thread(target=_fetch_one, args=(i, fav)) for i, fav in enumerate(favs)]
    for t in threads:
        t.daemon = True
        t.start()
    deadline = time.time() + 60
    for t in threads:
        remaining = deadline - time.time()
        if remaining > 0:
            t.join(timeout=remaining)

    all_items = []
    for r in results:
        if r:
            all_items.extend(r)

    # Kein Paging ueber alle Favoriten-Items — jede Gruppe hat bereits max. 100 Eintraege,
    # und die Gesamtzahl bleibt ueberschaubar. offset/size gelten nur fuer den API-Abruf
    # pro Gruppe (dort unveraendert), nicht fuer die zusammengefuehrte Ergebnisliste.
    return all_items, len(all_items), len(all_items)


# ------------------------------------------------------------------
# Suchverlauf
# ------------------------------------------------------------------
def load_search_history():
    """Gibt die gespeicherte Suchliste zurueck (neueste zuerst)."""
    try:
        if os.path.exists(SEARCH_HISTORY_FILE):
            with io.open(SEARCH_HISTORY_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [e for e in data if isinstance(e, str) and e]
    except Exception:
        pass
    return []


def save_search_history(term):
    """Fuegt einen Suchbegriff vorne ein, entfernt Duplikate und kuerzt die Liste."""
    try:
        term = _s(term).strip()
        if not term:
            return
        history = [e for e in load_search_history() if e != term]
        history.insert(0, term)
        history = history[:SEARCH_HISTORY_MAX]
        with io.open(SEARCH_HISTORY_FILE, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False)
    except Exception as e:
        _log("Suchverlauf speichern Fehler: " + str(e))
