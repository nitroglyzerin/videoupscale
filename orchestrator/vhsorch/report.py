"""Anzeige-Reports: Workmap (GPU->Video) und Video-/Kostenübersicht.

Reine Aufbereitung für die Konsole; kein State wird verändert. Beide Reports
werden vom Menü in einer Auto-Refresh-Schleife aufgerufen ("aktiv pullen").
"""
from __future__ import annotations

import os
import time

from .config import Config
from .db import DB
from .remote import Remote
from .vast import VastClient

# ANSI-Kürzel (gleiche Palette wie menu.sh, damit die Ausgabe einheitlich wirkt).
_TITLE = "\033[1;36m"
_DIM = "\033[2m"
_OK = "\033[1;32m"
_WARN = "\033[1;33m"
_RST = "\033[0m"

# Zustands-Reihenfolge für die Video-Liste (aktive zuerst).
_STATUS_LABEL = {
    "assigned": ("zugewiesen", _DIM),
    "uploaded": ("hochgeladen", _WARN),
    "pending": ("wartet", _DIM),
    "done": ("fertig", _OK),
    "failed": ("FEHLER", _WARN),
}


def _fmt_minutes(mins: float) -> str:
    if mins <= 0:
        return "–"
    if mins < 60:
        return f"{mins:.1f}m"
    return f"{mins/60:.1f}h"


# Pipeline-Phasen in Reihenfolge (Schrittanzeige N/3).
_PHASE_STEP = {"Denoise": "1/3", "Upscale": "2/3", "Audio": "3/3"}


def _fmt_phase(phase: str, pct: str) -> str:
    """'Upscale 2/3 42%' — Phase + Schritt + (falls vorhanden) Prozent."""
    if not phase:
        return ""
    step = _PHASE_STEP.get(phase, "")
    txt = f"{phase} {step}".strip()
    if pct:
        txt += f" {pct}"
    return txt


# Segmentierter Phasen-Balken für die GPU-Übersicht: (Name, Zellenbreite).
# Upscale ist die mit Abstand längste Phase -> breitestes Segment.
_BAR_PHASES = [("Denoise", 4), ("Upscale", 12), ("Audio", 3)]


def _phase_bar(phase: str, pct: str) -> str:
    """Balken über alle Phasen: erledigt = voll (grün), laufend = anteilig nach
    %-Zahl (gelb), ausstehend = leer (grau). Ohne %-Wert (Denoise/Audio, kein
    tqdm) wird die laufende Phase als aktiv-schraffiert ▓ dargestellt.
    """
    names = [p[0] for p in _BAR_PHASES]
    cur = names.index(phase) if phase in names else -1
    try:
        pval = int(pct.rstrip("%")) if pct else -1
    except ValueError:
        pval = -1

    segs: list[str] = []
    for i, (name, w) in enumerate(_BAR_PHASES):
        if cur >= 0 and i < cur:                       # abgeschlossen
            seg = f"{_OK}{'█' * w}{_RST}"
        elif i == cur:                                 # laufend
            if pval >= 0:
                f = max(0, min(w, round(w * pval / 100)))
                seg = f"{_WARN}{'█' * f}{_DIM}{'░' * (w - f)}{_RST}"
            else:
                seg = f"{_WARN}{'▓' * w}{_RST}"
        else:                                          # ausstehend
            seg = f"{_DIM}{'░' * w}{_RST}"
        segs.append(f"{name} {seg}")

    tail = f"  {_WARN}{pval}%{_RST}" if pval >= 0 else ""
    return "   ".join(segs) + tail


def _live_state(cfg: Config, db: DB) -> tuple[dict[str, str], set[str]]:
    """Live-Zustand aller bereiten Nodes in EINEM SSH-Durchgang pro Node.

    Rückgabe:
      * live      : Basisname -> 'Phase Schritt %' der GERADE laufenden Clips.
      * node_done : Basisnamen, deren fertige .mp4 schon auf der Node liegt
                    (auf Node fertig — evtl. noch NICHT heruntergeladen).

    Basisname = ohne Endung (so loggt process.sh), matcht via splitext auf den
    DB-Dateinamen. SSH-Fehler werden geschluckt — die Anzeige darf nie crashen.
    """
    live: dict[str, str] = {}
    node_done: set[str] = set()
    for node in db.active_nodes():
        if node["status"] != "ready" or not node["ssh_host"] or not node["ssh_port"]:
            continue
        r = Remote(node["ssh_host"], node["ssh_port"], cfg.ssh_key_path)
        try:
            for _gpu, state, clip, phase, pct in r.gpu_activity():
                if state == "busy" and clip:
                    live[clip] = _fmt_phase(phase, pct)
        except Exception:  # noqa: BLE001 — Anzeige robust halten
            pass
        try:
            for fname in r.list_remote_final():
                node_done.add(os.path.splitext(fname)[0])
        except Exception:  # noqa: BLE001
            pass
    return live, node_done


def _gpu_load(stats: dict, gpu: int) -> str:
    """'· 87% · 5.2/32.6 GB' — Auslastung + VRAM einer GPU (leer, wenn unbekannt)."""
    s = stats.get(gpu)
    if not s:
        return ""
    util, used, total = s
    # VRAM-Farbe: viel belegt = grün (rechnet), fast leer = grau (idle/Fehler).
    memc = _OK if used >= 512 else _DIM
    return (f"  {_DIM}·{_RST} {util:>3}% util {_DIM}·{_RST} "
            f"{memc}{used/1024:.1f}{_RST}/{total/1024:.0f} GB")


# ---------------------------------------------------------------------------
#  Workmap: welche GPU welcher Node arbeitet gerade an welchem Video.
# ---------------------------------------------------------------------------
def render_workmap(cfg: Config, db: DB, vast: VastClient) -> str:
    lines: list[str] = []
    nodes = db.active_nodes()
    if not nodes:
        return f"{_WARN}Keine aktiven Nodes.{_RST}"

    total_gpus = 0
    total_busy = 0
    for node in nodes:
        iid = node["instance_id"]
        head = (f"{_TITLE}Node #{iid}{_RST}  {node['gpu_name']} x{node['num_gpus']}"
                f"  {_DIM}{(node['dph'] or 0):.3f} $/h · status={node['status']}{_RST}")
        lines.append(head)

        host, port = node["ssh_host"], node["ssh_port"]
        if node["status"] != "ready" or not host or not port:
            msg = "bootet/wird eingerichtet"
            if host and port:
                try:
                    st = Remote(host, port, cfg.ssh_key_path).bootstrap_status()
                    if st:
                        msg = st
                except Exception:  # noqa: BLE001 — Anzeige robust halten
                    pass
            lines.append(f"  {_DIM}(noch nicht bereit — {msg}){_RST}\n")
            continue

        r = Remote(host, port, cfg.ssh_key_path)
        try:
            activity = r.gpu_activity()
        except Exception as e:  # noqa: BLE001 — Anzeige darf nie crashen
            lines.append(f"  {_WARN}(nicht erreichbar: {e}){_RST}\n")
            continue
        try:
            stats = r.gpu_stats()
        except Exception:  # noqa: BLE001 — Auslastung ist optional
            stats = {}

        # Immer ALLE GPUs der Node zeigen — auch die, die noch keine Logdatei
        # haben (gestaffelter Worker-Start: GPU N startet erst nach N×Stagger).
        act_by_gpu = {gpu: (state, clip, phase, pct)
                      for gpu, state, clip, phase, pct in activity}
        ngpu = node["num_gpus"] or (max(act_by_gpu, default=-1) + 1)
        total_gpus += ngpu
        for gpu in range(ngpu):
            load = _gpu_load(stats, gpu)
            state, clip, phase, pct = act_by_gpu.get(gpu, ("waiting", "", "", ""))
            if state == "busy" and clip:
                total_busy += 1
                lines.append(f"  {_OK}● GPU {gpu}{_RST}  ▶ {clip}{load}")
                if phase:
                    lines.append(f"      {_phase_bar(phase, pct)}")
            elif state == "idle":
                lines.append(f"  {_DIM}○ GPU {gpu}   (frei / zwischen Clips){_RST}{load}")
            else:
                lines.append(f"  {_WARN}◌ GPU {gpu}{_RST}   {_DIM}(startet noch …){_RST}{load}")
        lines.append("")

    counts = db.counts()
    remaining = sum(counts.get(s, 0) for s in ("pending", "assigned", "uploaded"))
    summary = (f"{_TITLE}Σ{_RST}  {total_busy}/{total_gpus} GPUs aktiv · "
               f"{counts.get('done', 0)} fertig · {remaining} offen")
    lines.append(summary)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
#  Video-/Kostenübersicht.
#
#  Kosten pro Video (Schätzung nach Wunschformel):
#      belegte_minuten × GPU-Faktor × x
#  belegte_minuten = Zeit, die der Clip auf der Node belegt hat
#      (done: done_at−assigned_at, laufend: jetzt−assigned_at).
#  GPU-Faktor kommt aus der Config (relative Rechenleistung pro GPU-Typ),
#  x = COST_RATE_X ($ je faktor-gewichteter Minute).
# ---------------------------------------------------------------------------
def render_videos(cfg: Config, db: DB, limit: int | None = None) -> str:
    now = time.time()
    rows = db.clips_with_gpu()
    if not rows:
        return f"{_DIM}Noch keine Clips in der Queue.{_RST}"

    # Live-Zustand: laufende Phasen + auf Node fertige (evtl. noch nicht gepullte).
    live, node_done = _live_state(cfg, db)
    node_ready = 0   # auf Node fertig, aber Pull (Download) noch ausstehend

    est_total = 0.0
    active_min = 0.0
    body: list[str] = []
    shown = 0
    for c in rows:
        status = c["status"]
        assigned_at = c["assigned_at"]
        done_at = c["done_at"]
        gpu_name = c["gpu_name"]
        factor = cfg.gpu_factor(gpu_name)
        base = os.path.splitext(c["name"])[0]

        if status == "done" and assigned_at and done_at:
            minutes = max(0.0, (done_at - assigned_at) / 60.0)
        elif status in ("assigned", "uploaded") and assigned_at:
            minutes = max(0.0, (now - assigned_at) / 60.0)
            active_min += minutes
        else:
            minutes = 0.0

        cost = minutes * factor * cfg.cost_rate_x
        est_total += cost

        # Feinerer Zustand als der reine DB-Status:
        #   done              -> heruntergeladen (endgültig fertig)
        #   base in live      -> läuft gerade auf einer GPU (Phase anhängen)
        #   base in node_done -> auf Node fertig, Pull noch ausstehend
        #   uploaded          -> liegt auf Node, wartet auf freie GPU
        #   assigned          -> wird noch hochgeladen
        #   pending/failed    -> wie DB
        tail = ""
        if status == "done":
            label, color = "geladen ✓", _OK
        elif base in live:
            label, color = "läuft ●", _WARN
            tail = f"  {_WARN}[{live[base]}]{_RST}"
        elif base in node_done:
            label, color = "Node-fertig", _OK
            node_ready += 1
        elif status == "uploaded":
            label, color = "auf Node", _DIM
        elif status == "assigned":
            label, color = "hochladen", _DIM
        elif status == "failed":
            label, color = "FEHLER", _WARN
        else:
            label, color = "wartet", _DIM

        gpu_short = (gpu_name or "—").replace("RTX ", "")
        if limit is None or shown < limit:
            body.append(
                f"  {color}{label:<12}{_RST} "
                f"{_fmt_minutes(minutes):>6} ×{factor:>3.1f}  "
                f"{_OK if cost else _DIM}{cost:>7.3f} ${_RST}  "
                f"{_DIM}{gpu_short:<6}{_RST} {c['name']}{tail}"
            )
            shown += 1

    # Echte Node-Rechnung (tatsächliche Vast-Kosten) zum Abgleich.
    nodes = db.active_nodes()
    real_total = 0.0
    for n in nodes:
        hours = max(0.0, (now - (n["created_at"] or now)) / 3600.0)
        real_total += (n["dph"] or 0.0) * hours

    counts = db.counts()
    header = [
        f"{_TITLE}== Videos & Kosten =={_RST}",
        (f"  Geschätzt gesamt: {_OK}{est_total:8.2f} ${_RST}   "
         f"{_DIM}(Formel: belegte Min × GPU-Faktor × x={cfg.cost_rate_x}){_RST}"),
        (f"  Reale Node-Rechnung: {real_total:8.2f} $   "
         f"{_DIM}({len(nodes)} aktive Node(s), laufend){_RST}"),
        (f"  Clips: {counts.get('done', 0)} geladen · "
         f"{node_ready} auf Node fertig (Pull offen) · "
         f"{counts.get('uploaded', 0)+counts.get('assigned', 0)} in Arbeit · "
         f"{counts.get('pending', 0)} wartend · {counts.get('failed', 0)} Fehler"),
        "",
        (f"  {_DIM}{'Status':<12} {'belegt':>6} {'Fkt':>4}  {'Kosten':>8}  "
         f"{'GPU':<6} Video{_RST}"),
    ]
    footer = []
    if limit is not None and len(rows) > limit:
        footer = [f"  {_DIM}… {len(rows) - limit} weitere (alle: 'vhsorch videos --all'){_RST}"]
    return "\n".join(header + body + footer)
