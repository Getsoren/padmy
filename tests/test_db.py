import pprint

import pytest

from .utils import pprint_dataclass_diff


@pytest.fixture()
def setup_tmp_schema(engine):
    for schema in ["cschema", "cschema2"]:
        engine.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        engine.execute(f"CREATE SCHEMA {schema}")
    engine.commit()
    yield
    # exec_req(engine, 'DROP SCHEMA cschema CASCADE')


NO_CONSTRAINTS_QUERY = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id  SERIAL PRIMARY KEY,
    foo TEXT
);

CREATE TABLE IF NOT EXISTS cschema.table_2
(
    id           SERIAL PRIMARY KEY,
    bar   TEXT
);
"""

ONE_CONSTRAINT_QUERY = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id  SERIAL PRIMARY KEY,
    foo TEXT
);

CREATE TABLE IF NOT EXISTS cschema.table_2
(
    id           SERIAL PRIMARY KEY,
    table_1_id   INT REFERENCES cschema.table_1
);
"""

MULTIPLE_CONSTRAINTS_QUERY = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id  SERIAL PRIMARY KEY,
    foo TEXT
);

CREATE TABLE IF NOT EXISTS cschema.table_2
(
    id           SERIAL PRIMARY KEY,
    table_1_id   INT REFERENCES cschema.table_1
);

CREATE TABLE IF NOT EXISTS cschema.table_3
(
    id           SERIAL PRIMARY KEY,
    table_1_id   INT REFERENCES cschema.table_1,
    table_2_id   INT REFERENCES cschema.table_2
);
"""

MULTIPLE_SCHEMA_QUERY = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id  SERIAL PRIMARY KEY,
    foo TEXT
);

CREATE TABLE IF NOT EXISTS cschema2.table_1
(
    id           SERIAL PRIMARY KEY,
    table_1_id   INT REFERENCES cschema.table_1
);
"""

DEFAULT = {
    "schema": "cschema",
    "foreign_schema": "cschema",
    "foreign_column_names": ["id"],
    "column_names": ["table_1_id"],
}

_FKS_PARAMS = [
    ("No table", None, []),
    ("No constraints", NO_CONSTRAINTS_QUERY, []),
    (
        "One constraint",
        ONE_CONSTRAINT_QUERY,
        [
            {
                **DEFAULT,
                "table": "table_2",
                "foreign_table": "table_1",
                "constraint_name": "table_2_table_1_id_fkey",
            }
        ],
    ),
    (
        "Multiple constraints",
        MULTIPLE_CONSTRAINTS_QUERY,
        [
            {
                **DEFAULT,
                "table": "table_2",
                "foreign_table": "table_1",
                "constraint_name": "table_2_table_1_id_fkey",
            },
            {
                **DEFAULT,
                "table": "table_3",
                "foreign_table": "table_1",
                "constraint_name": "table_3_table_1_id_fkey",
            },
            {
                **DEFAULT,
                "table": "table_3",
                "foreign_table": "table_2",
                "constraint_name": "table_3_table_2_id_fkey",
                "column_names": ["table_2_id"],
            },
        ],
    ),
    (
        "Multiple schemas",
        MULTIPLE_SCHEMA_QUERY,
        [
            {
                **DEFAULT,
                "schema": "cschema2",
                "table": "table_1",
                "foreign_table": "table_1",
                "constraint_name": "table_1_table_1_id_fkey",
            }
        ],
    ),
]


@pytest.mark.usefixtures("setup_tmp_schema")
@pytest.mark.parametrize("name, query, expected", _FKS_PARAMS, ids=[x[0] for x in _FKS_PARAMS])
def test_load_fk_constraints(name, query, expected, aengine, engine, loop):
    from padmy.db import FKConstraint, load_foreign_keys

    if query:
        engine.execute(query)
        engine.commit()

    fks = loop.run_until_complete(load_foreign_keys(aengine, ["cschema", "cschema2"]))
    expected_fks = [FKConstraint(**x) for x in expected]
    assert fks == expected_fks, pprint_dataclass_diff(fks, expected_fks)


ONE_PK_TABLE = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id  SERIAL PRIMARY KEY,
    foo TEXT
);
"""

MULTIPLE_PK_TABLE = """
CREATE TABLE IF NOT EXISTS cschema.table_1
(
    id_1  INT,
    id_2  TEXT,
    bar   TEXT,
    PRIMARY KEY (id_1, id_2)
);
"""

DEFAULT_PK_CONSTRAINT = {"table": "table_1", "schema": "cschema"}

_PKS_PARAMS = [
    ("No table", None, []),
    ("One Primary key", ONE_PK_TABLE, [{**DEFAULT_PK_CONSTRAINT, "column_name": "id"}]),
    (
        "Multiple primary keys",
        MULTIPLE_PK_TABLE,
        [
            {**DEFAULT_PK_CONSTRAINT, "column_name": "id_1"},
            {**DEFAULT_PK_CONSTRAINT, "column_name": "id_2"},
        ],
    ),
]


@pytest.mark.usefixtures("setup_tmp_schema")
@pytest.mark.parametrize("name, table, expected", _PKS_PARAMS, ids=[x[0] for x in _PKS_PARAMS])
def test_load_pk_constraints(name, table, expected, aengine, engine, loop):
    from padmy.db import PKConstraint, load_primary_keys

    if table:
        engine.execute(table)
        engine.commit()

    pks = loop.run_until_complete(load_primary_keys(aengine, ["cschema"]))
    expected_pks = [PKConstraint(**x) for x in expected]
    assert pks == expected_pks, pprint_dataclass_diff(pks, expected_pks)


@pytest.mark.parametrize(
    "table,expected",
    [
        pytest.param(
            """
    CREATE TABLE IF NOT EXISTS cschema.table_1
    (
        id  INT PRIMARY KEY GENERATED ALWAYS AS IDENTITY
    );
    """,
            [{"name": "id", "is_generated": False}],
            id="PK generated always is copied as data",
        ),
        pytest.param(
            """
    CREATE TABLE IF NOT EXISTS cschema.table_1
    (
        id  INT PRIMARY KEY GENERATED BY DEFAULT AS IDENTITY
    );
    """,
            [{"name": "id", "is_generated": False}],
            id="PK generated by default",
        ),
        pytest.param(
            """
    CREATE TABLE IF NOT EXISTS cschema.table_1
    (
        foo TEXT
    );
    """,
            [{"name": "foo", "is_generated": False}],
            id="Default col",
        ),
        pytest.param(
            """
    CREATE TABLE IF NOT EXISTS cschema.table_1
    (
        foo  TEXT,
        foo_2 TEXT GENERATED ALWAYS AS( LOWER(foo || 'bar')) STORED
    );
    """,
            [
                {"name": "foo", "is_generated": False},
                {"name": "foo_2", "is_generated": True},
            ],
            id="Generated column",
        ),
    ],
)
@pytest.mark.usefixtures("setup_tmp_schema")
def test_get_columns(loop, engine, aengine, table, expected):
    from padmy.db import get_columns, Table, Column

    engine.execute(table)
    engine.commit()

    async def _test():
        return await get_columns(aengine, [Table("cschema", "table_1")])

    result = loop.run_until_complete(_test())
    assert result == {"cschema.table_1": [Column(**c) for c in expected]}


@pytest.mark.usefixtures("setup_tables", "clean_migration")
def test_explore(loop, apool):
    from padmy.db import Database

    db = Database(name="test")

    async def test():
        await db.explore(apool, ["public", "test"])

    loop.run_until_complete(test())

    assert len(db.tables) == 11, pprint.pprint(db.tables)

    table_1 = [x for x in db.tables if x.full_name == "public.table_1"][0]
    assert len(table_1.child_tables) == 3
    assert len(table_1.child_tables_safe) == 3
    assert table_1.has_children
    assert not table_1.children_has_been_processed
    # Parents
    assert not table_1.has_parent
    assert len(table_1.parent_tables) == 0
    assert len(table_1.parent_tables_safe) == 0
    # Circular
    single_circular = [x for x in db.tables if x.full_name == "public.single_circular"][0]

    assert len(single_circular.child_tables) == 1
    assert len(single_circular.child_tables_safe) == 0
    assert not single_circular.has_children
    # Schema
    table_multi_schema_1 = [x for x in db.tables if x.full_name == "public.multi_schema_1"][0]
    assert len(table_multi_schema_1.child_tables) == 1
    assert len(table_multi_schema_1.child_tables_safe) == 1
    assert table_multi_schema_1.has_children
    assert not table_multi_schema_1.children_has_been_processed


@pytest.mark.usefixtures("setup_tables")
def test_table_hash():
    from padmy.db import Table

    t2 = Table("public", "table_2")
    t3 = Table("public", "table_3")
    t1 = Table("public", "table_1")
    t1.child_tables = {t1, t2, t3}
    z = {t1, t2, t3} - {t1}

    assert z == {t2, t3}
    assert t1.child_tables_safe == {t2, t3}
