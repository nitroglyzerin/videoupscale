"""SQLite-State: Job-Queue (clips) + Node-Register (nodes).

Warum SQLite: übersteht Container-Restarts, erlaubt atomare Clip->Node-Zuweisung
(WAL + Transaktionen) und sauberes Resume über mehrere Nodes hinweg.
"""
from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS clips (
    name        TEXT PRIMARY KEY,   -- Dateiname des Rohclips (eindeutig)
    status      TEXT NOT NULL,      -- pending|assigned|uploaded|done|failed
    node_id     INTEGER,            -- Vast-Instanz-ID der zugewiesenen Node
    size        INTEGER,
    assigned_at REAL,
    done_at     REAL
);
CREATE TABLE IF NOT EXISTS nodes (
    instance_id INTEGER PRIMARY KEY, -- Vast-Instanz-ID
    offer_id    INTEGER,
    gpu_name    TEXT,
    num_gpus    INTEGER,
    dph         REAL,               -- $/h (dph_total zum Buchungszeitpunkt)
    ssh_host    TEXT,
    ssh_port    INTEGER,
    status      TEXT NOT NULL,       -- booked|ready|draining|destroyed
    worker_started INTEGER DEFAULT 0,
    bootstrap_started INTEGER DEFAULT 0,  -- 1, sobald wir den Bootstrap per SSH angestoßen haben
    created_at  REAL
);
"""

# Nachträglich hinzugekommene Spalten -> für bestehende DBs per ALTER TABLE
# ergänzen (CREATE TABLE IF NOT EXISTS ändert eine vorhandene Tabelle nicht).
_MIGRATIONS = [
    "ALTER TABLE nodes ADD COLUMN bootstrap_started INTEGER DEFAULT 0",
]


class DB:
    def __init__(self, path: str):
        self.path = path
        self._conn = sqlite3.connect(path, timeout=30, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        self._conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                self._conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Spalte existiert bereits — idempotent.

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialisierte Transaktion (IMMEDIATE) für atomare Zuweisungen."""
        self._conn.execute("BEGIN IMMEDIATE;")
        try:
            yield self._conn
            self._conn.execute("COMMIT;")
        except Exception:
            self._conn.execute("ROLLBACK;")
            raise

    # --- Clips ---------------------------------------------------------------
    def add_clip(self, name: str, size: int) -> bool:
        """Fügt einen neuen Clip als pending hinzu. True, wenn neu."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO clips(name, status, size) VALUES (?, 'pending', ?)",
            (name, size),
        )
        return cur.rowcount > 0

    def has_clip(self, name: str) -> bool:
        return self._conn.execute(
            "SELECT 1 FROM clips WHERE name=?", (name,)
        ).fetchone() is not None

    def pending_clips(self, limit: Optional[int] = None) -> list[sqlite3.Row]:
        q = "SELECT * FROM clips WHERE status='pending' ORDER BY name"
        if limit:
            q += f" LIMIT {int(limit)}"
        return self._conn.execute(q).fetchall()

    def clips_for_node(self, instance_id: int, status: Optional[str] = None) -> list[sqlite3.Row]:
        if status:
            return self._conn.execute(
                "SELECT * FROM clips WHERE node_id=? AND status=? ORDER BY name",
                (instance_id, status),
            ).fetchall()
        return self._conn.execute(
            "SELECT * FROM clips WHERE node_id=? ORDER BY name", (instance_id,)
        ).fetchall()

    def assign_clip(self, name: str, instance_id: int) -> None:
        self._conn.execute(
            "UPDATE clips SET status='assigned', node_id=?, assigned_at=? WHERE name=?",
            (instance_id, time.time(), name),
        )

    def set_clip_status(self, name: str, status: str) -> None:
        done_at = time.time() if status == "done" else None
        self._conn.execute(
            "UPDATE clips SET status=?, done_at=COALESCE(?, done_at) WHERE name=?",
            (status, done_at, name),
        )

    def reassign_node_clips(self, instance_id: int) -> int:
        """Setzt alle nicht-fertigen Clips einer (toten) Node zurück auf pending."""
        cur = self._conn.execute(
            "UPDATE clips SET status='pending', node_id=NULL, assigned_at=NULL "
            "WHERE node_id=? AND status IN ('assigned','uploaded')",
            (instance_id,),
        )
        return cur.rowcount

    def reassign_orphan_clips(self) -> int:
        """Setzt verwaiste Clips zurück auf pending (Selbstheilung).

        Verwaist = Status assigned/uploaded, aber die zugewiesene Node ist nicht
        mehr aktiv (booked/ready) — z. B. weil sie zerstört wurde/verschwand,
        bevor das Ergebnis eingesammelt wurde. Ohne 'done' (kein fertiges
        Ergebnis lokal) müssen diese Clips neu verarbeitet werden.
        """
        cur = self._conn.execute(
            "UPDATE clips SET status='pending', node_id=NULL, assigned_at=NULL "
            "WHERE status IN ('assigned','uploaded') AND ("
            "  node_id IS NULL OR node_id NOT IN ("
            "    SELECT instance_id FROM nodes WHERE status IN ('booked','ready')))"
        )
        return cur.rowcount

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT status, COUNT(*) c FROM clips GROUP BY status"
        ).fetchall()
        return {r["status"]: r["c"] for r in rows}

    def all_done(self) -> bool:
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM clips WHERE status NOT IN ('done','failed')"
        ).fetchone()
        return row["c"] == 0

    # --- Nodes ---------------------------------------------------------------
    def add_node(self, **kw) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO nodes"
            "(instance_id, offer_id, gpu_name, num_gpus, dph, ssh_host, ssh_port,"
            " status, worker_started, created_at) "
            "VALUES (:instance_id,:offer_id,:gpu_name,:num_gpus,:dph,:ssh_host,"
            ":ssh_port,:status,:worker_started,:created_at)",
            {
                "worker_started": 0,
                "created_at": time.time(),
                "ssh_host": None,
                "ssh_port": None,
                **kw,
            },
        )

    def update_node(self, instance_id: int, **kw) -> None:
        if not kw:
            return
        cols = ", ".join(f"{k}=:{k}" for k in kw)
        kw["instance_id"] = instance_id
        self._conn.execute(f"UPDATE nodes SET {cols} WHERE instance_id=:instance_id", kw)

    def active_nodes(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM nodes WHERE status IN ('booked','ready') ORDER BY created_at"
        ).fetchall()

    def get_node(self, instance_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM nodes WHERE instance_id=?", (instance_id,)
        ).fetchone()

    def all_nodes(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()

    # --- Reports (Clip x Node join) -----------------------------------------
    def clips_with_gpu(self) -> list[sqlite3.Row]:
        """Alle Clips samt GPU-Infos der zugewiesenen Node (LEFT JOIN).

        Liefert zusätzlich zu den clip-Spalten: gpu_name, num_gpus, dph der
        Node (NULL bei noch nicht zugewiesenen Clips). Basis für die
        Video-/Kostenübersicht.
        """
        return self._conn.execute(
            "SELECT c.*, n.gpu_name AS gpu_name, n.num_gpus AS num_gpus, "
            "       n.dph AS node_dph "
            "FROM clips c LEFT JOIN nodes n ON c.node_id = n.instance_id "
            "ORDER BY CASE c.status "
            "  WHEN 'assigned' THEN 0 WHEN 'uploaded' THEN 1 "
            "  WHEN 'pending'  THEN 2 WHEN 'done' THEN 3 ELSE 4 END, c.name"
        ).fetchall()
