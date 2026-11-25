"""Copia os dados do banco atual (Turso ou arquivo SQLite) para o PostgreSQL.

Copia: os hosts (agents), os dispositivos (devices) e as medições (metrics) dos últimos N dias.
Pode rodar mais de uma vez: hosts e dispositivos que já existem no destino são mantidos como estão.
As medições são acrescentadas, então rode só UMA vez (ou apague a tabela metrics do destino antes).

Uso (na pasta monitor/api, com o ambiente da API instalado):

    # origem = Turso (variáveis de ambiente do Turso)
    set TURSO_DATABASE_URL=libsql://...
    set TURSO_AUTH_TOKEN=...
    python migrate_to_postgres.py --destino postgresql://usuario:senha@host:5432/monitor

    # origem = arquivo SQLite
    python migrate_to_postgres.py --origem-sqlite monitor.db --destino postgresql://...

Opções: --dias N (histórico copiado, padrão 2; 0 = só hosts e dispositivos).
Os hosts e dispositivos vão por inteiro. Um histórico curto basta: os gráficos voltam a se encher sozinhos.
"""

import argparse
import os
import sys
from datetime import timedelta

from db import Database, rows_as_dicts, to_db_timestamp, utc_now
from devices import Devices

# Quantas linhas vão por comando ao copiar as medições.
BATCH = 500

METRIC_COLUMNS = (
    "agent_id", "received_at", "agent_timestamp", "cpu_percent", "memory_percent", "disk_percent",
    "temp_c", "net_bytes_sent", "net_bytes_recv", "uptime_s", "volumes",
)
AGENT_COLUMNS = (
    "id", "group_name", "name", "created_at", "last_seen", "hostname", "os",
    "expected_interval_s", "system_info",
)
DEVICE_COLUMNS = (
    "id", "name", "kind", "address", "group_name", "probe_id", "method", "port", "status",
    "latency_ms", "last_checked_at", "status_since", "created_at",
)


def build_source(sqlite_path: str | None) -> Database:
    # Origem: o arquivo SQLite informado ou, sem ele, o Turso das variáveis de ambiente.
    if sqlite_path:
        return Database(sqlite_path)
    url = os.getenv("TURSO_DATABASE_URL", "").strip()
    token = os.getenv("TURSO_AUTH_TOKEN", "").strip()
    if not url or not token:
        sys.exit("Informe a origem: --origem-sqlite ARQUIVO ou TURSO_DATABASE_URL + TURSO_AUTH_TOKEN.")
    return Database("-", turso_url=url, turso_auth_token=token)


def copy_rows(destination: Database, table: str, columns: tuple[str, ...], rows: list[dict], conflict: str) -> int:
    """Insere as linhas no destino, em lotes de um comando só. `conflict` é a cláusula ON CONFLICT."""
    placeholders = "(" + ", ".join(["?"] * len(columns)) + ")"
    copied = 0
    with destination.connect() as connection:
        for start in range(0, len(rows), BATCH):
            batch = rows[start:start + BATCH]
            values: list = []
            for row in batch:
                values.extend(row[column] for column in columns)
            connection.execute(
                f"INSERT INTO {table} ({', '.join(columns)}) VALUES {', '.join([placeholders] * len(batch))} {conflict}",
                values,
            )
            copied += len(batch)
    return copied


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--destino", required=True, help="URL do PostgreSQL (postgresql://usuario:senha@host:5432/banco)")
    parser.add_argument("--origem-sqlite", help="Arquivo SQLite de origem (sem isso, usa o Turso das variáveis de ambiente)")
    parser.add_argument("--dias", type=int, default=2, help="Dias de medições a copiar (padrão 2)")
    args = parser.parse_args()

    source = build_source(args.origem_sqlite)
    destination = Database("-", database_url=args.destino)
    destination.initialize()  # cria as tabelas de hosts e medições, se ainda não existirem
    Devices(destination).initialize()  # e a de dispositivos

    with source.connect() as connection:
        agents = rows_as_dicts(connection.execute(f"SELECT {', '.join(AGENT_COLUMNS)} FROM agents"))
        try:
            devices = rows_as_dicts(connection.execute(f"SELECT {', '.join(DEVICE_COLUMNS)} FROM devices"))
        except Exception:  # origem anterior à função de dispositivos
            devices = []
        metrics: list[dict] = []
        if args.dias > 0:
            cutoff = to_db_timestamp(utc_now() - timedelta(days=args.dias))
            metrics = rows_as_dicts(
                connection.execute(
                    f"SELECT {', '.join(METRIC_COLUMNS)} FROM metrics WHERE received_at >= ? ORDER BY received_at",
                    (cutoff,),
                )
            )

    print(f"Origem: {len(agents)} hosts, {len(devices)} dispositivos, {len(metrics)} medições.")
    done = copy_rows(destination, "agents", AGENT_COLUMNS, agents, "ON CONFLICT (id) DO NOTHING")
    print(f"Hosts enviados: {done}")
    done = copy_rows(destination, "devices", DEVICE_COLUMNS, devices, "ON CONFLICT (id) DO NOTHING")
    print(f"Dispositivos enviados: {done}")
    # Só medições de hosts que existem (a chave estrangeira exige).
    known = {agent["id"] for agent in agents}
    done = copy_rows(destination, "metrics", METRIC_COLUMNS, [m for m in metrics if m["agent_id"] in known], "")
    print(f"Medições enviadas: {done}")
    print("Concluído. Confira no painel e, só depois, aponte os agentes para o servidor novo.")


if __name__ == "__main__":
    main()
