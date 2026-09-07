import logging

import pytest

from .conftest import STATIC_DIR, PG_SAMPLE_DATABASE, PG_DATABASE, PG_PASSWORD


@pytest.fixture(autouse=True)
def restore_log_lvl():
    from padmy.logs import logs

    yield
    logs.setLevel(logging.INFO)


def test_run_anonymize():
    from padmy.run import cli

    cli.run_with_args("anonymize", "--db", "test", "-f", str(STATIC_DIR / "config.yml"), "-p", PG_PASSWORD)


@pytest.fixture()
def setup_test_schemas(engine):
    engine.execute("DROP SCHEMA IF EXISTS test CASCADE")
    engine.execute("CREATE SCHEMA test")
    engine.commit()
    yield
    engine.execute("DROP SCHEMA test CASCADE")
    engine.commit()


@pytest.mark.usefixtures("setup_test_db", "setup_test_schemas")
def test_run_sample(capsys, loop):
    from padmy.run import cli

    cli.run_with_args(
        "sample",
        "--db",
        PG_DATABASE,
        "--db-to",
        PG_SAMPLE_DATABASE,
        "--schemas",
        "public test",
        "--sample",
        "5",
        "--copy-db",
    )
    capsys.readouterr()


def test_run_copy_db(capsys):
    from padmy.run import cli

    cli.run_with_args("copy-db", "--db", "test", "--db-to", "test2", "--schemas", "public")
    capsys.readouterr()
    # _ = capsys.readouterr().out


def test_run_schema_diff():
    from padmy.run import cli

    cli.run_with_args("schema-diff", "--db", "test", "--schemas", "public")
