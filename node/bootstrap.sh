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

# SeedVR2 braucht ZWEI Modelle (DiT fp8 + VAE fp16), beide unter ./models/SEEDVR2.
# Sie werden NICHT mehr auf der Node geladen (Node-Egress unzuverlässig), sondern
# vom Orchestrator per rsync gepusht (siehe Abschnitt 4c). Wir legen hier nur das
# Verzeichnis an. process.sh macht `cd $SEEDVR2_DIR`, daher ist ./models/SEEDVR2 hier:
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

# Einmal-Lock: Bootstrap kann von ZWEI Seiten angestoßen werden — von Vasts
# onstart UND vom Orchestrator per SSH (start_bootstrap), sobald sshd oben ist.
# Ohne Lock liefen dann u. U. zwei Bootstraps parallel -> apt/pip/Modell-
# Wettläufe. flock stellt sicher: nur der erste läuft, der zweite endet sofort.
exec 9>/workspace/.bootstrap.lock
if ! flock -n 9; then
  log "Bootstrap läuft bereits (lock gehalten) — dieser Aufruf beendet sich."
  exit 0
fi

# Bei Abbruch: Fehler mit Zeilennummer festhalten UND den Start-Marker löschen,
# damit der Orchestrator (start_bootstrap) den Bootstrap im nächsten Takt neu
# anstößt -> selbstheilend statt dauerhaft "booked".
trap 'rm -f /workspace/.bootstrap.launched; status "FEHLER in Zeile $LINENO — Bootstrap abgebrochen (siehe onstart-Log)"' ERR

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
  # Reste eines abgebrochenen Klons entfernen, sonst scheitert git clone an
  # "destination path already exists and is not an empty directory".
  rm -rf "$SEEDVR2_DIR"
  cloned=0
  for try in 1 2 3; do
    if git clone --depth 1 "$SEEDVR2_REPO" "$SEEDVR2_DIR"; then cloned=1; break; fi
    log "git clone Versuch $try/3 fehlgeschlagen — verwerfe und versuche erneut …"
    rm -rf "$SEEDVR2_DIR"; sleep 5
  done
  [ "$cloned" -eq 1 ] || die "SeedVR2-Clone nach 3 Versuchen fehlgeschlagen ($SEEDVR2_REPO)."
else
  log "SeedVR2 bereits vorhanden — git pull …"
  git -C "$SEEDVR2_DIR" pull --ff-only || true
fi

# --- 4. Python-Abhängigkeiten ------------------------------------------------
cd "$SEEDVR2_DIR"
# Manche Images (z. B. vastai/pytorch) nutzen ein Debian-System-Python mit
# PEP-668-Schutz ("externally-managed-environment"). Ohne das folgende Flag
# würde JEDER pip-Install dort abbrechen. Harmlos auf conda-/venv-Images.
export PIP_BREAK_SYSTEM_PACKAGES=1
export PIP_ROOT_USER_ACTION=ignore
PIP="python3 -m pip"
# pip-Upgrade ist optional und scheitert auf Debian-pip ("Cannot uninstall
# pip 24.0, RECORD file not found") -> NICHT fatal machen; altes pip genügt.
$PIP install --upgrade pip || log "pip-Upgrade übersprungen (Debian-verwaltetes pip) — unkritisch."

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

# --- 4c. SeedVR2-Modelle: NICHT von der Node laden ---------------------------
# Der Node-Egress ist auf Vast unzuverlässig (IPv6-only-HF, TLS-Reset, 429) —
# ein HF-Download hier lässt den Bootstrap regelmäßig hängen/scheitern. Deshalb
# liefert der ORCHESTRATOR die Modelle per rsync-über-SSH (einmal pro Node,
# stabiler Kanal). Wir legen hier nur das Zielverzeichnis an; die Dateien landen
# durch den Push in $MODEL_DIR, BEVOR der Worker gestartet wird.
status "6/7 Modell-Verzeichnis vorbereiten (Modelle kommen per rsync vom Orchestrator)"
mkdir -p "$MODEL_DIR"
log "Kein HF-Download auf der Node — Modelle werden vom Orchestrator gepusht."

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
