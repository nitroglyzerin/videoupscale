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
      [1-9])                                   # Zifferntaste: 1-basiert wählen
        if [ "$key" -le "$n" ]; then printf '\033[?25h'; REPLY=$((key - 1)); return; fi
        ;;
      0)                                       # 0 = 10. Eintrag
        if [ "$n" -ge 10 ]; then printf '\033[?25h'; REPLY=9; return; fi
        ;;
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

# SSH-Optionen für die Node-Schnappschüsse (kurzer Timeout, kein Prompt).
SSH_OPTS=(-i secrets/id_ed25519 -o StrictHostKeyChecking=accept-new
          -o UserKnownHostsFile=state/known_hosts -o ConnectTimeout=8
          -o BatchMode=yes)

# ssh=host:port-Endpunkte der aktuellen Nodes holen.
node_endpoints() { ORCH nodes 2>/dev/null | grep -oE 'ssh=[^ ]+' | sed 's/^ssh=//'; }

# Ein-Blick-Schnappschuss EINER Node über SSH:
#   Worker-Prozesse, fertige/gesamte Clips, GPU-Auslastung, letzte Log-Zeile.
node_snapshot() {
  local host="$1" port="$2"
  local remote='
    w=$(pgrep -c -f "[p]rocess\.sh" 2>/dev/null || echo 0)
    fin=$(ls /workspace/final 2>/dev/null | grep -ic "\.mp4$")
    inp=$(ls /workspace/input 2>/dev/null | wc -l)
    echo "WORKER=$w"
    echo "DONE=$fin/$inp"
    echo "--- GPUs (idx util mem) ---"
    nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
               --format=csv,noheader 2>/dev/null || echo "  (nvidia-smi n/a)"
    echo "--- letzte Aktivität ---"
    tail -n 2 /workspace/work/run.log 2>/dev/null | sed "s/^/  /" || true
  '
  local out
  if out="$(ssh "${SSH_OPTS[@]}" -p "$port" "root@$host" "$remote" 2>/dev/null)"; then
    local worker done_
    worker="$(echo "$out" | sed -n 's/^WORKER=//p')"
    done_="$(echo  "$out" | sed -n 's/^DONE=//p')"
    if [ "${worker:-0}" -gt 0 ] 2>/dev/null; then
      echo -e "  ${C_OK}● Worker aktiv${C_RST} ($worker Prozesse)   fertig: ${C_OK}$done_${C_RST}"
    else
      echo -e "  ${C_WARN}○ kein Worker läuft${C_RST}            fertig: $done_"
    fi
    echo "$out" | sed -n '/^--- GPUs/,$p'
  else
    echo -e "  ${C_WARN}(nicht erreichbar — bootet noch oder SSH nicht bereit)${C_RST}"
  fi
}

# Angereicherte, einmalige Nodes-Ansicht: Vast-Status + Live-Schnappschuss.
act_nodes() {
  clear; echo -e "${C_TITLE}== Nodes: Vast-Status ==${C_RST}\n"
  ORCH nodes
  echo; echo -e "${C_TITLE}== Live-Schnappschuss pro Node ==${C_RST}"
  local eps ep; mapfile -t eps < <(node_endpoints)
  if [ "${#eps[@]}" -eq 0 ]; then echo "  (noch keine SSH-Endpunkte)"; else
    for ep in "${eps[@]}"; do
      echo -e "\n${C_DIM}» ${ep}${C_RST}"; node_snapshot "${ep%:*}" "${ep##*:}"
    done
  fi
  pause
}

# Live-Monitor: aktualisiert sich automatisch, beliebige Taste = zurück.
act_monitor() {
  local iv=6
  while true; do
    clear
    echo -e "${C_TITLE}== Live-Monitor ==${C_RST}   ${C_DIM}(Refresh ${iv}s · beliebige Taste = zurück)${C_RST}\n"
    ORCH status 2>/dev/null
    echo; echo -e "${C_TITLE}-- Nodes live --${C_RST}"
    local eps ep; mapfile -t eps < <(node_endpoints)
    if [ "${#eps[@]}" -eq 0 ]; then
      echo "  (noch keine SSH-Endpunkte — Node bootet)"
    else
      for ep in "${eps[@]}"; do
        echo -e "\n${C_DIM}» ${ep}${C_RST}"; node_snapshot "${ep%:*}" "${ep##*:}"
      done
    fi
    # bis zu iv Sekunden auf eine Taste warten; Taste -> raus.
    read -rsn1 -t "$iv" _ && return
  done
}

# Generischer Live-Refresh OHNE Flackern: die (langsame) ORCH-Abfrage läuft
# ERST in eine Variable — das alte Bild bleibt derweil stehen. Danach wird der
# Cursor nur nach oben gesetzt (\033[H) und Zeile für Zeile überschrieben
# (jede mit \033[K bis Zeilenende geräumt), am Ende Rest löschen (\033[J).
# So kein 'clear' -> kein schwarzer Blitz zwischen den Frames.
live_orch() {
  local title="$1"; shift
  local iv=5 out line stamp rem
  # Spinner + Sekunden-Countdown in der Kopfzeile: die (langsame) ORCH-Abfrage
  # läuft nur alle iv s, aber die Kopfzeile wird JEDE Sekunde neu geschrieben
  # (nur Zeile 1, Body bleibt stehen) -> man sieht runterzählen + Spinner.
  local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' si=0
  printf '\033[2J'   # einmal initial leeren
  while true; do
    out="$(ORCH "$@" 2>/dev/null)"       # läuft ERST (altes Bild bleibt stehen)
    stamp="$(date '+%H:%M:%S')"
    printf '\033[H\033[K\n\033[K\n'       # zwei Kopfzeilen freihalten
    while IFS= read -r line; do
      printf '%s\033[K\n' "$line"         # ANSI aus ORCH bleibt erhalten (%s)
    done <<<"$out"
    printf '\033[J'                       # alles darunter (altes, längeres Bild) weg
    # Countdown bis zum nächsten Refresh; Kopfzeile jede Sekunde aktualisieren.
    for ((rem = iv; rem > 0; rem--)); do
      printf '\033[H%b\033[K' \
        "${C_TITLE}== ${title} ==${C_RST}   ${C_DIM}${spin:si:1} Stand: ${stamp} · nächster Refresh in ${rem}s · beliebige Taste = zurück${C_RST}"
      si=$(((si + 1) % ${#spin}))
      read -rsn1 -t 1 _ && { printf '\n'; return; }
    done
  done
}

# Workmap: welche GPU/Node arbeitet gerade an welchem Video (aktiv pullend).
act_workmap() { live_orch "Workmap — GPU ▶ Video (live)" workmap; }

# Video-Tab: Liste mit Zustand + Gesamtkosten oben + Kosten pro Video (live).
act_videos()  { live_orch "Videos & Kosten (live)" videos; }

# Offers suchen UND direkt daraus buchen (Zifferntaste -> Bestätigung -> book).
act_plan() {
  clear; echo -e "${C_TITLE}== Offers suchen & buchen ==${C_RST}\n"
  echo "Suche Offers …"; echo
  local out; out="$(ORCH plan 2>/dev/null)"
  echo "$out"

  # Datenzeilen der Tabelle: $1 = laufende Nummer, $2 = OFFER-ID (beide Ganzzahl).
  local ids=() rows=()
  mapfile -t ids  < <(echo "$out" | awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {print $2}')
  mapfile -t rows < <(echo "$out" | awk '$1 ~ /^[0-9]+$/ && $2 ~ /^[0-9]+$/ {$1=""; sub(/^[ \t]+/,""); print}')
  if [ "${#ids[@]}" -eq 0 ]; then
    echo -e "\n${C_WARN}Keine buchbaren Offers gefunden.${C_RST}"; pause; return
  fi

  echo -e "\n${C_TITLE}Offer wählen — Zifferntaste (1/2/3…) oder ↑/↓, ENTER. q = nur ansehen.${C_RST}\n"
  local labels=() i
  for i in "${!ids[@]}"; do labels+=("$((i + 1)))  [ID ${ids[$i]}]  ${rows[$i]}"); done
  choose "${labels[@]}"
  [ "$REPLY" -eq 255 ] && return

  local oid="${ids[$REPLY]}"
  echo; read -rp "Offer $oid buchen? [j/N]: " ok
  [[ "$ok" =~ ^[jJyY]$ ]] || { echo "Abgebrochen."; pause; return; }
  echo; ORCH book "$oid"; pause
}

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
  "Workmap — LIVE: welche GPU ▶ welches Video (auto-refresh)"
  "Videos  — LIVE: Liste + Gesamtkosten + Kosten/Video (auto-refresh)"
  "Status  — Queue & Nodes (Momentaufnahme)"
  "Monitor — LIVE: Nodes, GPUs, Fortschritt (auto-refresh)"
  "Plan    — Offers suchen & direkt buchen"
  "Book    — Node per ID buchen (manuell)"
  "Nodes   — Vast-Status + Live-Schnappschuss"
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
    0) act_workmap;;
    1) act_videos;;
    2) act_status;;
    3) act_monitor;;
    4) act_plan;;
    5) act_book;;
    6) act_nodes;;
    7) act_up;;
    8) act_logs;;
    9) act_down;;
    10) act_destroy;;
    11) act_ssh;;
    12|255) clear; echo "Tschüss."; exit 0;;
  esac
done
