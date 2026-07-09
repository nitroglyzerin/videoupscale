"""Scheduler: verteilt Clips auf aktive Nodes, synchronisiert, räumt auf.

Zwei-Lane-Architektur (damit die TUI NIE hakt)
----------------------------------------------
Der Scheduler ist der EINZIGE Prozess, der zu den Nodes SSHt — und damit der
einzige Schreiber der Remote-Wahrheit. Er läuft in zwei Bahnen:

  * SCHNELLER Loop (~1 s, Haupt-Thread): arbeitet die Command-Queue ab
    (idempotente Nudges der TUI), pollt read-only alle Nodes im ThreadPool
    (probe(), 1 SSH je Node dank ControlPersist) und schreibt am Ende einen
    atomaren Status-SNAPSHOT (state_dir/snapshot.json). Blockiert NIE auf einem
    langen Transfer.
  * HEAVY-Pool (wenige Threads): die langen Operationen (Modelle pushen ~min,
    Clips hochladen, Ergebnisse pullen). Jede Node ist per busy-Flag single-
    flight — nie mutieren zwei Threads dieselbe Node gleichzeitig.

Die TUI ist ein reiner LESER des Snapshots + Schreiber in die Command-Queue —
kein SSH, kein docker-run im UI-Pfad.

Verteilungslogik (Multi-Node)
-----------------------------
Jeder Clip wird GENAU EINER Node zugewiesen (Spalte clips.node_id),
kapazitätsgewichtet nach GPUs. Stirbt eine Node, werden ihre nicht-fertigen
Clips atomar auf 'pending' zurückgesetzt und neu verteilt (Selbstheilung).

Kostenschutz
------------
Ist die gesamte Queue leer (alle Clips done/failed) und alle Ergebnisse
eingesammelt, wird bei AUTO_DESTROY=1 jede Node zerstört. Eine 'draining' Node
(manuell rausgenommen) wird zerstört, sobald sie keine Arbeit mehr trägt.
"""
from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

from .config import Config, SEEDVR2_MODEL_FILES
from .db import DB
from .ingest import Ingest
from .models import ensure_models_cached, models_present
from .remote import Remote
from .vast import VastClient

SNAPSHOT_SCHEMA = 1

# Gewichtung der Pipeline-Phasen für einen monotonen Gesamt-Fortschritt je Clip.
# (Start-%, End-%) — Upscale ist mit Abstand die längste Phase.
_PHASE_RANGE = {"Denoise": (0, 20), "Upscale": (20, 90), "Audio": (90, 100)}
_PHASE_STEP = {"Denoise": "1/3", "Upscale": "2/3", "Audio": "3/3"}


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


class Scheduler:
    def __init__(self, cfg: Config, db: DB, vast: VastClient):
        self.cfg = cfg
        self.db = db
        self.vast = vast
        self.ingest = Ingest(db, cfg.raw_dir, cfg.stable_checks)
        os.makedirs(cfg.done_dir, exist_ok=True)
        os.makedirs(cfg.ssh_mux_dir, exist_ok=True)

        # Nebenläufigkeit: Heavy-Pool + Probe-Pool + Per-Node-Single-Flight.
        self._pool = ThreadPoolExecutor(max_workers=max(1, cfg.heavy_workers),
                                        thread_name_prefix="heavy")
        self._probe_pool = ThreadPoolExecutor(max_workers=8,
                                              thread_name_prefix="probe")
        # Eigener kleiner Pool NUR für harte Kills (destroy): so wartet der
        # Kostenstopp NIE hinter einem 15-40-min-Transfer im Heavy-Pool.
        self._kill_pool = ThreadPoolExecutor(max_workers=2,
                                             thread_name_prefix="kill")
        self._busy: set[int] = set()          # instance_ids mit laufender Mutation
        self._busy_label: dict[int, str] = {}  # instance_id -> was gerade läuft
        self._busy_lock = threading.Lock()

        # In-Memory-Caches für den Snapshot.
        self._probes: dict[int, dict] = {}     # iid -> {"data": <probe>, "at": ts}
        self._mono: dict[tuple[int, int], tuple[str, int]] = {}  # (iid,gpu) -> (clip, max_pct)

        # Heartbeat-Zeitstempel (Wallclock) für Stale-Erkennung in der TUI.
        self._started_wall = time.time()
        self._last_work_wall = 0.0
        self._last_probe_wall = 0.0

    # -- Per-Node-Single-Flight ----------------------------------------------
    def _acquire(self, iid: int, label: str) -> bool:
        with self._busy_lock:
            if iid in self._busy:
                return False
            self._busy.add(iid)
            self._busy_label[iid] = label
            return True

    def _release(self, iid: int) -> None:
        with self._busy_lock:
            self._busy.discard(iid)
            self._busy_label.pop(iid, None)

    def _busy_now(self, iid: int) -> str | None:
        with self._busy_lock:
            return self._busy_label.get(iid)

    def _remote(self, node) -> Remote | None:
        if not node["ssh_host"] or not node["ssh_port"]:
            return None
        return Remote(node["ssh_host"], node["ssh_port"], self.cfg.ssh_key_path,
                      control_dir=self.cfg.ssh_mux_dir)

    # ========================================================================
    #  Haupt-Loop
    # ========================================================================
    def run_forever(self) -> None:
        """Haupt-Loop: NUR schnelle, nie-blockierende Arbeit (Command-Drain +
        Snapshot). Das (potenziell langsame) Probing und der Arbeits-Tick laufen
        in EIGENEN Threads — so blockiert eine hängende Node/Vast-API die UI nie
        (der Snapshot wird weiter jede Sekunde geschrieben)."""
        log("Scheduler-Loop gestartet (Zwei-Lane, Snapshot + Commands).")
        stuck = self.db.requeue_running_commands()
        if stuck:
            log(f"{stuck} unterbrochene(s) Command(s) nach Neustart erneut eingereiht.")
        # SeedVR2-Modelle automatisch in den Home-Cache laden, falls sie fehlen —
        # im Hintergrund (der ~4-GB-Download darf den Loop nicht blockieren). Bis
        # sie da sind, wartet _push_models ohnehin (Worker startet erst mit Modellen).
        if not models_present(self.cfg):
            threading.Thread(target=self._fetch_models_bg,
                             name="fetch-models", daemon=True).start()
        threading.Thread(target=self._probe_loop, name="prober", daemon=True).start()
        threading.Thread(target=self._work_loop, name="worker-tick", daemon=True).start()
        while True:
            try:
                self._process_commands()
                self._write_snapshot()
            except Exception as e:  # noqa: BLE001
                log(f"loop-Fehler (weiter): {e}")
            time.sleep(1)

    def _probe_loop(self) -> None:
        while True:
            try:
                self._probe_all()
            except Exception as e:  # noqa: BLE001
                log(f"probe-Fehler (weiter): {e}")
            time.sleep(self.cfg.probe_interval)

    def _work_loop(self) -> None:
        while True:
            try:
                self.work_tick()
            except Exception as e:  # noqa: BLE001
                log(f"tick-Fehler (weiter): {e}")
            time.sleep(self.cfg.poll_interval)

    def _fetch_models_bg(self) -> None:
        try:
            log("SeedVR2-Modelle fehlen im Home-Cache — lade sie automatisch "
                "(Orchestrator, einmalig) …")
            if ensure_models_cached(self.cfg, log):
                log("SeedVR2-Modell-Cache bereit — Nodes bekommen sie per rsync.")
            else:
                log("Modell-Download unvollständig — nächster Neustart versucht "
                    "erneut (oder manuell: vhsorch fetch-models).")
        except Exception as e:  # noqa: BLE001
            log(f"Auto-Modell-Fetch fehlgeschlagen: {e}")

    # -- Command-Queue (idempotente Nudges der TUI) --------------------------
    def _process_commands(self) -> None:
        for cmd in self.db.queued_commands():
            cid = cmd["id"]
            action = cmd["action"]
            node_id = cmd["node_id"]
            arg = cmd["arg"]

            # DB-only, sofort im Haupt-Thread (kein SSH, kein busy nötig).
            if action == "requeue":
                requeued, old_nid = self.db.requeue_clip(arg) if arg else (False, None)
                if requeued and old_nid:
                    # Claim auf der alten Node freigeben, damit der Clip neu
                    # gegriffen werden kann (best effort, Pool, kein busy nötig).
                    self._pool.submit(self._release_claim_task, old_nid, arg)
                self.db.set_command_status(
                    cid, "done", f"requeue {arg}: {'ok' if requeued else 'nichts'}")
                continue
            if action == "finalize":
                n = self.db.abandon_failed()
                self.db.set_command_status(
                    cid, "done", f"{n} Fehler-Clips finalisiert (abandoned)")
                log(f"Finalize: {n} failed-Clips -> abandoned (Auto-Destroy frei).")
                continue
            if action == "drain":
                if node_id is None:
                    self.db.set_command_status(cid, "failed", "drain ohne node_id")
                    continue
                self.db.update_node(node_id, status="draining")
                n = self.db.reassign_node_clips(node_id)
                self.db.set_command_status(
                    cid, "done", f"draining — {n} Clips neu eingereiht")
                log(f"Node {node_id}: draining (manuell), {n} Clips neu eingereiht.")
                continue
            if action == "destroy":
                if node_id is None:
                    self.db.set_command_status(cid, "failed", "destroy ohne node_id")
                    continue
                # Harter Kostenstopp: SOFORT, NICHT hinter dem busy-Gate (nur
                # Vast-API, kein SSH-Transfer). Status zuerst -> kein neuer
                # Service-Task; ein parallel laufender rsync bricht harmlos ab.
                self.db.update_node(node_id, status="destroyed")
                self.db.reassign_node_clips(node_id)
                self.db.set_command_status(cid, "running")
                self._kill_pool.submit(self._run_destroy, cid, node_id)
                continue

            # SSH-Aktionen (bootstrap/models/worker/pull) -> Heavy-Pool, single-flight.
            if node_id is None:
                self.db.set_command_status(cid, "failed", f"{action} ohne node_id")
                continue
            if not self._acquire(node_id, f"cmd:{action}"):
                continue  # Node busy -> Befehl bleibt queued, nächster Durchlauf
            try:
                self.db.set_command_status(cid, "running")
                self._pool.submit(self._run_node_command, cid, node_id, action, arg)
            except Exception as e:  # noqa: BLE001 — Flag nie leaken, Befehl retryt
                self._release(node_id)
                self.db.set_command_status(cid, "queued", f"Dispatch-Fehler: {e}")

    def _run_node_command(self, cid: int, node_id: int, action: str, arg) -> None:
        try:
            node = self.db.get_node(node_id)
            if node is None:
                self.db.set_command_status(cid, "failed", "Node unbekannt")
                return
            r = self._remote(node)
            if r is None:
                self.db.set_command_status(cid, "failed", "kein SSH-Endpunkt")
                return

            if action == "bootstrap":
                ok = r.start_bootstrap(self.cfg.repo_raw_url)
                if ok:
                    self.db.update_node(node_id, bootstrap_started=1)
                self.db.set_command_status(
                    cid, "done" if ok else "failed",
                    "Bootstrap angestoßen" if ok else "SSH fehlgeschlagen")
            elif action == "models":
                ok = self._push_models(node)
                self.db.set_command_status(
                    cid, "done" if ok else "failed",
                    "Modelle gepusht" if ok else "Push fehlgeschlagen/Modelle fehlen")
            elif action == "worker":
                # IDEMPOTENT: läuft der Worker schon, NICHT erneut starten (sonst
                # zweiter Worker-Baum -> OOM). remote.start_worker ist zwar jetzt
                # selbst pgrep-gewacht, aber wir melden hier den echten Zustand.
                if r.worker_running():
                    self.db.set_command_status(cid, "done", "Worker läuft bereits")
                else:
                    ok = r.start_worker()
                    if ok:
                        self.db.update_node(node_id, worker_started=1)
                    self.db.set_command_status(
                        cid, "done" if ok else "failed",
                        "Worker gestartet" if ok else "Worker-Start fehlgeschlagen")
            elif action == "pull":
                self._pull_and_mark(node)
                self.db.set_command_status(cid, "done", "Ergebnisse gepullt")
            else:
                self.db.set_command_status(cid, "failed", f"unbekannte Aktion {action}")
        except Exception as e:  # noqa: BLE001
            self.db.set_command_status(cid, "failed", str(e)[:300])
        finally:
            self._release(node_id)

    def _run_destroy(self, cid: int, node_id: int) -> None:
        """Harter Kill im kill_pool (DB-Status wurde schon auf destroyed gesetzt)."""
        try:
            self.vast.destroy_instance(node_id)
            self.db.set_command_status(cid, "done", "Node zerstört")
            log(f"Node {node_id}: zerstört (manuell).")
        except Exception as e:  # noqa: BLE001
            self.db.set_command_status(cid, "failed", f"Kill fehlgeschlagen: {e}")
            log(f"Node {node_id}: Kill FEHLGESCHLAGEN — evtl. manuell 'destroy' nötig: {e}")

    def _release_claim_task(self, node_id: int, clip_name: str) -> None:
        """Gibt den Claim-Lock eines requeueten Clips auf seiner alten Node frei."""
        try:
            node = self.db.get_node(node_id)
            r = self._remote(node) if node else None
            if r is not None:
                r.release_claim(clip_name)
        except Exception as e:  # noqa: BLE001
            log(f"Claim-Release (Node {node_id}, {clip_name}) fehlgeschlagen: {e}")

    # -- Read-only Probe aller Nodes (ThreadPool-Fanout) ---------------------
    def _probe_all(self) -> None:
        nodes = [n for n in self.db.active_nodes()
                 if n["ssh_host"] and n["ssh_port"]]
        if nodes:
            def probe_one(n):
                r = self._remote(n)
                try:
                    return n["instance_id"], r.probe()
                except Exception:  # noqa: BLE001 — Anzeige robust halten
                    return n["instance_id"], None
            for iid, data in self._probe_pool.map(probe_one, nodes):
                if data is not None:
                    self._probes[iid] = {"data": data, "at": time.time()}
        self._last_probe_wall = time.time()
        self._apply_probe_effects()

    def _apply_probe_effects(self) -> None:
        """Reine DB-Effekte aus den Probe-Daten (kein neuer SSH): ready-Erkennung
        und FAIL-Markierung."""
        for node in self.db.active_nodes():
            iid = node["instance_id"]
            pc = self._probes.get(iid)
            p = pc["data"] if pc else None
            if not p or not p.get("reachable"):
                continue
            # Ready, sobald process.sh vorhanden ist (Bootstrap fertig).
            if node["status"] == "booked" and p.get("process_present"):
                self.db.update_node(iid, status="ready")
                log(f"Node {iid} ist READY (bootstrap abgeschlossen).")
            # FAIL-Markierung: ein Clip, der auf der Node FAIL geloggt hat, NICHT
            # fertig ist und gerade nicht mehr läuft -> als 'failed' sichtbar.
            fails = p.get("fails") or []
            if not fails:
                continue
            finals = {os.path.splitext(f)[0] for f in p.get("final", [])}
            busy_clips = {clip for (_g, st, clip, _ph, _pct)
                          in p.get("gpus_activity", []) if st == "busy" and clip}
            failset = set(fails) - finals - busy_clips
            if not failset:
                continue
            for c in self.db.clips_for_node(iid):
                # NUR tatsächlich hochgeladene Clips können auf der Node fehl-
                # schlagen. 'assigned'/'pending' (z.B. frisch requeued) NICHT
                # anfassen -> eine alte FAIL-Logzeile re-failt keinen Retry.
                if c["status"] != "uploaded":
                    continue
                if os.path.splitext(c["name"])[0] in failset:
                    self.db.set_clip_status(c["name"], "failed")
                    log(f"Node {iid}: Clip '{c['name']}' als FEHLER markiert (FAIL im Log).")

    # ========================================================================
    #  Arbeits-Tick (~poll_interval): Vast-Sync, Verteilung, Service, Aufräumen
    # ========================================================================
    def work_tick(self) -> None:
        new = self.ingest.scan()
        if new:
            log(f"{new} neue Rohclips in die Queue aufgenommen.")
        self.sync_vast()
        self._poke_bootstraps()
        self.distribute()
        self.dispatch_service()
        if self.maybe_destroy():
            log("Alle Nodes zerstört. Lauf abgeschlossen.")
        self.reap_drained()
        self.db.prune_commands()
        self._last_work_wall = time.time()
        c = self.db.counts()
        log(f"Queue {dict(c)} | {self.cost_line()}")

    def sync_vast(self) -> None:
        """Gleicht DB-Nodes mit dem realen Vast-Zustand ab (SSH-Zugang, Tod)."""
        live = {int(i["id"]): i for i in self.vast.show_instances()}
        for node in self.db.active_nodes():
            iid = node["instance_id"]
            inst = live.get(iid)
            if inst is None:
                n = self.db.reassign_node_clips(iid)
                self.db.update_node(iid, status="destroyed")
                log(f"Node {iid} verschwunden — {n} Clips neu eingereiht.")
                continue
            host = inst.get("ssh_host")
            port = inst.get("ssh_port")
            if host and port and (host != node["ssh_host"] or port != node["ssh_port"]):
                self.db.update_node(iid, ssh_host=host, ssh_port=int(port))
                log(f"Node {iid}: SSH {host}:{port}")
        orphans = self.db.reassign_orphan_clips()
        if orphans:
            log(f"{orphans} verwaiste Clips (tote Node) neu eingereiht.")

    def _poke_bootstraps(self) -> None:
        """Stößt für gebuchte, noch-nicht-ready Nodes den Bootstrap an (idempotent,
        selbstheilend). Läuft im Heavy-Pool, damit der SSH-Call den Tick nicht hält."""
        for node in self.db.active_nodes():
            if node["status"] != "booked" or not node["ssh_host"]:
                continue
            iid = node["instance_id"]
            if not self._acquire(iid, "bootstrap"):
                continue
            try:
                self._pool.submit(self._run_bootstrap_poke, iid)
            except Exception as e:  # noqa: BLE001 — Flag nie leaken
                self._release(iid)
                log(f"Node {iid}: Bootstrap-Dispatch fehlgeschlagen: {e}")

    def _run_bootstrap_poke(self, iid: int) -> None:
        try:
            node = self.db.get_node(iid)
            r = self._remote(node) if node else None
            if r is None:
                return
            if r.start_bootstrap(self.cfg.repo_raw_url) and not node["bootstrap_started"]:
                self.db.update_node(iid, bootstrap_started=1)
                log(f"Node {iid}: Bootstrap per SSH angestoßen.")
        except Exception as e:  # noqa: BLE001
            log(f"Node {iid}: Bootstrap-Poke-Fehler: {e}")
        finally:
            self._release(iid)

    def distribute(self) -> None:
        """Weist pending Clips kapazitätsgewichtet den ready-Nodes zu."""
        nodes = [n for n in self.db.active_nodes() if n["status"] == "ready"]
        if not nodes:
            return
        pending = self.db.pending_clips()
        if not pending:
            return
        slots: list[int] = []
        for n in nodes:
            slots += [n["instance_id"]] * max(1, n["num_gpus"])
        with self.db.tx():
            for i, clip in enumerate(pending):
                self.db.assign_clip(clip["name"], slots[i % len(slots)])
        log(f"{len(pending)} Clips auf {len(nodes)} Node(s) verteilt.")

    def dispatch_service(self) -> None:
        """Dispatcht pro Node EINEN Service-Task in den Heavy-Pool (single-flight):
        Ergebnisse pullen, Modelle pushen, Worker starten, Clips hochladen."""
        for node in self.db.active_nodes():
            if node["status"] not in ("ready", "draining"):
                continue
            if not node["ssh_host"]:
                continue
            iid = node["instance_id"]
            if not self._acquire(iid, "service"):
                continue
            try:
                self._pool.submit(self._run_service, iid)
            except Exception as e:  # noqa: BLE001 — Flag nie leaken
                self._release(iid)
                log(f"Node {iid}: Service-Dispatch fehlgeschlagen: {e}")

    def _run_service(self, iid: int) -> None:
        try:
            node = self.db.get_node(iid)
            if node is None:
                return
            r = self._remote(node)
            if r is None:
                return
            # 1) Immer zuerst fertige Ergebnisse einsammeln (auch bei draining).
            self._pull_and_mark(node)
            if node["status"] != "ready":
                return

            # 2) SeedVR2-Modelle EINMAL pushen (Worker startet erst danach).
            if not node["models_pushed"]:
                if not self._push_models(node):
                    return  # Modelle fehlen/Push offen -> Worker wartet.

            # 3) Worker früh starten (idempotent).
            if not r.worker_running():
                if r.start_worker():
                    self.db.update_node(iid, worker_started=1)
                    log(f"Node {iid}: Worker gestartet (process.sh, detached).")

            # 4) Clips flow-controlled hochladen (in Arbeit = GPUs × Puffer).
            node_gpus = node["num_gpus"] or 1
            target = max(1, node_gpus * self.cfg.inflight_per_gpu)
            backlog = len(self.db.clips_for_node(iid, status="uploaded"))
            room = target - backlog
            if room > 0:
                assigned = self.db.clips_for_node(iid, status="assigned")
                to_push = [c for c in assigned[:room]
                           if os.path.isfile(os.path.join(self.cfg.raw_dir, c["name"]))]
                paths = [os.path.join(self.cfg.raw_dir, c["name"]) for c in to_push]
                if paths and r.push_files(paths):
                    # Geführter Übergang: nur markieren, wenn der Clip noch dieser
                    # Node als 'assigned' gehört (ein zwischenzeitliches drain/
                    # requeue gewinnt -> keine 'uploaded'-Zeile ohne node_id).
                    marked = 0
                    with self.db.tx():
                        for c in to_push:
                            if self.db.mark_uploaded(c["name"], iid):
                                marked += 1
                    log(f"Node {iid}: {len(paths)} Clips hochgeladen "
                        f"({marked} markiert, in Arbeit {backlog + marked}/{target}).")
        except Exception as e:  # noqa: BLE001
            log(f"Node {iid}: Service-Fehler: {e}")
        finally:
            self._release(iid)

    def _push_models(self, node) -> bool:
        """Pusht die SeedVR2-Modelle einmalig auf die Node. True bei Erfolg."""
        iid = node["instance_id"]
        have = all(os.path.isfile(os.path.join(self.cfg.models_dir, n))
                   for n in SEEDVR2_MODEL_FILES)
        if not have:
            log(f"Node {iid}: SeedVR2-Modelle fehlen im Home-Cache "
                f"({self.cfg.models_dir}) — bitte 'vhsorch fetch-models' laufen lassen.")
            return False
        r = self._remote(node)
        if r is None:
            return False
        log(f"Node {iid}: pushe SeedVR2-Modelle (einmalig, ~4 GB) …")
        if r.push_models(self.cfg.models_dir):
            self.db.update_node(iid, models_pushed=1)
            log(f"Node {iid}: Modelle bereitgestellt.")
            return True
        log(f"Node {iid}: Modell-Push fehlgeschlagen/Timeout — nächster Takt erneut.")
        return False

    def _pull_and_mark(self, node) -> None:
        """Zieht Ergebnisse der Node und markiert die zugehörigen Clips als done."""
        r = self._remote(node)
        if r is None:
            return
        iid = node["instance_id"]
        r.pull_results(self.cfg.done_dir)
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

    def collect(self) -> None:
        """Synchrones Einsammeln von ALLEN bereiten Nodes (für `vhsorch pull`)."""
        for node in self.db.active_nodes():
            if node["status"] not in ("ready", "draining"):
                continue
            self._pull_and_mark(node)

    # -- Kostenschutz ---------------------------------------------------------
    def maybe_destroy(self) -> bool:
        """Zerstört alle Nodes, wenn die Queue leer ist. True = zerstört."""
        if not self.cfg.auto_destroy:
            return False
        if not self.db.all_done():
            return False
        counts = self.db.counts()
        if not counts:
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

    def reap_drained(self) -> None:
        """Zerstört 'draining' Nodes, sobald sie keine Arbeit mehr tragen."""
        for node in self.db.active_nodes():
            if node["status"] != "draining":
                continue
            iid = node["instance_id"]
            inflight = (self.db.clips_for_node(iid, status="assigned")
                        + self.db.clips_for_node(iid, status="uploaded"))
            if inflight:
                continue
            if self._busy_now(iid):
                continue  # noch ein Service-/Pull-Task offen
            try:
                self.vast.destroy_instance(iid)
                self.db.update_node(iid, status="destroyed")
                log(f"Node {iid}: drain abgeschlossen — zerstört (Kostenstopp).")
            except Exception as e:  # noqa: BLE001
                log(f"FEHLER beim Zerstören (drain) von {iid}: {e}")

    def cost_line(self) -> str:
        now = time.time()
        nodes = self.db.active_nodes()
        total_dph = sum(n["dph"] or 0.0 for n in nodes)
        # Pro-Node-Summe: jede Node zählt mit IHREM Alter (nicht Flottenrate ×
        # Alter des ältesten Nodes — das überschätzt bei ungleich alten Nodes).
        accrued = sum((n["dph"] or 0.0) * (now - (n["created_at"] or now)) / 3600
                      for n in nodes)
        return (f"Aktive Nodes: {len(nodes)} | {total_dph:.3f} $/h | "
                f"grob aufgelaufen: {accrued:.2f} $")

    # ========================================================================
    #  Snapshot (der einzige Live-Datenkanal der TUI)
    # ========================================================================
    def _gpu_progress(self, iid: int, gpu: int, state: str, clip: str,
                      phase: str, pct: str) -> tuple[int, int]:
        """Monotoner Fein-% (innerhalb der Phase) + Gesamt-% (über alle Phasen).

        Der rohe tqdm-% springt, weil SeedVR2 mehrere Bars nacheinander ausgibt.
        Wir klemmen den angezeigten Phasen-% auf monoton pro (Node,GPU,Clip) und
        rechnen daraus einen ebenfalls monotonen Gesamt-Fortschritt.
        """
        key = (iid, gpu)
        if state != "busy" or not clip:
            self._mono.pop(key, None)
            return -1, -1
        try:
            pval = int(pct.rstrip("%")) if pct else -1
        except ValueError:
            pval = -1
        prev = self._mono.get(key)
        if prev and prev[0] == clip:
            pval = max(prev[1], pval)          # nie rückwärts innerhalb desselben Clips
        self._mono[key] = (clip, pval)

        lo, hi = _PHASE_RANGE.get(phase, (0, 0))
        if pval >= 0:
            overall = lo + (hi - lo) * pval / 100.0
        else:
            overall = lo                        # Phase ohne tqdm -> Phasenstart
        return pval, int(round(overall))

    def _build_snapshot(self) -> dict:
        now = time.time()
        counts = self.db.counts()
        by_node = self.db.counts_by_node()
        active = self.db.active_nodes()

        dph_total = sum(n["dph"] or 0.0 for n in active)
        oldest = min((n["created_at"] for n in active), default=now)
        hours = (now - oldest) / 3600 if active else 0.0

        nodes_out = []
        for n in active:
            iid = n["instance_id"]
            pc = self._probes.get(iid)
            p = pc["data"] if pc else None
            probe_age = round(now - pc["at"], 1) if pc else None
            reachable = bool(p and p.get("reachable"))
            ncounts = by_node.get(iid, {})

            act_by_gpu = {g: (st, clip, ph, pct)
                          for (g, st, clip, ph, pct) in (p.get("gpus_activity", []) if p else [])}
            stats = p.get("gpu_stats", {}) if p else {}
            finals = {os.path.splitext(f)[0] for f in (p.get("final", []) if p else [])}

            ngpu = n["num_gpus"] or (max(act_by_gpu, default=-1) + 1)
            gpus = []
            for g in range(ngpu):
                st, clip, ph, pct = act_by_gpu.get(g, ("waiting", "", "", ""))
                fine, overall = self._gpu_progress(iid, g, st, clip, ph, pct)
                util, used, total = stats.get(g, (None, None, None))
                gpus.append({
                    "index": g, "state": st, "clip": clip, "phase": ph,
                    "step": _PHASE_STEP.get(ph, ""),
                    "pct": fine, "progress": overall,
                    "util": util, "vram_used_mib": used, "vram_total_mib": total,
                })

            busy_gpus = sum(1 for x in gpus if x["state"] == "busy")
            # auf Node fertig, aber lokal noch nicht als done markiert (Pull offen).
            node_done_pending = sum(
                1 for c in self.db.clips_for_node(iid)
                if c["status"] not in ("done",) and os.path.splitext(c["name"])[0] in finals
            )

            nodes_out.append({
                "instance_id": iid,
                "gpu_name": n["gpu_name"],
                "num_gpus": n["num_gpus"],
                "dph": n["dph"],
                "status": n["status"],
                "ssh": (f"{n['ssh_host']}:{n['ssh_port']}"
                        if n["ssh_host"] and n["ssh_port"] else None),
                "reachable": reachable,
                "busy": self._busy_now(iid),
                "flags": {
                    "bootstrap_started": bool(n["bootstrap_started"]),
                    "models_pushed": bool(n["models_pushed"]),
                    "worker_running": bool(p.get("worker_running")) if p else False,
                    "worker_started": bool(n["worker_started"]),
                },
                "bootstrap_status": p.get("bootstrap_status", "") if p else "",
                "log_tail": p.get("log_tail", []) if p else [],
                "created_at": n["created_at"],
                "uptime_h": round((now - (n["created_at"] or now)) / 3600, 2),
                "cost_accrued": round((n["dph"] or 0.0)
                                      * (now - (n["created_at"] or now)) / 3600, 2),
                "clips": {
                    "assigned": ncounts.get("assigned", 0),
                    "uploaded": ncounts.get("uploaded", 0),
                    "done": ncounts.get("done", 0),
                    "failed": ncounts.get("failed", 0),
                    "node_done_pending_pull": node_done_pending,
                },
                "busy_gpus": busy_gpus,
                "gpus": gpus,
                "probe_age_s": probe_age,
                # Stuck-Heuristik-Signal: ready, aber keine GPU aktiv trotz Backlog.
                "idle_with_backlog": bool(
                    n["status"] == "ready" and busy_gpus == 0
                    and (ncounts.get("uploaded", 0) > 0)),
            })

        return {
            "schema": SNAPSHOT_SCHEMA,
            "generated_at": now,
            "scheduler": {
                "started_at": self._started_wall,
                "last_tick_at": self._last_work_wall or None,
                "last_probe_at": self._last_probe_wall or None,
                "poll_interval": self.cfg.poll_interval,
                "probe_interval": self.cfg.probe_interval,
                "auto_destroy": self.cfg.auto_destroy,
            },
            "queue": {
                "pending": counts.get("pending", 0),
                "assigned": counts.get("assigned", 0),
                "uploaded": counts.get("uploaded", 0),
                "done": counts.get("done", 0),
                "failed": counts.get("failed", 0),
                "total": sum(counts.values()),
            },
            "cost": {
                "dph_total": round(dph_total, 3),
                "hours": round(hours, 2),
                # Pro-Node-Summe (jede Node mit ihrem eigenen Alter) — korrekt
                # auch bei ungleich alten Nodes (nachgebucht mitten im Lauf).
                "accrued": round(sum((n["dph"] or 0.0)
                                     * (now - (n["created_at"] or now)) / 3600
                                     for n in active), 2),
            },
            "nodes": nodes_out,
            "commands": [dict(c) for c in self.db.recent_commands(25)],
        }

    def _write_snapshot(self) -> None:
        try:
            snap = self._build_snapshot()
        except Exception as e:  # noqa: BLE001 — Snapshot darf den Loop nie killen
            log(f"Snapshot-Bau-Fehler: {e}")
            return
        path = self.cfg.snapshot_path
        tmp = path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(snap, fh, ensure_ascii=False)
            os.replace(tmp, path)   # atomar (POSIX-Rename) — nie halbfertig lesbar
        except OSError as e:
            log(f"Snapshot-Schreibfehler: {e}")
