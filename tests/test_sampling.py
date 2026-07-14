import asyncio
import logging

import asyncpg
import pytest
from tracktolib.pg_sync import fetch_all, insert_many
from tracktolib.tests import assert_equals

from .conftest import PG_URL, PG_SAMPLE_DATABASE, PG_DATABASE


def get_table_no_constraints():
    from padmy.db import Table

    table = Table(schema="public", table="table_1")
    table.count = 10

    return table


def get_table_one_constraint():
    from padmy.db import Table, FKConstraint

    fks = [
        FKConstraint(
            column_names=["table_1_id"],
            constraint_name="t2_t1_id",
            foreign_schema="public",
            foreign_table="table_1",
            foreign_column_names=["id"],
        )
    ]
    table = Table(schema="public", table="table_2", foreign_keys=fks)
    table.count = 10
    return table


def get_table_multiple_constraints():
    from padmy.db import Table, FKConstraint

    table_fks = [
        FKConstraint(
            column_names=["table_1_id"],
            constraint_name="t3_t1_id",
            foreign_schema="public",
            foreign_table="table_1",
            foreign_column_names=["id"],
        ),
        FKConstraint(
            column_names=["table_2_id"],
            constraint_name="t3_t2_id",
            foreign_schema="public",
            foreign_table="table_2",
            foreign_column_names=["id"],
        ),
    ]
    table = Table(schema="public", table="table_3", foreign_keys=table_fks)

    return table


def test_sample_db_circular_single(loop, aengine):
    from padmy.sampling.sampling import create_temp_tables
    from padmy.db import Table, FKConstraint
    from padmy.utils import check_tmp_table_exists

    fks = [
        FKConstraint(
            column_names=["parent_id"],
            constraint_name="test",
            foreign_schema="public",
            foreign_table="single_circular",
            foreign_column_names=["id"],
        )
    ]
    table = Table(schema="public", table="single_circular", foreign_keys=fks, sample_size=20)

    async def test():
        await table.load_count(aengine)
        async with aengine.transaction():
            await create_temp_tables(aengine, [table])
            tmp_exists = await check_tmp_table_exists(aengine, table.tmp_name)
            assert tmp_exists

    loop.run_until_complete(test())


def test_sample_db_circular_multiple(loop, aengine):
    from padmy.sampling.sampling import create_temp_tables
    from padmy.db import Table, FKConstraint
    from padmy.utils import check_tmp_table_exists

    table1 = Table(
        schema="public",
        table="single_circular",
        foreign_keys=[
            FKConstraint(
                column_names=["parent_id"],
                constraint_name="test",
                foreign_schema="public",
                foreign_table="single_circular",
                foreign_column_names=["id"],
            )
        ],
        sample_size=20,
    )
    table1.count = 1
    table2 = Table(
        schema="public",
        table="single_circular",
        foreign_keys=[
            FKConstraint(
                column_names=["multiple_circular_id"],
                constraint_name="test2",
                foreign_schema="public",
                foreign_table="multiple_circular",
                foreign_column_names=["id"],
            )
        ],
        sample_size=20,
    )

    table2.count = 1

    tables = [table1, table2]

    async def test():
        async with aengine.transaction():
            await create_temp_tables(aengine, tables)
            for table in tables:
                tmp_exists = await check_tmp_table_exists(aengine, table.tmp_name)
                assert tmp_exists

    loop.run_until_complete(test())


def test_get_layout(loop):
    from padmy.db import Database
    from padmy.sampling.viz import get_layout, convert_db

    db = Database(name="test")
    table = get_table_multiple_constraints()
    table.count = 0
    db.tables = [table]
    g = convert_db(db)
    layout = get_layout(g)
    assert layout is not None


@pytest.fixture()
def setup_2_simple_tables(engine, sample_engine):
    query = """
    DROP SCHEMA IF EXISTS tmptest CASCADE;
    CREATE SCHEMA tmptest;
    CREATE TABLE IF NOT EXISTS tmptest.table_1
    (
        id  SERIAL PRIMARY KEY,
        foo TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS tmptest.table_2
    (
        id         SERIAL PRIMARY KEY,
        table_1_id INT REFERENCES tmptest.table_1 ON DELETE CASCADE
    );
    """
    engine.execute(query)
    engine.commit()
    table_1 = [
        {
            "id": i,
            "foo": f"foo-{i}",
        }
        for i in range(10)
    ]
    table_2 = [{"id": 0, "table_1_id": table_1[0]["id"]}]

    insert_many(engine, "tmptest.table_1", table_1)
    insert_many(engine, "tmptest.table_2", table_2)

    sample_engine.execute(query)
    sample_engine.commit()


@pytest.fixture()
def setup_3_simple_tables(engine, sample_engine):
    query = """
    DROP SCHEMA IF EXISTS tmptest CASCADE;
    CREATE SCHEMA tmptest;
    CREATE TABLE IF NOT EXISTS tmptest.table_1
    (
        id  SERIAL PRIMARY KEY,
        foo TEXT NOT NULL
    );
    
    CREATE TABLE IF NOT EXISTS tmptest.table_2
    (
        id         SERIAL PRIMARY KEY,
        table_1_id INT REFERENCES tmptest.table_1 ON DELETE CASCADE
    );
    
    CREATE TABLE IF NOT EXISTS tmptest.table_3
    (
        id         SERIAL PRIMARY KEY,
        table_2_id INT REFERENCES tmptest.table_2 ON DELETE CASCADE
    );
    """
    engine.execute(query)
    engine.commit()
    table_1 = [
        {
            "id": i,
            "foo": f"foo-{i}",
        }
        for i in range(10)
    ]
    table_2 = [{"id": 0, "table_1_id": table_1[0]["id"]}]

    insert_many(engine, "tmptest.table_1", table_1)
    insert_many(engine, "tmptest.table_2", table_2)

    sample_engine.execute(query)
    sample_engine.commit()


@pytest.fixture()
def setup_multi_fk_tables(engine, sample_engine):
    query = """
    DROP SCHEMA IF EXISTS tmptest CASCADE;
    CREATE SCHEMA tmptest;
    CREATE TABLE IF NOT EXISTS tmptest.table_1
    (
        id  SERIAL PRIMARY KEY,
        foo TEXT NOT NULL
    );

    -- Two FK paths to the same parent (new table name to avoid reusing the
    -- session connection's cached statements on a different table shape)
    CREATE TABLE IF NOT EXISTS tmptest.table_4
    (
        id            SERIAL PRIMARY KEY,
        table_1_id    INT REFERENCES tmptest.table_1 ON DELETE CASCADE,
        table_1_bis_id INT REFERENCES tmptest.table_1 ON DELETE CASCADE
    );
    """
    engine.execute(query)
    engine.commit()
    table_1 = [
        {
            "id": i,
            "foo": f"foo-{i}",
        }
        for i in range(10)
    ]
    table_4 = [{"id": 0, "table_1_id": 0, "table_1_bis_id": 1}]

    insert_many(engine, "tmptest.table_1", table_1)
    insert_many(engine, "tmptest.table_4", table_4)

    sample_engine.execute(query)
    sample_engine.commit()


@pytest.mark.parametrize(
    "patch_scenario, sample_size, expected",
    [
        pytest.param(
            "setup_2_simple_tables",
            {
                "tmptest.table_1": 0,
                "tmptest.table_2": 100,
            },
            {
                "_tmptest_table_1_tmp": [{"id": 0, "foo": "foo-0"}],
                "_tmptest_table_2_tmp": [{"id": 0, "table_1_id": 0}],
            },
            id="2 tables, parent sample too small",
        ),
        pytest.param(
            "setup_2_simple_tables",
            {
                "tmptest.table_1": 100,
                "tmptest.table_2": 0,
            },
            {
                "_tmptest_table_1_tmp": [{"id": i, "foo": f"foo-{i}"} for i in range(10)],
                "_tmptest_table_2_tmp": [],
            },
            id="2 tables, No data child",
        ),
        pytest.param(
            "setup_3_simple_tables",
            {
                "tmptest.table_1": 0,
                "tmptest.table_2": 100,
                "tmptest.table_3": 100,
            },
            {
                "_tmptest_table_1_tmp": [{"id": 0, "foo": "foo-0"}],
                "_tmptest_table_2_tmp": [{"id": 0, "table_1_id": 0}],
                "_tmptest_table_3_tmp": [],
            },
            id="3 tables, parent sample too small",
        ),
        pytest.param(
            "setup_multi_fk_tables",
            {
                "tmptest.table_1": 0,
                "tmptest.table_4": 100,
            },
            {
                "_tmptest_table_1_tmp": [{"id": 0, "foo": "foo-0"}, {"id": 1, "foo": "foo-1"}],
                "_tmptest_table_4_tmp": [{"id": 0, "table_1_id": 0, "table_1_bis_id": 1}],
            },
            id="2 fk paths to the same parent, both referenced parents kept",
        ),
    ],
)
@pytest.mark.usefixtures("setup_test_db", "setup_sample_tables")
def test_create_tmp_tables(
    loop,
    aengine,
    apool,
    sample_size,
    expected,
    patch_scenario,
    request: pytest.FixtureRequest,
):
    request.getfixturevalue(patch_scenario)
    from padmy.sampling.sampling import create_temp_tables
    from padmy.db import Database

    db = Database(name=PG_DATABASE)

    async def test():
        await db.explore(apool, ["tmptest"])
        for table in db.tables:
            table.sample_size = sample_size[table.full_name]

        async with aengine.transaction():
            await create_temp_tables(aengine, tables=db.tables)
            tables_data = {}
            for table in db.tables:
                _items = await aengine.fetch(
                    f"SELECT * FROM {table.tmp_name} order by id",
                )
                tables_data[table.tmp_name] = [dict(_item) for _item in _items]
        return tables_data

    data = loop.run_until_complete(test())
    assert_equals(data, expected)
    # assert False


@pytest.mark.usefixtures("setup_test_db", "setup_2_simple_tables")
def test_sample_database_simple(loop, apool, sample_engine):
    from padmy.sampling import sample_database
    from padmy.db import Database
    from padmy.config import Config, ConfigSchema, ConfigTable

    db = Database(name=PG_DATABASE)
    config = Config(
        sample=100.0,
        schemas=[ConfigSchema(schema="tmptest")],
        tables=[ConfigTable(schema="tmptest", table="table_1", sample=0.0)],
    )

    async def test():
        conn = await asyncpg.connect(f"{PG_URL}/{PG_DATABASE}")
        target_conn = await asyncpg.connect(f"{PG_URL}/{PG_SAMPLE_DATABASE}")

        try:
            await db.explore(apool, ["tmptest"])
            db.load_config(config)
            await sample_database(
                conn=conn,
                target_conn=target_conn,
                show_progress=False,
                chunk_size=1,
                db=db,
                no_trigger=False,
            )
        finally:
            await asyncio.wait_for(conn.close(), timeout=1)
            await asyncio.wait_for(target_conn.close(), timeout=1)

    loop.run_until_complete(test())

    data = fetch_all(sample_engine, "SELECT * FROM tmptest.table_2")
    assert len(data) == 1
    data = fetch_all(sample_engine, "SELECT * FROM tmptest.table_1")
    assert len(data) == 1


@pytest.mark.usefixtures(
    "setup_test_db",
    "setup_tables",
    "populate_data",
    "setup_sample_db",
    "setup_sample_tables",
)
def test_sample_database(loop, apool, sample_engine):
    from padmy.sampling import sample_database
    from padmy.db import Database
    from padmy.config import Config, ConfigSchema, ConfigTable
    from padmy.logs import logs

    logs.setLevel(logging.DEBUG)
    db = Database(name=PG_DATABASE)
    config = Config(
        sample=100.0,
        schemas=[ConfigSchema(schema="public")],
        tables=[ConfigTable(schema="public", table="table_1", sample=0.0)],
    )

    async def test():
        conn = await asyncpg.connect(f"{PG_URL}/{PG_DATABASE}")
        target_conn = await asyncpg.connect(f"{PG_URL}/{PG_SAMPLE_DATABASE}")

        try:
            await db.explore(apool, ["public"])
            db.load_config(config)
            await sample_database(
                conn=conn,
                target_conn=target_conn,
                show_progress=False,
                chunk_size=1,
                db=db,
                no_trigger=False,
            )
        finally:
            await asyncio.wait_for(conn.close(), timeout=1)
            await asyncio.wait_for(target_conn.close(), timeout=1)

    loop.run_until_complete(test())

    data = fetch_all(sample_engine, "SELECT * FROM public.table_2")
    assert len(data) == 1
    data = fetch_all(sample_engine, "SELECT * FROM public.table_1")
    assert len(data) == 1
