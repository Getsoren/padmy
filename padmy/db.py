from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field

import asyncpg
from rich.table import Table as RTable

from padmy.config import Config, ConfigTable, ConfigSchema
from padmy.logs import logs
from padmy.utils import get_first, get_conn, PgConnection
from padmy.env import CONSOLE

if sys.version_info < (3, 11):
    from typing_extensions import Self
else:
    from typing import Self


def _get_full_name(schema: str | None, table: str | None) -> str:
    if schema is None or table is None:
        raise ValueError("schema and table must not be empty")
    return f"{schema}.{table}"


@dataclass
class FKConstraint:
    column_names: list[str]
    constraint_name: str

    # references
    foreign_schema: str
    foreign_table: str
    foreign_column_names: list[str]

    table: str | None = None
    schema: str | None = None

    @property
    def foreign_full_name(self):
        return _get_full_name(self.foreign_schema, self.foreign_table)

    @property
    def full_name(self):
        return _get_full_name(self.schema, self.table)


@dataclass
class PKConstraint:
    column_name: str
    table: str
    schema: str

    @property
    def full_name(self):
        return _get_full_name(self.schema, self.table)


@dataclass
class Column:
    name: str
    is_generated: bool


@dataclass(eq=False)
class Table:
    schema: str
    table: str

    columns: list[Column] | None = None

    _count: int | None = None  # field(init=False)

    foreign_keys: list[FKConstraint] = field(default_factory=list)
    primary_keys: list[PKConstraint] = field(default_factory=list)

    parent_tables: set[Self] = field(default_factory=set)
    child_tables: set[Self] = field(default_factory=set)

    # Has already been sampled, and it's temporary table has been created
    has_been_processed: bool = False

    # Sample size
    sample_size: int | None = None
    # Ignore this table
    ignore: bool = False

    @property
    def parent_tables_safe(self):
        # return self.parent_tables - {self}
        return {x for x in self.parent_tables if x.full_name != self.full_name and not x.ignore}

    @property
    def child_tables_safe(self):
        # Returns the child table that are not the current table
        return {x for x in self.child_tables if x.full_name != self.full_name and not x.ignore}

    @property
    def full_name(self):
        return _get_full_name(self.schema, self.table)

    @property
    def tmp_name(self):
        return f"_{self.schema}_{self.table}_tmp"

    @property
    def has_parent(self):
        return len(self.parent_tables_safe) > 0

    @property
    def has_children(self):
        return len(self.child_tables_safe) > 0

    @property
    def children_has_been_processed(self):
        return all(_child.has_been_processed for _child in self.child_tables_safe)

    @property
    def count(self) -> int:
        if self._count is None:
            raise ValueError("Count must be loaded first")
        return self._count

    @count.setter
    def count(self, v: int):
        self._count = v

    @property
    def insert_columns(self) -> list[str]:
        if self.columns is None:
            raise ValueError("Columns must be loaded first")
        return sorted(x.name for x in self.columns if not x.is_generated)

    def get_values(self, table: str | None = None):
        _table = f"{table}." if table is not None else ""
        return ", ".join(f'{_table}"{name}"' for name in self.insert_columns)

    @property
    def values(self):
        return self.get_values()

    @property
    def is_leaf(self):
        return not self.has_children and self.has_parent

    @property
    def is_root(self):
        return not self.has_parent

    async def load_count(self, conn: asyncpg.Connection):
        self._count = await conn.fetchval(f"SELECT count(*) from {self.full_name}")

    def __eq__(self, other: object):
        return self.__hash__() == other.__hash__()

    def __hash__(self):
        return hash((self.full_name, self.has_been_processed))

    def __repr__(self):
        return (
            f"Table(full_name={self.full_name!r} "
            f"count={self._count} "
            f"foreign_keys={len(self.foreign_keys)} "
            f"parents={len(self.parent_tables)} "
            f"children={len(self.child_tables)}"
            f")"
        )

    def __post_init__(self):
        for fk in self.foreign_keys:
            fk.table = self.table
            fk.schema = self.schema


async def get_tables(conn: PgConnection, schemas: list[str]):
    query = """
    SELECT 
    table_schema AS schema, 
    table_name AS table
    FROM information_schema.tables
    WHERE table_schema = ANY ($1::TEXT[]) AND
    table_type = 'BASE TABLE'
    ORDER BY table_schema, table_name
    """
    data = await conn.fetch(query, schemas)
    return [Table(**x) for x in data]


async def get_columns(conn: PgConnection, tables: list[Table]) -> dict[str, list[Column]]:
    query = """
    SELECT full_name,
           JSON_AGG(
                   JSON_BUILD_OBJECT(
                           'name', column_name,
                           'is_generated', COALESCE(generation_expression IS NOT NULL OR identity_generation = 'ALWAYS', FALSE)
                       )
               ) AS columns
    FROM (SELECT *, table_schema || '.' || table_name AS full_name FROM information_schema.columns) t
    WHERE full_name = ANY ($1::TEXT[])
    GROUP BY full_name
    """
    data = await conn.fetch(query, [x.full_name for x in tables])
    columns = {}
    for x in data:
        columns[x["full_name"]] = [Column(**col) for col in x["columns"]]
    return columns


SCHEMA_FK_QUERY = """
SELECT c.conname                                         AS constraint_name,
       sch.nspname                                       AS schema_name,
       tbl.relname                                       AS table_name,
       ARRAY_AGG(col.attname ORDER BY u.attposition)     AS column_names,
       f_sch.nspname                                     AS foreign_schema_name,
       f_tbl.relname                                     AS foreign_table_name,
       ARRAY_AGG(f_col.attname ORDER BY f_u.attposition) AS foreign_column_names
FROM pg_constraint c
         LEFT JOIN LATERAL UNNEST(c.conkey) WITH ORDINALITY AS u(attnum, attposition) ON TRUE
         LEFT JOIN LATERAL UNNEST(c.confkey) WITH ORDINALITY AS f_u(attnum, attposition)
                   ON f_u.attposition = u.attposition
         JOIN pg_class tbl ON tbl.oid = c.conrelid
         JOIN pg_namespace sch ON sch.oid = tbl.relnamespace
         LEFT JOIN pg_attribute col ON (col.attrelid = tbl.oid AND col.attnum = u.attnum)
         LEFT JOIN pg_class f_tbl ON f_tbl.oid = c.confrelid
         LEFT JOIN pg_namespace f_sch ON f_sch.oid = f_tbl.relnamespace
         LEFT JOIN pg_attribute f_col ON (f_col.attrelid = f_tbl.oid AND f_col.attnum = f_u.attnum)
WHERE c.contype = 'f' 
    AND sch.nspname = ANY ($1::TEXT[]) 
GROUP BY constraint_name, "schema_name", "table_name", f_sch.nspname, f_tbl.relname
ORDER BY "schema_name", "table_name", "constraint_name";
"""

SCHEMA_PK_QUERY = """
SELECT 
    tc.table_schema AS schema, 
    tc.table_name AS table, 
    c.column_name
FROM information_schema.table_constraints tc
JOIN information_schema.constraint_column_usage AS ccu USING (constraint_schema, constraint_name)
JOIN information_schema.columns AS c ON c.table_schema = tc.constraint_schema
  AND tc.table_name = c.table_name AND ccu.column_name = c.column_name
WHERE constraint_type = 'PRIMARY KEY' AND tc.table_schema = ANY ($1::TEXT[]);
"""


async def load_foreign_keys(conn: PgConnection, schemas: list[str]):
    data = await conn.fetch(SCHEMA_FK_QUERY, schemas)
    return [
        FKConstraint(
            column_names=x["column_names"],
            constraint_name=x["constraint_name"],
            foreign_schema=x["foreign_schema_name"],
            foreign_table=x["foreign_table_name"],
            foreign_column_names=x["foreign_column_names"],
            table=x["table_name"],
            schema=x["schema_name"],
        )
        for x in data
    ]


async def load_primary_keys(conn: PgConnection, schemas: list[str]):
    data = await conn.fetch(SCHEMA_PK_QUERY, schemas)
    return [PKConstraint(**x) for x in data]


# format_type gives a valid SQL type expression (composite types, arrays, varchar(n), ...),
# unlike information_schema.columns.data_type which yields literals like 'USER-DEFINED'
GET_COLUMNS_TYPE_QUERY = """
SELECT a.attname                            AS column_name,
       format_type(a.atttypid, a.atttypmod) AS data_type,
       CASE
           WHEN a.atttypid IN ('varchar'::regtype, 'bpchar'::regtype) AND a.atttypmod > 4
               THEN a.atttypmod - 4
           END                              AS max_length
FROM pg_catalog.pg_attribute a
JOIN pg_catalog.pg_class c ON c.oid = a.attrelid
JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = $1
  AND c.relname = $2
  AND a.attname = ANY ($3::TEXT[])
  AND a.attnum > 0
  AND NOT a.attisdropped
"""


@dataclass
class ColumnType:
    sql_type: str
    # character maximum length for varchar(n)/char(n) columns, None otherwise
    max_length: int | None = None


async def load_columns_type(conn: asyncpg.Connection, schema: str, table: str, columns: list[str]):
    data = await conn.fetch(GET_COLUMNS_TYPE_QUERY, schema, table, columns)
    return {x["column_name"]: ColumnType(sql_type=x["data_type"], max_length=x["max_length"]) for x in data}


@dataclass
class Database:
    name: str
    tables: list[Table] = field(default_factory=list)

    async def explore(
        self,
        pool: asyncpg.Pool,
        schemas: list[str] | None = None,
        *,
        load_count: bool = True,
    ):
        _schemas = schemas or list(set(x.schema for x in self.tables))
        if not _schemas:
            raise ValueError("No schemas to explore")

        _schemas_str = ", ".join(_schemas)
        async with pool.acquire() as conn:
            logs.debug(f"Loading tables for {_schemas_str}...")
            self.tables = await get_tables(conn, _schemas)
            logs.debug(f"{len(self.tables)} tables found")

            logs.debug("Loading table columns...")
            columns = await get_columns(conn, self.tables)
            for table in self.tables:
                table.columns = columns[table.full_name]
            logs.debug("Loading foreign keys...")
            fks = await load_foreign_keys(conn, _schemas)
            logs.debug(f"{len(fks)} tables found")
            logs.debug("Loading primary keys...")
            pks = await load_primary_keys(conn, _schemas)
            logs.debug(f"{len(pks)} primary keys found")

        _tables: dict[str, Table] = {_table.full_name: _table for _table in self.tables}

        for _pk in pks:
            _tables[_pk.full_name].primary_keys.append(_pk)

        for _fk in fks:
            # Skip foreign keys that reference tables outside the analyzed schemas
            if _fk.foreign_full_name not in _tables:
                logs.debug(
                    f"Skipping foreign key from {_fk.full_name} to {_fk.foreign_full_name} (target table not in analyzed schemas)"
                )
                continue

            _tables[_fk.full_name].foreign_keys.append(_fk)
            _tables[_fk.full_name].parent_tables.add(_tables[_fk.foreign_full_name])
            _tables[_fk.foreign_full_name].child_tables.add(_tables[_fk.full_name])

        if load_count:
            logs.info("Counting number of rows in tables...")
            await asyncio.gather(*[get_conn(pool, table.load_count) for table in self.tables])

    def load_config(self, config: Config):
        """
        Loads the sample sizes for each tables from the config file.
        Tables need to have been loaded first
        """
        if not self.tables:
            raise ValueError("Tables must be loaded first")

        _schemas: dict[str, ConfigSchema] = {schema.schema: schema for schema in config.schemas}
        _tables: dict[str, ConfigTable] = {f"{_table.schema}.{_table.table}": _table for _table in config.tables}
        for _table in self.tables:
            _config_schema = _schemas.get(_table.schema)
            _config_table = _tables.get(_table.full_name)
            _sample = get_first(
                _config_table.sample if _config_table is not None else None,
                _config_schema.sample if _config_schema is not None else None,
                config.sample,
                fn=lambda x: x is not None,
            )
            if _sample is None:
                raise ValueError("_sample must not be empty")
            _table.sample_size = int(_sample)
            if _config_table is not None:
                _table.ignore = _config_table.ignore


def pretty_print_stats(database: Database):
    table = RTable(title=f"Stats for {database.name}")

    table.add_column("Table", justify="left", style="cyan")
    table.add_column("Count", justify="right", style="green")
    table.add_column("# FKs", justify="right", style="magenta")
    table.add_column("# Parents", justify="right", style="magenta")
    table.add_column("# Children", justify="right", style="magenta")

    for _table in sorted(database.tables, key=lambda x: x.full_name):
        table.add_row(
            _table.full_name,
            str(_table.count),
            str(len(_table.foreign_keys)),
            str(len(_table.parent_tables)),
            str(len(_table.child_tables)),
        )

    CONSOLE.print(table)


def pprint_compared_dbs(db_1: Database, db_2: Database):
    table = RTable(title=f"Comparing {db_1.name} and {db_2.name}")

    table.add_column("Table", justify="left", style="blue")
    table.add_column(f"Count {db_1.name!r}", justify="right", style="cyan")
    table.add_column(f"Count {db_2.name!r}", justify="right", style="cyan")
    table.add_column("Diff", justify="right", style="green")

    tables_1, tables_2 = sorted(db_1.tables, key=lambda x: x.full_name), sorted(db_2.tables, key=lambda x: x.full_name)

    for _table1, _table2 in zip(tables_1, tables_2):
        perc_diff = 100 if _table2.count == 0 else int(_table2.count * 100 / _table1.count)
        if perc_diff > 0:
            pass
        elif perc_diff < 0:
            pass
        else:
            pass
        table.add_row(_table1.full_name, str(_table1.count), str(_table2.count), f"{perc_diff}%")

    CONSOLE.print(table)
