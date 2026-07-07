# TODO / Backlog

Stand: 2026-07-08 — erster erfolgreicher Lauf mit **4/4 GPUs** auf einer frischen Node.

## Aktueller Stand (was steht)

- **Multi-GPU** funktioniert: jeder Worker isoliert per `CUDA_VISIBLE_DEVICES` + `--cuda_device 0` (behebt den `torch.compile` „Invalid device id"-Crash auf `cuda:N>0`).
- **Modelle** (SeedVR2 DiT fp8 + VAE fp16) kommen **per rsync vom Orchestrator**, nicht per HF-Download auf der Node — der Node-Egress auf Vast ist unzuverlässig (IPv6-only-HF, TLS-Reset, 429). Home-Cache unter `MODELS_DIR=/data/models` (WD Red), befüllt mit `vhsorch fetch-models`, gepusht einmal pro Node (`models_pushed`-Flag), Worker startet erst danach.
- **Bootstrap selbstheilend**: SSH-getrieben (nicht auf Vast-onstart wartend), `flock` gegen Doppelläufe, Retry bei git-clone-TLS-Fehlern, ERR-Trap setzt Marker zurück, Skript-Downloads mit Timeout, Phasen-Status in `/workspace/bootstrap.status`.
- **Image**: `vastai/pytorch:@vastai-automatic-tag` (CUDA 13) — torch besteht die GPU-Probe (kein cu128-Reinstall). Achtung: `requirements.txt` installiert torch trotzdem frisch (~2 GB/Boot) → größter verbleibender Zeitfresser.

## Offen zu verifizieren

- [ ] **`sageattn_2` end-to-end abhaken**: lief am 2026-07-08 ohne Crash (4/4 GPUs, echter Upscale-Fortschritt), aber es fehlt die explizite Bestätigung, dass ein Clip **komplett** durchläuft (`FERTIG` → `final/*.mp4` → Pull → „done") und **abspielbar** ist. Installiert ist SageAttention **1.0.6 (v1)**; falls Probleme → `ATTENTION_MODE` in `node/process.sh` auf `sdpa`.

## Backlog (ab 2026-07-09)

- [ ] **Menü/Flow allgemein aufräumen** — `orchestrator/menu.sh`.
- [ ] **Video-Fortschritt korrekt anzeigen.** Der Upscale-%-Balken zyklt/springt, weil SeedVR2 intern mehrere tqdm-Bars nacheinander ausgibt (VAE-Encode, DiT-Sampling, VAE-Decode). `remote.gpu_activity()` greift nur das letzte `%`. Echten, monotonen Fortschritt pro Video herleiten (Sub-Phasen erkennen/gewichten) — `orchestrator/vhsorch/remote.py` + `report.py` (`_phase_bar`).
- [ ] **GPU-Anzahl bei `plan` wählbar machen.** `--min-gpus` existiert schon (Default 4) in `cli.py`; im Menü interaktiv auswählbar. **8 GPUs** ausprobieren.
- [ ] **Setup-Flow verbessern.** Der Setup-Log-Tab (`act_setup_logs` in `menu.sh`) konnte während des Setups die Logs nicht immer folgen — vermutlich weil `node_endpoints()` über die Vast-API listet (späte ssh-Meldung). Idee: Node-Auswahl aus der DB (`vhsorch status`) + „warte auf Bootstrap …"-Anzeige, bis `/workspace/bootstrap.log` existiert.

## Optional / später

- [ ] Boot-Zeit drücken: torch-Reinstall pro Boot vermeiden (evtl. vorhandenes torch-venv des vastai-Image nutzen, oder Golden Docker Image mit gebackenen Deps+Modellen).
