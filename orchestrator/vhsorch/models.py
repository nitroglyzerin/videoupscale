"""SeedVR2-Modell-Cache auf dem Orchestrator (gut angebunden), von wo die Nodes
sie per rsync bekommen. Wird beim Scheduler-Start automatisch befüllt, falls
noch nicht vorhanden — und vom CLI-Befehl `fetch-models`.
"""
from __future__ import annotations

import os
import urllib.request

from .config import Config, SEEDVR2_MODEL_FILES

_MIN_BYTES = 10_000_000   # eine echte Modelldatei ist deutlich größer als 10 MB


def models_present(cfg: Config) -> bool:
    """True, wenn alle SeedVR2-Modelle vollständig im Home-Cache liegen."""
    for name in SEEDVR2_MODEL_FILES:
        dst = os.path.join(cfg.models_dir, name)
        if not (os.path.isfile(dst) and os.path.getsize(dst) > _MIN_BYTES):
            return False
    return True


def ensure_models_cached(cfg: Config, log=print) -> bool:
    """Lädt fehlende SeedVR2-Modelle einmalig in den Home-Cache (models_dir).

    Idempotent: bereits vorhandene Dateien werden übersprungen. Lädt atomar über
    eine .part-Datei (os.replace), sodass ein Abbruch keinen halben Cache
    hinterlässt. Rückgabe: True, wenn danach alle Modelle da sind.
    """
    os.makedirs(cfg.models_dir, exist_ok=True)
    for name, url in SEEDVR2_MODEL_FILES.items():
        dst = os.path.join(cfg.models_dir, name)
        if os.path.isfile(dst) and os.path.getsize(dst) > _MIN_BYTES:
            continue
        log(f"lade Modell {name} … (einmalig, auf dem Orchestrator)")
        tmp = dst + ".part"
        try:
            urllib.request.urlretrieve(url, tmp)
            os.replace(tmp, dst)
            log(f"  fertig: {os.path.getsize(dst) // 1024 // 1024} MB")
        except Exception as e:  # noqa: BLE001
            log(f"  FEHLER beim Laden von {name}: {e}")
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False
    return models_present(cfg)
