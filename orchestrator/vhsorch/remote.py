"""rsync-über-SSH: Rohvideos zur Node pushen, Ergebnisse pullen, Worker starten.

Nur AUSGEHENDE Verbindungen (Home -> Vast). Keine offenen Ports zuhause.
rsync ist delta-basiert und resumierbar (--partial), robust bei Abbrüchen.
"""
from __future__ import annotations

import subprocess
import time
from typing import Optional

# pgrep-Muster mit Bracket-Trick: '[p]rocess\.sh' matcht den echten
# process.sh-Prozess, aber NICHT die pgrep-Kommandozeile selbst (die den
# String literal '[p]rocess\.sh' enthält -> kein "process.sh"-Substring).
# Ohne diesen Trick würde pgrep -f seine eigene Wrapper-Shell finden und
# fälschlich "Worker läuft" melden.
_PGREP_WORKER = r"pgrep -f '[p]rocess\.sh'"


def _ssh_base(key_path: str, port: int, connect_timeout: int = 20) -> list[str]:
    return [
        "ssh", "-p", str(port),
        "-i", key_path,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/state/known_hosts",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=15",
        # BatchMode: nie interaktiv nach Passwort fragen -> hängt nie am Prompt,
        # scheitert stattdessen sofort (wichtig für die Live-Anzeige).
        "-o", "BatchMode=yes",
    ]


class Remote:
    """Eine SSH-erreichbare Vast-Node."""

    def __init__(self, host: str, port: int, key_path: str, user: str = "root"):
        self.host = host
        self.port = port
        self.key_path = key_path
        self.user = user

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}"

    def _rsync_e(self) -> str:
        base = _ssh_base(self.key_path, self.port)
        return " ".join(base)

    def reachable(self) -> bool:
        cmd = _ssh_base(self.key_path, self.port) + [self.target, "true"]
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0

    def exec(self, command: str, timeout: Optional[int] = None,
             connect_timeout: int = 20) -> subprocess.CompletedProcess:
        cmd = _ssh_base(self.key_path, self.port, connect_timeout) + [self.target, command]
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            # Harte Obergrenze überschritten (Node hängt): als sauberer
            # Fehlschlag zurückgeben, statt die Live-Anzeige zu blockieren.
            return subprocess.CompletedProcess(cmd, returncode=124, stdout="", stderr="")

    def push_files(self, local_paths: list[str], remote_dir: str = "/workspace/input/") -> bool:
        """Pusht eine Liste konkreter Dateien in remote_dir (delta, resumierbar)."""
        if not local_paths:
            return True
        cmd = [
            "rsync", "-az", "--partial", "--info=progress2",
            "-e", self._rsync_e(),
            *local_paths,
            f"{self.target}:{remote_dir}",
        ]
        return subprocess.run(cmd).returncode == 0

    def pull_results(self, local_dir: str, remote_dir: str = "/workspace/final/") -> bool:
        """Zieht fertige Clips von der Node (delta, überschreibt nur Neues)."""
        cmd = [
            "rsync", "-az", "--partial", "--ignore-existing",
            "-e", self._rsync_e(),
            f"{self.target}:{remote_dir}",
            local_dir if local_dir.endswith("/") else local_dir + "/",
        ]
        return subprocess.run(cmd).returncode == 0

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
            'curl -fsSL \"$REPO_RAW_URL/node/bootstrap.sh\" -o /workspace/bootstrap.sh && '
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

    def worker_running(self) -> bool:
        res = self.exec(f"{_PGREP_WORKER} >/dev/null 2>&1 && echo yes || echo no",
                        timeout=30)
        return res.stdout.strip() == "yes"

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
        # Detached starten (eigenes ssh-Kommando OHNE pgrep -> kein Selbsttreffer).
        launch = ("setsid /workspace/process.sh "
                  ">>/workspace/work/run.log 2>&1 </dev/null &")
        self.exec(launch, timeout=30)
        # Sauber verifizieren: separater pgrep-Aufruf mit Bracket-Trick, dessen
        # eigene Kommandozeile den echten Prozess NICHT vortäuscht.
        time.sleep(1.5)
        return self.worker_running()
