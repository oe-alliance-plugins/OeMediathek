# -*- coding: utf-8 -*-

from gettext import bindtextdomain, dgettext, gettext

from Components.Language import language
from Tools.Directories import SCOPE_PLUGINS, resolveFilename

PluginLanguageDomain = "OeMediathek"
PluginLanguagePath = "Extensions/OeMediathek/locale"


def localeInit():
    bindtextdomain(PluginLanguageDomain, resolveFilename(SCOPE_PLUGINS, PluginLanguagePath))


def _(txt):
    if dgettext(PluginLanguageDomain, txt):
        return dgettext(PluginLanguageDomain, txt)
    else:
        print("[" + PluginLanguageDomain + "] fallback to default translation for " + str(txt))
        return gettext(txt)


localeInit()
language.addCallback(localeInit)

__version__ = "1.9.8"
