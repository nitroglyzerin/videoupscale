# VHS-Upscale — verteiltes SeedVR2-Orchestrierungssystem (Home ↔ Vast.ai)

Automatisiertes Push-Pull-Upscaling alter VHS-Clips (PAL 720×576, deinterlaced)
mit **SeedVR2** auf gemieteten **Vast.ai**-GPU-Nodes. Rohvideos liegen zuhause
auf einem Proxmox-Server, werden zu einer oder mehreren Vast-Nodes geschoben,
dort auf 4 GPUs parallel verarbeitet und fertig wieder eingesammelt.
**Kostenschutz ist Prio 1:** Nodes laufen nie ohne Arbeit, leere Queue → Auto-Destroy.

```
   Home (Proxmox-LXC, Docker)                 Vast.ai Node (RTX 4090/5090 x4)
   ┌───────────────────────┐   rsync/ssh      ┌────────────────────────────┐
   │  vhsorch (Python)      │  ── push raw ──▶ │ /workspace/input           │
   │  · plan / book         │                  │  process.sh (4 Worker)     │
   │  · SQLite-Queue        │                  │   dechroma→SeedVR2→remux   │
   │  · Multi-Node-Verteil. │  ◀── pull final ─│ /workspace/final           │
   │  · Vast-Lifecycle      │                  └────────────────────────────┘
   └───────────────────────┘   nur AUSGEHEND
```

## Komponenten

| Datei | Rolle |
|---|---|
| [node/bootstrap.sh](node/bootstrap.sh) | **Komponente 1** — macht eine frische Vast-Node einsatzbereit (SeedVR2 klonen, deps, ffmpeg, Verzeichnisse, process.sh laden). Läuft per `onstart-cmd` oder `curl … \| bash`. |
| [node/process.sh](node/process.sh) | **Komponente 2** — 4-GPU-Parallel-Worker mit FP8-Guard, per-GPU torch.compile-Cache, gestaffeltem Start, robustem Audio-Remux, Resume. |
| [orchestrator/](orchestrator/) | **Komponente 3** — Python-Orchestrator als Docker-Container (Vast-API-Lifecycle, rsync push/pull, SQLite-Job-Queue, Multi-Node-Verteilung, Auto-Destroy). |

---

## Quickstart

### 0. Repo public auf GitHub pushen (für curl-Bootstrapping)
`bootstrap.sh` und `process.sh` werden von der Node per HTTPS-`curl` aus **diesem
public Repo** geladen. Passe die Raw-URL an deinen Handle an, falls sie abweicht —
sie steckt an einer Stelle: `REPO_RAW_URL` (in `.env` und oben in `bootstrap.sh`).
Default: `https://raw.githubusercontent.com/nitroglyzerin/videoupscale/main`.

### 1. Home-Orchestrator vorbereiten (im Docker-LXC)
```bash
cd orchestrator
cp ../.env.example .env      # ausfüllen: VAST_API_KEY, REPO_RAW_URL, …
mkdir -p secrets state

# SSH-Key erzeugen, mit dem der Orchestrator die Nodes erreicht:
ssh-keygen -t ed25519 -f secrets/id_ed25519 -N ""
chmod 600 secrets/id_ed25519
# -> den ÖFFENTLICHEN Teil (secrets/id_ed25519.pub) in deinem Vast-Account
#    hinterlegen: console.vast.ai → Account → SSH Keys. Sonst kein rsync-Zugang.

# WD-Red-Batch-Ordner anlegen (siehe Sicherheitshinweis unten):
#   /mnt/hassos/vhs-batch/raw   (Input)
#   /mnt/hassos/vhs-batch/done  (Output)

docker compose build
```

### 2. Node buchen — mit Handbremse (Zwei-Schritt)
```bash
# Top-Kandidaten anzeigen (nichts wird gebucht):
docker compose run --rm orchestrator plan

#   #   OFFER-ID  GPU          GPUs      $/h  DLPerf/$   Rel.   Disk  Ort
#   1   12345678  RTX 5090        4    2.400     412.5  99.8%   350G  DE
#   2   87654321  RTX 4090        4    1.900     388.1  99.7%   400G  US
#   …

# Genau EIN Offer buchen (löst onstart-Bootstrap auf der Node aus):
docker compose run --rm orchestrator book 12345678
```

### 3. Loop starten (verteilt, sammelt ein, zerstört bei leerer Queue)
```bash
docker compose up -d          # startet `vhsorch run`
docker compose logs -f        # Queue-Status + laufende Kosten live
```
Rohclips einfach nach `/mnt/hassos/vhs-batch/raw/` legen (am besten per `mv`
aus einem Staging-Ordner). Fertige landen in `…/done/`. Bei leerer Queue wird
jede Node automatisch zerstört (`AUTO_DESTROY=1`).

### Weitere Befehle
```bash
docker compose run --rm orchestrator status        # lokale Queue + Nodes
docker compose run --rm orchestrator nodes         # laufende Vast-Instanzen
docker compose run --rm orchestrator destroy all   # Not-Aus: alles zerstören
```

---

## Sicherheits-relevante Stellen (bewusst erklärt)

### rw-Mount der WD Red — strikt getrennt von Jellyfin
In [orchestrator/docker-compose.yml](orchestrator/docker-compose.yml) wird **nur**
das dedizierte Batch-Unterverzeichnis gemountet:
```yaml
- /mnt/hassos/vhs-batch/raw:/data/raw:rw
- /mnt/hassos/vhs-batch/done:/data/done:rw
```
Der Container sieht **nicht** die ganze Platte und **nicht** die Jellyfin-
Medienstruktur — er kann ausschließlich innerhalb `vhs-batch/` schreiben. Das ist
die Brandmauer gegen versehentliches Überschreiben deiner Mediathek. Passe den
linken Pfad an dein reales Mount an, aber halte ihn eng.

### API-Key- & SSH-Key-Handling
- **VAST_API_KEY** kommt ausschließlich aus `.env` (in `.gitignore`, nie im Code).
  Der Client legt ihn nur in den HTTP-Auth-Header und loggt ihn nie
  ([orchestrator/vhsorch/vast.py](orchestrator/vhsorch/vast.py)).
- **SSH-Private-Key** wird **read-only** (`:ro`) als Secret gemountet
  (`./secrets:/secrets:ro`), verlässt den Host nie. Nur der zugehörige
  **Public**-Key liegt in deinem Vast-Account.
- **Nur ausgehende Verbindungen** Home → Vast (rsync-über-SSH). Zuhause sind
  keine Portfreigabe und kein DynDNS nötig.

---

## Multi-Node-Verteilungslogik

Der Scheduler ([orchestrator/vhsorch/scheduler.py](orchestrator/vhsorch/scheduler.py))
verteilt so:

1. **Ingest (staging-sicher):** Ein Rohclip wird erst in die Queue aufgenommen,
   wenn seine Größe über `STABLE_CHECKS` Polls stabil ist (kein halb-
   hochgeladenes File). `mv` aus einem Staging-Ordner ist sofort stabil.
2. **Zuweisung (kapazitätsgewichtet):** Jeder Clip geht an **genau eine** Node
   (`clips.node_id`). Nodes mit mehr GPUs bekommen proportional mehr Clips
   (Slot-Gewichtung nach `num_gpus`). Keine zwei Nodes rechnen denselben Clip.
3. **Push → Worker → Pull:** Zugewiesene Clips werden per rsync in
   `/workspace/input` geschoben; `process.sh` läuft abbruchsicher in `tmux`;
   Ergebnisse werden aus `/workspace/final` delta-basiert zurückgezogen.
4. **Selbstheilung:** Verschwindet eine Node aus Vast oder wird unerreichbar,
   werden ihre noch offenen Clips atomar auf `pending` zurückgesetzt und im
   nächsten Takt neu verteilt.
5. **Kostenschutz:** Sobald alle Clips `done`/`failed` sind, wird jede Node
   zerstört. Kosten (`$/h` × Laufzeit) werden pro Takt geloggt.

Die gesamte Zuweisung ist in der SQLite-DB nachvollziehbar
(`state/vhsorch.sqlite`) und übersteht Container-Neustarts (Resume).

---

## Feste Verarbeitungs-Parameter (getestet — nicht abweichen)

- Modell `seedvr2_ema_3b_fp8_e4m3fn.safetensors`, `--resolution 720`,
  `--batch_size 25` (erfüllt die 4n+1-Regel), `--color_correction wavelet`,
  `--compile_dit --compile_vae`, `--attention_mode sageattn_2`,
  `--video_backend ffmpeg`.
- Pro Clip: **(1)** De-Chroma `ffmpeg -vf "hqdn3d=1:8:2:8" -c:v libx264 -crf 14
  -preset slow` → **(2)** SeedVR2-Upscale → **(3)** Audio-Remux.
- **Audio-Remux robust:** `ffprobe` prüft auf Audiospur. Mit Ton →
  `-map 0:v -map 1:a -c:v libx264 -crf 16 -c:a aac`; ohne Ton → nur Video.
  Fehlender Ton lässt den Clip **nicht** fehlschlagen.
- **Parallel-compile-Fix:** jeder GPU-Worker hat einen eigenen
  `TORCHINDUCTOR_CACHE_DIR=/workspace/tmp/inductor_gpu${N}` plus 30 s
  gestaffelten Start — verhindert das gegenseitige Zerschießen des
  torch.compile-Caches.
- **FP8-Guard:** FP8 kompiliert nur auf Ada/Blackwell. Auf Ampere (A100/3090)
  bricht `process.sh` mit klarer Meldung ab.
- **flash-attn wird NIE installiert** (20–60 Min Kompilierzeit, unnötig).

Referenz-Durchsatz: 3B FP8 @ 720, batch 25 ≈ 2,66 fps/GPU (5090); 4 GPUs ≈ 10 fps;
~1200 Clips ≈ ~43 h Wandzeit auf einer 4er-Node.

---

## Anmerkungen / anzupassen

- **Vast-API-Felder:** Die v0-Endpunkte sind stabil, können sich aber ändern.
  Alle Aufrufe liegen zentral in [vhsorch/vast.py](orchestrator/vhsorch/vast.py)
  und sind gegen die [Vast-Doku](https://vast.ai/docs/api) leicht prüfbar.
- **SeedVR2-CLI:** Entrypoint `inference_cli.py`, Modelle laden automatisch von
  HuggingFace nach `./models/SEEDVR2`. Flag-Namen ggf. gegen die aktuelle
  SeedVR2-README abgleichen (z. B. `--dit_model`).
