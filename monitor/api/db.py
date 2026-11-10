import os
import sqlite3
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

DEFAULT_RETENTION_DAYS = 14
PURGE_EVERY = timedelta(hours=6)
MAX_CHART_POINTS = 300
# Janela em que um reenvio do agente ainda é reconhecido como duplicado.
DEDUP_WINDOW = timedelta(minutes=10)


SCHEMA = """
CREATE TABLE IF NOT EXISTS agents (
    id TEXT PRIMARY KEY,
    group_name TEXT NOT NULL DEFAULT 'Sem grupo',
    name TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_seen TEXT,
    hostname TEXT,
    os TEXT,
    expected_interval_s INTEGER NOT NULL DEFAULT 30 CHECK (expected_interval_s > 0)
);

CREATE TABLE IF NOT EXISTS metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,
    received_at TEXT NOT NULL,
    agent_timestamp TEXT,
    cpu_percent REAL NOT NULL,
    memory_percent REAL NOT NULL,
    disk_percent REAL NOT NULL,
    temp_c REAL,
    net_bytes_sent INTEGER NOT NULL,
    net_bytes_recv INTEGER NOT NULL,
    uptime_s INTEGER NOT NULL,
    FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_metrics_agent_received_at
ON metrics(agent_id, received_at);
"""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_db_timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def from_db_timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def rows_as_dicts(cursor: Any) -> list[dict[str, Any]]:
    columns = [column[0] for column in cursor.description]
    return [dict(zip(columns, row, strict=True)) for row in cursor.fetchall()]


def row_as_dict(cursor: Any) -> dict[str, Any] | None:
    columns = [column[0] for column in cursor.description]
    row = cursor.fetchone()
    return dict(zip(columns, row, strict=True)) if row else None


class Database:
    def __init__(
        self,
        path: str | Path,
        *,
        turso_url: str | None = None,
        turso_auth_token: str | None = None,
        connection_factory: Callable[[], Any] | None = None,
        retention_days: int = DEFAULT_RETENTION_DAYS,
    ) -> None:
        self.path = str(path)
        self.turso_url = turso_url
        self.turso_auth_token = turso_auth_token
        self.connection_factory = connection_factory
        self.backend = "turso" if turso_url else "sqlite"
        self.retention_days = retention_days
        self._last_purge: datetime | None = None

    @classmethod
    def from_environment(cls, default_path: str | Path) -> "Database":
        turso_url = os.getenv("TURSO_DATABASE_URL", "").strip()
        turso_auth_token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
        if bool(turso_url) != bool(turso_auth_token):
            raise RuntimeError(
                "TURSO_DATABASE_URL and TURSO_AUTH_TOKEN must be configured together"
            )
        return cls(
            os.getenv("DATABASE_PATH", str(default_path)),
            turso_url=turso_url or None,
            turso_auth_token=turso_auth_token or None,
            retention_days=int(
                os.getenv("METRICS_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)
            ),
        )

    def _open_connection(self) -> Any:
        if self.connection_factory:
            return self.connection_factory()
        if self.turso_url:
            import libsql

            return libsql.connect(
                database=self.turso_url,
                auth_token=self.turso_auth_token or "",
                timeout=10,
                autocommit=True,
            )
        return sqlite3.connect(self.path, timeout=10)

    @contextmanager
    def connect(self) -> Iterator[Any]:
        connection = self._open_connection()
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            yield connection
            if self.backend == "sqlite":
                connection.commit()
        except Exception:
            if self.backend == "sqlite":
                connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        if self.backend == "sqlite":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            if self.backend == "sqlite":
                connection.execute("PRAGMA journal_mode=WAL")
                connection.execute("PRAGMA synchronous=NORMAL")
            connection.executescript(SCHEMA)
            columns = {
                row[1] for row in connection.execute("PRAGMA table_info(agents)").fetchall()
            }
            if "token_hash" in columns:  # coluna legada, nunca usada
                connection.execute("ALTER TABLE agents DROP COLUMN token_hash")

    def record_heartbeat(self, payload: dict[str, Any]) -> datetime:
        received_at = utc_now()
        agent_timestamp = payload.get("timestamp")
        agent_timestamp_db = (
            to_db_timestamp(agent_timestamp) if agent_timestamp else None
        )
        with self.connect() as connection:
            # Leitura barata em vez de upsert a cada heartbeat: escrita só quando
            # o agente é novo ou mudou hostname/os/intervalo. O last_seen vem da
            # última métrica (ver list_agents).
            known = row_as_dict(
                connection.execute(
                    "SELECT hostname, os, expected_interval_s FROM agents WHERE id = ?",
                    (payload["agent_id"],),
                )
            )
            if known is None:
                connection.execute(
                    """
                    INSERT INTO agents (
                        id, group_name, name, created_at, last_seen,
                        hostname, os, expected_interval_s
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        payload["agent_id"],
                        payload["group"],
                        payload["hostname"],
                        to_db_timestamp(received_at),
                        to_db_timestamp(received_at),
                        payload["hostname"],
                        payload["os"],
                        payload["interval_seconds"],
                    ),
                )
            elif (
                known["hostname"] != payload["hostname"]
                or known["os"] != payload["os"]
                or known["expected_interval_s"] != payload["interval_seconds"]
            ):
                connection.execute(
                    """
                    UPDATE agents SET hostname = ?, os = ?, expected_interval_s = ?,
                        last_seen = ?
                    WHERE id = ?
                    """,
                    (
                        payload["hostname"],
                        payload["os"],
                        payload["interval_seconds"],
                        to_db_timestamp(received_at),
                        payload["agent_id"],
                    ),
                )
            connection.execute(
                """
                INSERT INTO metrics (
                    agent_id, received_at, agent_timestamp, cpu_percent,
                    memory_percent, disk_percent, temp_c, net_bytes_sent,
                    net_bytes_recv, uptime_s
                ) SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE NOT EXISTS (
                    SELECT 1 FROM metrics
                    WHERE agent_id = ? AND received_at >= ? AND agent_timestamp IS ?
                )
                """,
                (
                    payload["agent_id"],
                    to_db_timestamp(received_at),
                    agent_timestamp_db,
                    payload["cpu_percent"],
                    payload["memory_percent"],
                    payload["disk_percent"],
                    payload["temp_c"],
                    payload["net_bytes_sent"],
                    payload["net_bytes_recv"],
                    payload["uptime_s"],
                    payload["agent_id"],
                    to_db_timestamp(received_at - DEDUP_WINDOW),
                    agent_timestamp_db,
                ),
            )
        self._purge_if_due(received_at)
        return received_at

    def _purge_if_due(self, now: datetime) -> None:
        if self.retention_days <= 0:
            return
        if self._last_purge and now - self._last_purge < PURGE_EVERY:
            return
        self._last_purge = now
        self.purge_old_metrics(self.retention_days)

    def purge_old_metrics(self, days: int) -> None:
        cutoff = to_db_timestamp(utc_now() - timedelta(days=days))
        with self.connect() as connection:
            # Por agente, para usar o índice (agent_id, received_at) em vez de varrer a tabela.
            agent_ids = [row[0] for row in connection.execute("SELECT id FROM agents").fetchall()]
            for agent_id in agent_ids:
                connection.execute(
                    "DELETE FROM metrics WHERE agent_id = ? AND received_at < ?",
                    (agent_id, cutoff),
                )

    def list_agents(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        where, params = ("WHERE a.id = ?", (agent_id,)) if agent_id else ("", ())
        with self.connect() as connection:
            rows = rows_as_dicts(
                connection.execute(
                    f"""
                    SELECT a.id, a.group_name, a.name,
                           COALESCE(m.received_at, a.last_seen) AS last_seen, a.hostname, a.os,
                           a.expected_interval_s,
                           m.received_at, m.agent_timestamp, m.cpu_percent,
                           m.memory_percent, m.disk_percent, m.temp_c,
                           m.net_bytes_sent, m.net_bytes_recv, m.uptime_s,
                           p.received_at AS prev_received_at,
                           p.net_bytes_sent AS prev_net_sent,
                           p.net_bytes_recv AS prev_net_recv
                    FROM agents a
                    LEFT JOIN metrics m ON m.id = (
                        SELECT id FROM metrics
                        WHERE agent_id = a.id
                        ORDER BY received_at DESC, id DESC
                        LIMIT 1
                    )
                    LEFT JOIN metrics p ON p.id = (
                        SELECT id FROM metrics
                        WHERE agent_id = a.id AND received_at < m.received_at
                        ORDER BY received_at DESC, id DESC
                        LIMIT 1
                    )
                    {where}
                    ORDER BY a.name COLLATE NOCASE
                    """,
                    params,
                )
            )

        now = utc_now()
        agents: list[dict[str, Any]] = []
        for row in rows:
            last_seen = from_db_timestamp(row["last_seen"])
            online = bool(
                last_seen
                and now - last_seen
                <= timedelta(seconds=3 * row["expected_interval_s"])
            )
            latest_metric = self._metric_from_row(row) if row["received_at"] else None
            agents.append(
                {
                    "id": row["id"],
                    "group": row["group_name"],
                    "name": row["name"],
                    "status": "online" if online else "offline",
                    "last_seen": last_seen,
                    "hostname": row["hostname"],
                    "os": row["os"],
                    "expected_interval_s": row["expected_interval_s"],
                    "latest_metric": latest_metric,
                    **self._network_rates(row),
                }
            )
        return agents

    def update_agent(
        self, agent_id: str, *, name: str | None, group: str | None
    ) -> bool:
        assignments: list[str] = []
        values: list[Any] = []
        if name is not None:
            assignments.append("name = ?")
            values.append(name)
        if group is not None:
            assignments.append("group_name = ?")
            values.append(group)
        if not assignments:
            return False

        with self.connect() as connection:
            exists = row_as_dict(
                connection.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
            )
            if not exists:
                return False
            values.append(agent_id)
            connection.execute(
                f"UPDATE agents SET {', '.join(assignments)} WHERE id = ?", values
            )
        return True

    def delete_agent(self, agent_id: str) -> bool:
        with self.connect() as connection:
            exists = row_as_dict(
                connection.execute("SELECT id FROM agents WHERE id = ?", (agent_id,))
            )
            if not exists:
                return False
            connection.execute("DELETE FROM metrics WHERE agent_id = ?", (agent_id,))
            connection.execute("DELETE FROM agents WHERE id = ?", (agent_id,))
        return True

    def get_metrics(self, agent_id: str, minutes: int) -> list[dict[str, Any]] | None:
        cutoff = to_db_timestamp(utc_now() - timedelta(minutes=minutes))
        with self.connect() as connection:
            exists = row_as_dict(
                connection.execute(
                    "SELECT id FROM agents WHERE id = ?", (agent_id,)
                )
            )
            if not exists:
                return None
            bucket_s = max(1, -(-minutes * 60 // MAX_CHART_POINTS))
            rows = rows_as_dicts(
                connection.execute(
                    """
                    SELECT MAX(received_at) AS received_at,
                           MAX(agent_timestamp) AS agent_timestamp,
                           AVG(cpu_percent) AS cpu_percent,
                           AVG(memory_percent) AS memory_percent,
                           AVG(disk_percent) AS disk_percent,
                           AVG(temp_c) AS temp_c,
                           MAX(net_bytes_sent) AS net_bytes_sent,
                           MAX(net_bytes_recv) AS net_bytes_recv,
                           MAX(uptime_s) AS uptime_s
                    FROM metrics
                    WHERE agent_id = ? AND received_at >= ?
                    GROUP BY CAST(strftime('%s', received_at) AS INTEGER) / ?
                    ORDER BY received_at ASC
                    """,
                    (agent_id, cutoff, bucket_s),
                )
            )
        return [self._metric_from_row(row) for row in rows]

    @staticmethod
    def _network_rates(row: dict[str, Any]) -> dict[str, float | None]:
        """Velocidade de rede (Mbps) entre as duas últimas medições do agente.

        Os contadores de bytes são acumulados; se diminuíram (reinício do
        computador) ou não há medição anterior, a velocidade fica indefinida.
        """
        rates: dict[str, float | None] = {"net_recv_mbps": None, "net_sent_mbps": None}
        if not row["received_at"] or not row["prev_received_at"]:
            return rates
        elapsed = (
            from_db_timestamp(row["received_at"]) - from_db_timestamp(row["prev_received_at"])
        ).total_seconds()
        if elapsed <= 0:
            return rates
        for key, now_bytes, before_bytes in (
            ("net_recv_mbps", row["net_bytes_recv"], row["prev_net_recv"]),
            ("net_sent_mbps", row["net_bytes_sent"], row["prev_net_sent"]),
        ):
            if now_bytes >= before_bytes:
                rates[key] = (now_bytes - before_bytes) * 8 / elapsed / 1_000_000
        return rates

    @staticmethod
    def _metric_from_row(row: dict[str, Any]) -> dict[str, Any]:
        return {
            "received_at": from_db_timestamp(row["received_at"]),
            "agent_timestamp": from_db_timestamp(row["agent_timestamp"]),
            "cpu_percent": row["cpu_percent"],
            "memory_percent": row["memory_percent"],
            "disk_percent": row["disk_percent"],
            "temp_c": row["temp_c"],
            "net_bytes_sent": row["net_bytes_sent"],
            "net_bytes_recv": row["net_bytes_recv"],
            "uptime_s": row["uptime_s"],
        }
