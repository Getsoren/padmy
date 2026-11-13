import textwrap
from typing import LiteralString

import pytest
from tracktolib.pg_sync import fetch_all, insert_many

from padmy.config import AnoFields


@pytest.mark.parametrize(
    "table,pks,fields,field_types,constants,expected",
    [
        pytest.param(
            "public.test",
            ["id1"],
            ["field_1"],
            {"id1": "text", "field_1": "integer"},
            None,
            """
    UPDATE public.test AS u
    SET
      field_1 = u2.field_1
    FROM unnest($1::TEXT[], $2::INTEGER[]) AS u2(id1, field_1)
    WHERE u2.id1 = u.id1
    """,
            id="One PK",
        ),
        pytest.param(
            "public.test",
            ["id1", "id2"],
            ["field_1", "field_2"],
            {"id1": "text", "id2": "text", "field_1": "integer", "field_2": "text"},
            {"hash": "NULL"},
            """
    UPDATE public.test AS u
    SET
      field_1 = u2.field_1, field_2 = u2.field_2, hash = NULL
    FROM unnest($1::TEXT[], $2::TEXT[], $3::INTEGER[], $4::TEXT[]) AS u2(id1, id2, field_1, field_2)
    WHERE u2.id1 = u.id1 AND u2.id2 = u.id2
    """,
            id="Multiple PKs + constant",
        ),
        pytest.param(
            "public.test",
            ["id1"],
            ["data__name", "data__code"],
            {"id1": "text", "data__name": "text", "data__code": "text"},
            None,
            """
    UPDATE public.test AS u
    SET
      data = jsonb_set(jsonb_set(u.data, '{name}', coalesce(to_jsonb(u2.data__name), 'null'), false), '{code}', coalesce(to_jsonb(u2.data__code), 'null'), false)
    FROM unnest($1::TEXT[], $2::TEXT[], $3::TEXT[]) AS u2(id1, data__name, data__code)
    WHERE u2.id1 = u.id1
    """,
            id="Two keys of one jsonb column",
        ),
    ],
)
def test_get_update_query(table, pks, fields, field_types, constants, expected):
    from padmy.anonymize.anonymize import get_update_query

    json_paths = {f: tuple(f.split("__")) for f in fields if "__" in f}
    query = get_update_query(table, pks, fields, field_types, constants=constants, json_paths=json_paths)
    assert textwrap.dedent(query).strip().lower() == textwrap.dedent(expected).strip().lower()


@pytest.fixture()
def add_table_1_data(engine):
    data = [{"id": 1, "foo": "foo_1"}, {"id": 2, "foo": "foo_2"}]
    engine.execute("TRUNCATE public.table_1 CASCADE")
    insert_many(engine, "public.table_1", data)
    yield
    engine.execute("TRUNCATE public.table_1 CASCADE")
    engine.commit()


@pytest.mark.usefixtures("add_table_1_data")
def test_anonymize_table(aengine, loop, engine, faker):
    from padmy.anonymize.anonymize import anonymize_table
    from padmy.config import ConfigTable, AnoFields

    table = ConfigTable("public", "table_1", fields=[AnoFields.load({"foo": "EMAIL"})])

    async def test():
        await anonymize_table(aengine, table, ["id"], faker)

    loop.run_until_complete(test())

    db = fetch_all(engine, "SELECT * FROM public.table_1 ORDER BY id")

    assert db == [
        {"id": 1, "foo": "achang@example.org"},
        {"id": 2, "foo": "tammy76@example.com"},
    ]


@pytest.fixture()
def setup_ano_types_table(engine):
    engine.execute(
        """
        DROP TABLE IF EXISTS public.ano_types;
        DROP TYPE IF EXISTS public.ano_address;
        CREATE TYPE public.ano_address AS (street TEXT, zipcode TEXT);
        CREATE TABLE public.ano_types
        (
            id    SERIAL PRIMARY KEY,
            addr  public.ano_address,
            phone VARCHAR(15),
            data  JSONB,
            phone_len INT GENERATED ALWAYS AS (length(phone)) STORED
        );
        """
    )
    insert_many(engine, "public.ano_types", [{"id": i, "phone": "0611223344"} for i in range(5)])
    engine.execute("""UPDATE public.ano_types SET data = '{"name": "Doe John", "keep": "x"}'""")
    engine.execute("UPDATE public.ano_types SET addr = ('street', 'zip')::public.ano_address")
    engine.commit()
    yield
    engine.execute("DROP TABLE IF EXISTS public.ano_types; DROP TYPE IF EXISTS public.ano_address")
    engine.commit()


@pytest.mark.usefixtures("setup_ano_types_table")
def test_load_columns_type(aengine, loop):
    """format_type-based introspection returns castable types (not 'USER-DEFINED') and varchar max lengths."""
    from padmy.db import load_columns_type, ColumnType

    types = loop.run_until_complete(load_columns_type(aengine, "public", "ano_types", ["id", "addr", "phone"]))
    assert types == {
        "id": ColumnType("integer"),
        "addr": ColumnType("ano_address"),
        "phone": ColumnType("character varying(15)", max_length=15),
    }


@pytest.mark.usefixtures("setup_ano_types_table")
def test_anonymize_table_composite_and_max_length(aengine, loop, engine, faker):
    """Composite-typed columns get a valid cast and generated values are truncated to fit varchar(n)."""
    from padmy.anonymize.anonymize import anonymize_table
    from padmy.config import ConfigTable, AnoFields

    table = ConfigTable(
        "public",
        "ano_types",
        fields=[
            AnoFields(column="addr", type="NULL"),
            AnoFields(column="phone", type="PHONE_NUMBER"),
        ],
    )

    loop.run_until_complete(anonymize_table(aengine, table, ["id"], faker))

    db = fetch_all(engine, "SELECT addr, phone FROM public.ano_types")
    assert len(db) == 5
    assert all(x["addr"] is None for x in db)
    assert all(x["phone"] and len(x["phone"]) <= 15 and x["phone"] != "0611223344" for x in db)


@pytest.mark.parametrize(
    "field_type, extra, predicate",
    [
        pytest.param("EMAIL", None, lambda v: v and "@" in v, id="EMAIL"),
        pytest.param("NULL", None, lambda v: v is None, id="NULL"),
        pytest.param("FIRST_NAME", None, lambda v: isinstance(v, str) and v, id="FIRST_NAME"),
        pytest.param("LAST_NAME", None, lambda v: isinstance(v, str) and v, id="LAST_NAME"),
        pytest.param("NAME", None, lambda v: isinstance(v, str) and " " in v, id="NAME"),
        pytest.param("PHONE_NUMBER", None, lambda v: isinstance(v, str) and v, id="PHONE_NUMBER"),
        pytest.param("NUMERIFY", {"text": "06########"}, lambda v: len(v) == 10 and v.isdigit(), id="NUMERIFY"),
        pytest.param("WORD", None, lambda v: isinstance(v, str) and v, id="WORD"),
    ],
)
def test_get_fake_value(faker, field_type, extra, predicate):
    """Each supported field type returns something matching its shape."""
    from padmy.anonymize.anonymize import _get_fake_value

    value = _get_fake_value(faker, field_type, extra)
    assert predicate(value), f"unexpected value for {field_type}: {value!r}"


@pytest.mark.parametrize(
    "fields, where, untouched_ids",
    [
        pytest.param([AnoFields(column="addr", type="NULL")], None, set(), id="constant only, set-based"),
        pytest.param([AnoFields(column="addr", type="NULL")], "id < 2", {2, 3, 4}, id="constant only, where filter"),
        pytest.param(
            [AnoFields(column="phone", type="NUMERIFY", extra_args={"text": "06########"})],
            "id <> 0",
            {0},
            id="faker, where filter",
        ),
        pytest.param([AnoFields(column="data", type="NAME", path="name")], "id <> 0", {0}, id="json key, where filter"),
        pytest.param([AnoFields(column="data", type="NULL", path="name")], "id <> 0", {0}, id="json key to null"),
    ],
)
@pytest.mark.usefixtures("setup_ano_types_table")
def test_anonymize_table_where(aengine, loop, engine, faker, fields, where, untouched_ids):
    """`where` keeps the rows it excludes untouched on both the set-based and the faker path."""
    from padmy.anonymize.anonymize import anonymize_table
    from padmy.config import ConfigTable

    table = ConfigTable("public", "ano_types", fields=fields, where=where)
    loop.run_until_complete(anonymize_table(aengine, table, ["id"], faker))

    untouched_queries: dict[str, LiteralString] = {
        "addr": "SELECT id FROM public.ano_types WHERE addr::text = '(street,zip)'",
        "phone": "SELECT id FROM public.ano_types WHERE phone = '0611223344'",
        "data": "SELECT id FROM public.ano_types WHERE data ->> 'name' = 'Doe John'",
    }
    db = fetch_all(engine, untouched_queries[fields[0].column])
    assert {x["id"] for x in db} == untouched_ids
    keys = fetch_all(engine, "SELECT DISTINCT data ->> 'keep' AS keep, data ? 'name' AS has_name FROM public.ano_types")
    assert keys == [{"keep": "x", "has_name": True}], "a json key rewrite keeps the other keys and the key itself"


@pytest.mark.parametrize(
    "keep, kept_ids",
    [
        pytest.param("id < 2", {0, 1}, id="filter"),
        pytest.param("false", set(), id="empty the table"),
        pytest.param(
            "id IN (SELECT id FROM (SELECT id, row_number() OVER (ORDER BY id DESC) rn FROM public.ano_types) s WHERE rn <= 2)",
            {3, 4},
            id="latest n rows",
        ),
    ],
)
@pytest.mark.usefixtures("setup_ano_types_table")
def test_anonymize_db_keep(apool, engine, loop, faker, keep, kept_ids):
    """`keep` runs before anonymizing, even on a table without fields, and generated columns come back."""
    from padmy.anonymize import anonymize_db
    from padmy.config import Config, ConfigTable

    loop.run_until_complete(anonymize_db(apool, Config(tables=[ConfigTable("public", "ano_types", keep=keep)]), faker))

    db = fetch_all(engine, "SELECT id, phone_len FROM public.ano_types")
    assert {x["id"] for x in db} == kept_ids
    assert all(x["phone_len"] == 10 for x in db), "generated columns are recomputed on the rows put back"


def test_gen_mock_data_unique(faker):
    """unique fields never repeat a value, even across chunks (see users_email_key incident)."""
    from padmy.anonymize.anonymize import gen_mock_data
    from padmy.config import AnoFields

    fields = [AnoFields(column="email", type="EMAIL", unique=True)]
    values = [row["email"] for _ in range(5) for row in gen_mock_data(faker, fields=fields, size=200)]
    assert len(set(values)) == len(values)


def test_get_fake_value_unknown_type_raises(faker):
    from padmy.anonymize.anonymize import _get_fake_value

    with pytest.raises(ValueError, match="unimplemented field type"):
        _get_fake_value(faker, "DOES_NOT_EXIST")  # type: ignore[arg-type]


@pytest.mark.usefixtures("add_table_1_data")
def test_anonymize_db(apool, engine, loop, faker):
    from padmy.anonymize import anonymize_db
    from padmy.config import Config, ConfigTable, AnoFields

    config = Config(
        tables=[
            ConfigTable(
                "public",
                "table_1",
                fields=[
                    AnoFields(
                        column="foo",
                        type="EMAIL",
                        extra_args={"domain": "my-domain.fr"},
                    )
                ],
            )
        ]
    )

    async def test():
        await anonymize_db(apool, config, faker)

    loop.run_until_complete(test())

    db = fetch_all(engine, "SELECT foo, id FROM public.table_1 ORDER BY id")

    assert db == [
        {"foo": "achang@my-domain.fr", "id": 1},
        {"foo": "greenwilliam@my-domain.fr", "id": 2},
    ]


@pytest.mark.usefixtures("add_table_1_data")
def test_anonymize_db_surfaces_table_errors(apool, engine, loop, faker):
    """A failing table raises an explicit error and does not prevent the other tables from completing."""
    from padmy.anonymize import anonymize_db
    from padmy.config import Config, ConfigTable, AnoFields

    config = Config(
        tables=[
            ConfigTable("public", "table_1", fields=[AnoFields(column="foo", type="EMAIL")]),
            ConfigTable("public", "table_1", fields=[AnoFields(column="does_not_exist", type="EMAIL")]),
        ]
    )

    with pytest.raises(ExceptionGroup, match="Could not anonymize 1 table") as exc_info:
        loop.run_until_complete(anonymize_db(apool, config, faker))

    assert len(exc_info.value.exceptions) == 1

    db = fetch_all(engine, "SELECT foo FROM public.table_1")
    assert all("@" in x["foo"] for x in db)
