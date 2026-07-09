"""rsync-über-SSH: Rohvideos zur Node pushen, Ergebnisse pullen, Worker starten.

Nur AUSGEHENDE Verbindungen (Home -> Vast). Keine offenen Ports zuhause.
rsync ist delta-basiert und resumierbar (--partial), robust bei Abbrüchen.
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional

# pgrep-Muster mit Bracket-Trick: matcht den echten Worker über seinen VOLLEN
# Pfad '/workspace/process.sh', aber NICHT die pgrep-Kommandozeile selbst
# (die '[/]workspace/...' enthält -> kein '/workspace/...'-Substring). Der volle
# Pfad ist wichtig: das frühere '[p]rocess\.sh' matchte auch fremde Zeilen wie
# 'curl .../node/process.sh' (Bootstrap) -> Fehlalarm "Worker läuft" -> der
# Worker wurde nie (neu) gestartet, obwohl er gar nicht lief.
_PGREP_WORKER = r"pgrep -f '[/]workspace/process\.sh'"


def _ssh_base(key_path: str, port: int, connect_timeout: int = 20,
              control_dir: Optional[str] = None) -> list[str]:
    base = [
        "ssh", "-p", str(port),
        "-i", key_path,
        # KEINE Host-Key-Prüfung: Vast recycelt Hostnamen (ssh9.vast.ai etc.)
        # über kurzlebige Nodes hinweg. Ein gepinnter Key in known_hosts fuehrt
        # dann zu "Host key verification failed" -> reachable()=False -> die Node
        # wird nie ready, kein Bootstrap, kein Worker ("immer noch nichts"). Die
        # Nodes sind ohnehin ephemer und per privatem SSH-Key authentifiziert;
        # Host-Key-Pinning bringt hier keine Sicherheit, nur Ausfaelle. /dev/null
        # -> nie schreiben, nie kollidieren; LogLevel=ERROR unterdrueckt die
        # "Permanently added ..."-Warnung.
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        # BatchMode: nie interaktiv nach Passwort fragen -> hängt nie am Prompt,
        # scheitert stattdessen sofort (wichtig für die Live-Anzeige).
        "-o", "BatchMode=yes",
    ]
    # ControlMaster/ControlPersist: die ERSTE Verbindung öffnet einen Master-
    # Socket, jede weitere (Probe, Nudge, rsync) multiplext darüber -> spart den
    # teuren TLS/Handshake pro Aufruf (~0.5-2 s) und macht die häufigen Probes
    # quasi-instant. control_dir muss existieren + schreibbar sein (state-Volume).
    if control_dir:
        base += [
            "-o", "ControlMaster=auto",
            "-o", "ControlPersist=60s",
            "-o", f"ControlPath={control_dir}/cm-%h-%p",
        ]
    return base


class Remote:
    """Eine SSH-erreichbare Vast-Node."""

    def __init__(self, host: str, port: int, key_path: str, user: str = "root",
                 control_dir: Optional[str] = None):
        self.host = host
        self.port = port
        self.key_path = key_path
        self.user = user
        # Verzeichnis für den ControlPersist-Master-Socket (None -> kein Muxing).
        self.control_dir = control_dir

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def _rsync_e(self) -> str:
        base = _ssh_base(self.key_path, self.port, control_dir=self.control_dir)
        return " ".join(base)

    def reachable(self) -> bool:
        cmd = _ssh_base(self.key_path, self.port,
                        control_dir=self.control_dir) + [self.target, "true"]
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0

    def exec(self, command: str, timeout: Optional[int] = None,
             connect_timeout: int = 20) -> subprocess.CompletedProcess:
        cmd = _ssh_base(self.key_path, self.port, connect_timeout,
                        self.control_dir) + [self.target, command]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Harte Obergrenze überschritten (Node hängt): als sauberer
            # Fehlschlag zurückgeben, statt die Live-Anzeige zu blockieren.
            return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="")

    def push_files(self, local_paths: list[str], remote_dir: str = "/workspace/input/",
                   timeout: int = 900) -> bool:
        """Pusht eine Liste konkreter Dateien in remote_dir (delta, resumierbar).

        Mit hartem timeout + rsync --timeout: ein hängender Transfer darf den
        (single-threaded, blockierenden) Scheduler-Tick NICHT einfrieren.
        """
        if not local_paths:
            return True
        cmd = [
            "rsync", "-az", "--partial", "--timeout=120", "--info=progress2",
            "-e", self._rsync_e(),
            *local_paths,
            f"{self.target}:{remote_dir}",
        ]
        try:
            return subprocess.run(cmd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def pull_results(self, local_dir: str, remote_dir: str = "/workspace/final/",
                     timeout: int = 900) -> bool:
        """Zieht fertige Clips von der Node (delta, überschreibt nur Neues)."""
        cmd = [
            "rsync", "-az", "--partial", "--ignore-existing", "--timeout=120",
            "-e", self._rsync_e(),
            f"{self.target}:{remote_dir}",
            local_dir if local_dir.endswith("/") else local_dir + "/",
        ]
        try:
            return subprocess.run(cmd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def download_models(self, specs: list[tuple[str, str, int]],
                        remote_dir: str = "/workspace/seedvr2/models/SEEDVR2/",
                        per_file_max_time: int = 600,
                        timeout: Optional[int] = None,
                        connect_timeout: int = 25) -> bool:
        """Lässt die NODE die Modelle selbst von HuggingFace laden (schneller Pfad).

        specs = [(dateiname, url, erwartete_bytes)]. Idempotent: bereits vollständig
        vorhandene Dateien (Größe >= erwartet) werden übersprungen. Lädt atomar über
        .part -> mv, verifiziert danach die Größe. Rückgabe True, wenn ALLE Dateien
        vollständig auf der Node liegen.

        --ipv4: umgeht die häufige Vast-Falle 'HF löst nur IPv6 auf + IPv6 tot ->
        Connect-Timeout'. Scheitert der Download (Netz/HF), meldet die Node ok=0 und
        der Aufrufer fällt auf den orchestrator-seitigen rsync-Push zurück.
        """
        rdir = remote_dir.rstrip("/")
        parts = [f"mkdir -p '{rdir}'", "ok=1"]
        for name, url, size in specs:
            dst = f"{rdir}/{name}"
            parts.append(
                f'cur=$(stat -c%s "{dst}" 2>/dev/null || echo 0)\n'
                f'if [ "$cur" -lt {size} ]; then\n'
                f'  echo "hole {name} …"\n'
                f'  curl -fL --ipv4 --connect-timeout {connect_timeout} '
                f'--max-time {per_file_max_time} --retry 2 --retry-delay 3 '
                f'"{url}" -o "{dst}.part" && mv "{dst}.part" "{dst}" || ok=0\n'
                f'  got=$(stat -c%s "{dst}" 2>/dev/null || echo 0)\n'
                f'  [ "$got" -ge {size} ] || ok=0\n'
                f"fi")
        parts.append('echo "DOWNLOAD_OK=$ok"')
        script = "\n".join(parts)
        if timeout is None:
            timeout = per_file_max_time * max(1, len(specs)) + 120
        res = self.exec(script, timeout=timeout, connect_timeout=connect_timeout)
        return "DOWNLOAD_OK=1" in res.stdout

    def push_models(self, local_dir: str,
                    remote_dir: str = "/workspace/seedvr2/models/SEEDVR2/",
                    timeout: int = 2400) -> bool:
        """rsync die vorab (zuhause) geladenen SeedVR2-Modelle auf die Node.

        Einmal pro Node (Scheduler-Flag models_pushed). Idempotent über rsync's
        Default (überspringt Dateien mit gleicher Größe+mtime dank -a).

        WICHTIG: KEIN --ignore-existing! In Kombination mit --partial würde eine
        abgebrochene Teilübertragung (partielle Zieldatei) beim nächsten Versuch
        als „existiert" ÜBERSPRUNGEN — die Node bekäme ein UNVOLLSTÄNDIGES Modell,
        rsync meldete aber Erfolg (models_pushed=1) und die Inferenz scheiterte.
        Stattdessen --partial-dir: Teildaten liegen in einem Unterordner, die
        FINALE Datei erscheint erst, wenn sie komplett ist (kein korrupter Rest).
        KEIN -z: safetensors sind kaum komprimierbar -> spart CPU.
        """
        src = local_dir if local_dir.endswith("/") else local_dir + "/"
        # Zielverzeichnis sicherstellen (rsync legt nur die letzte Komponente an).
        self.exec(f"mkdir -p {remote_dir}", timeout=30)
        cmd = [
            # --size-only: nur nach Größe entscheiden (nicht mtime). So werden
            # Dateien, die die Node bereits selbst geladen hat (andere mtime, aber
            # korrekte Größe), NICHT unnötig neu übertragen — der rsync ergänzt nur,
            # was der Node-Download nicht geschafft hat. Für unveränderliche Modelle
            # sicher (gleicher Name+Größe = gleiche Datei).
            "rsync", "-a", "--size-only", "--partial", "--partial-dir=.rsync-partial",
            "--timeout=120", "--info=progress2",
            "-e", self._rsync_e(),
            src,
            f"{self.target}:{remote_dir}",
        ]
        try:
            return subprocess.run(cmd, timeout=timeout).returncode == 0
        except subprocess.TimeoutExpired:
            return False

    def list_remote_final(self, remote_dir: str = "/workspace/final") -> list[str]:
        """Listet die .mp4-Dateinamen im final-Ordner der Node."""
        res = self.exec(
            f"ls -1 {remote_dir} 2>/dev/null | grep -i '\\.mp4$' || true",
            timeout=15, connect_timeout=8,
        )
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def start_bootstrap(self, repo_raw_url: str) -> bool:
        """Stößt bootstrap.sh detached auf der Node an — sobald sshd oben ist.

        Wir warten NICHT auf Vasts onstart (feuert oft mit Verzögerung), sondern
        laden bootstrap.sh selbst und starten es via setsid im Hintergrund.
        Idempotent und kollisionssicher:
          * bereits fertig (process.sh da)         -> exit 0, nichts tun,
          * schon angestoßen (.bootstrap.launched)  -> exit 0 (kein Re-curl, das
            wuerde das laufende Script auf der Platte ueberschreiben),
          * Marker wird ERST nach erfolgreichem curl gesetzt -> ein fehl-
            geschlagener curl wird im naechsten Takt erneut versucht.
        Ein zusaetzliches flock IN bootstrap.sh verhindert Ueberlappung mit
        einem parallel doch noch feuernden Vast-onstart.

        Rueckgabe: True, wenn das Kommando abgesetzt werden konnte (SSH ok).
        """
        cmd = (
            "bash -lc '"
            "test -x /workspace/process.sh && exit 0; "
            "test -e /workspace/.bootstrap.launched && exit 0; "
            "mkdir -p /workspace; "
            f'export REPO_RAW_URL=\"{repo_raw_url}\"; '
            'curl -fL --connect-timeout 15 --max-time 90 --retry 5 --retry-delay 3 '
            '\"$REPO_RAW_URL/node/bootstrap.sh?ts=$(date +%s)\" -o /workspace/bootstrap.sh && '
            "{ touch /workspace/.bootstrap.launched; "
            "setsid bash /workspace/bootstrap.sh >>/workspace/bootstrap.log 2>&1 </dev/null & }"
            "'"
        )
        return self.exec(cmd, timeout=40).returncode == 0

    def bootstrap_status(self, path: str = "/workspace/bootstrap.status") -> str:
        """Letzte Bootstrap-Statuszeile der Node (leer, wenn noch nichts da).

        bootstrap.sh schreibt bei jeder Phase eine Zeile dorthin. Solange die
        Node "booked" ist (process.sh noch nicht da), zeigt der Orchestrator
        diesen Text an -> sichtbarer Fortschritt statt blindes "booked".
        """
        res = self.exec(f"cat {path} 2>/dev/null || true", timeout=12, connect_timeout=8)
        return res.stdout.strip() if res.returncode == 0 else ""

    def gpu_activity(self) -> list[tuple[int, str, str, str, str]]:
        """Was gerade auf jeder GPU läuft — aus den Worker-Logs (Log-Marker).

        process.sh loggt pro GPU nach /workspace/work/logs/gpu<idx>.log in
        Reihenfolge:
          START: <clip>  ->  PHASE Denoise/Upscale/Audio: <clip>  ->  FERTIG: <clip>

        Ein awk-Durchlauf hält den ZULETZT gesehenen Zustand fest (monoton):
          * busy=1 ab START, wieder 0 bei FERTIG/FAIL/SKIP,
          * die zuletzt geloggte PHASE-Marke ist die aktuelle Phase.
        Das ist bewusst NICHT an der Existenz der Zwischendateien festgemacht:
        ffmpeg/SeedVR2 legen ihre Output-Datei schon beim START der Phase an,
        wodurch die Datei-Heuristik jede Phase zu früh (und bei Fehler-Retries
        sogar rückwärts) anzeigte.

        PROZENT = zuletzt im Log gesehener tqdm-Wert (v. a. beim Upscale).

        Rückgabe: Liste (gpu_index, state, clip, phase, percent), nach Index.
        """
        snippet = r'''
        shopt -s nullglob
        for lf in /workspace/work/logs/gpu*.log; do
          g=$(basename "$lf" .log); g=${g#gpu}
          info=$(awk '
            /START: /  { busy=1; c=$0; sub(/.*START: /,"",c); clip=c; ph="" }
            /PHASE /   { p=$0; sub(/.*PHASE /,"",p); sub(/:.*/,"",p); ph=p }
            /FERTIG:/  { busy=0 }
            /FAIL:/    { busy=0 }
            /SKIP/     { busy=0 }
            END { printf "%d|%s|%s", busy+0, ph, clip }
          ' "$lf")
          IFS="|" read -r busy ph clip <<<"$info"
          if [ "$busy" = "1" ] && [ -n "$clip" ]; then
            pct=$(tail -n 80 "$lf" | grep -oaE "[0-9]+%" | tail -1)
            echo "$g|busy|$ph|$pct|$clip"
          else
            echo "$g|idle|||"
          fi
        done
        '''
        res = self.exec(snippet, timeout=15, connect_timeout=8)
        if res.returncode != 0:
            return []
        out: list[tuple[int, str, str, str, str]] = []
        for line in res.stdout.splitlines():
            parts = line.split("|", 4)
            if len(parts) != 5 or not parts[0].isdigit():
                continue
            gpu, state, phase, pct, clip = parts
            out.append((int(gpu), state, clip.strip(), phase.strip(), pct.strip()))
        return sorted(out, key=lambda t: t[0])

    def gpu_stats(self) -> dict[int, tuple[int, int, int]]:
        """Pro GPU: (Auslastung %, VRAM belegt MiB, VRAM gesamt MiB) via nvidia-smi.

        Leeres Dict, wenn nvidia-smi fehlschlägt (Anzeige bleibt robust).
        """
        res = self.exec(
            "nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total "
            "--format=csv,noheader,nounits",
            timeout=15, connect_timeout=8,
        )
        if res.returncode != 0:
            return {}
        out: dict[int, tuple[int, int, int]] = {}
        for line in res.stdout.splitlines():
            parts = [p.strip() for p in line.split(",")]
            if len(parts) != 4 or not parts[0].isdigit():
                continue
            try:
                out[int(parts[0])] = (int(parts[1]), int(parts[2]), int(parts[3]))
            except ValueError:
                continue
        return out

    # Ein einziges Remote-Snippet, das ALLES für den Snapshot in EINEM SSH-Aufruf
    # liefert (statt 4-5 sequentieller Calls je 8-20 s ConnectTimeout). Abschnitte
    # sind mit @MARKERN getrennt und werden in probe() geparst.
    _PROBE_SNIPPET = r'''
      if [ -x /workspace/process.sh ]; then echo "PROC=1"; else echo "PROC=0"; fi
      if pgrep -f '[/]workspace/process\.sh' >/dev/null 2>&1; then echo "WORKER=1"; else echo "WORKER=0"; fi
      echo "BOOTSTRAP=$(cat /workspace/bootstrap.status 2>/dev/null | tail -n1)"
      echo "@GPUACT"
      shopt -s nullglob
      for lf in /workspace/work/logs/gpu*.log; do
        g=$(basename "$lf" .log); g=${g#gpu}
        info=$(awk '
          /START: /  { busy=1; c=$0; sub(/.*START: /,"",c); clip=c; ph=""; samp=""; dec="" }
          /PHASE /   { p=$0; sub(/.*PHASE /,"",p); sub(/:.*/,"",p); ph=p }
          /Upscaling batch [0-9]+\/[0-9]+/ { s=$0; sub(/.*Upscaling batch /,"",s); sub(/[^0-9\/].*/,"",s); samp=s }
          /Decoding batch [0-9]+\/[0-9]+/  { d=$0; sub(/.*Decoding batch /,"",d);  sub(/[^0-9\/].*/,"",d);  dec=d }
          /FERTIG:/  { busy=0 }
          /FAIL:/    { busy=0 }
          /SKIP/     { busy=0 }
          END { printf "%d|%s|%s|%s|%s", busy+0, ph, samp, dec, clip }
        ' "$lf")
        IFS="|" read -r busy ph samp dec clip <<<"$info"
        if [ "$busy" = "1" ] && [ -n "$clip" ]; then
          pct=$(tail -n 80 "$lf" | grep -oaE "[0-9]+%" | tail -1)
          echo "$g|busy|$ph|$pct|$samp|$dec|$clip"
        else
          echo "$g|idle|||||"
        fi
      done
      echo "@GPUSTATS"
      nvidia-smi --query-gpu=index,utilization.gpu,memory.used,memory.total \
                 --format=csv,noheader,nounits 2>/dev/null || true
      echo "@FINAL"
      ls -1 /workspace/final 2>/dev/null | grep -i '\.mp4$' || true
      echo "@FAILS"
      awk '
        /START: /  { c=$0; sub(/.*START: /,"",c);  last[c]="start" }
        /FERTIG: / { c=$0; sub(/.*FERTIG: /,"",c); last[c]="ok" }
        /SKIP /    { c=$0; sub(/.*SKIP[^:]*: /,"",c); last[c]="ok" }
        /FAIL: /   { c=$0; sub(/.*FAIL: /,"",c);   last[c]="fail" }
        END { for (k in last) if (last[k]=="fail") print k }
      ' /workspace/work/logs/gpu*.log 2>/dev/null | sort -u || true
      echo "@TAIL"
      { echo "── run.log ──"; tail -n 12 /workspace/work/run.log 2>/dev/null;
        for lf in /workspace/work/logs/gpu*.log; do
          g=$(basename "$lf" .log)
          echo "── $g ──"
          # \r (tqdm-Fortschritt) zu \n machen, Leerzeilen weg, letzte 10 Zeilen.
          tail -c 2000 "$lf" 2>/dev/null | tr "\r" "\n" | grep -v "^[[:space:]]*$" | tail -n 10
        done; } 2>/dev/null || true
      echo "@MODELS"
      du -sb /workspace/seedvr2/models/SEEDVR2 2>/dev/null | cut -f1 || true
      echo "@END"
    '''

    def probe(self, timeout: int = 20, connect_timeout: int = 10) -> dict:
        """Ein-Aufruf-Schnappschuss der Node für den Scheduler-Snapshot.

        Rückgabe (JSON-freundlich):
          reachable, process_present (=ready), worker_running, bootstrap_status,
          gpus_activity: [(idx, state, clip, phase, pct)],
          gpu_stats: {idx: (util, used_mib, total_mib)},
          final: [<name>.mp4, …]  (auf Node fertig),
          fails: [<clip-basisname>, …]  (mind. einmal FAIL geloggt).
        Bei SSH-Fehler/Timeout: reachable=False, Rest leer — nie Exception.
        """
        empty = {
            "reachable": False, "process_present": False, "worker_running": False,
            "bootstrap_status": "", "gpus_activity": [], "gpu_stats": {},
            "final": [], "fails": [], "log_tail": [], "models_bytes": 0,
        }
        res = self.exec(self._PROBE_SNIPPET, timeout=timeout, connect_timeout=connect_timeout)
        if res.returncode != 0:
            return empty

        out = dict(empty)
        out["reachable"] = True
        section = "head"
        for raw in res.stdout.splitlines():
            line = raw.rstrip("\n")
            if line in ("@GPUACT", "@GPUSTATS", "@FINAL", "@FAILS", "@TAIL",
                        "@MODELS", "@END"):
                section = line
                continue
            if section == "head":
                if line.startswith("PROC="):
                    out["process_present"] = line[5:].strip() == "1"
                elif line.startswith("WORKER="):
                    out["worker_running"] = line[7:].strip() == "1"
                elif line.startswith("BOOTSTRAP="):
                    out["bootstrap_status"] = line[10:].strip()
            elif section == "@GPUACT":
                parts = line.split("|", 6)
                if len(parts) == 7 and parts[0].isdigit():
                    gpu, state, phase, pct, samp, dec, clip = parts
                    out["gpus_activity"].append(
                        (int(gpu), state, clip.strip(), phase.strip(),
                         pct.strip(), samp.strip(), dec.strip()))
            elif section == "@GPUSTATS":
                parts = [p.strip() for p in line.split(",")]
                if len(parts) == 4 and parts[0].isdigit():
                    try:
                        out["gpu_stats"][int(parts[0])] = (
                            int(parts[1]), int(parts[2]), int(parts[3]))
                    except ValueError:
                        pass
            elif section == "@FINAL":
                if line.strip():
                    out["final"].append(line.strip())
            elif section == "@FAILS":
                if line.strip():
                    out["fails"].append(line.strip())
            elif section == "@TAIL":
                # Roh übernehmen (inkl. Kopfzeilen ── gpuN ──), max. ~80 Zeilen.
                if len(out["log_tail"]) < 80:
                    out["log_tail"].append(line)
            elif section == "@MODELS":
                s = line.strip()
                if s.isdigit():
                    out["models_bytes"] = int(s)
        out["gpus_activity"].sort(key=lambda t: t[0])
        return out

    def worker_running(self) -> bool:
        res = self.exec(f"{_PGREP_WORKER} >/dev/null 2>&1 && echo yes || echo no",
                        timeout=30)
        return res.stdout.strip() == "yes"

    def release_claim(self, clip_name: str,
                      claims_dir: str = "/workspace/work/claims") -> None:
        """Gibt den Claim-Lock eines Clips frei (best effort), damit ihn ein Worker
        NEU greifen kann. Nötig beim Retry: process.sh entfernt einen Lock sonst
        erst beim Neustart -> ein einmal fehlgeschlagener Clip würde auf derselben
        Node nie wieder angefasst. Der Lock-Name ist '<voller Dateiname>.lock'."""
        safe = clip_name.replace("'", "'\\''")
        self.exec(f"rmdir '{claims_dir}/{safe}.lock' 2>/dev/null || true",
                  timeout=15, connect_timeout=8)

    def start_worker(self) -> bool:
        """Startet process.sh detached via setsid (überlebt SSH-Trennung).

        Bewusst OHNE tmux — unabhängig von Vasts interaktivem Auto-tmux und
        immun gegen still fehlschlagende Session-Starts.
          * idempotent: läuft der Worker schon, passiert nichts (exit 0),
          * verifiziert: nach dem Start wird 1 s gewartet und per pgrep geprüft,
            ob der Prozess wirklich lebt -> sonst returncode != 0 (kein stiller
            Fehlstart mehr).
        Fortschritt/Fehler landen in /workspace/work/run.log (Monitor tailt es).
        """
        # WICHTIG: /workspace/work ZUERST anlegen — sonst schlägt die '>>run.log'-
        # Umleitung fehl (Verzeichnis fehlt), process.sh startet NIE und es
        # entsteht kein run.log (genau der beobachtete Deadlock: Modelle+Clips da,
        # GPU idle, run.log fehlt). process.sh legt work/ zwar selbst an, kommt
        # aber nie so weit, wenn die Umleitung schon vorher scheitert.
        # IDEMPOTENT: nur starten, wenn NICHT bereits ein Worker läuft (sonst
        # zweiter Worker-Baum -> OOM). Bracket-Trick verhindert Selbsttreffer.
        launch = (
            "mkdir -p /workspace/work; "
            f"if ! {_PGREP_WORKER} >/dev/null 2>&1; then "
            "setsid /workspace/process.sh >>/workspace/work/run.log 2>&1 </dev/null & "
            "fi")
        self.exec(launch, timeout=30)
        # Sauber verifizieren: separater pgrep-Aufruf mit Bracket-Trick, dessen
        # eigene Kommandozeile den echten Prozess NICHT vortäuscht.
        time.sleep(1.5)
        return self.worker_running()
