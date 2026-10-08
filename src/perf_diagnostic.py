"""Opt-in UI timing, including AI window opening. No work titles or paths."""

from __future__ import annotations

from collections import deque
from datetime import datetime
from pathlib import Path
from time import perf_counter

from PySide6.QtCore import QEvent, QObject, QTimer
from .app_paths import logs_dir


class PerfDiagnostic(QObject):
    def __init__(self, window):
        super().__init__(window)
        self._started = perf_counter()
        self._last_heartbeat = self._started
        self._entries = deque(maxlen=10000)
        self._dropped = 0
        self._stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        self._tab_bar = window.tabs.tabBar()
        self._tab_bar.installEventFilter(self)
        self._dialog_openings = {}
        self._heartbeat = QTimer(self)
        self._heartbeat.setInterval(50)
        self._heartbeat.timeout.connect(self._heartbeat_tick)
        self._heartbeat.start()
        self.record("diagnostic.start")

    def eventFilter(self, watched, event):
        if watched in self._dialog_openings and event.type() == QEvent.Type.Paint:
            started, opening = self._dialog_openings.pop(watched)
            self.mark('ai.open.first_paint', started, opening=opening)
        if watched is not self._tab_bar:
            return False
        kinds = {
            QEvent.Type.MouseButtonPress: "mouse_press",
            QEvent.Type.MouseButtonRelease: "mouse_release",
            QEvent.Type.Wheel: "wheel",
            QEvent.Type.KeyPress: "key_press",
        }
        kind = kinds.get(event.type())
        if kind is not None:
            self.record("tab.input", kind=kind)
        return False

    def watch_dialog_open(self, dialog, started, opening):
        self._dialog_openings[dialog] = (started, opening)
        dialog.installEventFilter(self)

    def _heartbeat_tick(self):
        now = perf_counter()
        elapsed_ms = (now - self._last_heartbeat) * 1000
        self._last_heartbeat = now
        if elapsed_ms >= 90:
            self.record("event_loop.gap", elapsed_ms)

    def record(self, event: str, duration_ms: float | None = None, **fields):
        timestamp_ms = (perf_counter() - self._started) * 1000
        if len(self._entries) == self._entries.maxlen:
            self._dropped += 1
        metadata = " ".join(f"{key}={value}" for key, value in fields.items())
        duration = "" if duration_ms is None else f" duration_ms={duration_ms:.3f}"
        self._entries.append(f"{timestamp_ms:012.3f} {event}{duration}{(' ' + metadata) if metadata else ''}\n")

    def mark(self, event: str, started: float, **fields) -> float:
        now = perf_counter()
        self.record(event, (now - started) * 1000, **fields)
        return now

    def flush(self):
        self._heartbeat.stop()
        path = logs_dir() / f"PERF-27-01_{self._stamp}.log"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as output:
            output.write("# PERF-27-01 diagnostic; timings in milliseconds from launch.\n")
            output.write("# event_loop.gap is an event-loop delay, not proof of its cause.\n")
            output.write("# ai.open.* measures history, build/refresh, show, event turn and first Qt paint.\n")
            output.write("# No manga titles, paths, search terms, or item identifiers are recorded.\n")
            output.write(f"# retained={len(self._entries)} dropped={self._dropped}\n")
            output.writelines(self._entries)
        return path
