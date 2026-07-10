"""Dünner Client für die Vast.ai REST-API (v0).

Deckt genau die vier Lifecycle-Operationen ab, die der Orchestrator braucht:
  * search_offers  — Angebote mit harten Filtern suchen
  * create_instance — aus einem Offer eine Instanz buchen (mit onstart-Bootstrap)
  * show_instances — laufende Instanzen + SSH-Zugang abfragen
  * destroy_instance — Instanz zerstören (Kostenschutz)

Der API-Key wird als Bearer-Header übergeben und NIE geloggt.

HINWEIS: Die v0-Endpunkte sind stabil, können sich aber ändern. Alle Aufrufe
sind bewusst schlank gehalten und leicht gegen die aktuelle Vast-Doku
(https://vast.ai/docs/api) prüfbar. Bei Feldabweichungen hier zentral anpassen.
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Optional

import requests

API_HOST = "https://console.vast.ai"
API_BASE = f"{API_HOST}/api/v0"      # bundles (search), asks (create), destroy
API_BASE_V1 = f"{API_HOST}/api/v1"   # instances-Liste (v0 ist deprecated -> 410)

# GPUs, die FP8 (e4m3) kompilieren können: Ada (4090) + Blackwell (5090).
# Ampere/A100/3090 sind AUSGESCHLOSSEN (FP8 kompiliert dort nicht).
ALLOWED_GPUS = ["RTX 4090", "RTX 5090"]


@dataclass
class Offer:
    id: int
    gpu_name: str
    num_gpus: int
    dph_total: float           # $/h gesamt
    dlperf: float
    dlperf_per_dph: float      # Preis-Leistung (höher = besser)
    reliability: float         # 0..1
    disk_space: float          # GB verfügbar
    cuda_max_good: float
    geolocation: str
    cpu_ram: float             # System-RAM gesamt in MB

    @property
    def ram_per_gpu_gb(self) -> float:
        """Host-RAM je GPU in GB — entscheidend gegen den VAE-Decode-OOM."""
        return (self.cpu_ram / 1024.0) / max(1, self.num_gpus)

    @classmethod
    def from_api(cls, d: dict[str, Any]) -> "Offer":
        return cls(
            id=d["id"],
            gpu_name=d.get("gpu_name", "?"),
            num_gpus=d.get("num_gpus", 0),
            dph_total=float(d.get("dph_total", 0.0)),
            dlperf=float(d.get("dlperf", 0.0)),
            dlperf_per_dph=float(d.get("dlperf_per_dphtotal", 0.0)),
            reliability=float(d.get("reliability2", 0.0)),
            disk_space=float(d.get("disk_space", 0.0)),
            cuda_max_good=float(d.get("cuda_max_good", 0.0)),
            geolocation=d.get("geolocation") or "?",
            cpu_ram=float(d.get("cpu_ram", 0.0)),
        )


class VastError(RuntimeError):
    pass


class VastClient:
    def __init__(self, api_key: str):
        if not api_key:
            raise VastError("Kein Vast API Key übergeben.")
        self._session = requests.Session()
        self._session.headers.update(
            {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
        )

    # Statuscodes, die sich mit kurzem Warten von selbst erledigen.
    _RETRY_STATUS = {429, 502, 503, 504}

    @staticmethod
    def _retry_after_seconds(resp: requests.Response, attempt: int) -> float:
        """Wartezeit vor dem nächsten Versuch: Retry-After-Header > JSON-Feld
        'retry_after' > exponentieller Backoff. Gedeckelt auf 30 s."""
        hdr = resp.headers.get("Retry-After")
        if hdr:
            try:
                return min(float(hdr), 30.0)
            except ValueError:
                pass
        try:
            body = resp.json()
            if isinstance(body, dict) and body.get("retry_after"):
                # +0.5 s Puffer, damit wir nicht exakt am Limit wieder anklopfen.
                return min(float(body["retry_after"]) + 0.5, 30.0)
        except ValueError:
            pass
        return min(2.0 ** attempt, 30.0)

    def _req(self, method: str, path: str, base: str = API_BASE,
             _retries: int = 4, **kw) -> Any:
        url = f"{base}{path}"
        for attempt in range(_retries + 1):
            resp = self._session.request(method, url, timeout=60, **kw)
            # Vast drosselt (429) oder ist kurz weg (5xx) -> warten und erneut.
            if resp.status_code in self._RETRY_STATUS and attempt < _retries:
                time.sleep(self._retry_after_seconds(resp, attempt))
                continue
            if resp.status_code >= 400:
                # API-Key wird nicht mitgeloggt (nur in Session-Header).
                raise VastError(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:400]}")
            try:
                return resp.json()
            except ValueError:
                return {}
        # Nach _retries erschöpften Versuchen: letzten Fehler sauberer melden.
        raise VastError(f"{method} {path} -> nach {_retries} Retries weiter gedrosselt.")

    # --- Suche ---------------------------------------------------------------
    def search_offers(self, disk_gb: int, min_gpus: int = 4,
                      min_reliability: float = 0.995,
                      gpu_names: Optional[list[str]] = None,
                      min_ram_per_gpu_gb: int = 0) -> list[Offer]:
        """Sucht Offers mit den HARTEN Filtern aus der Anforderung.

        Filter: GPU-Typ (Default RTX 4090|5090, über gpu_names einschränkbar),
        verified, rentable, num_gpus>=min_gpus, reliability>=min_reliability,
        disk_space>=disk_gb, Host-RAM/GPU>=min_ram_per_gpu_gb. Sortiert nach
        Preis-Leistung (dlperf_per_dphtotal).
        """
        allowed = gpu_names or ALLOWED_GPUS
        query = {
            "verified": {"eq": True},
            "rentable": {"eq": True},
            "rented": {"eq": False},
            "num_gpus": {"gte": min_gpus},
            "gpu_name": {"in": allowed},
            "reliability2": {"gte": min_reliability},
            "disk_space": {"gte": disk_gb},
            "type": "on-demand",
            "order": [["dlperf_per_dphtotal", "desc"]],
            "limit": 64,
        }
        data = self._req("GET", "/bundles/", params={"q": json.dumps(query)})
        offers = [Offer.from_api(o) for o in data.get("offers", [])]
        # Sicherheitsnetz: clientseitig erneut nach GPU-Typ filtern, falls die
        # API einen unerwarteten Namen (z.B. "RTX 4090 D") durchreicht.
        return [
            o for o in offers
            if any(g in o.gpu_name for g in allowed)
            and o.num_gpus >= min_gpus
            and o.reliability >= min_reliability
            and o.ram_per_gpu_gb >= min_ram_per_gpu_gb
        ]

    # --- Buchen --------------------------------------------------------------
    def create_instance(self, offer_id: int, image: str, disk_gb: int,
                        onstart_cmd: str, env: Optional[dict] = None) -> int:
        """Bucht ein Offer und gibt die neue Instanz-ID zurück.

        runtype 'ssh' -> Vast richtet SSH ein und injiziert die im Account
        hinterlegten Public-Keys. onstart_cmd läuft beim ersten Start.
        """
        body = {
            "client_id": "me",
            "image": image,
            "disk": disk_gb,
            "onstart": onstart_cmd,
            "runtype": "ssh",
            "env": env or {},
        }
        data = self._req("PUT", f"/asks/{offer_id}/", json=body)
        if not data.get("success", False):
            raise VastError(f"Buchung fehlgeschlagen: {data}")
        new_id = data.get("new_contract")
        if not new_id:
            raise VastError(f"Keine Instanz-ID in Antwort: {data}")
        return int(new_id)

    # --- Status --------------------------------------------------------------
    def show_instances(self) -> list[dict[str, Any]]:
        # v0 /instances/ ist deprecated (HTTP 410) -> v1 nutzen.
        data = self._req("GET", "/instances/", base=API_BASE_V1)
        # Defensiv: v1 kann {"instances": [...]} ODER eine nackte Liste liefern.
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            return data.get("instances", [])
        return []

    def get_instance(self, instance_id: int) -> Optional[dict[str, Any]]:
        for inst in self.show_instances():
            if int(inst.get("id", -1)) == instance_id:
                return inst
        return None

    # --- Zerstören (Kostenschutz) -------------------------------------------
    def destroy_instance(self, instance_id: int) -> None:
        self._req("DELETE", f"/instances/{instance_id}/")
