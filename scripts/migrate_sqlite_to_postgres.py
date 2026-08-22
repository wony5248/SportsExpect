"""Copy an existing Dugout Lab SQLite database into an empty PostgreSQL schema."""

from __future__ import annotations

import argparse
from datetime import datetime
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from sqlalchemy import DateTime, create_engine, func, insert, select, text

from backend.app.config import KST, database_url_from_environment
from backend.app.database.base import Base
from backend.app.models import entities  # noqa: F401


def postgres_url(value: str) -> str:
    if value.startswith("postgres://"):
        return value.replace("postgres://", "postgresql+psycopg://", 1)
    if value.startswith("postgresql://"):
        return value.replace("postgresql://", "postgresql+psycopg://", 1)
    return value


def normalized_row(table, row) -> dict:
    """SQLite stored the application's KST-aware timestamps without a timezone suffix."""
    output = dict(row)
    for column in table.columns:
        value = output.get(column.name)
        if isinstance(column.type, DateTime) and column.type.timezone and isinstance(value, datetime) and not value.tzinfo:
            output[column.name] = value.replace(tzinfo=KST)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", default="data/baseball.db", help="Source SQLite file")
    parser.add_argument("--target", default=None, help="PostgreSQL URL; defaults to database environment variables")
    args = parser.parse_args()

    target_url = postgres_url(args.target or database_url_from_environment())
    if not target_url.startswith("postgresql+"):
        raise SystemExit("PostgreSQL target URL is required via --target or BASEBALL_DATABASE_URL")

    source = create_engine(f"sqlite:///{os.path.abspath(args.sqlite)}")
    target = create_engine(target_url, connect_args={"prepare_threshold": None}, pool_pre_ping=True)
    tables = Base.metadata.sorted_tables

    with target.begin() as connection:
        occupied = [table.name for table in tables if connection.scalar(select(func.count()).select_from(table))]
        if occupied:
            raise SystemExit(f"Target must be empty; rows already exist in: {', '.join(occupied)}")

    copied: dict[str, int] = {}
    with source.connect() as source_connection, target.begin() as target_connection:
        for table in tables:
            rows = [normalized_row(table, row) for row in source_connection.execute(select(table)).mappings()]
            if rows:
                target_connection.execute(insert(table), rows)
            copied[table.name] = len(rows)

        preparer = target.dialect.identifier_preparer
        for table in tables:
            if "id" not in table.c or not table.c.id.primary_key:
                continue
            quoted = preparer.quote(table.name)
            target_connection.execute(text(
                "select setval(pg_get_serial_sequence(:table_name, 'id'), "
                f"coalesce((select max(id) from {quoted}), 1), "
                f"exists(select 1 from {quoted}))"
            ), {"table_name": table.name})

    for name, count in copied.items():
        print(f"{name}: {count}")
    print("SQLite to PostgreSQL migration complete")


if __name__ == "__main__":
    main()
