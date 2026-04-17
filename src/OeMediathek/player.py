# -*- coding: utf-8 -*-
# player.py
# Startet einen Stream im angepassten Enigma2-Mediaplayer

import os
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


_ORF_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"


def _has_serviceapp():
    return os.path.exists("/usr/lib/enigma2/python/Plugins/SystemPlugins/ServiceApp")


def play_stream(session, stream_url, title="ÖR Mediathek"):
    """
    Spielt eine URL im eigenen, angepassten Enigma2-Player ab.
    Nutzt standardmaessig 4097 (GStreamer). Nur bei ORF-Streams wird,
    falls verfuegbar, auf 5002 (exteplayer3) gewechselt.
    """

    if isinstance(stream_url, bytes):
        stream_url_str = stream_url.decode("utf-8", "replace")
    else:
        stream_url_str = str(stream_url)

    is_orf = "apasfiis.sf.apa.at" in stream_url_str
    if is_orf and "#" not in stream_url_str:
        stream_url_str = stream_url_str + "#User-Agent=" + _ORF_USER_AGENT

    stream_url_bytes = stream_url_str
    title_bytes = title.decode("utf-8", "replace") if isinstance(title, bytes) else str(title)

    player_id = 4097
    if is_orf and _has_serviceapp():
        player_id = 5002

    ref = eServiceReference(player_id, 0, stream_url_bytes)
    ref.setName(title_bytes)
    session.open(OeStreamPlayer, ref)
