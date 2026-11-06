import tempfile
from pathlib import Path
import logging
import pytest

from ..conftest import STATIC_DIR


@pytest.fixture
def migration_dir(tmp_path):
    with tempfile.TemporaryDirectory() as tmp_dir:
        yield Path(tmp_dir) / "migration"


VALID_MIGRATIONS_DIR = STATIC_DIR / "migrations" / "valid"
INVALID_MIGRATIONS_DIR = STATIC_DIR / "migrations" / "invalid-simple"
INVALID_MIGRATIONS_DIR_MULTIPLE: Path = STATIC_DIR / "migrations" / "invalid-multiple"
VALID_MIGRATIONS_SKIP_DIR = STATIC_DIR / "migrations" / "valid-skip"
VALID_MIGRATIONS_NO_TRANSACTION_DIR = STATIC_DIR / "migrations" / "valid-no-transaction"


@pytest.fixture()
def setup_test_schema(engine):
    with engine.cursor() as c:
        c.execute("CREATE SCHEMA IF NOT EXISTS general")
    engine.commit()

    yield

    with engine.cursor() as c:
        c.execute("DROP SCHEMA general CASCADE")
    engine.commit()


@pytest.fixture(autouse=True)
def restore_log_lvl():
    from padmy.logs import logs

    yield
    logs.setLevel(logging.INFO)
