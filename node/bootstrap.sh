#!/usr/bin/env bash
# ============================================================================
#  KOMPONENTE 1 — Vast-Bootstrap
#  Macht eine frische Vast.ai-Instanz einsatzbereit für SeedVR2-Upscaling.
#
#  Aufruf (eines von beidem):
#    * Vast onstart-cmd:
#        curl -fsSL https://raw.githubusercontent.com/nitroglyzerin/videoupscale/main/node/bootstrap.sh | bash
#    * Manuell auf der Node:
#        bash bootstrap.sh
#
#  Idempotent: mehrfaches Ausführen ist sicher (überspringt bereits Erledigtes).
# ============================================================================
set -euo pipefail

# --- Konfiguration -----------------------------------------------------------
# Raw-URL zu DIESEM Repo. Falls dein GitHub-Handle/Branch abweicht: hier ändern.
REPO_RAW_URL="${REPO_RAW_URL:-https://raw.githubusercontent.com/nitroglyzerin/videoupscale/main}"
SEEDVR2_REPO="https://github.com/numz/ComfyUI-SeedVR2_VideoUpscaler.git"
SEEDVR2_DIR="/workspace/seedvr2"

# SeedVR2-DiT-Modell (fp8) — MUSS zum DIT_MODEL in process.sh passen.
# Wird hier EINMAL vorab geladen (siehe Abschnitt 4c), damit zur Laufzeit kein
# Worker mehr herunterladen muss.
MODEL_NAME="${MODEL_NAME:-seedvr2_ema_3b_fp8_e4m3fn.safetensors}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/${MODEL_NAME}}"
# process.sh macht `cd $SEEDVR2_DIR`, daher ist ./models/SEEDVR2 genau hier:
MODEL_DIR="$SEEDVR2_DIR/models/SEEDVR2"

log() { echo -e "\033[1;36m[bootstrap]\033[0m $*"; }
die() { echo -e "\033[1;31m[bootstrap FEHLER]\033[0m $*" >&2; exit 1; }

log "Starte Node-Bootstrap …"

# --- 1. System-Pakete: ffmpeg + git + rsync sicherstellen --------------------
if ! command -v ffmpeg >/dev/null 2>&1; then
  log "ffmpeg fehlt — installiere via apt-get …"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -y
  apt-get install -y --no-install-recommends ffmpeg
fi
for tool in git rsync curl tmux; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y && apt-get install -y --no-install-recommends "$tool"
  fi
done
log "ffmpeg: $(ffmpeg -version | head -1)"

# --- 2. Arbeitsverzeichnisse -------------------------------------------------
mkdir -p /workspace/input /workspace/work /workspace/final /workspace/tmp
log "Verzeichnisse angelegt: input work final tmp"

# --- 3. SeedVR2 klonen -------------------------------------------------------
if [ ! -d "$SEEDVR2_DIR/.git" ]; then
  log "Klone SeedVR2 nach $SEEDVR2_DIR …"
  git clone --depth 1 "$SEEDVR2_REPO" "$SEEDVR2_DIR"
else
  log "SeedVR2 bereits vorhanden — git pull …"
  git -C "$SEEDVR2_DIR" pull --ff-only || true
fi

# --- 4. Python-Abhängigkeiten ------------------------------------------------
cd "$SEEDVR2_DIR"
PIP="python3 -m pip"
$PIP install --upgrade pip

if [ -f requirements.txt ]; then
  log "Installiere requirements.txt …"
  $PIP install -r requirements.txt
else
  log "WARNUNG: keine requirements.txt in $SEEDVR2_DIR gefunden — überspringe."
fi

# --- 4b. Blackwell/sm_120-Guard: kann PyTorch die GPU ansteuern? -------------
# RTX 5090 = compute cap 12.0 (sm_120). PyTorch-Builds mit CUDA < 12.8 haben
# KEINE sm_120-Kernel -> schon torch.randn(...,device='cuda') scheitert mit
# "no kernel image is available for execution on the device". Wir prüfen das
# und installieren bei Bedarf den cu128-Build nach (self-healing).
gpu_probe() {
  python3 - <<'PY' 2>/dev/null
import torch
torch.randn(8, 8, dtype=torch.bfloat16, device="cuda:0")
print("ok")
PY
}
log "Prüfe, ob PyTorch die GPU ansteuern kann (Blackwell/sm_120) …"
if [ "$(gpu_probe)" != "ok" ]; then
  log "PyTorch kann die GPU nicht ansteuern — installiere cu128-Build (sm_120) …"
  # WICHTIG: torch, torchvision UND torchaudio zusammen — sonst ABI-Mismatch
  # (undefined symbol in libtorchaudio.so), der SeedVR2 bei jedem Clip crasht.
  $PIP install --upgrade --force-reinstall torch torchvision torchaudio \
    --index-url https://download.pytorch.org/whl/cu128
  if [ "$(gpu_probe)" != "ok" ]; then
    die "PyTorch steuert die 5090 (sm_120) weiterhin nicht an. Nutze ein \
CUDA-12.8-Image (VAST_IMAGE) und prüfe den GPU-Treiber (>= 570). Abbruch, \
statt still jeden Clip fehlschlagen zu lassen."
  fi
  log "cu128-Build installiert — GPU jetzt ansteuerbar."
else
  log "PyTorch steuert die GPU an — ok."
fi

# SageAttention + Triton sind unsere Attention-Backends. SageAttention baut
# CUDA-Kernel für sm_120 -> braucht nvcc aus einem CUDA-12.8-Image.
log "Installiere sageattention + triton …"
$PIP install sageattention triton

# WICHTIG: flash-attn wird NICHT installiert. Es kompiliert 20–60 Min und wird
# nicht gebraucht — SageAttention (sageattn_2) genügt vollständig.
log "flash-attn wird bewusst NICHT installiert (SageAttention genügt)."

# --- 4c. SeedVR2-Modell VORAB laden (einmal, atomar) -------------------------
# Grund: SeedVR2 lädt das Modell sonst beim ersten Clip zur Laufzeit nach
# ./models/SEEDVR2/<datei>.download und benennt dann um. Laufen mehrere GPU-
# Worker parallel, reißen sie sich die .download weg ("No such file:
# ...download -> ...") und JEDER Clip scheitert. Wir laden es hier EINMAL,
# bevor je ein Worker startet -> kein Laufzeit-Download, kein Wettlauf.
MODEL_FILE="$MODEL_DIR/$MODEL_NAME"
MIN_BYTES=3000000000   # ~3 GB; kleiner = Ruine, neu laden
mkdir -p "$MODEL_DIR"

model_ok() {  # existiert und plausibel groß?
  local sz
  [ -f "$MODEL_FILE" ] || return 1
  sz="$(stat -c%s "$MODEL_FILE" 2>/dev/null || echo 0)"
  [ "$sz" -ge "$MIN_BYTES" ]
}

if model_ok; then
  log "Modell bereits vorhanden ($MODEL_NAME, $(du -h "$MODEL_FILE" | cut -f1)) — kein Download."
else
  rm -f "$MODEL_DIR"/*.download "$MODEL_FILE" 2>/dev/null || true
  downloaded=0
  for try in 1 2 3; do
    log "Lade SeedVR2-Modell vorab (Versuch $try/3): $MODEL_NAME …"
    if curl -fL --retry 3 --retry-delay 5 -o "$MODEL_FILE.part" "$MODEL_URL"; then
      sz="$(stat -c%s "$MODEL_FILE.part" 2>/dev/null || echo 0)"
      if [ "$sz" -ge "$MIN_BYTES" ]; then
        mv -f "$MODEL_FILE.part" "$MODEL_FILE"   # atomar an die Zielstelle
        downloaded=1; break
      fi
      log "Datei zu klein ($sz B) — verwerfe und versuche erneut."
    fi
    rm -f "$MODEL_FILE.part"
    sleep 5
  done
  [ "$downloaded" -eq 1 ] || die "Modell-Download fehlgeschlagen ($MODEL_NAME). \
Abbruch, statt jeden Clip an fehlendem Modell scheitern zu lassen."
  log "Modell vorab geladen: $MODEL_FILE ($(du -h "$MODEL_FILE" | cut -f1))"
fi

# --- 5. Verarbeitungs-Script laden -------------------------------------------
log "Lade process.sh aus $REPO_RAW_URL …"
curl -fsSL "$REPO_RAW_URL/node/process.sh" -o /workspace/process.sh
chmod +x /workspace/process.sh
log "process.sh installiert nach /workspace/process.sh"

# --- 6. SSH für eingehenden rsync --------------------------------------------
# Vast injiziert für den ssh-Runtype automatisch die in deinem Account
# hinterlegten Public-Keys in ~/.ssh/authorized_keys. Wir ergänzen optional
# einen zusätzlich übergebenen Key (Fallback).
mkdir -p /root/.ssh && chmod 700 /root/.ssh
if [ -n "${SSH_PUBKEY:-}" ]; then
  if ! grep -qF "$SSH_PUBKEY" /root/.ssh/authorized_keys 2>/dev/null; then
    echo "$SSH_PUBKEY" >> /root/.ssh/authorized_keys
    log "Zusätzlichen SSH_PUBKEY in authorized_keys eingetragen."
  fi
fi
chmod 600 /root/.ssh/authorized_keys 2>/dev/null || true

# --- Fertig ------------------------------------------------------------------
log "======================================================================"
log " Node ist einsatzbereit."
log "   SeedVR2:   $SEEDVR2_DIR"
log "   Worker:    /workspace/process.sh"
log "   Input:     /workspace/input   (rsync-Ziel vom Home-Orchestrator)"
log "   Final:     /workspace/final   (rsync-Quelle für Ergebnisse)"
log "======================================================================"
