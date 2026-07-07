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

# SeedVR2 braucht ZWEI Modelle, beide unter ./models/SEEDVR2:
#   1) DiT (fp8) — MUSS zum DIT_MODEL in process.sh passen.
#   2) VAE (fp16) — lädt SeedVR2 sonst zur LAUFZEIT selbst nach. Genau da
#      entsteht der .download-Wettlauf mehrerer Worker (No such file:
#      ...download -> ...). Deshalb laden wir BEIDE hier EINMAL vorab (Abschnitt
#      4c), damit zur Laufzeit KEIN Worker mehr irgendetwas herunterlädt.
MODEL_NAME="${MODEL_NAME:-seedvr2_ema_3b_fp8_e4m3fn.safetensors}"
MODEL_URL="${MODEL_URL:-https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/${MODEL_NAME}}"
VAE_NAME="${VAE_NAME:-ema_vae_fp16.safetensors}"
VAE_URL="${VAE_URL:-https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/${VAE_NAME}}"
# process.sh macht `cd $SEEDVR2_DIR`, daher ist ./models/SEEDVR2 genau hier:
MODEL_DIR="$SEEDVR2_DIR/models/SEEDVR2"

log() { echo -e "\033[1;36m[bootstrap]\033[0m $*"; }
die() { echo -e "\033[1;31m[bootstrap FEHLER]\033[0m $*" >&2; exit 1; }

# --- Fortschritts-Status -----------------------------------------------------
# Jede Phase schreibt EINE Zeile nach /workspace/bootstrap.status. Der
# Orchestrator liest die Datei per SSH und zeigt sie an, solange die Node noch
# "booked" ist -> statt minutenlang blindes "booked" sieht man den echten
# Schritt. Bricht der Bootstrap ab (set -e), bleibt die letzte Phase bzw. die
# FEHLER-Zeile (via ERR-Trap) stehen und verrät, WO es hakte.
mkdir -p /workspace
STATUS_FILE="/workspace/bootstrap.status"
status() { echo "$(date +%H:%M:%S) | $*" > "$STATUS_FILE"; log "STATUS: $*"; }
trap 'status "FEHLER in Zeile $LINENO — Bootstrap abgebrochen (siehe onstart-Log)"' ERR

status "0/7 Bootstrap gestartet"
log "Starte Node-Bootstrap …"

# --- 1. System-Pakete: ffmpeg + git + rsync sicherstellen --------------------
status "1/7 System-Pakete (ffmpeg, git, rsync, tmux)"
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
status "2/7 SeedVR2 klonen"
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

status "3/7 pip: requirements.txt"
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
status "4/7 GPU-Probe (ggf. PyTorch cu128 nachinstallieren — kann dauern)"
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
status "5/7 sageattention + triton"
log "Installiere sageattention + triton …"
$PIP install sageattention triton

# WICHTIG: flash-attn wird NICHT installiert. Es kompiliert 20–60 Min und wird
# nicht gebraucht — SageAttention (sageattn_2) genügt vollständig.
log "flash-attn wird bewusst NICHT installiert (SageAttention genügt)."

# --- 4c. SeedVR2-Modelle VORAB laden (einmal, atomar) ------------------------
# Grund: SeedVR2 lädt fehlende Modelle sonst beim ersten Clip zur Laufzeit nach
# ./models/SEEDVR2/<datei>.download und benennt dann um. Laufen mehrere GPU-
# Worker parallel, reißen sie sich die .download weg ("No such file:
# ...download -> ...") und JEDER Clip scheitert. Wir laden ALLE benötigten
# Modelle hier EINMAL, bevor je ein Worker startet -> kein Laufzeit-Download,
# kein Wettlauf. SeedVR2 braucht ZWEI: das DiT (fp8, ~3 GB) UND den VAE (fp16).
mkdir -p "$MODEL_DIR"

# fetch_model <name> <url> <min_bytes>
# Lädt EIN Modell atomar nach $MODEL_DIR/<name>, wenn es fehlt oder zu klein
# ist (Ruine/HTML-Fehlerseite). min_bytes = untere Plausibilitätsgrenze.
fetch_model() {
  local name="$1" url="$2" min_bytes="$3"
  local file="$MODEL_DIR/$name" sz
  if [ -f "$file" ] && [ "$(stat -c%s "$file" 2>/dev/null || echo 0)" -ge "$min_bytes" ]; then
    log "Modell bereits vorhanden ($name, $(du -h "$file" | cut -f1)) — kein Download."
    return 0
  fi
  rm -f "$MODEL_DIR/$name".download "$file".part "$file" 2>/dev/null || true
  for try in 1 2 3; do
    log "Lade Modell vorab (Versuch $try/3): $name …"
    if curl -fL --retry 3 --retry-delay 5 -o "$file.part" "$url"; then
      sz="$(stat -c%s "$file.part" 2>/dev/null || echo 0)"
      if [ "$sz" -ge "$min_bytes" ]; then
        mv -f "$file.part" "$file"   # atomar an die Zielstelle
        log "Modell vorab geladen: $file ($(du -h "$file" | cut -f1))"
        return 0
      fi
      log "Datei zu klein ($sz B, erwartet >= $min_bytes) — verwerfe und versuche erneut."
    fi
    rm -f "$file.part"
    sleep 5
  done
  die "Modell-Download fehlgeschlagen ($name). Abbruch, statt jeden Clip an \
fehlendem Modell scheitern zu lassen."
}

status "6/7 SeedVR2-Modelle (DiT ~3 GB + VAE) prüfen/laden"
fetch_model "$MODEL_NAME" "$MODEL_URL" 3000000000   # DiT: ~3 GB
fetch_model "$VAE_NAME"   "$VAE_URL"   50000000     # VAE: fp16, >= ~50 MB (HTML-Fehler < 1 MB)

# --- 5. Verarbeitungs-Script laden -------------------------------------------
status "7/7 process.sh laden"
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
status "READY — Node einsatzbereit"
log "======================================================================"
log " Node ist einsatzbereit."
log "   SeedVR2:   $SEEDVR2_DIR"
log "   Worker:    /workspace/process.sh"
log "   Input:     /workspace/input   (rsync-Ziel vom Home-Orchestrator)"
log "   Final:     /workspace/final   (rsync-Quelle für Ergebnisse)"
log "======================================================================"
