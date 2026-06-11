from __future__ import annotations

import asyncio
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


def get_update_query(table: str, pks: list[str], fields: list[str], field_types: dict):
    _table_keys = pks + fields
    _set_fields = ", ".join(f"{_field} = u2.{_field}" for _field in fields)
    _values = ", ".join(f"${i + 1}::{field_types[k]}" for i, k in enumerate(_table_keys))
    _where = " and ".join(f"u2.{_pk} = u.{_pk}" for _pk in pks)

    query = f"""
    UPDATE {table} as u
    SET
      {_set_fields}
    from (values
      ({_values})
    ) as u2({", ".join(_table_keys)})
    where {_where}
    """
    return query


def _get_fake_value(faker: Faker | UniqueProxy, field: FieldType, extra_fields: dict | None = None) -> Any:
    _extra_fields = extra_fields or {}
    match field:
        case "EMAIL":
            return faker.email(**_extra_fields)
        case "NULL":
            # Drop the column to NULL. Useful when no realistic placeholder is
            # wanted (password hashes, tokens, secrets) — the column shape is
            # preserved but the value carries no information.
            return None
        case "FIRST_NAME":
            return faker.first_name(**_extra_fields)
        case "LAST_NAME":
            return faker.last_name(**_extra_fields)
        case "NAME":
            return faker.name(**_extra_fields)
        case "PHONE_NUMBER":
            return faker.phone_number(**_extra_fields)
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


def dict_to_tuple(d: dict, fields: list[str]) -> tuple:
    return tuple(d[k] for k in fields)


async def anonymize_table(
    conn: asyncpg.Connection,
    table: ConfigTable,
    pks: list[str],
    faker: Faker,
    *,
    chunk_size: int = 1_000,
):
    if table.fields is None:
        raise ValueError("Fields must not be empty")
    if not pks:
        raise ValueError(f"No PKs found for {table.full_name!r}")

    query = f"SELECT {', '.join(pks)} from {table.schema}.{table.table}"

    fields = [x.column for x in table.fields]
    fields_types = await load_columns_type(conn, table.schema, table.table, pks + fields)
    update_query = get_update_query(table.full_name, pks, fields, fields_types)

    async with conn.transaction():
        async for chunk in iterate_pg(conn, query, chunk_size=chunk_size):
            mock_data = gen_mock_data(faker, fields=table.fields, size=chunk_size)
            new_data = [dict_to_tuple({**c, **m}, pks + fields) for c, m in zip(chunk, mock_data)]
            await conn.executemany(update_query, new_data)


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

    await asyncio.gather(
        *[
            get_conn(
                pool,
                partial(
                    anonymize_table,
                    table=_table,
                    pks=[x.column_name for x in _pks[_table.full_name]],
                    faker=faker,
                ),
            )
            for _table in _tables_to_anonymize
        ]
    )
