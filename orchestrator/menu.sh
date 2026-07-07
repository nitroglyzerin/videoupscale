#!/usr/bin/env bash
# ============================================================================
#  menu.sh — interaktives Terminal-Menü für den VHS-Upscale-Orchestrator
#
#  Navigation:  ↑/↓ (oder k/j) bewegen, ENTER wählt, q beendet.
#  Reines Bash — keine Zusatzpakete (kein whiptail/dialog nötig).
#
#  Start:  cd ~/videoupscale/orchestrator && ./menu.sh
# ============================================================================
set -uo pipefail

# Immer aus dem Verzeichnis dieses Scripts arbeiten (dort liegt docker-compose).
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

ORCH() { $DC run --rm orchestrator "$@"; }

C_TITLE="\033[1;36m"; C_SEL="\033[7m"; C_DIM="\033[2m"; C_OK="\033[1;32m"
C_WARN="\033[1;33m"; C_RST="\033[0m"

pause() { echo; read -rsn1 -p "── ENTER/Taste für zurück ──"; echo; }

# ---------------------------------------------------------------------------
#  Pfeiltasten-Menü. Argumente = Einträge. Rückgabe: gewählter Index in $REPLY.
#  Rückgabe 255 = Abbruch (q).
# ---------------------------------------------------------------------------
choose() {
  local options=("$@") selected=0 key rest n=${#options[@]} i
  printf '\033[?25l'  # Cursor aus
  while true; do
    for i in "${!options[@]}"; do
      if [ "$i" -eq "$selected" ]; then
        printf "  ${C_SEL} ▸ %s ${C_RST}\033[K\n" "${options[$i]}"
      else
        printf "     %s\033[K\n" "${options[$i]}"
      fi
    done
    IFS= read -rsn1 key
    case "$key" in
      $'\x1b')
        read -rsn2 -t 0.05 rest || rest=""
        case "$rest" in
          '[A') ((selected--));;   # Pfeil hoch
          '[B') ((selected++));;   # Pfeil runter
        esac
        ;;
      k|K) ((selected--));;
      j|J) ((selected++));;
      q|Q) printf '\033[?25h'; REPLY=255; return;;
      '')  printf '\033[?25h'; REPLY=$selected; return;;   # ENTER
    esac
    ((selected < 0)) && selected=$((n - 1))
    ((selected >= n)) && selected=0
    printf "\033[%dA" "$n"   # Cursor zum Menüanfang zurück
  done
}

# ---------------------------------------------------------------------------
#  Aktionen
# ---------------------------------------------------------------------------
act_status()  { clear; echo -e "${C_TITLE}== Queue & Nodes ==${C_RST}\n"; ORCH status; pause; }
act_plan()    { clear; echo -e "${C_TITLE}== Offers suchen (bucht nichts) ==${C_RST}\n"; ORCH plan; pause; }
act_nodes()   { clear; echo -e "${C_TITLE}== Laufende Vast-Instanzen ==${C_RST}\n"; ORCH nodes; pause; }

act_book() {
  clear; echo -e "${C_TITLE}== Node buchen ==${C_RST}\n"
  echo "Zuerst 'plan' ausführen, um eine OFFER-ID zu bekommen."; echo
  read -rp "OFFER-ID (leer = abbrechen): " oid
  [ -z "$oid" ] && return
  echo; ORCH book "$oid"; pause
}

act_up()   { clear; echo -e "${C_TITLE}== Loop starten (Hintergrund) ==${C_RST}\n"; $DC up -d && echo -e "\n${C_OK}Loop läuft.${C_RST} Logs im Menüpunkt 'Loop-Logs'."; pause; }
act_logs() { clear; echo -e "${C_TITLE}== Loop-Logs (Strg-C = zurück) ==${C_RST}\n"; trap ' ' INT; $DC logs -f --tail=50; trap - INT; pause; }
act_down() { clear; echo -e "${C_WARN}== Loop stoppen ==${C_RST}\n"; $DC down && echo -e "\n${C_OK}Loop gestoppt.${C_RST} (Vast-Node läuft weiter — separat 'destroy'!)"; pause; }

act_destroy() {
  clear; echo -e "${C_WARN}== Node(s) zerstören (Kostenstopp) ==${C_RST}\n"
  ORCH nodes; echo
  echo "Instanz-ID eingeben, oder 'all' für alle."
  read -rp "Ziel (leer = abbrechen): " tgt
  [ -z "$tgt" ] && return
  read -rp "Wirklich '$tgt' zerstören? [j/N]: " ok
  [[ "$ok" =~ ^[jJyY]$ ]] || { echo "Abgebrochen."; pause; return; }
  echo; ORCH destroy "$tgt"; pause
}

act_ssh() {
  clear; echo -e "${C_TITLE}== SSH auf eine Node ==${C_RST}\n"
  # ssh=host:port aus der nodes-Ausgabe ziehen.
  mapfile -t eps < <(ORCH nodes 2>/dev/null | grep -oE 'ssh=[^ ]+' | sed 's/^ssh=//')
  if [ "${#eps[@]}" -eq 0 ]; then
    echo "Keine erreichbaren Nodes (ssh-Endpoint noch nicht vergeben?)."; pause; return
  fi
  local ep
  if [ "${#eps[@]}" -eq 1 ]; then
    ep="${eps[0]}"
  else
    echo "Node wählen:"; choose "${eps[@]}"; [ "$REPLY" -eq 255 ] && return; ep="${eps[$REPLY]}"
  fi
  local host="${ep%:*}" port="${ep##*:}"
  echo -e "\nVerbinde zu ${C_OK}root@$host:$port${C_RST} …\n"
  ssh -p "$port" -i secrets/id_ed25519 -o StrictHostKeyChecking=accept-new "root@$host"
  pause
}

# ---------------------------------------------------------------------------
#  Hauptschleife
# ---------------------------------------------------------------------------
LABELS=(
  "Status  — Queue & Nodes anzeigen"
  "Plan    — Offers suchen (bucht nichts)"
  "Book    — Node buchen"
  "Nodes   — laufende Vast-Instanzen"
  "Up      — Loop starten (Hintergrund)"
  "Logs    — Loop-Logs live ansehen"
  "Down    — Loop stoppen"
  "Destroy — Node(s) zerstören (Kostenstopp)"
  "SSH     — auf eine Node verbinden"
  "Beenden"
)

while true; do
  clear
  echo -e "${C_TITLE}╔══════════════════════════════════════════════╗${C_RST}"
  echo -e "${C_TITLE}║   VHS-Upscale-Orchestrator — Steuerung        ║${C_RST}"
  echo -e "${C_TITLE}╚══════════════════════════════════════════════╝${C_RST}"
  echo -e "${C_DIM}   ↑/↓ bewegen · ENTER wählen · q beenden${C_RST}\n"
  choose "${LABELS[@]}"
  case "$REPLY" in
    0) act_status;;
    1) act_plan;;
    2) act_book;;
    3) act_nodes;;
    4) act_up;;
    5) act_logs;;
    6) act_down;;
    7) act_destroy;;
    8) act_ssh;;
    9|255) clear; echo "Tschüss."; exit 0;;
  esac
done
