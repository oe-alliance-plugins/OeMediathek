# -*- coding: utf-8 -*-
# player.py
# Startet einen Stream im angepassten Enigma2-Mediaplayer

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


def play_stream(session, stream_url, title="ÖR Mediathek"):
    """
    Spielt eine m3u8-URL im eigenen, angepassten Enigma2-Player ab.
    """

    if isinstance(stream_url, bytes):
        stream_url_str = stream_url.decode('utf-8', 'replace')
    else:
        stream_url_str = stream_url

    stream_url_bytes = stream_url_str
    title_bytes = title.decode('utf-8', 'replace') if isinstance(title, bytes) else str(title)

    # Enigma2 Service-Referenz für HLS/m3u8
    # Typ 4097 = gstreamer / externer Player
    ref = eServiceReference(4097, 0, stream_url_bytes)
    ref.setName(title_bytes)

    # Hier wird der erstellte Player aufgerufen
    session.open(OeStreamPlayer, ref)