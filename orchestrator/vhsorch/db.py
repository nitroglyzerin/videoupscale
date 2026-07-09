"""SQLite-State: Job-Queue (clips) + Node-Register (nodes) + Command-Queue (commands).

Warum SQLite: übersteht Container-Restarts, erlaubt atomare Clip->Node-Zuweisung
(WAL + Transaktionen) und sauberes Resume über mehrere Nodes hinweg.

Thread-Sicherheit: der Scheduler arbeitet Befehle/Transfers in einem Heavy-Pool
nebenläufig ab. sqlite3-Connections sind NICHT thread-übergreifend nutzbar ->
jede Thread bekommt ihre EIGENE Connection (threading.local). WAL + busy_timeout
erlauben mehrere Leser und einen Schreiber ohne "database is locked".
"""
from __future__ import annotations

import sqlite3
import threading
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
    models_pushed INTEGER DEFAULT 0,      -- 1, sobald die SeedVR2-Modelle auf die Node gepusht sind
    created_at  REAL
);
CREATE TABLE IF NOT EXISTS commands (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id      INTEGER,            -- NULL = global (alle zutreffenden Nodes)
    action       TEXT NOT NULL,      -- bootstrap|models|worker|pull|requeue|drain|destroy
    arg          TEXT,               -- z.B. Clip-Name bei requeue
    status       TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
    result       TEXT,
    requested_at REAL,
    started_at   REAL,
    done_at      REAL
);
"""

# Nachträglich hinzugekommene Spalten -> für bestehende DBs per ALTER TABLE
# ergänzen (CREATE TABLE IF NOT EXISTS ändert eine vorhandene Tabelle nicht).
_MIGRATIONS = [
    "ALTER TABLE nodes ADD COLUMN bootstrap_started INTEGER DEFAULT 0",
    "ALTER TABLE nodes ADD COLUMN models_pushed INTEGER DEFAULT 0",
]

# Node-Status, die als "aktiv" gelten (Kosten laufen, Node sichtbar, Ergebnisse
# noch einsammelbar). 'draining' ist bewusst dabei: eine drainende Node bekommt
# keine neue Arbeit mehr, ihre Ergebnisse werden aber noch gepullt.
_ACTIVE_STATUS = ("booked", "ready", "draining")


class DB:
    def __init__(self, path: str):
        self.path = path
        self._local = threading.local()
        # Schema/Migrationen einmalig auf einer Bootstrap-Connection anlegen.
        conn = self._conn
        conn.executescript(SCHEMA)
        for stmt in _MIGRATIONS:
            try:
                conn.execute(stmt)
            except sqlite3.OperationalError:
                pass  # Spalte existiert bereits — idempotent.

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA foreign_keys=ON;")
        # Mehrere Threads/Prozesse schreiben (TUI enqueued, Scheduler + Heavy-Pool
        # aktualisieren) -> statt sofort "database is locked" bis 30 s warten.
        conn.execute("PRAGMA busy_timeout=30000;")
        return conn

    @property
    def _conn(self) -> sqlite3.Connection:
        """Thread-lokale Connection (eine pro Thread, dieselbe Datei)."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._new_connection()
            self._local.conn = conn
        return conn

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Serialisierte Transaktion (IMMEDIATE) für atomare Zuweisungen."""
        conn = self._conn
        conn.execute("BEGIN IMMEDIATE;")
        try:
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
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

    def all_clip_names(self) -> list[str]:
        return [r["name"] for r in
                self._conn.execute("SELECT name FROM clips").fetchall()]

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

    def claim_pending_clips(self, instance_id: int, limit: int) -> int:
        """Weist bis zu `limit` Clips aus dem gemeinsamen pending-Pool dieser Node
        zu (atomar). So holt sich auch eine SPÄT dazugekommene Node Arbeit."""
        if limit <= 0:
            return 0
        with self.tx() as conn:
            rows = conn.execute(
                "SELECT name FROM clips WHERE status='pending' ORDER BY name LIMIT ?",
                (int(limit),),
            ).fetchall()
            now = time.time()
            for r in rows:
                conn.execute(
                    "UPDATE clips SET status='assigned', node_id=?, assigned_at=? "
                    "WHERE name=? AND status='pending'",
                    (instance_id, now, r["name"]),
                )
            return len(rows)

    def release_assigned_clips(self, instance_id: int, limit: int) -> int:
        """Gibt bis zu `limit` NUR-zugewiesene (nicht hochgeladene) Clips einer Node
        zurück in den pending-Pool — für die Balance, wenn eine Node zu viel hält."""
        if limit <= 0:
            return 0
        cur = self._conn.execute(
            "UPDATE clips SET status='pending', node_id=NULL, assigned_at=NULL "
            "WHERE name IN (SELECT name FROM clips WHERE node_id=? AND status='assigned' "
            "               ORDER BY name LIMIT ?)",
            (instance_id, int(limit)),
        )
        return cur.rowcount

    def set_clip_status(self, name: str, status: str) -> None:
        done_at = time.time() if status == "done" else None
        self._conn.execute(
            "UPDATE clips SET status=?, done_at=COALESCE(?, done_at) WHERE name=?",
            (status, done_at, name),
        )

    def requeue_clip(self, name: str) -> tuple[bool, Optional[int]]:
        """Setzt einen (fehlgeschlagenen/steckengebliebenen) Clip zurück auf pending.

        Rückgabe: (requeued, alte_node_id). Die alte node_id braucht der Aufrufer,
        um den Claim-Lock auf jener Node freizugeben (sonst greift ihn kein Worker
        neu). requeued=False, wenn der Clip fehlt oder schon 'done' ist.
        """
        row = self._conn.execute(
            "SELECT node_id, status FROM clips WHERE name=?", (name,)
        ).fetchone()
        if row is None or row["status"] == "done":
            return (False, None)
        old_node = row["node_id"]
        self._conn.execute(
            "UPDATE clips SET status='pending', node_id=NULL, assigned_at=NULL "
            "WHERE name=?", (name,),
        )
        return (True, old_node)

    def mark_uploaded(self, name: str, instance_id: int) -> bool:
        """Setzt 'assigned' -> 'uploaded' NUR, wenn der Clip noch dieser Node gehört.

        Geführter Übergang: ist der Clip zwischenzeitlich per drain/requeue auf
        pending/node_id=NULL gewandert, ist das ein No-op — so entsteht nie eine
        inkonsistente 'uploaded'-Zeile ohne node_id. True, wenn gesetzt.
        """
        cur = self._conn.execute(
            "UPDATE clips SET status='uploaded' "
            "WHERE name=? AND node_id=? AND status='assigned'",
            (name, instance_id),
        )
        return cur.rowcount > 0

    def abandon_failed(self) -> int:
        """Markiert alle 'failed' Clips als terminal 'abandoned' (Finalisieren).

        Erst danach gilt der Lauf als abgeschlossen (all_done) und AUTO_DESTROY
        darf greifen — sonst hält ein dauerhaft scheiternder Clip den Lauf offen.
        """
        cur = self._conn.execute(
            "UPDATE clips SET status='abandoned' WHERE status='failed'"
        )
        return cur.rowcount

    def reassign_node_clips(self, instance_id: int) -> int:
        """Setzt alle nicht-fertigen Clips einer (toten/drainenden) Node zurück auf pending."""
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

    def counts_by_node(self) -> dict[int, dict[str, int]]:
        """Pro Node: {status: count} — Basis für die Per-Node-Clip-Zähler im Snapshot."""
        rows = self._conn.execute(
            "SELECT node_id, status, COUNT(*) c FROM clips "
            "WHERE node_id IS NOT NULL GROUP BY node_id, status"
        ).fetchall()
        out: dict[int, dict[str, int]] = {}
        for r in rows:
            out.setdefault(r["node_id"], {})[r["status"]] = r["c"]
        return out

    def all_done(self) -> bool:
        """True, wenn kein Clip mehr offen ist. 'failed' zählt bewusst NICHT als
        erledigt: ein Lauf mit Fehlern bleibt offen (Nodes leben weiter für Retry),
        bis der Bediener sie retryt oder per 'finalize' auf 'abandoned' setzt."""
        row = self._conn.execute(
            "SELECT COUNT(*) c FROM clips WHERE status NOT IN ('done','abandoned')"
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
            "SELECT * FROM nodes WHERE status IN ('booked','ready','draining') "
            "ORDER BY created_at"
        ).fetchall()

    def get_node(self, instance_id: int) -> Optional[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM nodes WHERE instance_id=?", (instance_id,)
        ).fetchone()

    def all_nodes(self) -> list[sqlite3.Row]:
        return self._conn.execute("SELECT * FROM nodes ORDER BY created_at").fetchall()

    # --- Commands (Absichts-Queue: TUI schreibt, Scheduler führt aus) --------
    def add_command(self, action: str, node_id: Optional[int] = None,
                    arg: Optional[str] = None) -> int:
        cur = self._conn.execute(
            "INSERT INTO commands(node_id, action, arg, status, requested_at) "
            "VALUES (?,?,?, 'queued', ?)",
            (node_id, action, arg, time.time()),
        )
        return int(cur.lastrowid)

    def queued_commands(self) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM commands WHERE status='queued' ORDER BY id"
        ).fetchall()

    def requeue_running_commands(self) -> int:
        """Setzt beim Scheduler-Start 'running' gebliebene Commands zurück auf
        'queued' (der Prozess starb mitten in der Ausführung). Sicher, weil alle
        Aktionen idempotent sind und nur EIN Scheduler die Queue abarbeitet."""
        cur = self._conn.execute(
            "UPDATE commands SET status='queued', started_at=NULL, "
            "result='durch Neustart erneut eingereiht' WHERE status='running'"
        )
        return cur.rowcount

    def set_command_status(self, command_id: int, status: str,
                           result: Optional[str] = None) -> None:
        now = time.time()
        started = now if status == "running" else None
        done = now if status in ("done", "failed") else None
        self._conn.execute(
            "UPDATE commands SET status=?, "
            "  result=COALESCE(?, result), "
            "  started_at=COALESCE(?, started_at), "
            "  done_at=COALESCE(?, done_at) "
            "WHERE id=?",
            (status, result, started, done, command_id),
        )

    def recent_commands(self, limit: int = 30) -> list[sqlite3.Row]:
        return self._conn.execute(
            "SELECT * FROM commands ORDER BY id DESC LIMIT ?", (int(limit),)
        ).fetchall()

    def prune_commands(self, keep: int = 200) -> None:
        """Alte, erledigte Command-Zeilen kappen (Tabelle klein halten)."""
        self._conn.execute(
            "DELETE FROM commands WHERE status IN ('done','failed') AND id NOT IN ("
            "  SELECT id FROM commands ORDER BY id DESC LIMIT ?)",
            (int(keep),),
        )

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
