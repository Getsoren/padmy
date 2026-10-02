import datetime as dt
import logging
import re
from contextlib import nullcontext
from typing import Literal
from unittest.mock import Mock

import psycopg
import pytest
from tracktolib.pg_sync import fetch_all, insert_one, insert_many

from padmy.migration.utils import parse_filename, MigrationFileError
from .conftest import (
    VALID_MIGRATIONS_DIR,
    INVALID_MIGRATIONS_DIR,
    INVALID_MIGRATIONS_DIR_MULTIPLE,
    VALID_MIGRATIONS_SKIP_DIR,
    VALID_MIGRATIONS_NO_TRANSACTION_DIR,
)
from ..conftest import PG_DATABASE
from ..utils import check_table_exists, check_column_exists

TEST_EMAIL = "foo@baz.baz"


@pytest.fixture()
def setup(loop, aengine):
    from padmy.migration.migration import migrate_setup

    loop.run_until_complete(migrate_setup(aengine))


@pytest.mark.parametrize("version", [None, "0.0.0"])
def test_create_new_migration(monkeypatch, migration_dir, capsys, version):
    from padmy.migration import create_new_migration

    monkeypatch.setattr("padmy.migration.utils.get_git_email", lambda x: TEST_EMAIL)

    time_call_count = 0

    def mock_time():
        nonlocal time_call_count
        time_call_count += 1
        return time_call_count

    monkeypatch.setattr("uuid.uuid4", lambda: "0000000000")
    monkeypatch.setattr("time.time", mock_time)

    class PromptMock:
        @staticmethod
        def ask(*args, **kwargs):
            return TEST_EMAIL

    monkeypatch.setattr("padmy.migration.create_files.Prompt", PromptMock)
    create_new_migration(folder=migration_dir, version=version)
    new_files = list(migration_dir.glob("*.sql"))
    assert capsys.readouterr().out.strip().startswith("Creating new migration file")
    assert len(new_files) == 2
    down_file, up_file = sorted(new_files)
    up_content = [x.rstrip() for x in up_file.open().readlines() if x.strip()]

    _optional_args = [f"-- Version: {version}"] if version else []

    assert up_content == ["-- Prev-file:", f"-- Author: {TEST_EMAIL}"] + _optional_args
    down_content = [x.rstrip() for x in down_file.open().readlines() if x.strip()]
    assert down_content == ["-- Prev-file:", f"-- Author: {TEST_EMAIL}"] + _optional_args

    # Creating second migration with skip verify

    create_new_migration(folder=migration_dir, version=version, skip_verify=True)
    new_files = list(migration_dir.glob("*.sql"))
    assert capsys.readouterr().out.strip().startswith("Creating new migration file")
    up_file2, down_file2 = sorted(new_files, key=lambda x: x.name, reverse=True)[:2]
    up_content = [x.rstrip() for x in up_file2.open().readlines() if x.strip()]
    assert up_content == [f"-- Prev-file: {up_file.name}", f"-- Author: {TEST_EMAIL}"] + _optional_args
    down_content = [x.rstrip() for x in down_file2.open().readlines() if x.strip()]
    assert down_content == [
        f"-- Prev-file: {down_file.name}",
        f"-- Author: {TEST_EMAIL}",
        *_optional_args,
        "-- Skip-verify: no reason provided",
    ]


@pytest.mark.usefixtures("clean_migration")
def test_migrate_setup(engine, loop, aengine):
    from padmy.migration import migrate_setup

    loop.run_until_complete(migrate_setup(aengine))
    assert check_table_exists(engine, "public", "migration")


@pytest.mark.usefixtures("setup_test_schema")
@pytest.mark.parametrize("only_last", [True, False])
def test_migrate_verify_valid(tmp_path, only_last):
    from padmy.migration import migrate_verify
    from padmy.migration.migration import MigrationError

    migrate_verify(
        database=PG_DATABASE,
        schemas=["general"],
        dump_dir=tmp_path,
        migration_folder=VALID_MIGRATIONS_DIR,
        only_last=only_last,
    )
    # Rerunning again for double check

    try:
        migrate_verify(
            database=PG_DATABASE,
            schemas=["general"],
            dump_dir=tmp_path,
            migration_folder=VALID_MIGRATIONS_DIR,
            only_last=only_last,
        )
    except MigrationError as e:
        print(e.diff)
        raise e


@pytest.mark.usefixtures("setup_test_schema")
def test_migrate_verify_valid_skip(tmp_path):
    from padmy.migration import migrate_verify
    from padmy.utils import PGError

    migrate_verify(
        database=PG_DATABASE,
        schemas=["general"],
        dump_dir=tmp_path,
        migration_folder=VALID_MIGRATIONS_SKIP_DIR,
        only_last=True,
        skip_down_restore=True,
    )
    # Should raise an error since the down file is not skipped
    with pytest.raises(PGError, match=r'ERROR:  column "baz" of relation "test" already exists'):
        migrate_verify(
            database=PG_DATABASE,
            schemas=["general"],
            dump_dir=tmp_path,
            migration_folder=VALID_MIGRATIONS_SKIP_DIR,
            only_last=True,
            skip_down_restore=True,
        )


@pytest.mark.usefixtures("setup_test_schema")
@pytest.mark.parametrize("only_last", [False, True])
@pytest.mark.parametrize(
    "migration_dir, error_msg",
    [
        pytest.param(INVALID_MIGRATIONS_DIR, "Difference found for migration: 00000000", id="invalid"),
        pytest.param(
            INVALID_MIGRATIONS_DIR_MULTIPLE, "Difference found for migration: 00000001", id="multiple invalid"
        ),
    ],
)
def test_migrate_verify(engine, tmp_path, only_last, migration_dir, error_msg):
    from padmy.migration import migrate_verify
    from padmy.migration.migration import MigrationError

    with pytest.raises(MigrationError, match=re.escape(error_msg)):
        migrate_verify(
            database=PG_DATABASE,
            schemas=["general"],
            dump_dir=tmp_path,
            migration_folder=migration_dir,
            only_last=only_last,
        )

    # TODO: flaky on CI
    # error_file = tmp_path / "error.diff"
    # with error_file.open("w") as f:
    #     f.write(e.value.diff + "\n")
    #
    # compare_files(
    #     error_file, INVALID_MIGRATIONS_DIR / "1-00000000.diff", ignore_order=True
    # )


SETUP_ERROR_MSG = re.escape(
    'Could not find table "public.migration", did you forget to setup the table by running "migration setup" ?'
)


@pytest.mark.parametrize("migration_type", ["up", "down"])
@pytest.mark.usefixtures("clean_migration")
def test_migrate_no_setup(engine, aengine, loop, migration_type):
    from padmy.migration.migration import migrate_up, NoSetupTableError, migrate_down

    fn = migrate_up if migration_type == "up" else migrate_down

    async def _test():
        with pytest.raises(NoSetupTableError, match=SETUP_ERROR_MSG):
            await fn(aengine, folder=VALID_MIGRATIONS_DIR)

    loop.run_until_complete(_test())


def _insert_migrations(
    engine: psycopg.Connection,
    nb_migrations: int,
    migration_type: Literal["up", "down"] = "up",
    *,
    offset_seconds: int = 0,
):
    _files = sorted(list(VALID_MIGRATIONS_DIR.glob(f"*{migration_type}.sql")), reverse=migration_type == "down")
    if nb_migrations > (nb_max_migrations := len(_files)):
        raise ValueError(f"Invalid number of migrations {nb_migrations} > {nb_max_migrations}")

    _now = dt.datetime.now() + dt.timedelta(seconds=offset_seconds)
    _data = [
        {
            "file_name": _files[i].name,
            "applied_at": _now + dt.timedelta(microseconds=i),
            **parse_filename(_files[i].name),
        }
        for i in range(nb_migrations)
    ]
    insert_many(engine, "migration", _data)


@pytest.mark.parametrize(
    "migration_type, setup_fn, params,expected",
    [
        pytest.param("up", None, {"nb_migrations": 1}, ["00000000"], id="one migration up"),
        pytest.param("down", None, {"nb_migrations": 1}, [], id="one migration down with no applied migrations"),
        pytest.param(
            "down",
            lambda engine: _insert_migrations(engine, nb_migrations=1),
            {"nb_migrations": 1},
            ["00000000"],
            id="one migration down",
        ),
        pytest.param("up", None, {"nb_migrations": -1}, ["00000000", "00000001"], id="multiple migrations up"),
        pytest.param(
            "up",
            lambda engine: _insert_migrations(engine, nb_migrations=1),
            {"nb_migrations": 1},
            ["00000001"],
            id="up with applied migrations",
        ),
        pytest.param(
            "up",
            lambda engine: _insert_migrations(engine, nb_migrations=2),
            {"nb_migrations": 1},
            [],
            id="up with no more migrations",
        ),
        pytest.param(
            "up",
            lambda engine: (
                insert_one(
                    engine,
                    "migration",
                    {
                        "file_name": "2-00000001-up.sql",
                        **parse_filename("2-00000001-up.sql"),
                    },
                )
            ),
            {"nb_migrations": 1},
            [],
            id="up with missing intermediate migration",
        ),
        pytest.param(
            "down",
            lambda engine: _insert_migrations(engine, nb_migrations=1),
            {"nb_migrations": 2},
            ["00000000"],
            id="two rollback with 1 migration only",
        ),
        pytest.param(
            "down",
            lambda engine: (
                _insert_migrations(engine, nb_migrations=2),
                _insert_migrations(engine, nb_migrations=1, migration_type="down", offset_seconds=1),
            ),
            {"nb_migrations": 2},
            ["00000000"],
            id="rollback a second time",
        ),
        pytest.param(
            "down",
            lambda engine: _insert_migrations(engine, nb_migrations=2),
            {"migration_id": "00000000"},
            ["00000001", "00000000"],
            id="rollback until 00000000",
        ),
        pytest.param(
            "down",
            lambda engine: _insert_migrations(engine, nb_migrations=2),
            {"migration_id": "00000001"},
            ["00000001"],
            id="rollback until 00000001",
        ),
        pytest.param(
            "down",
            lambda engine: _insert_migrations(engine, nb_migrations=2),
            {"migration_id": "toto"},
            pytest.raises(ValueError, match="Could not find migration_id"),
            id="rollback until not found",
        ),
        pytest.param(
            "up",
            lambda engine: (
                _insert_migrations(engine, nb_migrations=2),
                _insert_migrations(engine, nb_migrations=1, migration_type="down", offset_seconds=1),
            ),
            {"nb_migrations": 1},
            ["00000001"],
            id="reapply migration",
        ),
        pytest.param(
            "up",
            lambda engine: (
                _insert_migrations(engine, nb_migrations=2),
                _insert_migrations(engine, nb_migrations=2, migration_type="down"),
            ),
            {"nb_migrations": 2},
            ["00000000", "00000001"],
            id="reapply migrations",
        ),
        # TODO: fix this case
        # pytest.param(
        #     "up",
        #     lambda engine: (
        #         _insert_migrations(engine, nb_migrations=2),
        #         _insert_migrations(engine, nb_migrations=2, migration_type="down", offset_seconds=1),
        #         _insert_migrations(engine, nb_migrations=1, offset_seconds=2),
        #     ),
        #     {"nb_migrations": 1},
        #     ["00000001"],
        #     id="reapply migration a second time",
        # ),
    ],
)
@pytest.mark.usefixtures("clean_migration", "setup")
def test_migration_files(engine, aengine, loop, migration_type, setup_fn, params, expected):
    if setup_fn is not None:
        setup_fn(engine)

    from padmy.migration.migration import get_migration_files, get_rollback_files

    fn = get_migration_files if migration_type == "up" else get_rollback_files

    async def _test():
        _files = await fn(aengine, folder=VALID_MIGRATIONS_DIR, **params)
        return _files

    _expected = nullcontext(expected) if isinstance(expected, list) else expected
    with _expected as e:
        files = loop.run_until_complete(_test())
        assert [x.file_id for x in files] == e


@pytest.mark.usefixtures("clean_migration", "setup_test_schema")
def test_migrate_up_down(engine, caplog, aengine, loop):
    def _get_migrations():
        return fetch_all(engine, "SELECT * FROM public.migration order by applied_at desc")

    caplog.set_level(logging.INFO)
    from padmy.migration import migrate_up, migrate_setup, migrate_down

    # Setting up migration

    loop.run_until_complete(migrate_setup(aengine))
    assert not check_table_exists(engine, "general", "test")

    migrations = _get_migrations()
    assert len(migrations) == 0

    # 1rst migration
    loop.run_until_complete(migrate_up(aengine, folder=VALID_MIGRATIONS_DIR, nb_migrations=1, metadata={"foo": "bar"}))

    assert check_table_exists(engine, "general", "test")
    assert check_table_exists(engine, "general", "test2")
    assert not check_column_exists(engine, "general", "test", "baz")
    migrations = _get_migrations()
    assert len(migrations) == 1
    last_migration = migrations[0]
    assert last_migration["meta"] == {"foo": "bar"}
    assert last_migration.pop("applied_at")
    assert last_migration.pop("id") is not None
    assert last_migration["file_id"] == "00000000"
    assert last_migration["migration_type"] == "up"

    # 2nd migration

    loop.run_until_complete(migrate_up(aengine, folder=VALID_MIGRATIONS_DIR, nb_migrations=1))

    assert check_column_exists(engine, "general", "test", "baz")
    migrations = _get_migrations()
    assert len(migrations) == 2
    last_migration = migrations[0]
    assert last_migration["file_id"] == "00000001"
    assert last_migration["migration_type"] == "up"

    # 3rd migration
    has_applied_migration = loop.run_until_complete(migrate_up(aengine, folder=VALID_MIGRATIONS_DIR, nb_migrations=1))
    assert not has_applied_migration

    # Migrate down
    loop.run_until_complete(
        migrate_down(aengine, folder=VALID_MIGRATIONS_DIR, nb_migrations=1, metadata={"bar": "baz"})
    )
    assert check_table_exists(engine, "general", "test")
    assert check_table_exists(engine, "general", "test2")
    assert not check_table_exists(engine, "general", "baz")
    migrations = _get_migrations()
    assert len(migrations) == 3
    last_migration = migrations[0]
    assert last_migration["migration_type"] == "down"
    assert last_migration["file_id"] == "00000001"
    assert last_migration["meta"] == {"bar": "baz"}
    # Migrate down a second time
    loop.run_until_complete(migrate_down(aengine, folder=VALID_MIGRATIONS_DIR, nb_migrations=1))
    assert not check_table_exists(engine, "general", "test")
    assert not check_table_exists(engine, "general", "test2")
    migrations = _get_migrations()
    assert len(migrations) == 4
    last_migration = migrations[0]
    assert last_migration["file_id"] == "00000000", "Invalid file_id after second down migration"
    assert last_migration["migration_type"] == "down"

    # Migrate down no more rollback
    has_applied_migration = loop.run_until_complete(migrate_down(aengine, folder=VALID_MIGRATIONS_DIR))
    # messages = [rec.message for rec in caplog.records]
    # assert messages[-1] == "No rollback files to apply"
    assert not has_applied_migration

    # Migrating up again

    loop.run_until_complete(migrate_up(aengine, folder=VALID_MIGRATIONS_DIR))

    assert check_table_exists(engine, "general", "test")
    assert check_table_exists(engine, "general", "test2")
    assert check_column_exists(engine, "general", "test", "baz")
    data = fetch_all(engine, "SELECT * FROM public.migration ORDER BY applied_at DESC")
    assert len(data) == 6


class TestVerifyMigrations:
    @pytest.fixture(autouse=True)
    def setup_tables(self, setup):
        pass

    _no_data = pytest.param({"before": [], "after": []}, None, None, id="no data")
    _missing_migrations_in_db = pytest.param(
        {
            "before": [],
            "after": [
                {"migration_type": "up", "file_name": "1-00000000-up.sql", "meta": {"missing": True}},
                {"migration_type": "up", "file_name": "2-00000001-up.sql", "meta": {"missing": True}},
            ],
        },
        VALID_MIGRATIONS_DIR,
        None,
        id="missing migrations in db",
    )

    _missing_intermediate_migration = pytest.param(
        {
            "before": [
                {"migration_type": "up", "file_name": "2-00000001-up.sql", "meta": None},
            ],
            "after": [
                {"migration_type": "up", "file_name": "2-00000001-up.sql", "meta": None},
                {"migration_type": "up", "file_name": "1-00000000-up.sql", "meta": {"missing": True}},
            ],
        },
        VALID_MIGRATIONS_DIR,
        lambda engine: (
            insert_one(
                engine,
                "migration",
                {
                    "id": 0,
                    "migration_type": "up",
                    "file_name": "2-00000001-up.sql",
                    "file_id": "00000001",
                    "file_ts": dt.datetime(1970, 1, 1, 0, 0, 1),
                },
            ),
            engine.commit(),
        ),
        id="missing intermediate migration in db",
    )

    @pytest.mark.usefixtures("setup_test_schema")
    @pytest.mark.parametrize(
        "expected, migrations_folder, setup_fn",
        [_no_data, _missing_migrations_in_db, _missing_intermediate_migration],
    )
    def test_verify_migrations(self, aengine, loop, engine, migrations_folder, expected, tmp_path, setup_fn):
        from padmy.migration import verify_migrations

        if setup_fn is not None:
            setup_fn(engine)

        def _get_migrations():
            return fetch_all(engine, "SELECT migration_type, file_name, meta from migration order by applied_at")

        before_migrations = _get_migrations()
        # assert before_migrations
        assert before_migrations == expected["before"]
        loop.run_until_complete(verify_migrations(aengine, folder=migrations_folder or tmp_path))
        after_migrations = _get_migrations()
        assert after_migrations == expected["after"]


class TestMigrationFiles:
    @pytest.fixture(autouse=True)
    def setup(self, monkeypatch, migration_dir):
        monkeypatch.setattr("padmy.migration.create_files._get_user_email", lambda: TEST_EMAIL)
        yield
        for _file in migration_dir.glob("*.sql"):
            _file.unlink()

    @pytest.fixture()
    def setup_valid_migration_files(self, migration_dir, monkeypatch):
        monkeypatch.setattr("padmy.migration.create_files._get_user_email", lambda: TEST_EMAIL)
        mock = Mock()
        monkeypatch.setattr("time.time", mock)
        from padmy.migration import create_new_migration

        mock.return_value = 1
        create_new_migration(migration_dir)
        mock.return_value = 2
        create_new_migration(migration_dir)
        mock.return_value = 3
        create_new_migration(migration_dir)

    @pytest.fixture()
    def setup_invalid_up_migration_files(self, migration_dir, monkeypatch):
        monkeypatch.setattr("padmy.migration.create_files._get_user_email", lambda: TEST_EMAIL)
        monkeypatch.setattr("padmy.migration.create_files._get_last_migration_name", lambda _: "0-10000000")
        uuid_mock = Mock()
        mock = Mock()
        monkeypatch.setattr("uuid.uuid4", uuid_mock)
        monkeypatch.setattr("time.time", mock)
        from padmy.migration import create_new_migration

        mock.return_value = 1
        uuid_mock.return_value = "10000000"
        create_new_migration(migration_dir)
        mock.return_value = 2
        uuid_mock.return_value = "20000000"
        up_file, _ = create_new_migration(migration_dir)

    @pytest.mark.parametrize(
        "setup, expected",
        [
            pytest.param("setup_valid_migration_files", nullcontext([]), id="valid"),
            pytest.param(
                "setup_invalid_up_migration_files",
                pytest.raises(
                    MigrationFileError,
                    match=re.escape(
                        "Invalid header for up file 2-20000000-up.sql : (expected '1-10000000-up.sql' got '0-10000000')"
                    ),
                ),
                id="invalid",
            ),
        ],
    )
    def test_verify_migration_files(self, migration_dir, setup, expected, request, capsys):
        request.getfixturevalue(setup)
        from padmy.migration.utils import verify_migration_files

        with expected as e:
            assert verify_migration_files(migration_dir, raise_error=True) == e

        assert capsys.readouterr().out.strip()


@pytest.mark.usefixtures("clean_migration", "setup_test_schema", "setup")
def test_migrate_up_down_no_transaction(engine, aengine, loop):
    from padmy.migration import migrate_up, migrate_down

    def _has_index():
        return bool(fetch_all(engine, "SELECT 1 FROM pg_indexes WHERE indexname = 'test_foo_idx'"))

    loop.run_until_complete(migrate_up(aengine, folder=VALID_MIGRATIONS_NO_TRANSACTION_DIR))
    assert _has_index()
    assert check_column_exists(engine, "general", "test", "baz")

    loop.run_until_complete(migrate_down(aengine, folder=VALID_MIGRATIONS_NO_TRANSACTION_DIR))
    assert not _has_index()
    assert not check_table_exists(engine, "general", "test")
    migrations = fetch_all(engine, "SELECT migration_type FROM public.migration")
    assert [m["migration_type"] for m in migrations] == ["up"] * 3 + ["down"] * 3
