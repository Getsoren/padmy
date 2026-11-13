from __future__ import annotations

import asyncio
import contextlib
import itertools
import operator
from functools import partial
from typing import Any, Iterator, TYPE_CHECKING

import asyncpg


if TYPE_CHECKING:
    from faker import Faker
    from faker.proxy import UniqueProxy

from padmy.logs import logs
from ..config import Config, ConfigTable, FieldType, AnoFields
from ..db import load_primary_keys, load_columns_type
from ..utils import get_conn, iterate_pg

# Field types whose value does not depend on the row: applied in one set-based UPDATE
CONSTANT_TYPES: dict[FieldType, str] = {"NULL": "NULL"}


def get_update_query(
    table: str,
    pks: list[str],
    fields: list[str],
    field_types: dict,
    *,
    constants: dict[str, str] | None = None,
    json_paths: dict[str, tuple[str, str]] | None = None,
) -> str:
    """One UPDATE per chunk: generated values as unnest'ed arrays, `json_paths` keys via jsonb_set, constants inline."""
    _json_paths = json_paths or {}
    _table_keys = pks + fields
    _set = [f"{_field} = u2.{_field}" for _field in fields if _field not in _json_paths]
    _json_exprs: dict[str, str] = {}
    for _slot, (_column, _path) in _json_paths.items():
        _expr = _json_exprs.get(_column, f"u.{_column}")
        _json_exprs[_column] = f"jsonb_set({_expr}, '{{{_path}}}', coalesce(to_jsonb(u2.{_slot}), 'null'), false)"
    _set += [f"{_column} = {_expr}" for _column, _expr in _json_exprs.items()]
    _set += [f"{k} = {v}" for k, v in (constants or {}).items()]
    _arrays = ", ".join(f"${i + 1}::{field_types[k]}[]" for i, k in enumerate(_table_keys))
    _where = " AND ".join(f"u2.{_pk} = u.{_pk}" for _pk in pks)
    return f"""
    UPDATE {table} AS u
    SET
      {", ".join(_set)}
    FROM unnest({_arrays}) AS u2({", ".join(_table_keys)})
    WHERE {_where}
    """


def get_constant_query(table: str, constants: dict[str, str], where: str | None = None) -> str:
    """Set-based UPDATE for tables where every field is a constant (no row walk needed)."""
    _set = ", ".join(f"{k} = {v}" for k, v in constants.items())
    return f"UPDATE {table} SET {_set}" + (f" WHERE {where}" if where else "")


def _get_fake_value(faker: Faker | UniqueProxy, field: FieldType, extra_fields: dict | None = None) -> Any:
    _extra_fields = extra_fields or {}
    match field:
        case "EMAIL":
            return faker.email(**_extra_fields)
        case "NULL":
            return None
        case "FIRST_NAME":
            return faker.first_name(**_extra_fields)
        case "LAST_NAME":
            return faker.last_name(**_extra_fields)
        case "NAME":
            return faker.name(**_extra_fields)
        case "PHONE_NUMBER":
            return faker.phone_number(**_extra_fields)
        case "NUMERIFY":
            return faker.numerify(**_extra_fields)
        case "DATE_OF_BIRTH":
            return faker.date_of_birth(**_extra_fields)
        case "TEXT":
            return faker.text(**_extra_fields)
        case "WORD":
            return faker.word(**_extra_fields)
        case _:
            raise ValueError(f"Got unimplemented field type {field!r}")


def _as_text(value: Any) -> str | None:
    return None if value is None else str(value)


def gen_mock_data(faker: Faker, fields: list[AnoFields], size: int) -> Iterator[dict]:
    for _ in range(size):
        yield {v.slot: _get_fake_value(faker.unique if v.unique else faker, v.type, v.extra_args) for v in fields}


async def anonymize_table(
    conn: asyncpg.Connection,
    table: ConfigTable,
    pks: list[str],
    faker: Faker,
    *,
    chunk_size: int = 5_000,
):
    if table.fields is None:
        raise ValueError("Fields must not be empty")

    # A JSON key is always generated: a constant would need its own jsonb_set
    constant_fields = [x for x in table.fields if x.type in CONSTANT_TYPES and x.path is None]
    constants = {x.column: CONSTANT_TYPES[x.type] for x in constant_fields}
    generated = [x for x in table.fields if x not in constant_fields]

    if not generated:
        await conn.execute(get_constant_query(table.full_name, constants, table.where))
        return
    if not pks:
        raise ValueError(f"No PKs found for {table.full_name!r}")

    fields = [x.slot for x in generated]
    json_paths = {x.slot: (x.column, x.path) for x in generated if x.path is not None}
    columns = pks + [x.column for x in generated if x.path is None]
    columns_type = await load_columns_type(conn, table.schema, table.table, columns)
    sql_types = {_column: _type.sql_type for _column, _type in columns_type.items()} | dict.fromkeys(json_paths, "text")
    max_lengths = {_column: _type.max_length for _column, _type in columns_type.items() if _type.max_length is not None}
    update_query = get_update_query(table.full_name, pks, fields, sql_types, constants=constants, json_paths=json_paths)
    select_query = f"SELECT {', '.join(pks)} FROM {table.full_name}" + (f" WHERE {table.where}" if table.where else "")

    _warned: set[str] = set()

    def _fit(field: str, value: Any) -> Any:
        """Truncates generated strings to the column's max length (eg. phone numbers in varchar(15))."""
        _max_length = max_lengths.get(field)
        if _max_length is None or not isinstance(value, str) or len(value) <= _max_length:
            return value
        if field not in _warned:
            _warned.add(field)
            logs.warning(f"{table.full_name}.{field}: truncating generated values to {_max_length} chars")
        return value[:_max_length]

    async with conn.transaction():
        # aclosing so the cursor generator is closed on error while the connection is still usable
        async with contextlib.aclosing(iterate_pg(conn, select_query, chunk_size=chunk_size)) as chunks:
            async for chunk in chunks:
                mock_data = list(gen_mock_data(faker, fields=generated, size=len(chunk)))
                arrays = [[row[pk] for row in chunk] for pk in pks] + [
                    [_fit(f, _as_text(mock[f]) if f in json_paths else mock[f]) for mock in mock_data] for f in fields
                ]
                await conn.execute(update_query, *arrays)


# Columns an INSERT can write (generated ones are recomputed)
_INSERTABLE_COLUMNS_QUERY = """
SELECT attname FROM pg_attribute
WHERE attrelid = format('%I.%I', $1::text, $2::text)::regclass AND attnum > 0 AND NOT attisdropped AND attgenerated = ''
ORDER BY attnum
"""


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


async def keep_rows(conn: asyncpg.Connection, table: ConfigTable):
    """Keeps only the rows matching `table.keep`: copied aside, TRUNCATE, copied back (no DELETE on a big table)."""
    if table.keep is None:
        raise ValueError(f"No keep filter for {table.full_name!r}")
    _columns = ", ".join(
        _quote_ident(x["attname"]) for x in await conn.fetch(_INSERTABLE_COLUMNS_QUERY, table.schema, table.table)
    )
    async with conn.transaction():
        await conn.execute(
            f"CREATE TEMP TABLE padmy_keep ON COMMIT DROP AS SELECT {_columns} FROM {table.full_name} WHERE {table.keep}"
        )
        await conn.execute(f"TRUNCATE {table.full_name}")
        status = await conn.execute(
            f"INSERT INTO {table.full_name} ({_columns}) OVERRIDING SYSTEM VALUE SELECT {_columns} FROM padmy_keep"
        )
    logs.info(f"{table.full_name}: kept {status.split()[-1]} rows")


async def anonymize_db(pool: asyncpg.Pool, config: Config, faker: Faker):
    _tables = [_table for _table in config.tables if _table.has_ano_rules]
    if not _tables:
        logs.info("No tables found to anonymize in config file")
        return

    # keep first, so the anonymization walks only the rows left
    for _table in _tables:
        if _table.keep is not None:
            await get_conn(pool, partial(keep_rows, table=_table))

    _tables_to_anonymize = [_table for _table in _tables if _table.has_ano_fields]
    if not _tables_to_anonymize:
        return

    async with pool.acquire() as conn:
        pks = await load_primary_keys(conn, list({_table.schema for _table in _tables_to_anonymize}))

    _pks = {
        _table_name: list(_table_pks)
        for _table_name, _table_pks in itertools.groupby(pks, operator.attrgetter("full_name"))
    }

    async def _run(tables: list[ConfigTable]):
        # same-table entries run sequentially: concurrent UPDATEs on one table can deadlock
        for _table in tables:
            await get_conn(
                pool,
                partial(
                    anonymize_table,
                    table=_table,
                    pks=[x.column_name for x in _pks.get(_table.full_name, [])],
                    faker=faker,
                ),
            )

    groups: dict[str, list[ConfigTable]] = {}
    for _table in _tables_to_anonymize:
        groups.setdefault(_table.full_name, []).append(_table)

    # return_exceptions so one failing table does not cancel the others mid-query
    # (cancellation used to surface as a misleading InterfaceError on pool release)
    results = await asyncio.gather(*[_run(_tables) for _tables in groups.values()], return_exceptions=True)
    errors = {_name: _res for _name, _res in zip(groups, results) if isinstance(_res, BaseException)}
    for _table_name, _error in errors.items():
        logs.error(f"Could not anonymize {_table_name!r}: {_error!r}")
    if errors:
        raise BaseExceptionGroup(f"Could not anonymize {len(errors)} table(s)", list(errors.values()))
