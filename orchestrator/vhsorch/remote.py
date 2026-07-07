"""rsync-über-SSH: Rohvideos zur Node pushen, Ergebnisse pullen, Worker starten.

Nur AUSGEHENDE Verbindungen (Home -> Vast). Keine offenen Ports zuhause.
rsync ist delta-basiert und resumierbar (--partial), robust bei Abbrüchen.
"""
from __future__ import annotations

import subprocess
from typing import Optional


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

    def worker_running(self) -> bool:
        res = self.exec("pgrep -f process.sh >/dev/null 2>&1 && echo yes || echo no", timeout=30)
        return res.stdout.strip() == "yes"

    def start_worker(self) -> bool:
        """Startet process.sh abbruchsicher in tmux (überlebt SSH-Trennung)."""
        cmd = (
            "tmux has-session -t upscale 2>/dev/null || "
            "tmux new-session -d -s upscale "
            "'/workspace/process.sh 2>&1 | tee -a /workspace/work/run.log'"
        )
        return self.exec(cmd, timeout=60).returncode == 0
