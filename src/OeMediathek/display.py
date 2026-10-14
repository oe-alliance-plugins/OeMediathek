# -*- coding: utf-8 -*-
from os.path import abspath, dirname, join

from enigma import eTimer, getDesktop

from Screens.Screen import Screen

from .downloader import load_settings, save_settings


# Some images do not provide pixmap support for their front display.
try:
	from Components.Pixmap import Pixmap
	from Tools.LoadPixmap import LoadPixmap
except ImportError:
	Pixmap = LoadPixmap = None


class DisplayHelper:
	ICON_PATH = join(dirname(abspath(__file__)), "display_icon.png")

	def getMode(self):
		return load_settings().get("lcd_icon_mode", "menu")

	def setMode(self, mode):
		settings = load_settings()
		settings["lcd_icon_mode"] = mode
		save_settings(settings)

	def getModeLabel(self):
		return {"off": "Aus", "always": "Immer", "menu": "Menü"}.get(self.getMode(), "Aus")

	def showSettings(self, screen, pickerClass):
		def modeChosen(value):
			if value is not None:
				self.setMode(value)
			screen._refresh()

		choices = [("Aus", "off"), ("Immer", "always"), ("Nur im Plugin", "menu")]
		screen.session.openWithCallback(modeChosen, pickerClass, title="Icon auf Frontdisplay:", choices=choices)

	def getIconX(self, iconWidth=220, displayWidth=400):
		position = (displayWidth - iconWidth) // 2
		try:
			with open("/proc/stb/lcd/bpp", encoding="ascii") as displayFile:
				# Nexus drivers halve the rendered X coordinate; older drivers do not.
				if displayFile.read().strip() == "10":
					position *= 2
		except OSError:
			pass
		return position

	def getSummaryClass(self, duringPlayback=False):
		try:
			if duringPlayback and self.getMode() != "always":
				return None
			desktop = getDesktop(1)
			# SWIG desktop wrappers can be falsey despite referencing a valid display.
			if desktop is not None:
				displaySize = desktop.size()
				if displaySize.width() >= 400 and displaySize.height() >= 240:
					return OeStreamPlayerSummary
		except Exception:
			pass
		return None


displayHelper = DisplayHelper()


class LcdIconSummaryMixin:
	def createSummary(self):
		return displayHelper.getSummaryClass()


class LcdPlayerSummaryMixin:
	def createSummary(self):
		summaryClass = displayHelper.getSummaryClass(duringPlayback=True)
		if summaryClass is None:
			try:
				summaryClass = super().createSummary()
			except Exception:
				pass
		return summaryClass


class OeStreamPlayerSummary(Screen):
	"""Frontdisplay summary; skins may override the existing 'icon' widget."""

	def __init__(self, session, parent):
		Screen.__init__(self, session, parent)
		self.duringPlayback = isinstance(parent, LcdPlayerSummaryMixin)
		self.visible = None
		self.skin = (
			'<screen position="0,0" size="400,240" backgroundColor="#000000">'
			f'<widget name="icon" position="{displayHelper.getIconX()},10" size="220,220" alphatest="blend" transparent="1" />'
			'</screen>'
		)
		if Pixmap is not None:
			self["icon"] = Pixmap()
			self.onLayoutFinish.append(self.setIcon)
			self.pollTimer = eTimer()
			self.pollTimer.callback.append(self.poll)
			self.pollTimer.start(1000, False)
			self.onClose.append(self.pollTimer.stop)

	def setIcon(self):
		if Pixmap is None or LoadPixmap is None:
			return
		try:
			pixmap = LoadPixmap(displayHelper.ICON_PATH)
			if pixmap is not None:
				self["icon"].instance.setPixmap(pixmap)
		except Exception:
			pass
		self.poll()

	def poll(self):
		try:
			mode = displayHelper.getMode()
			visible = mode == "always" or (mode == "menu" and not self.duringPlayback)
		except Exception:
			visible = False
		if visible != self.visible:
			self.visible = visible
			try:
				if visible:
					self["icon"].show()
				else:
					self["icon"].hide()
			except Exception:
				pass
