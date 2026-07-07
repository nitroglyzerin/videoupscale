"""CLI: plan / book / run / status / nodes / destroy.

Buchungs-Handbremse (Zwei-Schritt):
  1) `plan`  — sucht Offers, zeigt die Top-Kandidaten mit IDs & Preis.
  2) `book <offer-id>` — bucht GENAU das gewählte Offer (mit onstart-Bootstrap).
Nichts wird ohne deine explizite `book`-Aktion gebucht.
"""
from __future__ import annotations

import argparse
import sys

from . import report
from .config import Config
from .db import DB
from .scheduler import Scheduler
from .vast import VastClient


def _bootstrap_onstart(cfg: Config) -> str:
    """onstart-cmd der Node: lädt bootstrap.sh aus dem public Repo und führt ihn aus."""
    env = f'export REPO_RAW_URL="{cfg.repo_raw_url}"; '
    if cfg.ssh_pubkey:
        env += f'export SSH_PUBKEY="{cfg.ssh_pubkey}"; '
    return (
        "bash -lc '" + env +
        f'curl -fsSL {cfg.repo_raw_url}/node/bootstrap.sh | bash'
        "'"
    )


def cmd_plan(cfg: Config, args) -> int:
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    offers = vast.search_offers(disk_gb=cfg.vast_disk_gb, min_gpus=args.min_gpus)
    if not offers:
        print("Keine passenden Offers gefunden (Filter: RTX 4090/5090, verified, "
              f">= {args.min_gpus} GPUs, reliability >= 99.5%, disk >= {cfg.vast_disk_gb} GB).")
        return 1
    top = offers[: args.top]
    print(f"\nTop-{len(top)} Offers (sortiert nach DLPerf/$/h):\n")
    print(f"{'#':>2}  {'OFFER-ID':>9}  {'GPU':<12} {'GPUs':>4}  {'$/h':>7}  "
          f"{'DLPerf/$':>9}  {'Rel.':>6}  {'Disk':>6}  Ort")
    for i, o in enumerate(top, 1):
        print(f"{i:>2}  {o.id:>9}  {o.gpu_name:<12} {o.num_gpus:>4}  {o.dph_total:>7.3f}  "
              f"{o.dlperf_per_dph:>9.1f}  {o.reliability*100:>5.1f}%  "
              f"{o.disk_space:>5.0f}G  {o.geolocation}")
    print("\nZum Buchen:  vhsorch book <OFFER-ID>\n")
    return 0


def cmd_book(cfg: Config, args) -> int:
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    db = DB(cfg.db_path)

    # Offer vor der Buchung erneut verifizieren (Preis/Verfügbarkeit).
    offers = {o.id: o for o in vast.search_offers(disk_gb=cfg.vast_disk_gb, min_gpus=1)}
    offer = offers.get(args.offer_id)
    if offer is None:
        print(f"Offer {args.offer_id} nicht (mehr) verfügbar oder außerhalb der Filter. "
              "Führe erneut `plan` aus.")
        return 1

    print(f"Buche Offer {offer.id}: {offer.gpu_name} x{offer.num_gpus} @ "
          f"{offer.dph_total:.3f} $/h ({offer.geolocation}) …")
    onstart = _bootstrap_onstart(cfg)
    instance_id = vast.create_instance(
        offer_id=offer.id, image=cfg.vast_image, disk_gb=cfg.vast_disk_gb,
        onstart_cmd=onstart,
    )
    db.add_node(
        instance_id=instance_id, offer_id=offer.id, gpu_name=offer.gpu_name,
        num_gpus=offer.num_gpus, dph=offer.dph_total, status="booked",
    )
    print(f"Gebucht. Instanz-ID {instance_id}. Bootstrap läuft per onstart.")
    print("Starte danach den Loop mit:  vhsorch run")
    return 0


def cmd_run(cfg: Config, args) -> int:
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    db = DB(cfg.db_path)
    Scheduler(cfg, db, vast).run_forever()
    return 0


def cmd_status(cfg: Config, args) -> int:
    db = DB(cfg.db_path)
    counts = db.counts()
    print("Queue:", dict(counts) if counts else "(leer)")
    print("\nNodes:")
    for n in db.all_nodes():
        print(f"  #{n['instance_id']}  {n['gpu_name']} x{n['num_gpus']}  "
              f"{n['dph']:.3f}$/h  status={n['status']}  "
              f"ssh={n['ssh_host']}:{n['ssh_port']}")
    return 0


def cmd_nodes(cfg: Config, args) -> int:
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    insts = vast.show_instances()
    if not insts:
        print("Keine laufenden Vast-Instanzen.")
        return 0
    for i in insts:
        print(f"  #{i.get('id')}  {i.get('gpu_name')} x{i.get('num_gpus')}  "
              f"{i.get('dph_total'):.3f}$/h  {i.get('actual_status')}  "
              f"ssh={i.get('ssh_host')}:{i.get('ssh_port')}")
    return 0


def cmd_workmap(cfg: Config, args) -> int:
    """Zeigt, welche GPU welcher Node gerade an welchem Video arbeitet."""
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    db = DB(cfg.db_path)
    print(report.render_workmap(cfg, db, vast))
    return 0


def cmd_videos(cfg: Config, args) -> int:
    """Video-Liste mit Zustand + Kostenschätzung (gesamt und pro Video)."""
    db = DB(cfg.db_path)
    # Vor der Anzeige verwaiste Clips (tote Node, nie eingesammelt) heilen,
    # damit die Liste nicht faelschlich 'zugewiesen' zeigt.
    orphans = db.reassign_orphan_clips()
    limit = None if getattr(args, "all", False) else args.limit
    print(report.render_videos(cfg, db, limit=limit))
    if orphans:
        print(f"\n{report._WARN}{orphans} verwaiste Clips (tote Node) "
              f"zurück auf 'wartet' gesetzt.{report._RST}")
    return 0


def cmd_pull(cfg: Config, args) -> int:
    """Nur einsammeln: fertige Ergebnisse von allen Nodes ziehen (RETTUNG).

    Verteilt NICHTS und zerstört NICHTS — sicher, um vor einem geplanten
    Destroy alle bereits fertigen Clips nach Hause zu holen. Kann beliebig oft
    laufen (rsync ist idempotent).
    """
    db = DB(cfg.db_path)
    sched = Scheduler(cfg, db, vast=None)   # vast wird von collect() nicht gebraucht
    before = db.counts().get("done", 0)
    sched.collect()
    after = db.counts().get("done", 0)
    print(f"Pull fertig. Heruntergeladen (done): {before} -> {after} "
          f"(+{after - before}). Ziel: {cfg.done_dir}")
    return 0


def cmd_fetch_models(cfg: Config, args) -> int:
    """Füllt den Home-Modell-Cache (models_dir) einmalig von HuggingFace.

    Läuft auf dem gut angebundenen Orchestrator (nicht auf den Nodes). Danach
    pusht der Scheduler die Modelle per rsync auf jede Node (kein HF auf Nodes).
    """
    import os
    import urllib.request
    from .config import SEEDVR2_MODEL_FILES

    os.makedirs(cfg.models_dir, exist_ok=True)
    for name, url in SEEDVR2_MODEL_FILES.items():
        dst = os.path.join(cfg.models_dir, name)
        if os.path.isfile(dst) and os.path.getsize(dst) > 10_000_000:
            print(f"{name}: bereits vorhanden ({os.path.getsize(dst)//1024//1024} MB) — überspringe.")
            continue
        print(f"lade {name} …")
        tmp = dst + ".part"
        urllib.request.urlretrieve(url, tmp)
        os.replace(tmp, dst)
        print(f"  fertig: {os.path.getsize(dst)//1024//1024} MB")
    print(f"Modell-Cache bereit: {cfg.models_dir}")
    return 0


def cmd_reconcile(cfg: Config, args) -> int:
    """Setzt verwaiste Clips (Node zerstört, Ergebnis nie geholt) zurück."""
    db = DB(cfg.db_path)
    n = db.reassign_orphan_clips()
    print(f"{n} verwaiste Clips zurück auf 'pending' gesetzt.")
    return 0


def cmd_destroy(cfg: Config, args) -> int:
    cfg.require_api_key()
    vast = VastClient(cfg.vast_api_key)
    db = DB(cfg.db_path)
    if args.target == "all":
        targets = [n["instance_id"] for n in db.active_nodes()]
    else:
        targets = [int(args.target)]
    for iid in targets:
        print(f"Zerstöre Instanz {iid} …")
        vast.destroy_instance(iid)
        db.reassign_node_clips(iid)
        db.update_node(iid, status="destroyed")
    print("Fertig.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="vhsorch", description="VHS-Upscale-Orchestrator")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("plan", help="Offers suchen und Top-Kandidaten zeigen")
    sp.add_argument("--top", type=int, default=3)
    sp.add_argument("--min-gpus", type=int, default=4, dest="min_gpus")
    sp.set_defaults(func=cmd_plan)

    sp = sub.add_parser("book", help="ein Offer buchen (nach `plan`)")
    sp.add_argument("offer_id", type=int)
    sp.set_defaults(func=cmd_book)

    sp = sub.add_parser("run", help="Scheduler-Loop starten (push/pull/auto-destroy)")
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("status", help="lokalen Queue-/Node-Status zeigen")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("nodes", help="laufende Vast-Instanzen abfragen")
    sp.set_defaults(func=cmd_nodes)

    sp = sub.add_parser("workmap", help="welche GPU/Node arbeitet an welchem Video (live)")
    sp.set_defaults(func=cmd_workmap)

    sp = sub.add_parser("videos", help="Video-Liste mit Zustand + Kosten pro Video")
    sp.add_argument("--limit", type=int, default=40,
                    help="max. Zeilen (Default 40)")
    sp.add_argument("--all", action="store_true", help="alle Clips zeigen")
    sp.set_defaults(func=cmd_videos)

    sp = sub.add_parser("pull", help="fertige Ergebnisse jetzt einsammeln (kein Verteilen/Destroy)")
    sp.set_defaults(func=cmd_pull)

    sp = sub.add_parser("fetch-models", help="SeedVR2-Modelle einmalig in den Home-Cache laden")
    sp.set_defaults(func=cmd_fetch_models)

    sp = sub.add_parser("reconcile", help="verwaiste Clips (tote Node) zurücksetzen")
    sp.set_defaults(func=cmd_reconcile)

    sp = sub.add_parser("destroy", help="Instanz(en) zerstören: <id> oder 'all'")
    sp.add_argument("target")
    sp.set_defaults(func=cmd_destroy)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv if argv is not None else sys.argv[1:])
    cfg = Config.from_env()
    return args.func(cfg, args)


if __name__ == "__main__":
    raise SystemExit(main())
