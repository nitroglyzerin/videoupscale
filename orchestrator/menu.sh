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

# Wie ORCH, aber OHNE TTY/stdin (-T). Für Hintergrund-Fetches (live_orch): sonst
# allokiert 'docker compose run' ein Pseudo-TTY und greift auf die Tastatur zu —
# es konkurriert dann mit der Menü-Schleife um stdin und verschluckt Tastendrücke,
# sodass "beliebige Taste = zurück" während eines Fetches nicht reagiert.
ORCH_BG() { $DC run --rm -T orchestrator "$@"; }

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


# Generischer Live-Refresh OHNE Flackern: die (langsame) ORCH-Abfrage läuft
# ERST in eine Variable — das alte Bild bleibt derweil stehen. Danach wird der
# Cursor nur nach oben gesetzt (\033[H) und Zeile für Zeile überschrieben
# (jede mit \033[K bis Zeilenende geräumt), am Ende Rest löschen (\033[J).
# So kein 'clear' -> kein schwarzer Blitz zwischen den Frames.
live_orch() {
  local title="$1"; shift
  local iv=5 lead=3                       # Refresh-Intervall / Vorlauf zum Nachladen
  local spin='⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏' si=0
  local tmp; tmp="$(mktemp)"
  local body="" stamp="—" line status header
  local pid="" fetching=0 ready=0 secs=0 tick=0

  # Bei Verlassen laufenden Hintergrund-Fetch mitnehmen + Temp entfernen.
  _lo_cleanup() { [ -n "$pid" ] && kill "$pid" 2>/dev/null; rm -f "$tmp"; }

  printf '\033[2J'
  # Ersten Fetch SOFORT (im Hintergrund) anstoßen -> kein schwarzer Bildschirm,
  # stattdessen gleich Kopfzeile + Spinner sichtbar.
  ORCH_BG "$@" >"$tmp" 2>/dev/null & pid=$!; fetching=1

  while true; do
    # --- Kopfzeile (jede ~0.25 s, Body bleibt stehen) ---
    if   [ "$secs" -gt 0 ];   then status="nächster Refresh in ${secs}s"
    elif [ "$ready" -eq 1 ];  then status="aktualisiere …"
    else                            status="lädt Daten …"; fi
    header="${C_TITLE}== ${title} ==${C_RST}   ${C_DIM}${spin:si:1} Stand: ${stamp} · ${status} · beliebige Taste = zurück${C_RST}"
    printf '\033[H%b\033[K' "$header"
    si=$(((si + 1) % ${#spin}))

    # --- Hintergrund-Fetch fertig? -> Ergebnis vormerken ---
    if [ "$fetching" -eq 1 ] && ! kill -0 "$pid" 2>/dev/null; then
      wait "$pid" 2>/dev/null; pid=""; fetching=0; ready=1
    fi

    # --- Frisches Ergebnis einblenden: sofort (Erststart) oder wenn Countdown 0 ---
    if [ "$ready" -eq 1 ] && [ "$secs" -le 0 ]; then
      body="$(cat "$tmp")"; stamp="$(date '+%H:%M:%S')"; ready=0; secs=$iv
      printf '\033[H%b\033[K\n\033[K\n' "$header"     # Kopf + Leerzeile
      while IFS= read -r line; do printf '%s\033[K\n' "$line"; done <<<"$body"
      printf '\033[J'                                 # Rest (altes, längeres Bild) weg
    fi

    # --- Prefetch: schon vor Ablauf nachladen, damit bei 0 Daten bereitstehen ---
    if [ "$fetching" -eq 0 ] && [ "$ready" -eq 0 ] && [ "$secs" -gt 0 ] && [ "$secs" -le "$lead" ]; then
      ORCH_BG "$@" >"$tmp" 2>/dev/null & pid=$!; fetching=1
    fi

    # --- ~0.25 s warten; Taste = zurück ---
    read -rsn1 -t 0.25 _ && { printf '\n'; _lo_cleanup; return; }

    # --- Zeitbasis: alle 4 Ticks (~1 s) Countdown herunterzählen ---
    tick=$((tick + 1))
    if [ $((tick % 4)) -eq 0 ] && [ "$secs" -gt 0 ]; then secs=$((secs - 1)); fi
  done
}

# Workmap: welche GPU/Node arbeitet gerade an welchem Video (aktiv pullend).
act_workmap() { live_orch "Workmap — GPU ▶ Video (live)" workmap; }

# Video-Tab: Liste mit Zustand + Gesamtkosten oben + Kosten pro Video (live).
act_videos()  { live_orch "Videos & Kosten (live)" videos; }

# Pull: fertige Ergebnisse JETZT einsammeln (kein Verteilen, kein Destroy).
# Rettung vor einem geplanten Destroy oder wenn der Loop nicht läuft.
act_pull() {
  clear; echo -e "${C_TITLE}== Pull — Ergebnisse einsammeln ==${C_RST}\n"
  echo "Ziehe fertige Clips von allen bereiten Nodes …"; echo
  ORCH pull; pause
}

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

# Neue Textual-TUI starten (reiner Leser des Scheduler-Snapshots, immer flüssig).
# 'docker compose run --rm' allokiert ein TTY (im Gegensatz zu ORCH_BG mit -T),
# die interaktive TUI braucht das. Vorher den Loop sicherstellen ('up -d'),
# damit sofort Live-Daten im Snapshot stehen (sonst 'Warte auf Snapshot …').
act_tui() {
  clear
  echo -e "${C_TITLE}== TUI ==${C_RST}  stelle Loop sicher …"
  $DC up -d >/dev/null 2>&1
  $DC run --rm orchestrator tui
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

# Eine Node auswählen. Setzt PICKED_EP="host:port" und gibt 0 zurück; 1 bei
# Abbruch oder wenn (noch) keine Node einen ssh-Endpoint hat. Von act_ssh UND
# act_setup_logs genutzt, damit die Auswahl-Logik nur einmal existiert.
pick_node() {
  PICKED_EP=""
  local eps
  mapfile -t eps < <(node_endpoints)
  if [ "${#eps[@]}" -eq 0 ]; then
    echo "Keine erreichbaren Nodes (ssh-Endpoint noch nicht vergeben?)."; return 1
  fi
  if [ "${#eps[@]}" -eq 1 ]; then
    PICKED_EP="${eps[0]}"; return 0
  fi
  echo "Node wählen:"; choose "${eps[@]}"; [ "$REPLY" -eq 255 ] && return 1
  PICKED_EP="${eps[$REPLY]}"; return 0
}

act_ssh() {
  clear; echo -e "${C_TITLE}== SSH auf eine Node ==${C_RST}\n"
  pick_node || { pause; return; }
  local host="${PICKED_EP%:*}" port="${PICKED_EP##*:}"
  echo -e "\nVerbinde zu ${C_OK}root@$host:$port${C_RST} …\n"
  ssh -p "$port" -i secrets/id_ed25519 -o StrictHostKeyChecking=accept-new "root@$host"
  pause
}

# First-Setup-Logs (bootstrap.sh) einer Node LIVE mitlesen.
# bootstrap.sh schreibt sein komplettes Log detached nach /workspace/bootstrap.log
# (siehe remote.start_bootstrap). Wir hängen uns per 'tail -F' dran:
#   -F  folgt der Datei auch, wenn sie NOCH NICHT existiert (Node bootet noch)
#       oder rotiert wird -> kein Fehlschlag, sondern "waiting for output".
# Strg-C beendet den tail und kehrt ins Menü zurück (wie bei den Loop-Logs).
act_setup_logs() {
  clear; echo -e "${C_TITLE}== First-Setup-Logs (bootstrap) live ==${C_RST}\n"
  pick_node || { pause; return; }
  local host="${PICKED_EP%:*}" port="${PICKED_EP##*:}"
  echo -e "Folge ${C_OK}/workspace/bootstrap.log${C_RST} auf ${C_OK}root@$host:$port${C_RST}"
  echo -e "${C_DIM}(Kurz-Status je Phase steht zusätzlich in /workspace/bootstrap.status)${C_RST}"
  echo -e "${C_DIM}── Strg-C = zurück ──${C_RST}\n"
  # Konsistent mit remote.py: keine Host-Key-Prüfung (Vast recycelt Hostnamen),
  # ServerAlive hält die lange tail-Verbindung offen. Strg-C trifft nur den tail.
  trap ' ' INT
  ssh -tt -p "$port" -i secrets/id_ed25519 \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o LogLevel=ERROR -o ServerAliveInterval=15 \
      "root@$host" \
      "tail -n +1 -F /workspace/bootstrap.log"
  trap - INT
  pause
}

# ---------------------------------------------------------------------------
#  Hauptschleife
# ---------------------------------------------------------------------------
LABELS=(
  "TUI     — NEU: flüssige Steuerung (Snapshot-Leser, Toggles, Node-Detail)"
  "Workmap — LIVE: welche GPU ▶ welches Video + Auslastung (auto-refresh)"
  "Videos  — LIVE: Liste + Gesamtkosten + Kosten/Video (auto-refresh)"
  "Plan    — Offers suchen & direkt buchen"
  "Nodes   — Vast-Status + Live-Schnappschuss"
  "Up      — Loop starten (Hintergrund)"
  "Logs    — Loop-Logs live ansehen"
  "Setup   — LIVE: First-Setup-Logs (bootstrap) einer Node mitlesen"
  "Down    — Loop stoppen"
  "Pull    — fertige Ergebnisse jetzt einsammeln (kein Destroy)"
  "Destroy — Node(s) zerstören (Kostenstopp)"
  "SSH     — auf eine Node verbinden"
  "Beenden"
)

# Direkt in die neue TUI starten (Default-Wunsch). Nach dem Schließen der TUI
# landet man im klassischen Menü (Fallback für Down/SSH/Setup-Logs/alte Views;
# TUI erneut = Menüpunkt 1). '--no-tui' überspringt den Direktstart.
if [ "${1:-}" != "--no-tui" ]; then
  act_tui
fi

while true; do
  clear
  echo -e "${C_TITLE}╔══════════════════════════════════════════════╗${C_RST}"
  echo -e "${C_TITLE}║   VHS-Upscale-Orchestrator — Steuerung        ║${C_RST}"
  echo -e "${C_TITLE}╚══════════════════════════════════════════════╝${C_RST}"
  echo -e "${C_DIM}   ↑/↓ bewegen · ENTER wählen · q beenden · (TUI = Punkt 1)${C_RST}\n"
  choose "${LABELS[@]}"
  case "$REPLY" in
    0) act_tui;;
    1) act_workmap;;
    2) act_videos;;
    3) act_plan;;
    4) act_nodes;;
    5) act_up;;
    6) act_logs;;
    7) act_setup_logs;;
    8) act_down;;
    9) act_pull;;
    10) act_destroy;;
    11) act_ssh;;
    12|255) clear; echo "Tschüss."; exit 0;;
  esac
done
