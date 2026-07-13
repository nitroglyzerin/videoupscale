#!/usr/bin/env bash
# ============================================================================
#  menu.sh — Starter für den VHS-Upscale-Orchestrator.
#
#  Stellt den Scheduler-Loop sicher (docker compose up -d) und startet dann die
#  Textual-TUI (`vhsorch tui`). Das alte Bash-Menü wurde entfernt — die TUI ist
#  die Steuerung (Nodes buchen/zerstören, Toggles, Fortschritt, Recovery).
#
#  Beim Schließen der TUI (q) endet dieses Script -> zurück in die Shell.
#
#  Start:  cd ~/videoupscale/orchestrator && ./menu.sh
#
#  Nützliche Direktbefehle ohne TUI (falls je gebraucht):
#    docker compose logs -f            # Scheduler-Log (Bootstrap/Modelle/Ticks)
#    docker compose down               # Loop stoppen (Nodes laufen weiter!)
#    docker compose run --rm orchestrator destroy all   # harter Kostenstopp
#    docker compose run --rm orchestrator status        # Queue/Node-Status
# ============================================================================
set -uo pipefail

cd "$(dirname "$(readlink -f "$0")")"

# docker compose (v2) oder docker-compose (v1) erkennen.
if docker compose version >/dev/null 2>&1; then
  DC="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  DC="docker-compose"
else
  echo "Fehler: weder 'docker compose' noch 'docker-compose' gefunden." >&2
  exit 1
fi

# Aktuellen Branch in den Build reichen: Nodes laden bootstrap.sh/process.sh
# von DIESEM Branch statt hart von main (sonst kommen lokale Änderungen, die
# nur auf einem Feature-Branch gepusht sind, nie auf den Nodes an).
REPO_BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [ -z "$REPO_BRANCH" ] || [ "$REPO_BRANCH" = "HEAD" ]; then
  REPO_BRANCH="main"
fi
export REPO_BRANCH
echo "Stelle Orchestrator-Loop sicher (docker compose up -d --build, Branch: $REPO_BRANCH) …"
$DC up -d --build

# In die TUI wechseln. 'run --rm' allokiert ein TTY (nötig für die interaktive
# TUI); der Container teilt den State (/state) mit dem Loop-Container, liest also
# denselben snapshot.json und schreibt in dieselbe Command-Queue.
exec $DC run --rm orchestrator tui
