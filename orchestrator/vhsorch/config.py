"""Konfiguration — ausschließlich aus Umgebungsvariablen (siehe .env.example).

Secrets (VAST_API_KEY, SSH-Key) werden NIE hardcodiert und NIE geloggt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class Config:
    # Vast
    vast_api_key: str
    vast_image: str
    vast_disk_gb: int
    # SSH
    ssh_key_path: str
    ssh_pubkey: str
    # Pfade
    raw_dir: str
    done_dir: str
    state_dir: str
    # Bootstrap
    repo_raw_url: str
    # Scheduler
    poll_interval: int
    stable_checks: int
    auto_destroy: bool

    @property
    def db_path(self) -> str:
        return os.path.join(self.state_dir, "vhsorch.sqlite")

    @classmethod
    def from_env(cls) -> "Config":
        api_key = os.environ.get("VAST_API_KEY", "").strip()
        return cls(
            vast_api_key=api_key,
            vast_image=os.environ.get(
                "VAST_IMAGE", "pytorch/pytorch:2.6.0-cuda12.6-cudnn9-devel"
            ).strip(),
            vast_disk_gb=int(os.environ.get("VAST_DISK_GB", "320")),
            ssh_key_path=os.environ.get("SSH_KEY_PATH", "/secrets/id_ed25519").strip(),
            ssh_pubkey=os.environ.get("SSH_PUBKEY", "").strip(),
            raw_dir=os.environ.get("RAW_DIR", "/data/raw").strip(),
            done_dir=os.environ.get("DONE_DIR", "/data/done").strip(),
            state_dir=os.environ.get("STATE_DIR", "/state").strip(),
            repo_raw_url=os.environ.get(
                "REPO_RAW_URL",
                "https://raw.githubusercontent.com/nitroglyzerin/videoupscale/main",
            ).strip().rstrip("/"),
            poll_interval=int(os.environ.get("POLL_INTERVAL", "30")),
            stable_checks=int(os.environ.get("STABLE_CHECKS", "2")),
            auto_destroy=_bool("AUTO_DESTROY", "1"),
        )

    def require_api_key(self) -> None:
        if not self.vast_api_key:
            raise SystemExit(
                "VAST_API_KEY ist nicht gesetzt. Trage ihn in .env / als Env-Var ein."
            )
