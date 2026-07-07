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


def _ssh_base(key_path: str, port: int) -> list[str]:
    return [
        "ssh", "-p", str(port),
        "-i", key_path,
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/state/known_hosts",
        "-o", "ConnectTimeout=20",
        "-o", "ServerAliveInterval=15",
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

    def exec(self, command: str, timeout: Optional[int] = None) -> subprocess.CompletedProcess:
        cmd = _ssh_base(self.key_path, self.port) + [self.target, command]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

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
            f"ls -1 {remote_dir} 2>/dev/null | grep -i '\\.mp4$' || true", timeout=30
        )
        if res.returncode != 0:
            return []
        return [line.strip() for line in res.stdout.splitlines() if line.strip()]

    def gpu_activity(self) -> list[tuple[int, str, str]]:
        """Was gerade auf jeder GPU läuft — aus den Worker-Logs abgeleitet.

        process.sh loggt pro GPU nach /workspace/work/logs/gpu<idx>.log Zeilen
        wie 'START: <clip>' bzw. 'FERTIG: <clip> -> ...'. Die jeweils letzte
        dieser Marken bestimmt den Zustand:
          * letzte Marke START  -> GPU verarbeitet aktuell <clip>  (state 'busy')
          * letzte Marke FERTIG/SKIP -> GPU ist zwischen Clips     (state 'idle')

        Rückgabe: Liste (gpu_index, state, clip) sortiert nach GPU-Index.
        """
        snippet = (
            'shopt -s nullglob; '
            'for f in /workspace/work/logs/gpu*.log; do '
            '  g=$(basename "$f" .log); g=${g#gpu}; '
            '  line=$(grep -aE "START:|FERTIG:|SKIP" "$f" | tail -1); '
            '  case "$line" in '
            '    *START:*)  echo "$g|busy|${line#*START: }" ;; '
            '    *)         echo "$g|idle|" ;; '
            '  esac; '
            'done'
        )
        res = self.exec(snippet, timeout=30)
        if res.returncode != 0:
            return []
        out: list[tuple[int, str, str]] = []
        for line in res.stdout.splitlines():
            parts = line.split("|", 2)
            if len(parts) != 3 or not parts[0].isdigit():
                continue
            out.append((int(parts[0]), parts[1], parts[2].strip()))
        return sorted(out, key=lambda t: t[0])

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
