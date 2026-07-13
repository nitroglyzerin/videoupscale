#!/usr/bin/env bash
# ============================================================================
#  KOMPONENTE 2 — Verarbeitungs-Script (Multi-GPU-Parallel-Worker)
#
#  Läuft auf der Vast-Node. Verarbeitet alle Clips in INPUT_DIR round-robin
#  über die vorhandenen GPUs und schreibt fertige Clips nach FINAL_DIR.
#
#  Abbruchsicherer Start (empfohlen):
#      tmux new -s upscale '/workspace/process.sh 2>&1 | tee /workspace/work/run.log'
#    oder
#      nohup /workspace/process.sh > /workspace/work/run.log 2>&1 &
#
#  Resume: bereits fertige Clips (FINAL_DIR/<name>.mp4 existiert) werden
#  übersprungen — einfach erneut starten.
# ============================================================================
set -uo pipefail

# --- Konfiguration (FINALE, getestete Parameter — nicht abweichen) -----------
INPUT_DIR="${INPUT_DIR:-/workspace/input}"
WORK_DIR="${WORK_DIR:-/workspace/work}"
FINAL_DIR="${FINAL_DIR:-/workspace/final}"
TMP_DIR="${TMP_DIR:-/workspace/tmp}"
SEEDVR2_DIR="${SEEDVR2_DIR:-/workspace/seedvr2}"

DIT_MODEL="seedvr2_ema_3b_fp8_e4m3fn.safetensors"
RESOLUTION=720
BATCH_SIZE=25          # 4n+1-Regel erfüllt (4*6+1 = 25)
COLOR_CORRECTION="wavelet"
VIDEO_BACKEND="ffmpeg"

# --- A/B-Tunables (Kosten/Frame) --------------------------------------------
# Per Env ueberschreibbar; Default = getestete Werte, KEINE Abweichung ohne
# explizite Env-Vorgabe. So laesst sich auf einer Test-Node vergleichen, ohne
# die "finale" Konfig zu editieren. Immer mit Clip-Abnahme validieren.
#   ATTENTION_MODE=sageattn_3      -> Blackwell-Attention (5090), potenziell schneller
#   COMPILE_MODE=max-autotune-no-cudagraphs -> bessere DiT/VAE-Kernel (amortisiert
#                                    ueber Inductor-Cache); -no-cudagraphs meidet den
#                                    CUDA-Graph-Pool, der sonst den cgroup-OOM fuettert
ATTENTION_MODE="${ATTENTION_MODE:-sageattn_2}"
COMPILE_MODE="${COMPILE_MODE:-}"   # leer = SeedVR2-Default (nichts uebergeben)

# Versatz zwischen dem Start der Worker (Sekunden), damit die initiale
# torch.compile-Kompilierung nicht gleichzeitig auf allen GPUs losläuft.
STAGGER_SECONDS="${STAGGER_SECONDS:-30}"

mkdir -p "$WORK_DIR" "$FINAL_DIR" "$TMP_DIR" "$WORK_DIR/logs"

# Single-Instance-Guard: ein zweiter process.sh-Start (z. B. ein versehentlicher
# zweiter Worker-Nudge, während der erste noch läuft) darf KEINEN zweiten
# Worker-Baum aufbauen — sonst laufen 2 SeedVR2-Inferenzen pro physischer GPU
# -> VRAM/cgroup-OOM. flock hält genau EINE Instanz; ein zweiter Start beendet
# sich sofort sauber (exit 0). Fällt flock, läuft es wie bisher (best effort).
exec 200>/workspace/.process.lock
if command -v flock >/dev/null 2>&1; then
  flock -n 200 || { echo "[process] läuft bereits (flock) — zweiter Start beendet sich."; exit 0; }
fi

log()  { echo -e "\033[1;36m[process]\033[0m $*"; }
warn() { echo -e "\033[1;33m[process WARN]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[process FEHLER]\033[0m $*" >&2; exit 1; }

# SOFORT loggen (vor dem evtl. hängenden nvidia-smi/cd), damit run.log nie leer
# ist, wenn process.sh wirklich läuft. Leeres run.log + Worker="läuft" bedeutet
# dann: hängt VOR dieser Zeile (mkdir/flock) oder pgrep-Fehlalarm.
log "process.sh gestartet (PID $$) — prüfe Umgebung & GPUs …"

# In das SeedVR2-Verzeichnis wechseln: inference_cli.py sucht/erwartet das
# Modell unter ./models/SEEDVR2 (CWD-relativ). Ein festes CWD stellt sicher,
# dass es das in bootstrap.sh VORAB geladene Modell findet — kein Laufzeit-
# Download, kein Wettlauf mehrerer Worker um dieselbe .download-Datei.
cd "$SEEDVR2_DIR" || die "SEEDVR2_DIR ($SEEDVR2_DIR) nicht gefunden — Bootstrap gelaufen?"

# ============================================================================
#  FP8-Guard: fp8_e4m3 kompiliert nur auf Ada (8.9) / Blackwell (>=9.0).
#  Auf Ampere (A100=8.0, 3090=8.6) NICHT -> sauber abbrechen.
# ============================================================================
command -v nvidia-smi >/dev/null 2>&1 || die "nvidia-smi nicht gefunden — keine GPU?"

mapfile -t COMPUTE_CAPS < <(nvidia-smi --query-gpu=compute_cap --format=csv,noheader | tr -d ' ')
NGPU="${#COMPUTE_CAPS[@]}"
[ "$NGPU" -ge 1 ] || die "Keine GPU erkannt."

log "Erkannte GPUs: $NGPU"
for i in "${!COMPUTE_CAPS[@]}"; do
  cap="${COMPUTE_CAPS[$i]}"
  name="$(nvidia-smi -i "$i" --query-gpu=name --format=csv,noheader)"
  # Vergleich als Zahl: 8.9 wird zu 89, 8.0 zu 80, 12.0 zu 120 usw.
  cap_num="$(echo "$cap" | awk -F. '{ printf "%d", $1*10 + $2 }')"
  log "  GPU $i: $name (compute_cap $cap)"
  if [ "$cap_num" -lt 89 ]; then
    die "GPU $i ($name, cap $cap) ist Ampere/älter. FP8 (e4m3) wird dort nicht \
kompiliert. Diese Node ist für das fp8-Modell ungeeignet — bitte eine \
RTX 4090/5090 (Ada/Blackwell) buchen. Abbruch."
  fi
done
log "FP8-Guard bestanden — alle GPUs sind Ada/Blackwell-fähig."

# ============================================================================
#  Verarbeitung eines einzelnen Clips.
#    $1 = Pfad zum Input-Clip
#    $2 = GPU-Index
# ============================================================================
process_clip() {
  local input="$1" gpu="$2"
  local base name
  base="$(basename "$input")"
  name="${base%.*}"

  local final="$FINAL_DIR/$name.mp4"
  local dechroma="$WORK_DIR/${name}.dechroma.mp4"
  local upscaled="$WORK_DIR/${name}.up.mp4"

  # --- Resume: bereits fertig? ---
  if [ -f "$final" ]; then
    log "[GPU $gpu] SKIP (fertig): $name"
    return 0
  fi

  log "[GPU $gpu] START: $name"

  # --- 1. De-Chroma / Denoise --------------------------------------------
  # Phasen-Marker: der Monitor liest die ZULETZT geloggte PHASE-Zeile (monoton,
  # unabhängig davon, wann die Zwischendateien angelegt werden).
  log "[GPU $gpu] PHASE Denoise: $name"
  local t_dn=$SECONDS
  if ! ffmpeg -y -i "$input" \
        -vf "hqdn3d=1:8:2:8" \
        -c:v libx264 -crf 14 -preset slow \
        "$dechroma" </dev/null; then
    warn "[GPU $gpu] De-Chroma fehlgeschlagen: $name — überspringe Clip."
    log "[GPU $gpu] FAIL: $name"
    return 1
  fi
  # TIMING: Denoise laeuft auf der CPU -> in dieser Zeit IDLET die GPU (Kosten!).
  log "[GPU $gpu] TIMING Denoise ${name}: $(( SECONDS - t_dn ))s"

  # --- 2. SeedVR2-Upscale ------------------------------------------------
  # GPU-ISOLATION per CUDA_VISIBLE_DEVICES statt --cuda_device:
  # inference_cli.py/Torch sprechen intern teils fest cuda:0 an. Übergibt man
  # nur --cuda_device N (N>0), landen die Worker 1/2/3 im Leeren (0 MiB, 0 %)
  # während GPU 0 als einzige rechnet. Mit CUDA_VISIBLE_DEVICES sieht JEDER
  # Prozess ausschließlich SEINE physische Karte — und zwar als Index 0. Wir
  # geben deshalb konsequent --cuda_device 0.
  #
  # Caches PRO GPU: nicht nur der torch.inductor-Cache muss getrennt sein,
  # sondern AUCH der Triton-Cache. SageAttention/Triton kompilieren sonst alle
  # in das geteilte ~/.triton/cache -> Lock-Contention beim Erst-Compile
  # serialisiert die Worker (nur eine GPU „arbeitet" sichtbar).
  local cache_dir="$TMP_DIR/inductor_gpu${gpu}"
  mkdir -p "$cache_dir" "$cache_dir/triton"

  # Optionale A/B-Flags nur anhaengen, wenn per Env gesetzt (sonst SeedVR2-Default).
  local extra_args=()
  [ -n "$COMPILE_MODE" ] && extra_args+=(--compile_mode "$COMPILE_MODE")

  log "[GPU $gpu] PHASE Upscale: $name  (attn=$ATTENTION_MODE compile_mode=${COMPILE_MODE:-default})"
  local t_up=$SECONDS
  if ! CUDA_VISIBLE_DEVICES="$gpu" \
       TORCHINDUCTOR_CACHE_DIR="$cache_dir" \
       TRITON_CACHE_DIR="$cache_dir/triton" \
       python3 "$SEEDVR2_DIR/inference_cli.py" "$dechroma" \
        --output "$upscaled" \
        --output_format mp4 \
        --dit_model "$DIT_MODEL" \
        --resolution "$RESOLUTION" \
        --batch_size "$BATCH_SIZE" \
        --color_correction "$COLOR_CORRECTION" \
        --compile_dit --compile_vae \
        --attention_mode "$ATTENTION_MODE" \
        --video_backend "$VIDEO_BACKEND" \
        "${extra_args[@]}" \
        --cuda_device 0 </dev/null; then
    warn "[GPU $gpu] SeedVR2-Upscale fehlgeschlagen: $name — überspringe Clip."
    log "[GPU $gpu] FAIL: $name"   # Marker für den Monitor (busy=0, sichtbar als Fehler)
    rm -f "$dechroma"
    return 1
  fi
  # TIMING: reine GPU-Arbeit (Encode+Upscale+Decode). Vergleichsgroesse fuers
  # A/B von ATTENTION_MODE / COMPILE_MODE — kleiner = guenstiger pro Frame.
  log "[GPU $gpu] TIMING Upscale ${name}: $(( SECONDS - t_up ))s"

  # --- 3. Audio-Remux (robust) -------------------------------------------
  # Prüfe, ob der ORIGINAL-Clip eine Audiospur hat. Fehlender Ton darf den
  # Clip NICHT fehlschlagen lassen (realer Bug in der Vergangenheit).
  log "[GPU $gpu] PHASE Audio: $name"
  local has_audio
  has_audio="$(ffprobe -v error -select_streams a \
                 -show_entries stream=index -of csv=p=0 "$input" 2>/dev/null | head -1)"

  if [ -n "$has_audio" ]; then
    log "[GPU $gpu] Audiospur vorhanden — muxe Video+Audio: $name"
    if ! ffmpeg -y -i "$upscaled" -i "$input" \
          -map 0:v -map 1:a \
          -c:v libx264 -crf 16 -c:a aac \
          "$final" </dev/null; then
      warn "[GPU $gpu] Audio-Remux fehlgeschlagen: $name — überspringe Clip."
      log "[GPU $gpu] FAIL: $name"
      return 1
    fi
  else
    log "[GPU $gpu] Keine Audiospur — schreibe nur Video: $name"
    if ! ffmpeg -y -i "$upscaled" -map 0:v -c:v copy "$final" </dev/null; then
      warn "[GPU $gpu] Video-Schreiben fehlgeschlagen: $name — überspringe Clip."
      log "[GPU $gpu] FAIL: $name"
      return 1
    fi
  fi

  # --- Aufräumen der Zwischendateien -------------------------------------
  rm -f "$dechroma" "$upscaled"
  log "[GPU $gpu] FERTIG: $name -> $final"
  return 0
}

# ============================================================================
#  Streaming-Zuteilung
#  ---------------------------------------------------------------------------
#  Die Worker greifen Clips KONTINUIERLICH ab: sie scannen INPUT_DIR immer
#  wieder und schnappen sich neu eingetroffene Clips atomar per Claim-Lock
#  (mkdir ist atomar). So kann die Verarbeitung schon starten, WÄHREND der
#  Home-Orchestrator noch weitere Clips hochlädt — bei 1200 Videos essenziell.
#
#  Vorteil gegenüber starrem Round-Robin: dynamischer Lastausgleich. Eine GPU,
#  die früher fertig ist, greift sich einfach den nächsten freien Clip.
#
#  Wichtig: rsync schreibt in versteckte Temp-Dateien (.name.XXXX) und benennt
#  erst nach vollständigem Transfer um. `ls` (ohne -a) sieht daher nur FERTIG
#  übertragene Clips — halb-hochgeladene werden nie angefasst.
# ============================================================================
CLAIMS="$WORK_DIR/claims"
IDLE_SLEEP="${IDLE_SLEEP:-8}"   # Sekunden warten, wenn gerade nichts zu tun ist

# Beim (Neu-)Start alte Claims verwerfen: unterbrochene, nicht fertige Clips
# (kein final vorhanden) sollen erneut verarbeitet werden -> sauberes Resume.
rm -rf "$CLAIMS"; mkdir -p "$CLAIMS"

worker() {
  local gpu="$1"
  local logfile="$WORK_DIR/logs/gpu${gpu}.log"

  # Gestaffelter Start gegen gleichzeitige initiale torch.compile-Kompilierung.
  local delay=$(( gpu * STAGGER_SECONDS ))
  log "[GPU $gpu] Worker startet in ${delay}s …"
  sleep "$delay"

  # HINWEIS: Die frühere Modell-Warteschleife (GPU 1/2/3 warten auf einen
  # Log-Marker von GPU 0) ist ENTFERNT. Sie sollte den .download-Wettlauf
  # verhindern — aber bootstrap.sh lädt das Modell inzwischen EINMAL atomar
  # vorab (models/SEEDVR2/<datei> liegt fertig auf Platte), also gibt es zur
  # Laufzeit gar keinen Download und keinen Wettlauf mehr. Die Warteschleife
  # serialisierte den Start nur (bis zu 15 min) und trug so dazu bei, dass
  # sichtbar „nur eine GPU arbeitet". Alle Worker starten jetzt sofort.

  # Endlos abgreifen, bis die Node zerstört wird (Orchestrator bei leerer
  # Queue). Kein Selbst-Exit nötig — die Node wird von außen abgeräumt.
  while true; do
    local worked=0
    # leerzeichen-sicher iterieren (Namen wie "1994-1  T06  V01.mp4").
    while IFS= read -r base; do
      [ -n "$base" ] || continue
      local path="$INPUT_DIR/$base"
      [ -f "$path" ] || continue
      local name="${base%.*}"
      # schon fertig? überspringen (Resume).
      [ -f "$FINAL_DIR/$name.mp4" ] && continue
      # atomar claimen; nur der Gewinner verarbeitet diesen Clip.
      if mkdir "$CLAIMS/$base.lock" 2>/dev/null; then
        process_clip "$path" "$gpu" >>"$logfile" 2>&1
        worked=1
      fi
    # Größte Datei zuerst (≈ meiste Frames): der Orchestrator lädt die teuersten
    # Clips zuerst hoch — dieselbe Priorität auch beim Claimen auf der Node.
    done < <(ls -1S "$INPUT_DIR" 2>/dev/null)

    # Nichts abgegriffen? kurz warten, dann erneut scannen (Streaming-Intake).
    [ "$worked" -eq 0 ] && sleep "$IDLE_SLEEP"
  done
}

# ============================================================================
#  Main: starte einen Worker pro GPU. Läuft, bis die Node zerstört wird.
# ============================================================================
log "Starte $NGPU parallele Worker (Stagger ${STAGGER_SECONDS}s, Streaming-Intake) …"
log "Input:  $INPUT_DIR   (wird laufend nach neuen Clips gescannt)"
log "Final:  $FINAL_DIR"

pids=()
for gpu in $(seq 0 $(( NGPU - 1 ))); do
  worker "$gpu" &
  pids+=("$!")
done

# Auf die (endlos laufenden) Worker warten.
for pid in "${pids[@]}"; do
  wait "$pid"
done
