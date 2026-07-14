import textwrap

import pytest
from tracktolib.pg_sync import fetch_all, insert_many


@pytest.mark.parametrize(
    "table,pks,fields,field_types,expected",
    [
        pytest.param(
            "public.test",
            ["id1"],
            ["field_1"],
            {"id1": "text", "field_1": "integer"},
            """
    UPDATE public.test AS u
    SET
      field_1 = u2.field_1
    FROM (VALUES
      ($1::TEXT, $2::INTEGER)
    ) AS u2(id1, field_1)
    WHERE u2.id1 = u.id1
    """,
            id="One PK",
        ),
        pytest.param(
            "public.test",
            ["id1", "id2"],
            ["field_1", "field_2"],
            {"id1": "text", "id2": "text", "field_1": "integer", "field_2": "text"},
            """
    UPDATE public.test AS u
    SET
      field_1 = u2.field_1, field_2 = u2.field_2
    FROM (VALUES
      ($1::TEXT, $2::TEXT, $3::INTEGER, $4::TEXT)
    ) AS u2(id1, id2, field_1, field_2)
    WHERE u2.id1 = u.id1 AND u2.id2 = u.id2
    """,
            id="Multiple PKs",
        ),
    ],
)
def test_get_update_query(table, pks, fields, field_types, expected):
    from padmy.anonymize.anonymize import get_update_query

    query = get_update_query(table, pks, fields, field_types)
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
            phone VARCHAR(15)
        );
        """
    )
    insert_many(engine, "public.ano_types", [{"id": i, "phone": "0611223344"} for i in range(5)])
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
        pytest.param("WORD", None, lambda v: isinstance(v, str) and v, id="WORD"),
    ],
)
def test_get_fake_value(faker, field_type, extra, predicate):
    """Each supported field type returns something matching its shape."""
    from padmy.anonymize.anonymize import _get_fake_value

    value = _get_fake_value(faker, field_type, extra)
    assert predicate(value), f"unexpected value for {field_type}: {value!r}"


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
