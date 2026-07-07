"""Scheduler: verteilt Clips auf aktive Nodes, synchronisiert, räumt auf.

Verteilungslogik (Multi-Node)
-----------------------------
Jeder Clip wird GENAU EINER Node zugewiesen (Spalte clips.node_id). Die
Zuweisung erfolgt kapazitätsgewichtet: eine Node mit mehr GPUs bekommt
proportional mehr Clips. Dadurch bearbeitet keine zwei Nodes denselben Clip
(keine doppelte Rechenzeit), und der Split ist über die DB nachvollziehbar.

Stirbt eine Node (nicht mehr in Vast oder unerreichbar), werden ihre noch
nicht fertigen Clips atomar auf 'pending' zurückgesetzt und im nächsten Takt
neu verteilt (Selbstheilung).

Kostenschutz
------------
Sobald die gesamte Queue leer ist (alle Clips done/failed) und alle
Ergebnisse eingesammelt wurden, wird bei AUTO_DESTROY=1 jede Node zerstört.
Es läuft nie eine Node ohne aktive Arbeit.
"""
from __future__ import annotations

import os
import time

from .config import Config
from .db import DB
from .ingest import Ingest
from .remote import Remote
from .vast import VastClient


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Scheduler:
    def __init__(self, cfg: Config, db: DB, vast: VastClient):
        self.cfg = cfg
        self.db = db
        self.vast = vast
        self.ingest = Ingest(db, cfg.raw_dir, cfg.stable_checks)
        os.makedirs(cfg.done_dir, exist_ok=True)

    # -- Node-Handling --------------------------------------------------------
    def _remote(self, node) -> Remote | None:
        if not node["ssh_host"] or not node["ssh_port"]:
            return None
        return Remote(node["ssh_host"], node["ssh_port"], self.cfg.ssh_key_path)

    def refresh_nodes(self) -> None:
        """Gleicht DB-Nodes mit dem realen Vast-Zustand ab (SSH-Zugang, Tod)."""
        live = {int(i["id"]): i for i in self.vast.show_instances()}
        for node in self.db.active_nodes():
            iid = node["instance_id"]
            inst = live.get(iid)
            if inst is None:
                # Node existiert nicht mehr bei Vast -> Clips zurückgeben.
                n = self.db.reassign_node_clips(iid)
                self.db.update_node(iid, status="destroyed")
                log(f"Node {iid} verschwunden — {n} Clips neu eingereiht.")
                continue
            # SSH-Zugang übernehmen, sobald Vast ihn liefert.
            host = inst.get("ssh_host")
            port = inst.get("ssh_port")
            if host and port and (host != node["ssh_host"] or port != node["ssh_port"]):
                self.db.update_node(iid, ssh_host=host, ssh_port=int(port))
                log(f"Node {iid}: SSH {host}:{port}")
            # 'ready', sobald erreichbar und Bootstrap fertig (process.sh da).
            if node["status"] == "booked":
                r = self._remote(self.db.get_node(iid))
                if r and r.reachable():
                    res = r.exec("test -x /workspace/process.sh && echo ok || echo no")
                    if res.stdout.strip() == "ok":
                        self.db.update_node(iid, status="ready")
                        log(f"Node {iid} ist READY (bootstrap abgeschlossen).")

    # -- Verteilung -----------------------------------------------------------
    def distribute(self) -> None:
        """Weist pending Clips kapazitätsgewichtet den ready-Nodes zu."""
        nodes = [n for n in self.db.active_nodes() if n["status"] == "ready"]
        if not nodes:
            return
        pending = self.db.pending_clips()
        if not pending:
            return

        # Kapazität = GPUs pro Node; verteile round-robin gewichtet nach GPUs.
        slots: list[int] = []
        for n in nodes:
            slots += [n["instance_id"]] * max(1, n["num_gpus"])

        with self.db.tx():
            for i, clip in enumerate(pending):
                target = slots[i % len(slots)]
                self.db.assign_clip(clip["name"], target)
        log(f"{len(pending)} Clips auf {len(nodes)} Node(s) verteilt.")

    # -- Push / Worker / Pull -------------------------------------------------
    def push_and_run(self) -> None:
        for node in self.db.active_nodes():
            if node["status"] != "ready":
                continue
            iid = node["instance_id"]
            r = self._remote(node)
            if r is None:
                continue

            # Worker FRÜH starten (schon vor Upload-Ende), damit die Node
            # eintreffende Clips sofort abgreift -> Verarbeitung überlappt mit
            # dem laufenden Upload. process.sh idlet, bis die ersten Clips da
            # sind. Idempotent: läuft er schon, passiert nichts.
            if not r.worker_running():
                if r.start_worker():
                    self.db.update_node(iid, worker_started=1)
                    log(f"Node {iid}: Worker gestartet (process.sh, detached).")
                else:
                    log(f"Node {iid}: WARNUNG — Worker-Start fehlgeschlagen, "
                        f"nächster Takt versucht erneut. Prüfe run.log auf der Node.")

            # HÄPPCHEN = GRAFIKKARTENMENGE (Flow-Control): pro Node nur so viele
            # Clips "in Arbeit" halten wie GPUs vorhanden (× Puffer). Nachschub
            # kommt erst, wenn die Node Rückstand abbaut -> minimaler Node-
            # Speicher, Rohvideos bleiben zuhause, Upload+Verarbeitung überlappen.
            node_gpus = node["num_gpus"] or 1
            target = max(1, node_gpus * self.cfg.inflight_per_gpu)
            backlog = len(self.db.clips_for_node(iid, status="uploaded"))
            room = target - backlog
            if room > 0:
                assigned = self.db.clips_for_node(iid, status="assigned")
                # Nur Clips, deren Rohdatei tatsächlich (noch) existiert.
                to_push = [
                    c for c in assigned[:room]
                    if os.path.isfile(os.path.join(self.cfg.raw_dir, c["name"]))
                ]
                paths = [os.path.join(self.cfg.raw_dir, c["name"]) for c in to_push]
                if paths and r.push_files(paths):
                    with self.db.tx():
                        for c in to_push:
                            self.db.set_clip_status(c["name"], "uploaded")
                    still = len(self.db.clips_for_node(iid, status="assigned"))
                    log(f"Node {iid}: {len(paths)} Clips hochgeladen "
                        f"(in Arbeit {backlog + len(paths)}/{target}, "
                        f"{still} warten zuhause).")

    def collect(self) -> None:
        """Zieht Ergebnisse und markiert die zugehörigen Clips als done."""
        for node in self.db.active_nodes():
            # Nur von bereiten Nodes ziehen — sonst rsync-/ssh-Lärm, während
            # eine frisch gebuchte Node noch bootet (sshd noch nicht oben).
            if node["status"] != "ready":
                continue
            r = self._remote(node)
            if r is None:
                continue
            iid = node["instance_id"]
            r.pull_results(self.cfg.done_dir)
            # Ein Clip <name>.<ext> gilt als fertig, wenn <name>.mp4 lokal liegt.
            done_local = {
                os.path.splitext(f)[0]
                for f in os.listdir(self.cfg.done_dir)
                if f.lower().endswith(".mp4")
            }
            with self.db.tx():
                for c in self.db.clips_for_node(iid):
                    if c["status"] in ("done", "failed"):
                        continue
                    if os.path.splitext(c["name"])[0] in done_local:
                        self.db.set_clip_status(c["name"], "done")

    # -- Kostenschutz ---------------------------------------------------------
    def maybe_destroy(self) -> bool:
        """Zerstört alle Nodes, wenn die Queue leer ist. True = zerstört."""
        if not self.cfg.auto_destroy:
            return False
        if not self.db.all_done():
            return False
        counts = self.db.counts()
        if not counts:  # noch keine Clips ingested -> nicht zerstören
            return False
        destroyed = False
        for node in self.db.active_nodes():
            iid = node["instance_id"]
            log(f"KOSTENSCHUTZ: Queue leer — zerstöre Node {iid} …")
            try:
                self.vast.destroy_instance(iid)
                self.db.update_node(iid, status="destroyed")
                destroyed = True
            except Exception as e:  # noqa: BLE001
                log(f"FEHLER beim Zerstören von {iid}: {e}")
        return destroyed

    # -- Kosten-Logging -------------------------------------------------------
    def cost_line(self) -> str:
        nodes = self.db.active_nodes()
        total_dph = sum(n["dph"] or 0.0 for n in nodes)
        oldest = min((n["created_at"] for n in nodes), default=time.time())
        hours = (time.time() - oldest) / 3600 if nodes else 0.0
        return (f"Aktive Nodes: {len(nodes)} | {total_dph:.3f} $/h | "
                f"grob aufgelaufen: {total_dph*hours:.2f} $")

    # -- Haupt-Takt -----------------------------------------------------------
    def tick(self) -> None:
        new = self.ingest.scan()
        if new:
            log(f"{new} neue Rohclips in die Queue aufgenommen.")
        self.refresh_nodes()
        self.distribute()
        self.push_and_run()
        self.collect()
        c = self.db.counts()
        log(f"Queue {dict(c)} | {self.cost_line()}")
        if self.maybe_destroy():
            log("Alle Nodes zerstört. Lauf abgeschlossen.")

    def run_forever(self) -> None:
        log("Scheduler-Loop gestartet (Strg-C zum Beenden).")
        while True:
            try:
                self.tick()
            except Exception as e:  # noqa: BLE001
                log(f"tick-Fehler (weiter): {e}")
            time.sleep(self.cfg.poll_interval)
