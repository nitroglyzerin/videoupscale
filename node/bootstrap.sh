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
for tool in git rsync curl; do
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

# SageAttention + Triton sind unsere Attention-Backends.
log "Installiere sageattention + triton …"
$PIP install sageattention triton

# WICHTIG: flash-attn wird NICHT installiert. Es kompiliert 20–60 Min und wird
# nicht gebraucht — SageAttention (sageattn_2) genügt vollständig.
log "flash-attn wird bewusst NICHT installiert (SageAttention genügt)."

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
