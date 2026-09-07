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
) -> str:
    """One UPDATE per chunk: generated columns come in as unnest'ed arrays, constants inline in SET."""
    _table_keys = pks + fields
    _set = [f"{_field} = u2.{_field}" for _field in fields] + [f"{k} = {v}" for k, v in (constants or {}).items()]
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


def gen_mock_data(faker: Faker, fields: list[AnoFields], size: int) -> Iterator[dict]:
    for _ in range(size):
        yield {v.column: _get_fake_value(faker.unique if v.unique else faker, v.type, v.extra_args) for v in fields}


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

    constants = {x.column: CONSTANT_TYPES[x.type] for x in table.fields if x.type in CONSTANT_TYPES}
    generated = [x for x in table.fields if x.type not in CONSTANT_TYPES]

    if not generated:
        await conn.execute(get_constant_query(table.full_name, constants, table.where))
        return
    if not pks:
        raise ValueError(f"No PKs found for {table.full_name!r}")

    fields = [x.column for x in generated]
    columns_type = await load_columns_type(conn, table.schema, table.table, pks + fields)
    sql_types = {_column: _type.sql_type for _column, _type in columns_type.items()}
    max_lengths = {_column: _type.max_length for _column, _type in columns_type.items() if _type.max_length is not None}
    update_query = get_update_query(table.full_name, pks, fields, sql_types, constants=constants)
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
                    [_fit(f, mock[f]) for mock in mock_data] for f in fields
                ]
                await conn.execute(update_query, *arrays)


async def anonymize_db(pool: asyncpg.Pool, config: Config, faker: Faker):
    _tables_to_anonymize = [_table for _table in config.tables if _table.has_ano_fields]

    if not _tables_to_anonymize:
        logs.info("No tables found to anonymize in config file")
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
