"""Konfiguration — ausschließlich aus Umgebungsvariablen (siehe .env.example).

Secrets (VAST_API_KEY, SSH-Key) werden NIE hardcodiert und NIE geloggt.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _bool(name: str, default: str = "0") -> bool:
    return os.environ.get(name, default).strip().lower() in ("1", "true", "yes", "on")


# SeedVR2-Modelle: der Orchestrator hält sie EINMAL im Home-Cache (models_dir,
# auf der WD Red) und pusht sie per rsync auf jede Node. Nodes laden NICHTS mehr
# von HuggingFace (Node-Egress ist unzuverlässig: IPv6-only, TLS-Reset, 429).
# Cache füllen mit `vhsorch fetch-models`. Name -> HF-Download-URL.
SEEDVR2_MODEL_FILES = {
    "seedvr2_ema_3b_fp8_e4m3fn.safetensors":
        "https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/seedvr2_ema_3b_fp8_e4m3fn.safetensors",
    "ema_vae_fp16.safetensors":
        "https://huggingface.co/numz/SeedVR2_comfyUI/resolve/main/ema_vae_fp16.safetensors",
}


# Default-Rechenleistungs-Faktoren pro GPU-Typ (relative Upscale-Durchsatz-
# Gewichtung). Substring-Match gegen gpu_name. Über GPU_COST_FACTORS
# überschreibbar, z. B. "RTX 4090=1.0,RTX 5090=1.7".
_DEFAULT_GPU_FACTORS = {
    "RTX 5090": 1.7,
    "RTX 4090": 1.0,
}


def _parse_gpu_factors(raw: str) -> dict[str, float]:
    factors: dict[str, float] = {}
    for part in raw.split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, _, val = part.partition("=")
        try:
            factors[key.strip()] = float(val.strip())
        except ValueError:
            continue
    return factors or dict(_DEFAULT_GPU_FACTORS)


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
    models_dir: str   # Home-Cache der SeedVR2-Modelle (WD Red), wird auf Nodes gepusht
    # Bootstrap
    repo_raw_url: str
    # Scheduler
    poll_interval: int
    stable_checks: int
    auto_destroy: bool
    inflight_per_gpu: int
    probe_interval: int   # Sekunden zwischen (read-only) SSH-Probes der Nodes
    heavy_workers: int    # parallele Slots für lange Transfers (push_models/push/pull)
    # Kostenmodell (Video-/Kostenübersicht)
    cost_rate_x: float
    gpu_cost_factors: dict[str, float] = field(default_factory=lambda: dict(_DEFAULT_GPU_FACTORS))
    gpu_factor_default: float = 1.0

    @property
    def db_path(self) -> str:
        return os.path.join(self.state_dir, "vhsorch.sqlite")

    @property
    def snapshot_path(self) -> str:
        """Vom Scheduler-Tick geschriebener Live-Status (JSON), von der TUI gelesen."""
        return os.path.join(self.state_dir, "snapshot.json")

    @property
    def ssh_mux_dir(self) -> str:
        """Verzeichnis für SSH-ControlPersist-Sockets (warmer Handshake)."""
        return os.path.join(self.state_dir, "ssh-mux")

    def gpu_factor(self, gpu_name: str | None) -> float:
        """Kosten-Faktor für einen GPU-Namen (Substring-Match, sonst Default)."""
        if gpu_name:
            for key, val in self.gpu_cost_factors.items():
                if key.lower() in gpu_name.lower():
                    return val
        return self.gpu_factor_default

    @classmethod
    def from_env(cls) -> "Config":
        api_key = os.environ.get("VAST_API_KEY", "").strip()
        return cls(
            vast_api_key=api_key,
            vast_image=os.environ.get(
                # Auto-Tag wählt den GPU-passenden CUDA-Build (5090 -> cu128) ->
                # torch läuft sofort, cu128-Reinstall in bootstrap.sh entfällt.
                "VAST_IMAGE", "vastai/pytorch:@vastai-automatic-tag"
            ).strip(),
            vast_disk_gb=int(os.environ.get("VAST_DISK_GB", "320")),
            ssh_key_path=os.environ.get("SSH_KEY_PATH", "/secrets/id_ed25519").strip(),
            ssh_pubkey=os.environ.get("SSH_PUBKEY", "").strip(),
            raw_dir=os.environ.get("RAW_DIR", "/data/raw").strip(),
            done_dir=os.environ.get("DONE_DIR", "/data/done").strip(),
            state_dir=os.environ.get("STATE_DIR", "/state").strip(),
            models_dir=os.environ.get("MODELS_DIR", "/data/models").strip(),
            repo_raw_url=os.environ.get(
                "REPO_RAW_URL",
                "https://raw.githubusercontent.com/nitroglyzerin/videoupscale/main",
            ).strip().rstrip("/"),
            poll_interval=int(os.environ.get("POLL_INTERVAL", "30")),
            stable_checks=int(os.environ.get("STABLE_CHECKS", "2")),
            auto_destroy=_bool("AUTO_DESTROY", "1"),
            inflight_per_gpu=int(os.environ.get("INFLIGHT_PER_GPU", "2")),
            probe_interval=int(os.environ.get("PROBE_INTERVAL", "5")),
            heavy_workers=int(os.environ.get("HEAVY_WORKERS", "4")),
            cost_rate_x=float(os.environ.get("COST_RATE_X", "0.01")),
            gpu_cost_factors=_parse_gpu_factors(os.environ.get("GPU_COST_FACTORS", "")),
            gpu_factor_default=float(os.environ.get("GPU_FACTOR_DEFAULT", "1.0")),
        )

    def require_api_key(self) -> None:
        if not self.vast_api_key:
            raise SystemExit(
                "VAST_API_KEY ist nicht gesetzt. Trage ihn in .env / als Env-Var ein."
            )
