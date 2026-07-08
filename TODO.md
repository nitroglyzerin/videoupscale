# TODO / Backlog

Stand: 2026-07-08 — erster erfolgreicher Lauf mit **4/4 GPUs** auf einer frischen Node.

## Aktueller Stand (was steht)

- **Multi-GPU** funktioniert: jeder Worker isoliert per `CUDA_VISIBLE_DEVICES` + `--cuda_device 0` (behebt den `torch.compile` „Invalid device id"-Crash auf `cuda:N>0`).
- **Modelle** (SeedVR2 DiT fp8 + VAE fp16) kommen **per rsync vom Orchestrator**, nicht per HF-Download auf der Node — der Node-Egress auf Vast ist unzuverlässig (IPv6-only-HF, TLS-Reset, 429). Home-Cache unter `MODELS_DIR=/data/models` (WD Red), befüllt mit `vhsorch fetch-models`, gepusht einmal pro Node (`models_pushed`-Flag), Worker startet erst danach.
- **Bootstrap selbstheilend**: SSH-getrieben (nicht auf Vast-onstart wartend), `flock` gegen Doppelläufe, Retry bei git-clone-TLS-Fehlern, ERR-Trap setzt Marker zurück, Skript-Downloads mit Timeout, Phasen-Status in `/workspace/bootstrap.status`.
- **Image**: `vastai/pytorch:@vastai-automatic-tag` (CUDA 13) — torch besteht die GPU-Probe (kein cu128-Reinstall). Achtung: `requirements.txt` installiert torch trotzdem frisch (~2 GB/Boot) → größter verbleibender Zeitfresser.

## Kontroll-UI-Redesign — implementiert auf Branch `menu-redesign` (Test offen)

Neue **Textual-TUI** (`vhsorch tui`) ersetzt das hakelige Bash-Menü: reiner Leser eines vom Scheduler
geschriebenen `snapshot.json` (kein SSH/Container-Spin-up im UI-Pfad → immer flüssig), Toggles/Actions
über eine `commands`-Queue. Scheduler ist Zwei-Lane (schneller Snapshot/Command-Loop + eigener Probe-
+ Work-Thread + Heavy-Pool). Details + Verhaltensänderungen in **`REDESIGN.md`**. Headless verifiziert;
14 Review-Funde gefixt. **Noch offen: echter Lauf auf einer Node** (TUI-UX, drain/finalize, add-node
mitten im Job, Video-Fortschritt live). Damit erledigt (auf Branch): Menü-Rewrite, Video-Fortschritt
(monoton), Recovery (FAIL sichtbar + `[r]` Retry + `[f]` finalize), GPU-Anzahl bleibt via `--min-gpus`.

## Offen zu verifizieren

- [ ] **`sageattn_2` end-to-end abhaken**: lief am 2026-07-08 ohne Crash (4/4 GPUs, echter Upscale-Fortschritt), aber es fehlt die explizite Bestätigung, dass ein Clip **komplett** durchläuft (`FERTIG` → `final/*.mp4` → Pull → „done") und **abspielbar** ist. Installiert ist SageAttention **1.0.6 (v1)**; falls Probleme → `ATTENTION_MODE` in `node/process.sh` auf `sdpa`.

## Backlog (ab 2026-07-09)

- [ ] **cgroup-OOM: Worker-Supervisor + Retry + evtl. mehr RAM.** Am 2026-07-08 auf 4×RTX-5090-Node (`44159176`): der **cgroup-Memory-Killer** (`memory.max`=196 GB, `memory.events: oom_kill 4`) hat mitten im Lauf 4 Prozesse abgeschossen — Ursache: **`--compile_dit --compile_vae` zieht pro Worker ~45–50 GB Host-RAM**, und bei kurzen/ähnlichen Clips **überlappen mehrere Compiles** → 4×~50 GB > 196 GB. `free` zeigt Host (251 GB frei) und verschleiert es; `dmesg` im Container gesperrt → Beweis nur über `/sys/fs/cgroup/memory.events`. Steady-State danach nur ~6 GB. **Symptom:** OOM-Killer erwischt nicht nur `python3`, sondern auch **`worker`-Subshells selbst** → diese GPU bleibt **dauerhaft idle**, denn `process.sh` hat **keinen Supervisor**, der tote Worker respawnt (der `while true`-Loop überlebt nur, wenn *python* stirbt, nicht die Subshell). Nötig:
  1. **Supervisor**: `main` überwacht die Worker-PIDs, respawnt tote GPUs automatisch statt nur `wait` — sonst verlieren wir Geld/Zeit, GPU steht leer.
  2. **Retry statt permanentem FAIL**: bei OOM-Kill den Clip **nicht** als endgültig fehlgeschlagen behandeln — Claim-Lock (`$CLAIMS/$base.lock`) bei Nicht-Erfolg **freigeben**, damit ein anderer Worker (oder derselbe später) es erneut versucht. Aktuell bleibt der Lock liegen → Clip wird im Lauf nie retried.
  3. **RAM-Peak drücken**: `STAGGER_SECONDS` ≥ Compile-Dauer (~180–300s, nicht 30s) **oder** `--compile_dit/--compile_vae` weglassen (`node/process.sh:78`). 196 GB reichen, wenn Compiles nicht überlappen — mehr RAM buchen nur als Fallback.
- [ ] **FAIL wird nicht als Fehler in der DB/Liste sichtbar.** Trotz 4 `FAIL:`-Zeilen im gpu3-Log zeigte `vhsorch videos` „**0 Fehler**" und die Clips blieben „in Arbeit". Worker-FAIL propagiert nicht zum Orchestrator — Clips hängen bis `reconcile`/Orphan-Pass. Fehlerzustand vom Node zum Orchestrator durchreichen (z.B. FAIL-Marker im Log auswerten → DB `error`).
- [ ] **Menü/Flow neu bauen (nicht nur aufräumen).** — `orchestrator/menu.sh`. Am 2026-07-08 im Einsatz erlebt: **richtig laggy & unresponsive**, das Menü bleibt an Punkten hängen und **ignoriert Inputs**. Konkret: „Press any key to close" bei den Live-Ansichten reagiert oft **gar nicht** — man muss warten, bis die (blockierende) Datenabfrage über SSH durch ist und die Daten zurück sind, und erwischt nur in diesem kurzen Fenster überhaupt einen angenommenen Tastendruck. Ursache vermutlich: **synchrone Remote-Abfragen im Haupt-Loop blockieren das Key-Reading** (kein nicht-blockierendes Input, keine getrennte Render-/Fetch-Schleife). Der Ansatz ist von Grund auf fragil → **von vorne denken**: (a) Input-Handling von Datenabruf entkoppeln (Fetch async/im Hintergrund, UI bleibt responsiv), (b) nicht-blockierendes Key-Reading mit sofortiger Reaktion auf „quit/close", (c) letzten bekannten Stand cachen und anzeigen statt bei jedem Frame neu blockierend zu ziehen, (d) evtl. Menü als Python-TUI statt Bash. **Kandidat für kompletten Rewrite, nicht Flickschusterei.**
- [ ] **Video-Fortschritt aus Upscale-Logs herleiten (Referenz-Ansatz).** Der Upscale-%-Balken zyklt/springt, weil SeedVR2 intern **mehrere Phasen nacheinander** durchläuft (grob: VAE-Encode → DiT-Sampling → VAE-Decode, ~3–4 Unterphasen) und je Phase eine eigene tqdm-Bar ausgibt; `remote.gpu_activity()` greift nur das **letzte** `%` und springt darum zurück. **Plan:** Das Upscale-Tool so betreiben/parsen, dass es klar loggt **welche interne Phase** läuft und **welcher Step / wie viele Frames von wie vielen** (FPS-Zähler) fertig sind — das lässt sich sauber rückverfolgen. Daraus **echten, monotonen Gesamtfortschritt** je Video bauen: Unterphasen erkennen, gewichten (Frames × Phasen-Anteil), zu einem durchgehenden %-Wert + Rest-Zeit-Schätzung aggregieren. **Zuerst** herausfinden, wie wir an diese Logzeilen kommen (Log-Level/Flag im Upscale-Tool, Format der tqdm-/Phasen-Ausgabe) — das ist die Referenz. Betrifft `orchestrator/vhsorch/remote.py` + `report.py` (`_phase_bar`).
- [ ] **Recovery von gefailten Videos.** Clips, die abbrechen (OOM-Kill, Crash, Timeout), sollen erkannt und **automatisch neu eingeplant** werden statt hängen zu bleiben. Überlappt mit dem cgroup-OOM-Punkt (Claim-Lock bei Nicht-Erfolg freigeben + Retry) und dem FAIL-Sichtbarkeits-Punkt (FAIL vom Node zum Orchestrator durchreichen) — hier als **eigener Recovery-Pfad zusammenfassen**: Fehlerzustand erkennen → Lock lösen → Requeue → in der Liste als „retry n/m" sichtbar machen.
- [ ] **GPU-Anzahl bei `plan` wählbar machen.** `--min-gpus` existiert schon (Default 4) in `cli.py`; im Menü interaktiv auswählbar. **8 GPUs** ausprobieren.
- [ ] **Setup-Flow verbessern.** Der Setup-Log-Tab (`act_setup_logs` in `menu.sh`) konnte während des Setups die Logs nicht immer folgen — vermutlich weil `node_endpoints()` über die Vast-API listet (späte ssh-Meldung). Idee: Node-Auswahl aus der DB (`vhsorch status`) + „warte auf Bootstrap …"-Anzeige, bis `/workspace/bootstrap.log` existiert.

## Optional / später

- [ ] Boot-Zeit drücken: torch-Reinstall pro Boot vermeiden (evtl. vorhandenes torch-venv des vastai-Image nutzen, oder Golden Docker Image mit gebackenen Deps+Modellen).
