# -*- coding: utf-8 -*-
# player.py
# Startet einen Stream im angepassten Enigma2-Mediaplayer

import os
import io
import re
import threading
import time

try:
    from urllib2 import urlopen, Request as _Request
except ImportError:
    from urllib.request import urlopen, Request as _Request

try:
    from urlparse import urljoin as _urljoin
except ImportError:
    from urllib.parse import urljoin as _urljoin

from enigma import eServiceReference

try:
    from Screens.MoviePlayer import MoviePlayer
except ImportError:
    from Screens.InfoBar import MoviePlayer


class OeStreamPlayer(MoviePlayer):
    def __init__(self, session, service):
        MoviePlayer.__init__(self, session, service)
        # Nimmt den normalen MoviePlayer-Skin (für OSD/Statusleiste)
        self.skinName = ["MoviePlayer", "InfoBar"]

    def leavePlayer(self):
        # Verhindert die "Wiedergabe beenden?" Abfrage, wenn man EXIT drückt
        self.close()

    def doEofInternal(self, playing):
        # Schließt den Player sofort sauber, wenn der Stream von selbst zu Ende ist
        self.close()

    def showResumePoint(self):
        # Verhindert die Abfrage "An letzter Position fortsetzen?"
        pass


_ORF_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/120.0.0.0 Safari/537.36"
)

_TMP_DIR = "/tmp/OeMediathek"
_TMP_PLAYLIST = _TMP_DIR + "/live.m3u8"
LOG_FILE = _TMP_DIR + "/oemediathek.log"


def _decode_bytes(data):
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


def _to_text(value):
    if value is None:
        return ""
    if isinstance(value, bytes):
        return _decode_bytes(value)
    return str(value)


def _playable_url(url):
    url = _to_text(url).strip()
    if not url:
        return ""
    if url.lower() in ("offline", "null", "none", "false", "n/a", "-", ""):
        return ""
    if not url.startswith(("http://", "https://", "rtmp://", "rtsp://", "file://")):
        return ""
    return url


def _short_url(url, max_len=180):
    url = _to_text(url).strip()
    if len(url) > max_len:
        return url[:max_len] + "..."
    return url


def _log(message):
    try:
        if not os.path.isdir(_TMP_DIR):
            os.makedirs(_TMP_DIR)
        with io.open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write("[OeMediathek/player] " + _to_text(message) + "\n")
    except Exception:
        pass


def _has_serviceapp():
    return os.path.exists("/usr/lib/enigma2/python/Plugins/SystemPlugins/ServiceApp")


def _has_new_exteplayer3():
    """exteplayer3 >= v181 (feedplus) bringt eigene Libs in /usr/lib/exteplayer3_deps/."""
    return os.path.isdir("/usr/lib/exteplayer3_deps")


def _configure_serviceapp_for_live():
    """Setzt serviceapp-Einstellungen fuer synchrone HLS-Live-Streams.
    Bei exteplayer3 >= v181 wird aac_swdecoding nicht gesetzt (inkompatibel mit
    altem serviceapp.so: generiert '-a' ohne Wert, v181 erwartet '-a 0|1|2|3').
    """
    try:
        from Components.config import config
        from Plugins.SystemPlugins.ServiceApp.serviceapp_client import (
            setExtEplayer3Settings, setServiceAppSettings, OPTIONS_SERVICEEXTEPLAYER3
        )
        key = "serviceexteplayer3"
        opts = config.plugins.serviceapp.options[key]
        ext3 = config.plugins.serviceapp.exteplayer3[key]
        changed = False

        if not ext3.downmix.value:
            ext3.downmix.value = True
            ext3.downmix.save()
            changed = True
        if _has_new_exteplayer3():
            # v181+: exteplayer3's ffmpeg parst Master-Playlist inkl. EXT-X-MEDIA selbst.
            # HLS-Explorer deaktivieren damit serviceapp die URL unveraendert durchreicht.
            if opts.hls_explorer.value:
                opts.hls_explorer.value = False
                opts.hls_explorer.save()
                changed = True
        else:
            # Alte exteplayer3: HLS-Explorer an, autoselect aus (kein ABR-Stutter), AAC SW-Decode an.
            if not opts.hls_explorer.value:
                opts.hls_explorer.value = True
                opts.hls_explorer.save()
                changed = True
            if opts.autoselect_stream.value:
                opts.autoselect_stream.value = False
                opts.autoselect_stream.save()
                changed = True
            if not ext3.aac_swdecoding.value:
                ext3.aac_swdecoding.value = True
                ext3.aac_swdecoding.save()
                changed = True

        # Bei v181 aac_swdecoding=False erzwingen: altes serviceapp.so wuerde sonst
        # '-a' ohne Wert generieren (Boolean-Flag statt 0|1|2|3) -> exteplayer3 v181 haengt.
        aac_sw = False if _has_new_exteplayer3() else ext3.aac_swdecoding.value
        setExtEplayer3Settings(
            OPTIONS_SERVICEEXTEPLAYER3,
            aac_sw,
            ext3.dts_swdecoding.value,
            ext3.wma_swdecoding.value,
            ext3.lpcm_injecion.value,
            ext3.downmix.value
        )
        setServiceAppSettings(
            OPTIONS_SERVICEEXTEPLAYER3,
            opts.hls_explorer.value,
            opts.autoselect_stream.value,
            opts.connection_speed_kb.value,
            opts.autoturnon_subtitles.value
        )
        return changed
    except Exception:
        return False


def _serve_playlist_via_http(content):
    """Serve an in-memory HLS playlist for a short playback startup window."""
    try:
        try:
            from BaseHTTPServer import HTTPServer, BaseHTTPRequestHandler
        except ImportError:
            from http.server import HTTPServer, BaseHTTPRequestHandler

        data = content.encode("utf-8") if isinstance(content, str) else content

        class _Handler(BaseHTTPRequestHandler):
            def _send_headers(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()

            def do_HEAD(self):
                self._send_headers()

            def do_GET(self):
                self._send_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        server = HTTPServer(("127.0.0.1", 0), _Handler)
        server.timeout = 0.5
        port = server.server_address[1]

        def _serve():
            deadline = time.time() + 120
            try:
                while time.time() < deadline:
                    server.handle_request()
            except Exception as e:
                _log("playlist server error: " + _to_text(e))
            try:
                server.server_close()
            except Exception:
                pass

        thread = threading.Thread(target=_serve)
        thread.daemon = True
        thread.start()

        return "http://127.0.0.1:%d/live.m3u8" % port
    except Exception as e:
        _log("playlist server setup failed: " + _to_text(e))
        return None


def _build_single_quality_playlist(master_url):
    """
    Laedt die HLS-Master-Playlist, waehlt die beste Variante und gibt eine
    modifizierte Playlist zurueck, die nur diese eine Variante enthaelt
    (kein ABR-Wechsel) aber alle Audio-Tracks behaelt.
    Bei exteplayer3 >= v181 wird die Playlist per localhost-HTTP bereitgestellt,
    da file:// nicht unterstuetzt wird. Sonst wird sie nach /tmp/ geschrieben.
    Gibt master_url zurueck bei Fehler.
    """
    try:
        req = _Request(master_url)
        req.add_header("User-Agent", "Mozilla/5.0 (Linux; Android 10) AppleWebKit/537.36")
        resp = urlopen(req, timeout=4)
        content = _decode_bytes(resp.read())
        lines = content.splitlines()

        # Beste Variante (hoechste Bandbreite) finden
        best_bw = -1
        best_stream_inf = None
        best_variant = None

        i = 0
        while i < len(lines):
            line = lines[i]
            if line.startswith('#EXT-X-STREAM-INF'):
                m = re.search(r'BANDWIDTH=(\d+)', line)
                bw = int(m.group(1)) if m else 0
                for j in range(i + 1, len(lines)):
                    v = lines[j].strip()
                    if v and not v.startswith('#'):
                        if bw > best_bw:
                            best_bw = bw
                            best_stream_inf = line
                            best_variant = _urljoin(master_url, v)
                        break
            i += 1

        if not best_variant:
            return master_url

        out = ["#EXTM3U", "#EXT-X-VERSION:4", "#EXT-X-INDEPENDENT-SEGMENTS", ""]

        for line in lines:
            if line.startswith('#EXT-X-MEDIA'):
                line = re.sub(
                    r'URI="([^"]+)"',
                    lambda m: 'URI="' + _urljoin(master_url, m.group(1)) + '"',
                    line
                )
                out.append(line)

        out.append('')
        out.append(best_stream_inf)
        out.append(best_variant)
        out.append('')

        playlist = "\n".join(out)

        if _has_new_exteplayer3():
            # v181: file:// funktioniert nicht, stattdessen localhost HTTP
            http_url = _serve_playlist_via_http(playlist)
            if http_url:
                return http_url
        else:
            if not os.path.isdir(_TMP_DIR):
                os.makedirs(_TMP_DIR)
            with io.open(_TMP_PLAYLIST, "w", encoding="utf-8") as f:
                f.write(playlist)
            return "file://" + _TMP_PLAYLIST

    except Exception:
        pass
    return master_url


def _open_player(session, ref):
    # Prefer the stock MoviePlayer. Some images accept session.open() but do not
    # actually start playback with custom MoviePlayer subclasses.
    try:
        _log("opening MoviePlayer")
        session.open(MoviePlayer, ref)
        return True
    except Exception as e:
        _log("MoviePlayer open failed: " + _to_text(e))

    try:
        _log("opening OeStreamPlayer fallback")
        session.open(OeStreamPlayer, ref)
        return True
    except Exception as e:
        _log("OeStreamPlayer open failed: " + _to_text(e))

    try:
        _log("starting nav.playService fallback")
        session.nav.playService(ref)
        return True
    except Exception as e:
        _log("nav.playService failed: " + _to_text(e))

    return False


def play_stream(
    session, stream_url, title="ÖR Mediathek", force_player_id=None,
    is_live=False, autoconfigure_serviceapp=True,
):
    """
    Spielt eine URL im eigenen, angepassten Enigma2-Player ab.
    Nutzt standardmaessig 4097 (GStreamer). Nur bei ORF-Streams wird,
    falls verfuegbar, auf 5002 (exteplayer3) gewechselt.
    Bei is_live=True wird die Master-Playlist auf eine fixe Qualitaet reduziert
    und, falls autoconfigure_serviceapp=True, serviceapp fuer synchrone Wiedergabe konfiguriert.
    force_player_id erzwingt einen bestimmten Service-Typ.
    """
    stream_url_str = _playable_url(stream_url)
    title_text = _to_text(title) or "ÖR Mediathek"

    if not stream_url_str:
        _log("empty or invalid stream URL for title: " + title_text)
        return False

    try:
        is_orf = "apasfiis.sf.apa.at" in stream_url_str
        if (
            not is_live
            and "ard-mcdn.de" in stream_url_str
            and "-progressive." not in stream_url_str
            and stream_url_str.split("?")[0].endswith(".m3u8")
        ):
            is_live = True
            stream_url_str = re.sub(r"master\w+\.m3u8", "master.m3u8", stream_url_str)

        if is_orf and "#" not in stream_url_str:
            stream_url_str = stream_url_str + "#User-Agent=" + _ORF_USER_AGENT

        if is_live:
            stream_url_str = _build_single_quality_playlist(stream_url_str)

        if force_player_id is not None:
            player_id = force_player_id
        elif (is_live or is_orf) and _has_serviceapp():
            if is_live and autoconfigure_serviceapp:
                _configure_serviceapp_for_live()
            player_id = 5002
        else:
            player_id = 4097

        _log("play_stream title=%s player_id=%s url=%s" % (title_text, str(player_id), _short_url(stream_url_str)))
        ref = eServiceReference(player_id, 0, stream_url_str)
        ref.setName(title_text)
        started = _open_player(session, ref)
        _log("play_stream started=" + _to_text(started))
        return started
    except Exception as e:
        _log("play_stream failed: " + _to_text(e))

    return False
