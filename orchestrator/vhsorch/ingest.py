"""Ingest: Watch-Ordner /data/raw sicher gegen halb-hochgeladene Dateien.

Ein Clip wird erst in die Queue aufgenommen, wenn seine Größe über
STABLE_CHECKS aufeinanderfolgende Polls stabil bleibt (kein wachsendes File
mehr). Alternativ: der Nutzer legt Dateien fertig per `mv` aus einem Staging-
Verzeichnis ab — dann ist die Größe sofort stabil.
"""
from __future__ import annotations

import os
import subprocess
from typing import Dict, Optional

from .db import DB

VIDEO_EXT = {".mp4", ".mkv", ".avi", ".mov", ".m2ts", ".ts", ".mpg", ".mpeg", ".webm"}


def probe_frames(path: str) -> Optional[int]:
    """Frame-Anzahl eines Videos via ffprobe — NUR aus dem Container-Index
    (nb_frames, bei MP4 instant), Fallback Dauer x fps. Bewusst KEIN
    -count_packets: das liest die ganze Datei (bei WD-Red-Volumen ewig).

    ACHTUNG ffprobe-Falle: die CSV-Ausgabe hält sich NICHT an die angefragte
    Feld-Reihenfolge -> key=value (-of default=nw=1) parsen. None = Messung
    fehlgeschlagen (kein ffprobe / kaputte Datei).
    """
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=nb_frames,avg_frame_rate,duration",
             "-of", "default=nw=1", path],
            capture_output=True, text=True, timeout=60)
        kv = dict(l.split("=", 1) for l in r.stdout.splitlines() if "=" in l)
        if kv.get("nb_frames", "").isdigit():
            return int(kv["nb_frames"])
        num, den = (kv.get("avg_frame_rate", "0/1").split("/") + ["1"])[:2]
        n = int(float(kv.get("duration", "0")) * float(num) / float(den or 1))
        return n if n > 0 else None
    except Exception:  # noqa: BLE001 — Messung ist optional, nie crashen
        return None


class Ingest:
    def __init__(self, db: DB, raw_dir: str, stable_checks: int):
        self.db = db
        self.raw_dir = raw_dir
        self.stable_checks = max(1, stable_checks)
        # name -> (letzte_größe, wie_oft_gleich)
        self._seen: Dict[str, tuple[int, int]] = {}
        # Dateien, für die wir schon eine Stamm-Kollision gemeldet haben (nur 1x).
        self._warned_collisions: set[str] = set()

    def scan(self) -> int:
        """Nimmt neu-stabile Clips in die Queue auf. Gibt Anzahl neuer Clips."""
        if not os.path.isdir(self.raw_dir):
            return 0
        # Bekannte Stämme (Dateiname ohne Endung): das Node-Ergebnis heißt
        # <stamm>.mp4, also würden zwei Rohclips mit gleichem Stamm (z. B.
        # tape.avi + tape.mp4) auf DIESELBE Ergebnisdatei zeigen -> stiller
        # Datenverlust. Zweiten gleich-stämmigen Clip ablehnen.
        existing_stems = {os.path.splitext(n)[0] for n in self.db.all_clip_names()}
        added = 0
        for entry in os.scandir(self.raw_dir):
            if not entry.is_file():
                continue
            if os.path.splitext(entry.name)[1].lower() not in VIDEO_EXT:
                continue
            if self.db.has_clip(entry.name):
                continue
            stem = os.path.splitext(entry.name)[0]
            if stem in existing_stems:
                if entry.name not in self._warned_collisions:
                    print(f"[ingest] WARNUNG: '{entry.name}' hat denselben Stamm wie ein "
                          f"bereits eingereihter Clip -> abgelehnt (gleicher Final-Name).",
                          flush=True)
                    self._warned_collisions.add(entry.name)
                continue

            size = entry.stat().st_size
            last_size, stable = self._seen.get(entry.name, (-1, 0))
            if size == last_size and size > 0:
                stable += 1
            else:
                stable = 0
            self._seen[entry.name] = (size, stable)

            # +1, weil der erste "gleich"-Vergleich erst beim zweiten Poll zählt.
            if stable + 1 >= self.stable_checks:
                if self.db.add_clip(entry.name, size):
                    added += 1
                    existing_stems.add(stem)   # sperrt Kollisionen im selben Scan
                    self._seen.pop(entry.name, None)
        return added
