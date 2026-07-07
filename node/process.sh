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
ATTENTION_MODE="sageattn_2"
VIDEO_BACKEND="ffmpeg"

# Versatz zwischen dem Start der Worker (Sekunden), damit die initiale
# torch.compile-Kompilierung nicht gleichzeitig auf allen GPUs losläuft.
STAGGER_SECONDS="${STAGGER_SECONDS:-30}"

mkdir -p "$WORK_DIR" "$FINAL_DIR" "$TMP_DIR" "$WORK_DIR/logs"

log()  { echo -e "\033[1;36m[process]\033[0m $*"; }
warn() { echo -e "\033[1;33m[process WARN]\033[0m $*" >&2; }
die()  { echo -e "\033[1;31m[process FEHLER]\033[0m $*" >&2; exit 1; }

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
  if ! ffmpeg -y -i "$input" \
        -vf "hqdn3d=1:8:2:8" \
        -c:v libx264 -crf 14 -preset slow \
        "$dechroma" </dev/null; then
    warn "[GPU $gpu] De-Chroma fehlgeschlagen: $name — überspringe Clip."
    return 1
  fi

  # --- 2. SeedVR2-Upscale ------------------------------------------------
  # Eigener torch.compile-Cache PRO GPU -> verhindert die Kollision, wenn
  # mehrere Worker parallel kompilieren (/tmp/torchinductor_root wäre geteilt).
  local cache_dir="$TMP_DIR/inductor_gpu${gpu}"
  mkdir -p "$cache_dir"

  if ! TORCHINDUCTOR_CACHE_DIR="$cache_dir" \
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
        --cuda_device "$gpu" </dev/null; then
    warn "[GPU $gpu] SeedVR2-Upscale fehlgeschlagen: $name — überspringe Clip."
    rm -f "$dechroma"
    return 1
  fi

  # --- 3. Audio-Remux (robust) -------------------------------------------
  # Prüfe, ob der ORIGINAL-Clip eine Audiospur hat. Fehlender Ton darf den
  # Clip NICHT fehlschlagen lassen (realer Bug in der Vergangenheit).
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
      return 1
    fi
  else
    log "[GPU $gpu] Keine Audiospur — schreibe nur Video: $name"
    if ! ffmpeg -y -i "$upscaled" -map 0:v -c:v copy "$final" </dev/null; then
      warn "[GPU $gpu] Video-Schreiben fehlgeschlagen: $name — überspringe Clip."
      return 1
    fi
  fi

  # --- Aufräumen der Zwischendateien -------------------------------------
  rm -f "$dechroma" "$upscaled"
  log "[GPU $gpu] FERTIG: $name -> $final"
  return 0
}

# ============================================================================
#  Worker: verarbeitet die ihm per round-robin zugeteilten Clips.
#    $1 = GPU-Index (0..NGPU-1)
# ============================================================================
worker() {
  local gpu="$1"
  local logfile="$WORK_DIR/logs/gpu${gpu}.log"

  # Gestaffelter Start gegen gleichzeitige initiale Kompilierung.
  local delay=$(( gpu * STAGGER_SECONDS ))
  log "[GPU $gpu] Worker startet in ${delay}s …"
  sleep "$delay"

  # Deterministische, stabile Reihenfolge über alle Worker hinweg.
  local idx=0
  shopt -s nullglob
  for input in $(ls -1 "$INPUT_DIR" | sort); do
    local path="$INPUT_DIR/$input"
    [ -f "$path" ] || continue
    # round-robin: Clip idx gehört zu GPU (idx % NGPU).
    if [ $(( idx % NGPU )) -eq "$gpu" ]; then
      process_clip "$path" "$gpu" >>"$logfile" 2>&1
    fi
    idx=$(( idx + 1 ))
  done
  log "[GPU $gpu] Worker fertig."
}

# ============================================================================
#  Main: starte einen Worker pro GPU, warte auf alle.
# ============================================================================
log "Starte $NGPU parallele Worker (Stagger ${STAGGER_SECONDS}s) …"
log "Input:  $INPUT_DIR"
log "Final:  $FINAL_DIR"

pids=()
for gpu in $(seq 0 $(( NGPU - 1 ))); do
  worker "$gpu" &
  pids+=("$!")
done

fail=0
for pid in "${pids[@]}"; do
  wait "$pid" || fail=1
done

log "Alle Worker beendet."
[ "$fail" -eq 0 ] || warn "Mindestens ein Worker meldete einen Fehler — siehe $WORK_DIR/logs/."
log "Fertige Clips liegen in $FINAL_DIR."
