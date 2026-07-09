"""Textual-TUI: interaktive Steuerung des Orchestrators.

Architektur (siehe REDESIGN.md): Diese TUI ist ein REINER LESER des vom
Scheduler geschriebenen Snapshots (state_dir/snapshot.json) + Schreiber in die
Command-Queue (SQLite-Tabelle `commands`). Sie SSHt NIE und startet keinen
Container — deshalb bleibt sie flüssig, egal wie langsam eine Node/Vast ist.

  * Alle ~0,5 s: Snapshot neu einlesen -> Widgets neu rendern (kein SSH).
  * Aktion (Toggle/Destroy/…): winzige Zeile in `commands` -> der Scheduler
    führt sie aus (idempotent). Vast-API (Offers suchen/buchen) läuft im
    Worker-Thread, damit die UI nicht blockiert.

Start:  docker compose run --rm -it orchestrator tui
   (oder in den laufenden Loop-Container:  docker compose exec orchestrator python -m vhsorch tui)
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import Footer, Header, Static

from .config import Config
from .db import DB

# ---------------------------------------------------------------------------
#  Snapshot laden (robust — die UI darf an keinem I/O-Fehler crashen).
# ---------------------------------------------------------------------------
def load_snapshot(path: str) -> Optional[dict]:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _snapshot_age(snap: Optional[dict]) -> Optional[float]:
    if not snap:
        return None
    gen = snap.get("generated_at")
    return (time.time() - gen) if gen else None


def _is_stale(snap: Optional[dict]) -> bool:
    """True, wenn der Snapshot zu alt ist (Scheduler hängt/steht)."""
    age = _snapshot_age(snap)
    if age is None:
        return True
    probe_iv = (snap.get("scheduler") or {}).get("probe_interval") or 5
    return age > max(12.0, 3 * probe_iv)


def _lane_stalls(snap: Optional[dict]) -> list[str]:
    """Erkennt eine hängende Work-/Probe-Lane, OBWOHL der schnelle Haupt-Loop
    den Snapshot weiter frisch schreibt (generated_at bleibt jung). Auswertung
    der Lane-Heartbeats last_tick_at/last_probe_at. None = noch nie -> kein Alarm.
    """
    s = (snap or {}).get("scheduler") or {}
    now = time.time()
    out: list[str] = []
    lt = s.get("last_tick_at")
    poll = s.get("poll_interval") or 30
    if lt and now - lt > max(90.0, 4 * poll):
        out.append(f"Arbeits-Tick hängt ({int(now - lt)}s ohne Tick — Vast-API blockiert?)")
    lp = s.get("last_probe_at")
    piv = s.get("probe_interval") or 5
    if lp and now - lp > max(30.0, 6 * piv):
        out.append(f"Probe hängt ({int(now - lp)}s ohne Probe)")
    return out


# ---------------------------------------------------------------------------
#  Kleine Render-Helfer.
# ---------------------------------------------------------------------------
_STATUS_BADGE = {
    "booked": ("bootet", "yellow"),
    "ready": ("bereit", "green"),
    "draining": ("drainend", "yellow"),
    "destroyed": ("zerstört", "red"),
}
_PHASES = [("Denoise", 4), ("Upscale", 12), ("Audio", 3)]


def _bar(done: int, total: int, width: int = 30) -> Text:
    total = max(1, total)
    filled = max(0, min(width, round(width * done / total)))
    t = Text()
    t.append("█" * filled, style="green")
    t.append("░" * (width - filled), style="grey37")
    return t


def _phase_bar(phase: str, pct: int, progress: int) -> Text:
    """Segmentierter Phasen-Balken (erledigt=grün, laufend=anteilig, offen=grau)."""
    names = [p[0] for p in _PHASES]
    cur = names.index(phase) if phase in names else -1
    t = Text()
    for i, (name, w) in enumerate(_PHASES):
        t.append(f"{name} ")
        if cur >= 0 and i < cur:
            t.append("█" * w, style="green")
        elif i == cur:
            f = max(0, min(w, round(w * pct / 100))) if pct >= 0 else w
            if pct >= 0:
                t.append("█" * f, style="yellow")
                t.append("░" * (w - f), style="grey37")
            else:
                t.append("▓" * w, style="yellow")
        else:
            t.append("░" * w, style="grey37")
        t.append("   ")
    if progress >= 0:
        t.append(f" {progress}%", style="bold")
    return t


def _flag(label: str, on: bool, extra: str = "") -> Text:
    t = Text()
    t.append(f"{label}: ", style="bold")
    if on:
        t.append("✓", style="green")
    else:
        t.append("—", style="grey50")
    if extra:
        t.append(f"  {extra}", style="grey62")
    return t


def _busy_desc(busy: Optional[str]) -> tuple[str, str]:
    """Klartext + Farbe zum busy-Flag einer Node (statt rohem 'cmd:models')."""
    if not busy:
        return "bereit für Aktionen", "grey50"
    if "models" in busy:
        return "Modelle-Push läuft (~4 GB — kann einige Minuten dauern) …", "yellow"
    if busy == "service":
        return "Node wird bedient (Modelle/Worker/Upload) …", "yellow"
    if "bootstrap" in busy:
        return "Bootstrap läuft …", "yellow"
    if "pull" in busy:
        return "Ergebnisse werden gepullt …", "yellow"
    if "destroy" in busy:
        return "wird zerstört …", "red"
    return f"läuft: {busy}", "yellow"


# ===========================================================================
#  Modals
# ===========================================================================
class ConfirmScreen(ModalScreen):
    """Ja/Nein-Bestätigung. Ruft on_confirm() bei 'j'."""

    BINDINGS = [
        Binding("j,y,enter", "yes", "Ja"),
        Binding("n,escape", "no", "Nein"),
    ]

    def __init__(self, question: str, on_confirm) -> None:
        super().__init__()
        self._question = question
        self._on_confirm = on_confirm

    def compose(self) -> ComposeResult:
        with Center():
            yield Static(
                Panel(Text(f"{self._question}\n\n[j] Ja    [n] Nein", justify="center"),
                      title="Bestätigen", border_style="yellow"))

    def action_yes(self) -> None:
        self.app.pop_screen()
        self._on_confirm()

    def action_no(self) -> None:
        self.app.pop_screen()


class LogScreen(ModalScreen):
    """Zeigt die letzten Commands + Bootstrap-Status je Node (aus dem Snapshot)."""

    BINDINGS = [Binding("escape,l,q", "close", "Zurück")]

    def compose(self) -> ComposeResult:
        with Vertical(id="logbox"):
            yield Static(id="logbody")

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(0.5, self._repaint)

    def _repaint(self) -> None:
        snap = self.app.snap
        t = Table.grid(padding=(0, 1))
        t.add_column(justify="right")
        t.add_column()
        t.add_column()
        cmds = (snap or {}).get("commands", [])
        if not cmds:
            t.add_row("", "(noch keine Befehle)", "")
        for c in cmds[:20]:
            st = c.get("status", "?")
            color = {"done": "green", "failed": "red",
                     "running": "yellow", "queued": "grey62"}.get(st, "white")
            node = c.get("node_id")
            t.add_row(
                Text(st, style=color),
                Text(f"{c.get('action')}" + (f" #{node}" if node else "")),
                Text(str(c.get("result") or ""), style="grey62"))
        self.query_one("#logbody", Static).update(
            Panel(t, title="Befehle & Log (Esc = zurück)", border_style="cyan"))

    def action_close(self) -> None:
        self.app.pop_screen()


class NodeLogScreen(ModalScreen):
    """Live-Tail der Node-Logs (run.log + gpuN.log) zum Lurken — aus dem Snapshot.

    Kein eigener SSH: die Probe zieht den Tail alle paar Sekunden mit; diese
    Ansicht liest ihn nur (Refresh ~0,5 s). Zeigt also 'was gerade im aktiven
    Prozess passiert' mit ein paar Sekunden Verzögerung.
    """

    BINDINGS = [Binding("escape,l,q", "close", "Zurück")]

    def __init__(self, instance_id: int) -> None:
        super().__init__()
        self.instance_id = instance_id

    def compose(self) -> ComposeResult:
        with Vertical(id="logbox"):
            yield Static(id="nodelogbody")

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(0.5, self._repaint)

    def _repaint(self) -> None:
        if not self.is_mounted:
            return
        node = None
        for n in (self.app.snap or {}).get("nodes", []):
            if n["instance_id"] == self.instance_id:
                node = n
                break
        piv = ((self.app.snap or {}).get("scheduler") or {}).get("probe_interval", 5)
        if node is None:
            body = Text("Node ist nicht mehr aktiv.", style="yellow")
        else:
            lines = node.get("log_tail") or []
            if not lines:
                if not (node.get("flags") or {}).get("models_pushed"):
                    body = Text("(noch keine Node-Logs — der Modell-Push läuft "
                                "ORCHESTRATOR-seitig (rsync), steht also nicht hier. "
                                "Fortschritt: Zeile 'Modelle' in der Node-Ansicht, "
                                "oder 'docker compose logs -f'.)", style="grey62")
                else:
                    body = Text("(noch keine Logausgabe — Worker läuft evtl. noch nicht, "
                                "oder die Probe war noch nicht dran)", style="grey62")
            else:
                body = Text()
                for ln in lines:
                    if ln.startswith("── "):
                        style = "bold cyan"
                    elif "FAIL" in ln or "FEHLER" in ln or "Error" in ln or "error" in ln:
                        style = "red"
                    elif "FERTIG" in ln or "START" in ln or "PHASE" in ln:
                        style = "green"
                    else:
                        style = "grey74"
                    body.append(ln + "\n", style=style)
        self.query_one("#nodelogbody", Static).update(
            Panel(body, title=f"Node #{self.instance_id} — Log  (Esc = zurück · "
                              f"aktualisiert alle ~{piv}s)", border_style="cyan"))

    def action_close(self) -> None:
        self.app.pop_screen()


class VideosScreen(ModalScreen):
    """Clip-Liste je Status aus der DB (namentlich), mit Retry-Hinweis."""

    BINDINGS = [
        Binding("escape,v,q", "close", "Zurück"),
        Binding("r", "requeue_failed", "Fehler erneut einreihen"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="vidbox"):
            yield Static(id="vidbody")

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(1.0, self._repaint)

    def _repaint(self) -> None:
        rows = self.app.db.clips_with_gpu()
        t = Table.grid(padding=(0, 2))
        t.add_column()
        t.add_column()
        t.add_column()
        color = {"done": "green", "failed": "red", "uploaded": "yellow",
                 "assigned": "grey62", "pending": "grey50"}
        shown = 0
        for c in rows:
            if shown >= 40:
                break
            st = c["status"]
            t.add_row(Text(st, style=color.get(st, "white")),
                      Text(c["gpu_name"] or "—", style="grey62"),
                      Text(c["name"]))
            shown += 1
        self.query_one("#vidbody", Static).update(
            Panel(t, title=f"Videos ({len(rows)}) — [r]=Fehler erneut · Esc=zurück",
                  border_style="cyan"))

    def action_requeue_failed(self) -> None:
        rows = self.app.db.clips_with_gpu()
        n = 0
        for c in rows:
            if c["status"] == "failed":
                self.app.db.add_command("requeue", arg=c["name"])
                n += 1
        self.app.notify(f"{n} fehlgeschlagene Clips zum Retry eingereiht.")

    def action_close(self) -> None:
        self.app.pop_screen()


class DestroyScreen(ModalScreen):
    """Zerstören-Auswahl: einzelne Node (Zifferntaste) oder [a] alle."""

    BINDINGS = [
        Binding("escape,q", "close", "Zurück"),
        Binding("a", "destroy_all", "Alle zerstören"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="destroybox"):
            yield Static(id="destroybody")

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(0.5, self._repaint)

    def _snap_nodes(self) -> list:
        return (self.app.snap or {}).get("nodes", [])

    def _repaint(self) -> None:
        if not self.is_mounted:
            return
        t = Table.grid(padding=(0, 2))
        t.add_column(justify="right")
        t.add_column()
        nodes = self._snap_nodes()
        if not nodes:
            t.add_row("", Text("Keine aktiven Nodes.", style="grey62"))
        for i, n in enumerate(nodes, 1):
            line = Text()
            line.append(f"#{n['instance_id']} ", style="bold")
            line.append(f"{n['gpu_name']} x{n['num_gpus']}  ", style="grey70")
            line.append(f"{(n['dph'] or 0):.3f} $/h", style="grey62")
            t.add_row(Text(f"[{i}]", style="cyan"), line)
        hint = Text("Zifferntaste = einzelne Node · [a] = ALLE · Esc = zurück",
                    style="grey62")
        self.query_one("#destroybody", Static).update(
            Panel(Group(t, Text(""), hint), title="Zerstören (Kostenstopp)",
                  border_style="red"))

    def _enqueue_destroy(self, node_id: int) -> None:
        if _is_stale(self.app.snap) or _lane_stalls(self.app.snap):
            self.app.notify("Scheduler hängt/steht — Destroy bleibt in der "
                            "Warteschlange. Notfalls CLI: 'vhsorch destroy'.",
                            severity="warning")
        self.app.db.add_command("destroy", node_id=node_id)

    def on_key(self, event) -> None:
        if event.key.isdigit() and event.key != "0":
            idx = int(event.key) - 1
            nodes = self._snap_nodes()
            if 0 <= idx < len(nodes):
                nid = nodes[idx]["instance_id"]
                self.app.push_screen(ConfirmScreen(
                    f"Node #{nid} SOFORT zerstören? (hart, Kostenstopp)",
                    lambda: (self._enqueue_destroy(nid),
                             self.app.notify(f"Destroy für #{nid} angestoßen."),
                             self.app.pop_screen())))
                event.stop()

    def action_destroy_all(self) -> None:
        nodes = self._snap_nodes()
        if not nodes:
            self.app.notify("Keine Nodes zum Zerstören.")
            return

        def do_all():
            for n in nodes:
                self._enqueue_destroy(n["instance_id"])
            self.app.notify(f"Destroy für ALLE {len(nodes)} Node(s) angestoßen.")
            self.app.pop_screen()
        self.app.push_screen(ConfirmScreen(
            f"ALLE {len(nodes)} Nodes SOFORT zerstören? (harter Kostenstopp)", do_all))

    def action_close(self) -> None:
        self.app.pop_screen()


class AddNodeScreen(ModalScreen):
    """Node dazu: Offers live suchen (Worker-Thread), wählen, buchen."""

    BINDINGS = [
        Binding("escape,q", "close", "Abbrechen"),
        Binding("g", "cycle_gpus", "min. GPUs"),
        Binding("t", "cycle_type", "GPU-Typ"),
        # Zifferntaste wählt ein Offer — via on_key.
    ]

    # Wählbare Mindest-GPU-Anzahl und GPU-Typ-Filter (None = beide erlaubten).
    _GPU_STEPS = [1, 2, 4, 6, 8]
    _TYPE_OPTS = [("beide", None), ("RTX 5090", ["RTX 5090"]), ("RTX 4090", ["RTX 4090"])]

    def __init__(self, min_gpus: int = 4) -> None:
        super().__init__()
        self._min_gpus = min_gpus if min_gpus in self._GPU_STEPS else 4
        self._type_idx = 0
        self._offers: list = []
        self._loading = True
        self._error: Optional[str] = None

    def compose(self) -> ComposeResult:
        with Vertical(id="addbox"):
            yield Static(id="addbody")

    def on_mount(self) -> None:
        self._repaint()
        self._search()

    def _restart_search(self) -> None:
        self._loading = True
        self._error = None
        self._offers = []
        self._repaint()
        self._search()

    def action_cycle_gpus(self) -> None:
        i = (self._GPU_STEPS.index(self._min_gpus) + 1) % len(self._GPU_STEPS)
        self._min_gpus = self._GPU_STEPS[i]
        self._restart_search()

    def action_cycle_type(self) -> None:
        self._type_idx = (self._type_idx + 1) % len(self._TYPE_OPTS)
        self._restart_search()

    @work(thread=True, exclusive=True)
    def _search(self) -> None:
        try:
            from .vast import VastClient
            vast = VastClient(self.app.cfg.vast_api_key)
            offers = vast.search_offers(disk_gb=self.app.cfg.vast_disk_gb,
                                        min_gpus=self._min_gpus,
                                        gpu_names=self._TYPE_OPTS[self._type_idx][1])
            self.app.call_from_thread(self._got_offers, offers[:9], None)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._got_offers, [], str(e)[:200])

    def _got_offers(self, offers: list, error: Optional[str]) -> None:
        self._offers = offers
        self._error = error
        self._loading = False
        self._repaint()

    def _repaint(self) -> None:
        # Der Such-/Buch-Worker kann zurückkehren, NACHDEM das Overlay schon
        # geschlossen wurde -> query_one würde NoMatches werfen. Guard.
        if not self.is_mounted:
            return
        body = self.query_one("#addbody", Static)
        typename = self._TYPE_OPTS[self._type_idx][0]
        filt = Text()
        filt.append(f"Filter: ≥{self._min_gpus} GPUs · {typename}", style="bold")
        filt.append("   [g] min. GPUs ändern · [t] GPU-Typ ändern", style="grey62")

        def panel(inner, border="cyan"):
            body.update(Panel(Group(filt, Text(""), inner),
                              title="Node dazu", border_style=border))

        if self._loading:
            panel(Text("Suche Offers … (Esc = abbrechen)"))
            return
        if self._error:
            panel(Text(f"Fehler: {self._error}", style="red"), border="red")
            return
        if not self._offers:
            panel(Text("Keine Offers für diesen Filter — [g]/[t] anpassen."),
                  border="yellow")
            return
        t = Table(box=None, pad_edge=False)
        t.add_column("#", justify="right")
        t.add_column("GPU")
        t.add_column("GPUs", justify="right")
        t.add_column("$/h", justify="right")
        t.add_column("DLPerf/$", justify="right")
        t.add_column("Ort")
        for i, o in enumerate(self._offers, 1):
            t.add_row(str(i), o.gpu_name, str(o.num_gpus), f"{o.dph_total:.3f}",
                      f"{o.dlperf_per_dph:.0f}", o.geolocation)
        warn = ""
        q = (self.app.snap or {}).get("queue", {})
        auto = ((self.app.snap or {}).get("scheduler") or {}).get("auto_destroy")
        if auto and q.get("total", 0) and (q.get("pending", 0) + q.get("assigned", 0)
                                           + q.get("uploaded", 0)) == 0:
            warn = "\n⚠ Queue fast leer + AUTO_DESTROY: neue Node wird evtl. sofort zerstört."
        panel(Group(
            t,
            Text(f"Spalte 'GPUs' = Karten dieser Maschine. Zifferntaste 1–{len(self._offers)} "
                 f"= buchen · Esc = abbrechen" + warn, style="grey62")))

    def on_key(self, event) -> None:
        if self._loading or self._error:
            return
        if event.key.isdigit():
            idx = int(event.key) - 1
            if 0 <= idx < len(self._offers):
                offer = self._offers[idx]
                self._confirm_book(offer)
                event.stop()

    def _confirm_book(self, offer) -> None:
        def do_book():
            self._book(offer.id)
            self.app.notify(f"Buche Offer {offer.id} ({offer.gpu_name} x{offer.num_gpus}) …")
        self.app.push_screen(ConfirmScreen(
            f"Offer {offer.id} buchen?  {offer.gpu_name} x{offer.num_gpus} "
            f"@ {offer.dph_total:.3f} $/h", do_book))

    @work(thread=True, exclusive=True)
    def _book(self, offer_id: int) -> None:
        try:
            from .cli import book_offer
            from .vast import VastClient
            vast = VastClient(self.app.cfg.vast_api_key)
            instance_id, _offer = book_offer(self.app.cfg, vast, self.app.db, offer_id)
            self.app.call_from_thread(self._booked, instance_id, None)
        except Exception as e:  # noqa: BLE001
            self.app.call_from_thread(self._booked, None, str(e)[:200])

    def _booked(self, instance_id: Optional[int], error: Optional[str]) -> None:
        if error:
            self.app.notify(f"Buchung fehlgeschlagen: {error}", severity="error")
            return
        # Nur schließen, wenn DIESES Overlay noch oben liegt (der Nutzer könnte
        # während der ~3s-Buchung weggeblättert sein -> sonst poppt man die falsche
        # Screen, z.B. HomeScreen).
        if self.is_mounted and self.app.screen is self:
            self.app.pop_screen()
        self.app.notify(f"Gebucht: Instanz {instance_id}. Node kommt hoch …")
        self.app.push_screen(NodeScreen(instance_id))   # direkt auf die neue Node

    def action_close(self) -> None:
        self.app.pop_screen()


# ===========================================================================
#  Node-Detail-Screen
# ===========================================================================
class NodeScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "back", "Zurück"),
        Binding("b", "cmd_bootstrap", "Bootstrap"),
        Binding("m", "cmd_models", "Modelle"),
        Binding("w", "cmd_worker", "Worker"),
        Binding("u", "cmd_pull", "Pull"),
        Binding("d", "cmd_drain", "Drain"),
        Binding("x", "cmd_destroy", "Destroy"),
        Binding("l", "node_log", "Node-Log"),
    ]

    def __init__(self, instance_id: int) -> None:
        super().__init__()
        self.instance_id = instance_id

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="nodebody")
        yield Footer()

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(0.5, self._repaint)

    def _node(self) -> Optional[dict]:
        for n in (self.app.snap or {}).get("nodes", []):
            if n["instance_id"] == self.instance_id:
                return n
        return None

    def _repaint(self) -> None:
        body = self.query_one("#nodebody", Static)
        n = self._node()
        if n is None:
            body.update(Panel(Text("Diese Node ist nicht mehr aktiv (zerstört?).\n"
                                   "Esc = zurück.", style="yellow"),
                              title=f"Node #{self.instance_id}", border_style="red"))
            return

        badge, bcolor = _STATUS_BADGE.get(n["status"], (n["status"], "white"))
        head = Text()
        head.append(f"Node #{n['instance_id']}  ", style="bold cyan")
        head.append(f"{n['gpu_name']} x{n['num_gpus']}  ")
        head.append(f"{badge}", style=bcolor)
        head.append(f"   {(n['dph'] or 0):.3f} $/h · {n['uptime_h']}h · "
                    f"{n['cost_accrued']:.2f} $")
        if n.get("ssh"):
            head.append(f"   ssh {n['ssh']}", style="grey58")
        age = n.get("probe_age_s")
        if age is not None:
            head.append(f"   Probe {age}s alt", style="grey42")

        # Setup-Block (Flag-Toggles: Label spiegelt den Zustand).
        f = n["flags"]
        busy = n.get("busy")
        setup = Table.grid(padding=(0, 3))
        setup.add_column()
        setup.add_column()
        boot_extra = n.get("bootstrap_status") or ""
        if busy == "bootstrap":
            boot_extra = "… läuft"
        pushing_models = (busy and ("models" in busy or busy == "service")
                          and not f["models_pushed"])
        models_extra = ""
        if pushing_models:
            mb = n.get("models_bytes", 0) or 0
            mt = n.get("models_total", 0) or 0
            if mt:
                models_extra = (f"wird gepusht … {mb/1e9:.1f}/{mt/1e9:.1f} GB "
                                f"({100 * mb // mt}%)")
            else:
                models_extra = "wird gepusht …"
        setup.add_row(_flag("Bootstrap [b]", f["bootstrap_started"], boot_extra),
                      _flag("Modelle [m]", f["models_pushed"], models_extra))
        busy_txt, busy_col = _busy_desc(busy)
        setup.add_row(_flag("Worker [w]", f["worker_running"],
                            f"{n['busy_gpus']}/{n['num_gpus']} aktiv" if f["worker_running"] else ""),
                      Text(busy_txt, style=busy_col))

        # GPU-Grid.
        gtab = Table.grid(padding=(0, 1))
        gtab.add_column()
        gtab.add_column()
        for g in n["gpus"]:
            if g["state"] == "busy" and g["clip"]:
                left = Text()
                left.append(f"● GPU {g['index']} ", style="green")
                left.append(f"▶ {g['clip']}")
                load = ""
                if g.get("util") is not None:
                    load = f"{g['util']}% · {(g['vram_used_mib'] or 0)/1024:.1f}/" \
                           f"{(g['vram_total_mib'] or 0)/1024:.0f}G"
                gtab.add_row(left, Text(load, style="grey62"))
                gtab.add_row(_phase_bar(g["phase"], g["pct"], g["progress"]), Text(""))
            elif g["state"] == "idle":
                gtab.add_row(Text(f"○ GPU {g['index']}  (frei / zwischen Clips)",
                                  style="grey58"), Text(""))
            else:
                gtab.add_row(Text(f"◌ GPU {g['index']}  (startet noch …)",
                                  style="yellow"), Text(""))

        cl = n["clips"]
        clip_line = Text(
            f"Clips: {cl['done']} fertig · {cl['node_done_pending_pull']} Node-fertig "
            f"(Pull offen) · {cl['uploaded']} auf Node · {cl['assigned']} hochladen · "
            f"{cl['failed']} FEHLER", style="grey70")

        alarm = None
        if n.get("idle_with_backlog"):
            alarm = Text("⚠ ready, aber keine GPU aktiv trotz Backlog — Worker gecrasht? "
                         "[w] Worker neu starten.", style="bold red")

        actions = Text("[b] Bootstrap  [m] Modelle  [w] Worker  [u] Pull  "
                       "[d] Drain  [x] Destroy  [l] Node-Log  [Esc] zurück", style="grey62")

        parts = [head, Text(""), setup, Text(""), gtab, clip_line]
        if alarm:
            parts += [Text(""), alarm]
        parts += [Text(""), actions]
        body.update(Panel(Group(*parts), title=f"Node #{n['instance_id']}",
                          border_style=bcolor))

    # -- Aktionen (enqueuen in commands) -------------------------------------
    def _enqueue(self, action: str, label: str) -> None:
        if _is_stale(self.app.snap) or _lane_stalls(self.app.snap):
            self.app.notify(
                "Scheduler wirkt gestoppt/hängt — Befehl bleibt in der Warteschlange, "
                "bis der Loop wieder tickt.", severity="warning")
        self.app.db.add_command(action, node_id=self.instance_id)
        self.app.notify(f"{label} für Node #{self.instance_id} angestoßen.")

    def action_cmd_bootstrap(self) -> None:
        self._enqueue("bootstrap", "Bootstrap")

    def action_cmd_models(self) -> None:
        self._enqueue("models", "Modell-Push")

    def action_cmd_worker(self) -> None:
        self._enqueue("worker", "Worker-Start")

    def action_cmd_pull(self) -> None:
        self._enqueue("pull", "Pull")

    def action_cmd_drain(self) -> None:
        self.app.push_screen(ConfirmScreen(
            f"Node #{self.instance_id} drainen? (keine neue Arbeit, Rest umverteilt, "
            "wird nach Leerlauf zerstört)",
            lambda: self._enqueue("drain", "Drain")))

    def action_cmd_destroy(self) -> None:
        self.app.push_screen(ConfirmScreen(
            f"Node #{self.instance_id} SOFORT zerstören? (Kostenstopp, harte Aktion)",
            lambda: self._enqueue("destroy", "Destroy")))

    def action_node_log(self) -> None:
        self.app.push_screen(NodeLogScreen(self.instance_id))

    def action_back(self) -> None:
        self.app.pop_screen()


# ===========================================================================
#  Home / Dashboard
# ===========================================================================
class HomeScreen(Screen):
    BINDINGS = [
        Binding("a", "add_node", "Node dazu"),
        Binding("p", "pull_all", "Pull alle"),
        Binding("x", "destroy", "Zerstören"),
        Binding("v", "videos", "Videos"),
        Binding("f", "finalize", "Finalisieren", show=False),
        Binding("l", "log", "Log"),
        # 'app.quit' (nicht 'quit'): eine Screen-Bindung löst die Aktion im
        # Screen-Namespace auf — action_quit liegt aber auf der App. Ohne den
        # 'app.'-Präfix schließt 'q' still NICHT (nur Textuals eingebautes ctrl+q).
        Binding("q", "app.quit", "Beenden"),
        # Zifferntasten öffnen die N-te Node — via on_key (saubere Ziffer).
    ]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with VerticalScroll():
            yield Static(id="homebody")
        yield Footer()

    def on_mount(self) -> None:
        self._repaint()
        self.set_interval(0.5, self._repaint)

    def _repaint(self) -> None:
        snap = self.app.snap
        body = self.query_one("#homebody", Static)
        if snap is None:
            body.update(Panel(
                Text("Warte auf Scheduler-Snapshot …\n\n"
                     "Läuft der Loop?  docker compose up -d   (oder: vhsorch run)",
                     style="yellow"),
                title="VHS-Upscale-Orchestrator", border_style="yellow"))
            return

        q = snap["queue"]
        cost = snap["cost"]
        sched = snap.get("scheduler", {})
        stale = _is_stale(snap)
        stalls = _lane_stalls(snap)

        # Kopf: Fortschritt + Kosten + Autopilot-Herzschlag.
        head = Table.grid(padding=(0, 2))
        head.add_column()
        prog = Text()
        prog.append("Fortschritt  ")
        prog.append(_bar(q["done"], q["total"]))
        prog.append(f"  {q['done']}/{q['total']}")
        head.add_row(prog)
        head.add_row(Text(
            f"Kosten: {cost['accrued']:.2f} $ aufgelaufen · {cost['dph_total']:.3f} $/h · "
            f"{cost['hours']}h", style="grey70"))
        if stale:
            head.add_row(Text("● AUTOPILOT GESTOPPT/HÄNGT — Snapshot veraltet. "
                              "Läuft `vhsorch run`?", style="bold red"))
        elif stalls:
            head.add_row(Text("● " + " · ".join(stalls), style="bold yellow"))
        else:
            age = _snapshot_age(snap)
            head.add_row(Text(f"● Autopilot läuft · Snapshot {age:.0f}s alt · "
                              f"Tick alle {sched.get('poll_interval','?')}s",
                              style="green"))

        # Alarmzeile (nur wenn was klemmt).
        alarms = list(stalls)
        if q["failed"]:
            alarms.append(f"{q['failed']} Clips FEHLER — Lauf offen gehalten "
                          f"([v]→[r] Retry oder [f] finalisieren)")
        for n in snap["nodes"]:
            if n.get("idle_with_backlog"):
                alarms.append(f"Node #{n['instance_id']}: ready, 0 GPUs aktiv trotz Backlog")
            if n.get("reachable") is False and n["status"] != "booked":
                alarms.append(f"Node #{n['instance_id']}: nicht erreichbar")

        # Node-Kacheln.
        nodes = snap["nodes"]
        ntab = Table.grid(padding=(0, 2))
        ntab.add_column(justify="right")
        ntab.add_column()
        if not nodes:
            ntab.add_row("", Text("Keine aktiven Nodes. [a] = Node dazu.", style="grey62"))
        for i, n in enumerate(nodes, 1):
            badge, bcolor = _STATUS_BADGE.get(n["status"], (n["status"], "white"))
            line = Text()
            line.append(f"#{n['instance_id']} ", style="bold")
            line.append(f"{n['gpu_name']} x{n['num_gpus']}  ")
            line.append(f"{badge}", style=bcolor)
            line.append(f"  busy {n['busy_gpus']}/{n['num_gpus']}  ")
            line.append(f"{(n['dph'] or 0):.3f} $/h", style="grey62")
            if n["status"] == "booked" and n.get("bootstrap_status"):
                line.append(f"  {n['bootstrap_status'][:32]}", style="grey58")
            if n.get("busy"):
                line.append(f"  ({n['busy']} …)", style="yellow")
            ntab.add_row(Text(f"[{i}]", style="cyan"), line)

        parts = [head]
        if alarms:
            parts += [Text(""), Text("⚠ " + "  ·  ".join(alarms), style="bold yellow")]
        parts += [Text(""),
                  Text("Nodes — Zifferntaste öffnet Detail:", style="bold"),
                  ntab, Text(""),
                  Text("[a] Node dazu   [p] Pull alle   [x] Zerstören   [v] Videos   "
                       "[f] Finalisieren   [l] Log   [q] Beenden", style="grey62")]
        border = "red" if stale else ("yellow" if stalls else "cyan")
        body.update(Panel(Group(*parts), title="VHS-Upscale-Orchestrator — Der Lauf",
                          border_style=border))

    # -- Aktionen ------------------------------------------------------------
    def on_key(self, event) -> None:
        if event.key.isdigit() and event.key != "0":
            idx = int(event.key) - 1
            nodes = (self.app.snap or {}).get("nodes", [])
            if 0 <= idx < len(nodes):
                self.app.push_screen(NodeScreen(nodes[idx]["instance_id"]))
                event.stop()

    def action_finalize(self) -> None:
        failed = (self.app.snap or {}).get("queue", {}).get("failed", 0)
        if not failed:
            self.app.notify("Keine Fehler-Clips zum Finalisieren.")
            return
        self.app.push_screen(ConfirmScreen(
            f"{failed} Fehler-Clips endgültig als 'abandoned' abhaken? "
            "(gibt den Auto-Destroy/Kostenstopp frei)",
            lambda: (self.app.db.add_command("finalize"),
                     self.app.notify("Finalisieren angestoßen."))))

    def action_add_node(self) -> None:
        self.app.push_screen(AddNodeScreen())

    def action_pull_all(self) -> None:
        nodes = (self.app.snap or {}).get("nodes", [])
        n = 0
        for node in nodes:
            if node["status"] in ("ready", "draining"):
                self.app.db.add_command("pull", node_id=node["instance_id"])
                n += 1
        self.app.notify(f"Pull für {n} Node(s) angestoßen.")

    def action_destroy(self) -> None:
        self.app.push_screen(DestroyScreen())

    def action_videos(self) -> None:
        self.app.push_screen(VideosScreen())

    def action_log(self) -> None:
        self.app.push_screen(LogScreen())

    def action_refresh(self) -> None:
        self._repaint()


# ===========================================================================
#  App
# ===========================================================================
class VhsApp(App):
    CSS = """
    Screen { align: center top; }
    #homebody, #nodebody { width: 100%; }
    #addbox, #logbox, #vidbox, #destroybox { align: center middle; width: 90%; }
    """
    TITLE = "vhsorch"

    def __init__(self, cfg: Config) -> None:
        super().__init__()
        self.cfg = cfg
        self.db = DB(cfg.db_path)
        self.snap: Optional[dict] = load_snapshot(cfg.snapshot_path)

    def on_mount(self) -> None:
        self.push_screen(HomeScreen())
        # Snapshot global alle 0,5 s neu laden; die Screens rendern selbst.
        self.set_interval(0.5, self._reload_snapshot)

    def _reload_snapshot(self) -> None:
        snap = load_snapshot(self.cfg.snapshot_path)
        if snap is not None:
            self.snap = snap


def run_tui(cfg: Config) -> int:
    VhsApp(cfg).run()
    return 0
