# Kontroll-UI Redesign — Plan & Umsetzung

Stand: 2026-07-08 · Ausgangspunkt: Das Bash-Menü (`orchestrator/menu.sh`) ist im Einsatz „richtig
schlecht" — laggy, unresponsive, ignoriert Inputs, „Press any key" reagiert oft erst nach dem
blockierenden Fetch.

## Umsetzungsstand (Branch `menu-redesign`)

**Alle vier Phasen sind implementiert** und headless verifiziert (py_compile + Import + Textual
`run_test`-Smoke + Scheduler-Logik-Test). Eine adversarielle Multi-Agent-Review fand 14 Defekte
(2 HIGH, 8 MEDIUM, 2 LOW) — **alle gefixt und mit Tests abgedeckt**.

Neue/geänderte Dateien: `vhsorch/tui.py` (neu, Textual), `vhsorch/scheduler.py` (Zwei-Lane + Snapshot
+ Commands), `vhsorch/db.py` (thread-lokal + commands + drain/finalize), `vhsorch/remote.py`
(`probe()` + ControlPersist + idempotenter Worker + `release_claim`), `vhsorch/config.py`,
`vhsorch/cli.py` (`tui`), `vhsorch/ingest.py` (Stamm-Kollision), `node/process.sh` (flock-Guard),
`orchestrator/menu.sh` (TUI-Starteintrag), `requirements.txt` (textual).

> **Wichtige Verhaltensänderung (Review-Fix [11]):** Ein Lauf mit `failed`-Clips gilt NICHT mehr als
> abgeschlossen — **AUTO_DESTROY greift nicht**, solange Fehler offen sind. So bleiben Nodes für den
> Retry am Leben (der User braucht Recovery, s. cgroup-OOM). Das HOME zeigt „Lauf offen gehalten:
> N FEHLER". Der Bediener **retryt** (`[v]`→`[r]`) oder **finalisiert** (`[f]`, setzt failed→abandoned
> und gibt Auto-Destroy frei). Trade-off: laufen lassen kostet Geld → deshalb sichtbar + expliziter
> Finalize-Ausweg statt stillem Kill-mit-Retry-Verlust.

Starten: **`./menu.sh`** — stellt den Loop sicher (`docker compose up -d`) und öffnet direkt die TUI;
mit `q` schließen = zurück in die Shell (kein Bash-Menü mehr). Der Scheduler lädt beim Start die
**SeedVR2-Modelle automatisch** in den Home-Cache, falls sie fehlen (im Hintergrund; Worker startet
erst, wenn sie da sind) — `vhsorch fetch-models` ist nur noch optional/manuell.

---

---

## Wurzelursache (warum das Menü hakt)

Kein Rendering- und **kein Sprachproblem, sondern ein Architekturproblem**:

- **Container-Spin-up pro Aktion:** [`menu.sh:25`](orchestrator/menu.sh#L25) — `ORCH() { docker compose run --rm orchestrator … }`.
  Jeder Klick startet einen frischen Container + Python.
- **N sequentielle SSH-Round-Trips im UI-Thread:** jeder Live-Refresh ruft `gpu_activity`,
  `gpu_stats`, `list_remote_final`, `bootstrap_status` einzeln ([`remote.py`](orchestrator/vhsorch/remote.py)),
  jeder mit ConnectTimeout 8–20 s. Bootet/hängt eine Node, blockiert der ganze Fetch.
- **Bash hat kein nicht-blockierendes Event-Loop:** [`live_orch`](orchestrator/menu.sh#L142) pollt die
  Tastatur nur in den Lücken zwischen blockierenden Fetches (`read -t 0.25`) → verschluckt Inputs.

**Fazit:** Solange die UI die Live-Daten selbst beschafft, bleibt sie hakelig — egal in welcher Sprache.

---

## Verdikt: Python-TUI (Textual), kein Bash-Rewrite

Zuerst wird die Wurzel gekappt (Snapshot-Reader, s. u.) — **sprachunabhängig**. Danach ist die
Sprachwahl eine reine Interaktionsfrage, und genau die geforderten Flows (Per-Node-Konsole mit
Toggles deren Label den Zustand spiegelt; Auto-Refresh bei sofort greifenden Tasten; Log-Tail-Pane;
Bestätigungs-Modals) sind Bashs Todeszone.

[Textual](https://textual.textualize.io/) liefert exakt das Fehlende: echtes asyncio-Event-Loop
(**Input ist per Konstruktion nie an IO gekoppelt**), `set_interval` fürs Snapshot-Repolling,
DataTable/Static/Button-Widgets, eigener Detail-Screen pro Node, `@work(thread=True)` für die wenigen
blockierenden Aufrufe — im **selben Container** wie `vhsorch` (nur `pip install textual`), mit direktem
Zugriff auf `config.py`/`db.py`/`remote.py`. `curses` = zu low-level, `rich` = kein Input-Modell.

---

## Architektur in einem Bild

```
                 ZUHAUSE (Docker, EIN langlebiger orchestrator-Container)
 ┌───────────────────────────────────────────────────────────────────────────┐
 │  SCHEDULER-PROZESS  =  EINZIGER SCHREIBER der Remote-Wahrheit               │
 │                                                                            │
 │  Haupt-Loop (1 Thread, bleibt IMMER schnell, ~1 s Raster):                 │
 │     drain_commands(light)  → idempotente Nudges sofort feuern              │
 │     if tick_due:  refresh_nodes()  ── probe() 1×SSH/Node, ThreadPool ──┐   │
 │                   distribute() / collect()                             │   │
 │     write_snapshot(state_dir/snapshot.json)   ← tmp + os.replace (atom)│   │
 │                                                                        │   │
 │  Heavy-Op-Worker (kleiner Thread-Pool):                                │   │
 │     push_models (40 min), große rsync-Pushes, pull                     │   │
 │     Per-Node "busy"-Flag → Haupt-Loop mutiert diese Node nicht         │   │
 │                                                                            │
 │  SQLite (WAL): clips · nodes · commands (Absichts-Tabelle)                 │
 └───────────────────────────────────────────────────────────────────────────┘
        ▲ schreibt snapshot.json + DB          ▲ liest snapshot.json + DB (nur lokal!)
        │  INSERT Absicht in commands           │  0 SSH · 0 docker-run im UI-Pfad
 ┌──────┴───────────────────────────────────────┴──────────────────────────────┐
 │  TEXTUAL-TUI  =  reiner LESER + Absichts-Schreiber                           │
 │  set_interval(0.5s): json.load(snapshot) + kleine DB-Reads → repaint         │
 │  Toggle-Taste → INSERT commands(node,action) → optimistisch "wird gestartet" │
 │  Start: docker compose exec orchestrator vhsorch tui  (EINMAL pro Session)   │
 └──────────────────────────────────────────────────────────────────────────────┘
```

**Warum die UI nie blockiert:** Sie SSHt nie, spinnt nie einen Container, führt nie eine Remote-Op
aus. Sie liest eine kleine lokale Datei (< 1 ms) auf einem Timer und schreibt für Aktionen nur eine
winzige DB-Zeile (µs). Alle Latenz (SSH, rsync, Vast-API) lebt ausschließlich im Scheduler-Prozess.

**Command-Ausführung läuft IM Scheduler-Prozess** (nicht in einem zweiten Prozess), getrennt nach
*leicht* (Haupt-Loop, ≤ 1–2 s) und *schwer* (Worker-Pool). Das bewahrt die single-writer-Invariante
pro Node (nie SSHen zwei Prozesse dieselbe Node) und verhindert, dass ein 40-min-Push den Snapshot
40 min einfriert.

---

## Screen-Struktur (radikal vereinfacht)

Weg vom 12-Punkte-Flachmenü (`workmap`/`videos`/`nodes`/`pull` sind CLI-Verben, keine Bediener-
Momente). **Zwei Screens + drei Overlays**, alles aus dem Snapshot:

- **HOME „Lauf" (Landeplatz):** Gesamt-Fortschrittsbalken (done/total) · Kosten-Uhr ($ bisher · $/h ·
  ETA) · **Autopilot-Herzschlag** („läuft · Tick vor 3 s" / rot „GESTOPPT — [Enter] starten") ·
  Alarm-Zeile nur wenn was klemmt · **Node-Kacheln** (`#id · GPU ×n · Status-Badge · busy 3/4 · $/h`).
  Tasten: `[a]` Node dazu · `[s]` Stoppen · `[p]` Pull-all · `[l]` Log · `[v]` Videos/Queue · `[q]`.
- **NODE-DETAIL (der Kern, pro Maschine):** Kopf (`#id/GPU/status/$/h/ssh/uptime/Node-Kosten/
  Snapshot-Alter`); **Setup-Block** mit drei Flag-Toggles (Bootstrap/Modelle/Worker, Label = Zustand);
  **GPU-Grid** mit 3-Phasen-Balken + util/VRAM je GPU (aus Snapshot, kein SSH); Clip-Liste dieser
  Node; Aktionsleiste `[b] [m] [w] [u]pull [d]rain [x]destroy [L]og [Esc]`.
- **Overlays:** (1) **Node dazu** = `plan`-Offerliste → wählen → bestätigen → `book`; (2) **Log-Pane**
  = tail von Autopilot-Log oder bootstrap/run-Log der Node; (3) **Confirm-Modal** für destroy/drain/stop.

Reine Wartungs-CLI (`fetch-models`, `reconcile`) bleibt **CLI**, nicht im Menü.

---

## Flow 1: gebucht → Status-Ansicht (automatisch)

1. HOME → `[a]` → Offer-Overlay (`plan` läuft im Worker-Thread mit Spinner, friert nichts ein).
2. Offer wählen → Confirm „Offer 4453 buchen? [j/N]".
3. `book()` erzeugt die Vast-Instanz + legt die Node als `booked` in der DB an → **UI hat die Zeile sofort**.
4. TUI schließt das Overlay und **öffnet unmittelbar den NODE-DETAIL genau dieser instance_id**.
5. Erster Moment ehrlich aus DB/Snapshot (kein SSH): `Status: booked · warte auf sshd · Autopilot
   bootstrappt in ≤ 30 s`, Toggles grau.
6. Nächster Scheduler-Tick (≤ 30 s): `refresh_nodes` übernimmt ssh_host/port, stößt Bootstrap an.
   Der 0,5-s-Refresh tickert live durch: `bootstrap_status` → Toggle „gesendet ✓" → `booked→ready`
   → „Worker läuft ✓ 4/4" → GPU-Balken erwachen. **Der User muss nichts drücken.**

Ohne laufenden Loop funktioniert die Ansicht auch — dann sind die Toggles der einzige Weg, die Node
hochzuziehen, und die Konsole sagt es explizit („Loop läuft nicht — Aktionen manuell").

---

## Flow 2: Per-Node-Toggles — Auflösung Auto-Scheduler vs. manuell

**Entscheidung: idempotente „jetzt-statt-nächsten-Tick"-Nudges. KEIN Auto/Manuell-Modus pro Node.**
Die eine echte Übersteuerung läuft über `nodes.status`, den der Scheduler ohnehin respektiert.

**Klasse A — Nudges (idempotent, kein Modus):** Bootstrap, Modelle pushen, Worker starten, Pull jetzt.
Alle drei sind im Code schon kollisionssicher:
- `start_bootstrap` = no-op wenn `process.sh` da / `.bootstrap.launched` gesetzt
- `push_models` gated auf Flag + `rsync --ignore-existing`
- `start_worker` prüft `worker_running` per `pgrep`

Ein Nudge **rennt nicht gegen den Loop** — er ist der Tick, nur früher. Er schreibt nur die Absicht
in `commands`. **„Sehen OB gesendet"** kommt direkt aus den DB-Flags im Snapshot; das Label spiegelt:
- `Bootstrap: gesendet ✓ 14:03 · läuft: git clone…`  ⟷  `Bootstrap starten ▸`
- `Modelle: gepusht ✓` / `pushing… (Heavy-Lane)`  ⟷  `Modelle jetzt pushen ▸`
- `Worker: läuft ✓ 4/4`  ⟷  `Worker starten ▸`

**Klasse B — einzige echte Zustandsänderung: `drain`.** `draining` steht schon im Schema-Enum, wird
aber noch nicht genutzt. `drain` = `update_node(status='draining')` → `distribute`/`push_and_run`
überspringen es (filtern auf `ready`) → `reassign_node_clips` schiebt offene Clips zurück auf pending.
> **Pflicht-Zusatz:** `collect` und `maybe_destroy` müssen zusätzlich `draining` zulassen (heute nur
> `ready`, s. [`db.py:175 active_nodes`](orchestrator/vhsorch/db.py#L175) + [`scheduler.py:198`](orchestrator/vhsorch/scheduler.py#L198)) —
> sonst bleiben Ergebnisse einer drainenden Node liegen = Datenverlust beim Kostenstopp.

`destroy` `[x]` = sofort, hart, + reassign. **Ergebnis: 3 idempotente Buttons + 1 Lifecycle-Übergang
(drain) + 1 harte Aktion (destroy). Ein Schreiber, keine Kollision, keine Modus-Matrix.**

---

## Flow 3: 2./3. Maschine später dazu

**Ja — technisch schon heute vollständig. Es fehlt NUR die erstklassige Menü-Aktion.**

Funktioniert schon (nichts anfassen): Man bucht jederzeit ein weiteres Offer; der laufende Scheduler
nimmt es nächsten Tick auf (`refresh_nodes` bootstrappt, `push_and_run` pusht Modelle + startet Worker),
`distribute` verteilt kapazitätsgewichtet neu ([`scheduler.py:113`](orchestrator/vhsorch/scheduler.py#L113):
`slots += [id]*num_gpus`, jeden Tick auf der aktuellen `ready`-Menge). Kein Stop/Restart.

Neu (reine UI-Verdrahtung): `[a]` „Node dazu" ist auf dem Dashboard **dauerhaft** sichtbar — egal ob
0 oder 5 Nodes laufen, egal ob gerade verarbeitet wird. Derselbe Book-Flow wie Flow 1, danach
Auto-Landung auf dem Node-Detail der Neuen. Im Overlay wählbar: **`[g]` min. GPU-Anzahl** (1/2/4/6/8)
und **`[t]` GPU-Typ** (beide / RTX 5090 / RTX 4090) — die Offer-Liste sucht bei jeder Änderung neu; die
Spalte „GPUs" zeigt die Kartenzahl je Maschine, also bucht man die gewünschte Anzahl über das Offer.

> **Warnung im Book-Flow:** Läuft `AUTO_DESTROY=1` und ist die Queue gerade fast leer, könnte
> `maybe_destroy` die frische Node sofort killen → vor dem `book` einen Hinweis zeigen.

---

## Recovery — failed/stuck sichtbar + Retry

Alles wird im **Tick** erkannt (nicht in der UI) und im Snapshot getragen; die UI zeigt/triggert nur.

- **Failed Clips:** `status='failed'` rot; `[r]` schreibt `command(requeue)` → Claim frei + pending +
  node_id NULL → nächster `distribute` verteilt neu.
- **Toter Worker / cgroup-OOM-Compile-Crash** (s. `MEMORY` cgroup-oom-compile): Signatur =
  `status=ready` ABER alle GPUs idle trotz `uploaded`-Backlog. Kachel flaggt rot; `[w]` „Worker neu
  starten" = direkter Retry ohne Node-Neubau. (Der echte Auto-Respawn = Supervisor in `process.sh`
  bleibt Node-Arbeit; die UI macht den Crash sicht- und retry-bar.)
- **Stalled GPU:** UI vergleicht `pct` über mehrere Snapshots; unverändert über N Ticks → „stallt?".
  Achtung: der bekannte Upscale-%-Sprung (mehrere tqdm-Bars) darf nicht fälschlich als Stall zählen.
- **Tote Node:** `probe()` rc 124/nonzero → „nicht erreichbar (tot?)" + Alter des letzten Kontakts.
- **Bootstrap failed:** ERR-Trap löscht `.bootstrap.launched` → nächster Tick retryt; `[b]` erzwingt sofort.

---

## Video-Fortschritt (bekannter offener Punkt)

Problem: SeedVR2 gibt in der Upscale-Phase **mehrere tqdm-Bars nacheinander** aus; `gpu_activity()`
greift nur den *letzten* %-Wert → der Balken springt/fällt, nicht monoton.

Fix in drei Stufen, **monoton by design**:
1. **Primärsignal = grober 3-Phasen-Balken** (Denoise → Upscale → Audio, feste Gewichte z. B.
   20/70/10 %). Phasen advancieren nur (aus den `PHASE …`-Marker-Zeilen) → **immer monoton**.
2. **Feinanzeige = tqdm-% mit Monoton-Klemme:** angezeigter Phasen-% sinkt nie; ein Rückwärtssprung =
   neue Sub-Bar → Segmentwechsel, nicht Rückschritt.
3. **Optional (Node-Seite, Backlog):** `process.sh` loggt zusätzlich `FRAME n/total` bzw. Sub-Bar-Index
   → echter Nenner, dann exakter Feinbalken statt Klemme.

Zusätzlich `FAIL:`-Zeilen aus `gpuN.log` parsen (werden heute verworfen) → in den Snapshot.

---

## Phasenplan (nach Schmerz-Abbau)

**PHASE 0 — Lag-Wurzel kappen, sprachunabhängig, PFLICHT (~0,5–1 Tag). Nimmt den Schmerz allein, sogar ohne neue UI.**
- `remote.py`: **kombiniertes `probe()`** (worker-status + gpu_activity + gpu_stats + bootstrap_status +
  final-count in **1** ssh-exec statt 4–5) + **ControlMaster/ControlPersist** in `_ssh_base`
  (`-o ControlMaster=auto -o ControlPersist=60s -o ControlPath=/state/ssh-%h-%p`).
- `scheduler.py`: `probe` über alle Nodes per **ThreadPoolExecutor-Fan-out** + am Tick-Ende
  `write_snapshot()` atomar (`tmp` + `os.replace`), inkl. `generated_at`/`schema`-Version.
- Danach ist **jede** UI sofort schnell — `menu.sh` kann übergangsweise dasselbe File lesen.

**PHASE 1 — Command-Kanal + idempotente Toggles + drain (~0,5–1 Tag).**
- `commands`-Tabelle (`node_id, action, arg, requested_at, done_at, result`); Haupt-Loop 1-s-Poll mit
  `drain_commands(light)`; **Heavy-Op-Worker-Pool** für `push_models`/große Pushes/`pull` mit
  Per-Node-`busy`-Flag; getrennte SQLite-Connection pro Thread + WAL + `busy_timeout`.
- Idempotente Nudges an bestehende `remote.py`-Methoden verdrahten; `draining` in `collect`/
  `maybe_destroy` zulassen.

**PHASE 2 — Textual-TUI (~1,5–2 Tage). Ersetzt `menu.sh` als Standard-Einstieg.**
- `tui.py`: HOME + NODE-DETAIL (`set_interval(0.5s)`-Reader), Flag-Toggles, Aktionsleiste,
  `[a]`-Add-Overlay mit Auto-Landung, Confirm-Modals, Herzschlag/Stale-Ausgrauen, Log-Tail im
  `@work(thread=True)`. Start via `docker compose exec orchestrator vhsorch tui`.

**PHASE 3 — Recovery-/Fortschritt-Politur (~0,5–1 Tag).**
- `FAIL:`-Parsing + Stuck-Heuristik in den Snapshot; Dashboard-Alarmbanner; `[r]`-Retry; monotoner
  3-Phasen-Balken + %-Klemme; `--min-gpus`-Wahl (8 testen).

**Gesamt ~3,5–5 Tage.** Kritisch: **Phase 0 allein macht „wieder responsiv".** Der teure Teil steckt
nur in `tui.py` (Phase 2); die Engine bleibt weitgehend unberührt. `menu.sh` bleibt roher Not-Fallback
bis zur Phase-2-Abnahme.

---

## Risiken (adversariell geprüft)

1. **Snapshot-Staleness / toter Scheduler = Single Point of Failure (das gefährlichste).** Der reine
   Leser hat keinen eigenen SSH-Pfad — steht der Scheduler, friert die UI auf altem Stand ein und
   `commands` stapeln sich stumm. *Entschärfung:* `generated_at` prominent; Snapshot älter als ~3×
   Poll → alles ausgrauen + roter „GESTOPPT"-Banner; die UI **verweigert** neues Enqueue mit Warnung,
   wenn der Daemon tot ist; Per-Node-Probe-Timeout + Fan-out, damit eine zähe Node den Tick nie staut.
   Notausgang bleibt die volle **CLI** (`docker compose run … vhsorch destroy/pull`).
2. **Neue Nebenläufigkeit im bisher single-threaded Loop.** Threads = neue Race-Quelle. *Entschärfung:*
   nur `push_models`/`pull`/große Pushes in den Pool, Rest bleibt Haupt-Thread; **getrennte SQLite-
   Connection pro Thread**, WAL, `busy_timeout`; **ein** Koordinationsprimitiv (Per-Node-`busy`-Flag).
3. **Plan/Book bleibt die eine inhärent blockierende Live-API-Abfrage** (nicht snapshotbar).
   *Entschärfung:* strikt als **Modal im `@work(thread=True)`-Worker mit Spinner**, `Esc` bricht ab;
   optimistische Toggles werden über `command.result` rückgespiegelt (nach Timeout „fehlgeschlagen ▸
   erneut" statt hängen).
