"""Anzeige-Reports: Workmap (GPU->Video) und Video-/Kostenübersicht.

Reine Aufbereitung für die Konsole; kein State wird verändert. Beide Reports
werden vom Menü in einer Auto-Refresh-Schleife aufgerufen ("aktiv pullen").
"""
from __future__ import annotations

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
            lines.append(f"  {_DIM}(noch nicht bereit — bootet/wird eingerichtet){_RST}\n")
            continue

        r = Remote(host, port, cfg.ssh_key_path)
        try:
            activity = r.gpu_activity()
        except Exception as e:  # noqa: BLE001 — Anzeige darf nie crashen
            lines.append(f"  {_WARN}(nicht erreichbar: {e}){_RST}\n")
            continue

        if not activity:
            lines.append(f"  {_DIM}(keine GPU-Logs — Worker startet gleich){_RST}\n")
            continue

        for gpu, state, clip in activity:
            total_gpus += 1
            if state == "busy" and clip:
                total_busy += 1
                lines.append(f"  {_OK}● GPU {gpu}{_RST}  ▶ {clip}")
            else:
                lines.append(f"  {_DIM}○ GPU {gpu}   (frei / zwischen Clips){_RST}")
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

        if status == "done" and assigned_at and done_at:
            minutes = max(0.0, (done_at - assigned_at) / 60.0)
        elif status in ("assigned", "uploaded") and assigned_at:
            minutes = max(0.0, (now - assigned_at) / 60.0)
            active_min += minutes
        else:
            minutes = 0.0

        cost = minutes * factor * cfg.cost_rate_x
        est_total += cost

        label, color = _STATUS_LABEL.get(status, (status, _RST))
        gpu_short = (gpu_name or "—").replace("RTX ", "")
        if limit is None or shown < limit:
            body.append(
                f"  {color}{label:<11}{_RST} "
                f"{_fmt_minutes(minutes):>6} ×{factor:>3.1f}  "
                f"{_OK if cost else _DIM}{cost:>7.3f} ${_RST}  "
                f"{_DIM}{gpu_short:<6}{_RST} {c['name']}"
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
        (f"  Clips: {counts.get('done', 0)} fertig · "
         f"{counts.get('uploaded', 0)+counts.get('assigned', 0)} in Arbeit · "
         f"{counts.get('pending', 0)} wartend · {counts.get('failed', 0)} Fehler"),
        "",
        (f"  {_DIM}{'Status':<11} {'belegt':>6} {'Fkt':>4}  {'Kosten':>8}  "
         f"{'GPU':<6} Video{_RST}"),
    ]
    footer = []
    if limit is not None and len(rows) > limit:
        footer = [f"  {_DIM}… {len(rows) - limit} weitere (alle: 'vhsorch videos --all'){_RST}"]
    return "\n".join(header + body + footer)
