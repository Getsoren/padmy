import dataclasses
import datetime as dt
import difflib
import filecmp
import functools
import textwrap
import time
import typing
from contextlib import nullcontext
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from typing import Callable, Iterator

import asyncpg
from asyncpg import Connection
from asyncpg.exceptions import UndefinedTableError
from tracktolib.pg import insert_one
from tracktolib.utils import get_chunks

from padmy.logs import logs
from padmy.utils import exec_file, pg_dump, exec_psql_file, remove_restrict_clauses
from .utils import get_files, iter_migration_files, MigrationFile

_GET_LATEST_MIGRATION_QUERY = textwrap.dedent(
    """
SELECT _m.file_ts, _m.file_name, _m.file_id
FROM public.migration _m
where _m.migration_type = 'up'
-- a rollback only cancels the ups applied before it
and not exists(select 1
               from public.migration _r
               where _r.migration_type = 'down' and _r.file_id = _m.file_id and _r.id > _m.id)
order by _m.applied_at desc, file_ts desc limit 1
"""
)

# PG_GLOBAL_DUMP_DIR = GLOBAL_TMP / 'matching-pg-backup' / 'dump'
SETUP_FILE = Path(__file__).parent / "db.sql"


class MigrationError(Exception):
    def __init__(self, msg: str, diff: str):
        self.msg = msg
        self.diff = diff


async def migrate_setup(conn: Connection):
    logs.info("Setting up migration tables")
    await exec_file(conn, SETUP_FILE)
    logs.info("Done")


CompareFilesFn = Callable[[Path, Path], Iterator[str] | None]


def compare_files(file1: Path, file2: Path, ignore_restrict: bool = True) -> Iterator[str] | None:
    """
    Compares 2 files. If they are the same, returns None, otherwise returns the diff.
    If ignore_restrict is True, ignores \restrict clauses in the
    diff ([PostgreSQL 17.6+](https://www.postgresql.org/docs/17/release-17-6.html#RELEASE-17-6-CHANGES) feature).
    """
    if ignore_restrict:
        remove_restrict_clauses(file1)
        remove_restrict_clauses(file2)
    _no_diff = filecmp.cmp(file1, file2)
    if _no_diff:
        return None
    diff = difflib.unified_diff(
        file1.read_text().split("\n"),
        file2.read_text().split("\n"),
        fromfile=file1.name,
        tofile=file2.name,
    )
    return diff


default_compare_files: CompareFilesFn = functools.partial(compare_files, ignore_restrict=True)


def _verify_migration(
    database: str,
    schemas: list[str],
    migration_id: str,
    up_file: Path,
    down_file: Path,
    dump_dir: Path,
    *,
    compare_files_fn: CompareFilesFn = default_compare_files,
):
    _before_dump, _after_dump = (
        f"{migration_id}-before.sql",
        f"{migration_id}-after.sql",
    )

    # Dump before
    pg_dump(
        database,
        schemas,
        dump_path=str(dump_dir / _before_dump),
        options=["-E", "utf8", "--schema-only"],
    )

    logs.info(f"Applying {up_file.name}")
    exec_psql_file(database, str(up_file))

    logs.info(f"Applying {down_file.name}")
    exec_psql_file(database, str(down_file))

    # Dump after
    pg_dump(
        database,
        schemas,
        dump_path=str(dump_dir / _after_dump),
        options=["-E", "utf8", "--schema-only"],
    )

    _before_dump_file, _after_dump_file = (
        dump_dir / _before_dump,
        dump_dir / _after_dump,
    )

    _diff = compare_files_fn(_before_dump_file, _after_dump_file)
    if _diff is not None:
        raise MigrationError(f"Difference found for migration: {migration_id}", diff="\n".join(_diff))


def migrate_verify(
    database: str,
    schemas: list[str],
    dump_dir: Path,
    migration_folder: Path,
    *,
    only_last: bool = False,
    compare_files_fn: CompareFilesFn = default_compare_files,
    skip_down_restore: bool = False,
):
    """
    Verifies that the up/down migration is correct
    """
    _pg_dump = functools.partial(pg_dump, database=database, schemas=schemas)

    migration_files = get_files(migration_folder)
    logs.info(f"Found {len(migration_files)} migration files")

    logs.info("Checking...")
    _down_files: list[MigrationFile] = []
    _nb_migrations = int(len(migration_files) / 2)
    try:
        for i, (_up_file, _down_file) in enumerate(iter_migration_files(migration_files)):
            _skip = (
                _down_file.skip_verify
                or
                # We verify all if only_last is False or if we only have one migration (up/down)
                (only_last and i != _nb_migrations - 1 and len(migration_files) != 2)
            )
            if not _skip:
                logs.info(f"Checking migration {i + 1}/{_nb_migrations}")
                _verify_migration(
                    database=database,
                    schemas=schemas,
                    migration_id=_up_file.file_id,
                    up_file=_up_file.path,
                    down_file=_down_file.path,
                    dump_dir=dump_dir,
                    compare_files_fn=compare_files_fn,
                )
            else:
                logs.info(f"Skipping migration {_up_file.file_id}")
            # Need to be reapplied for the next tests
            exec_psql_file(database, str(_up_file.path))
            _down_files.append(_down_file)
    except MigrationError as e:
        logs.error(e.msg)
        logs.error(e.diff)
        raise e

    if not skip_down_restore:
        # Restoring to initial state
        logs.info("Restoring to initial state")
        for i, _down_file in enumerate(reversed(_down_files)):
            logs.info(f"Applying {_down_file.path.name} ({len(_down_files) - i}/{len(_down_files)})")
            exec_psql_file(database, str(_down_file.path))
        # else:
        #     if last_down_file:
        #         exec_psql_file(database, str(last_down_file.path))

    logs.info("Everything ok!")


@dataclasses.dataclass
class LatestMigration:
    timestamp: dt.datetime
    file_name: str
    file_id: str

    @classmethod
    def load(cls, data: dict):
        return cls(timestamp=data["file_ts"], file_name=data["file_name"], file_id=data["file_id"])


class NoSetupTableError(Exception):
    pass


async def _get_latest_migration(conn: Connection):
    try:
        resp = await conn.fetchrow(_GET_LATEST_MIGRATION_QUERY)
    except UndefinedTableError as e:
        if 'relation "public.migration" does not exist' in str(e):
            raise NoSetupTableError(
                'Could not find table "public.migration", did you forget to setup the table'
                ' by running "migration setup" ?'
            )
        else:
            raise
    return LatestMigration.load(dict(resp)) if resp else None


async def get_migration_files(
    conn: asyncpg.Connection, folder: Path, *, nb_migrations: int = -1
) -> list[MigrationFile]:
    latest_migration = await _get_latest_migration(conn)
    logs.info(
        f"Latest timestamp: {latest_migration.timestamp} ({latest_migration.file_id})"
        if latest_migration
        else "No previous migration found"
    )
    migration_files = get_files(folder)

    migrations_to_apply = [
        _up_file
        for _up_file, _ in iter_migration_files(migration_files)
        if (latest_migration is None)
        or (_up_file.ts >= latest_migration.timestamp and _up_file.path.name != latest_migration.file_name)
    ]

    if nb_migrations > 0:
        migrations_to_apply = migrations_to_apply[:nb_migrations]
    return migrations_to_apply


NO_TRANSACTION_FAILURE_MSG = (
    "This migration ran outside a transaction and was not rolled back: "
    "drop what it left behind (eg: an invalid index) before running it again"
)


_TABLES_QUERY = """
SELECT c.oid, c.relfilenode, format('%I.%I', n.nspname, c.relname) AS name
FROM pg_class c
         JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'm')
  AND n.nspname NOT IN ('pg_catalog', 'information_schema')
  AND n.nspname !~ '^pg_toast'
"""

_EXCLUSIVE_LOCKS_QUERY = """
SELECT relation
FROM pg_locks
WHERE pid = pg_backend_pid()
  AND locktype = 'relation'
  AND mode = 'AccessExclusiveLock'
  AND granted
"""


@dataclasses.dataclass
class MigrationReport:
    """What applying one migration did to the tables that existed before it."""

    file_name: str
    duration: float
    locked_tables: list[str]
    """Tables it holds an ACCESS EXCLUSIVE lock on until commit (only seen inside a transaction)."""
    rewritten_tables: list[str]
    """Tables whose data file changed: a full rewrite (or a TRUNCATE), blocking for as long as the table is large."""
    rewrites_reason: str | None

    @property
    def has_unacknowledged_rewrites(self) -> bool:
        return bool(self.rewritten_tables) and self.rewrites_reason is None


async def _get_tables(conn: Connection) -> dict[int, tuple[int, str]]:
    return {row["oid"]: (row["relfilenode"], row["name"]) for row in await conn.fetch(_TABLES_QUERY)}


async def _get_exclusive_locks(conn: Connection) -> set[int]:
    return {row["relation"] for row in await conn.fetch(_EXCLUSIVE_LOCKS_QUERY)}


async def apply_migration(
    conn: asyncpg.Connection, migration: MigrationFile, metadata: dict | None = None
) -> MigrationReport:
    logs.info(f"Running {migration.path.name} (ts: {migration.ts})...")
    tables_before = await _get_tables(conn)
    locks_before = await _get_exclusive_locks(conn)
    start = time.perf_counter()
    try:
        await exec_file(conn, migration.path)
    except Exception as e:
        logs.error(f'Failed to execute migration_id "{migration.file_id}" ({e})')
        if migration.no_transaction:
            logs.error(NO_TRANSACTION_FAILURE_MSG)
        raise e
    duration = time.perf_counter() - start
    tables_after = await _get_tables(conn)
    new_locks = await _get_exclusive_locks(conn) - locks_before

    report = MigrationReport(
        file_name=migration.path.name,
        duration=duration,
        locked_tables=sorted(name for oid, (_, name) in tables_before.items() if oid in new_locks),
        rewritten_tables=sorted(
            name
            for oid, (filenode, name) in tables_before.items()
            if oid in tables_after and tables_after[oid][0] != filenode
        ),
        rewrites_reason=migration.rewrites_reason,
    )
    logs.info(f"Applied {migration.path.name} in {duration:.3f}s")
    if report.rewritten_tables:
        logs.warning(f"{migration.path.name} rewrote {', '.join(report.rewritten_tables)}")

    migration_data = {
        "file_ts": migration.ts,
        "file_id": migration.file_id,
        "migration_type": "up",
        "file_name": migration.path.name,
    }
    if metadata:
        migration_data["meta"] = metadata
    await insert_one(conn, "public.migration", migration_data)
    return report


async def _set_timeouts(conn: Connection, *, lock_timeout: str | None, statement_timeout: str | None):
    """Scope the timeouts (Postgres durations, e.g. `5s`) to the current transaction."""
    timeouts = {"lock_timeout": lock_timeout, "statement_timeout": statement_timeout}
    if not any(timeouts.values()):
        return
    if not conn.is_in_transaction():
        raise ValueError("Migration timeouts need a transaction (use_transaction=True or an outer one)")
    for name, value in timeouts.items():
        if value is not None:
            await conn.execute("SELECT set_config($1, $2, true)", name, value)


async def migrate_up(
    conn: Connection,
    folder: Path,
    *,
    nb_migrations: int = -1,
    metadata: dict | None = None,
    use_transaction: bool = True,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> bool:
    """Migrate from the latest migration applied available in the database
    to the latest one in the `folder` dir.
    `lock_timeout` and `statement_timeout` apply to the whole transaction; a migration's own `SET LOCAL` wins.
    """
    migration_files = await get_migration_files(conn, folder, nb_migrations=nb_migrations)

    if not migration_files:
        logs.info("No migrations to apply")
        return False

    logs.info(f"Found {len(migration_files)} migrations to apply")
    for _no_transaction, _migrations in groupby(migration_files, key=attrgetter("no_transaction")):
        async with conn.transaction() if use_transaction and not _no_transaction else nullcontext():
            if not _no_transaction:
                await _set_timeouts(conn, lock_timeout=lock_timeout, statement_timeout=statement_timeout)
            for _migration in _migrations:
                await apply_migration(conn, _migration, metadata)
    logs.info("Done!")
    return True


async def migrate_report(
    conn: Connection,
    folder: Path,
    *,
    nb_migrations: int = -1,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> list[MigrationReport]:
    """Apply the pending migrations in one transaction, report what each one locked and rewrote, then roll back.
    `-- No-transaction:` migrations cannot run in that transaction, so they are skipped.
    """
    migration_files = []
    for _migration in await get_migration_files(conn, folder, nb_migrations=nb_migrations):
        if _migration.no_transaction:
            logs.warning(f"Skipping {_migration.path.name}: it runs outside a transaction")
        else:
            migration_files.append(_migration)
    transaction = conn.transaction()
    await transaction.start()
    try:
        await _set_timeouts(conn, lock_timeout=lock_timeout, statement_timeout=statement_timeout)
        return [await apply_migration(conn, _migration) for _migration in migration_files]
    finally:
        await transaction.rollback()


class AppliedMigration(typing.TypedDict):
    file_id: str
    has_applied_rollback: bool


async def get_applied_migrations(conn: asyncpg.Connection) -> list[AppliedMigration]:
    try:
        data = await conn.fetch(
            """
            SELECT _m.file_id,
                   exists(select 1
                          from public.migration _r
                          where _r.migration_type = 'down' and _r.file_id = _m.file_id and _r.id > _m.id)
                       as has_applied_rollback
            FROM public.migration _m
            where _m.migration_type = 'up'
            order by _m.applied_at desc
            """
        )
    except UndefinedTableError as e:
        if 'relation "public.migration" does not exist' in str(e):
            raise NoSetupTableError(
                'Could not find table "public.migration", did you forget to setup the table'
                ' by running "migration setup" ?'
            )
        else:
            raise
    return [
        AppliedMigration(file_id=item["file_id"], has_applied_rollback=item["has_applied_rollback"]) for item in data
    ]


async def get_rollback_files(
    conn: asyncpg.Connection,
    folder: Path,
    *,
    nb_migrations: int = -1,
    migration_id: str | None = None,
) -> list[MigrationFile]:
    if migration_id is not None and nb_migrations >= 0:
        raise ValueError('Specify either "nb_migrations" or "migration_id"')

    applied_migrations = await get_applied_migrations(conn)
    applied_migration_ids = {item["file_id"] for item in applied_migrations if not item["has_applied_rollback"]}
    rollback_to_apply = [
        _down_file
        for _up_file, _down_file in iter_migration_files(get_files(folder))
        if _down_file.file_id in applied_migration_ids
    ][::-1]

    if migration_id is not None:
        for idx, _rollback in enumerate(rollback_to_apply):
            if _rollback.file_id == migration_id:
                rollback_to_apply = rollback_to_apply[: idx + 1]
                break
        else:
            raise ValueError(f"Could not find migration_id {migration_id!r}")
    elif nb_migrations > 0:
        rollback_to_apply = rollback_to_apply[:nb_migrations]

    return rollback_to_apply


async def migrate_down(
    conn: Connection,
    folder: Path,
    *,
    nb_migrations: int | None = None,
    metadata: dict | None = None,
    use_transaction: bool = True,
    migration_id: str | None = None,
    lock_timeout: str | None = None,
    statement_timeout: str | None = None,
) -> bool:
    """
    Rollback `nb_migrations` back (default -1 for all available) or until migration_id (if set).
    The timeouts behave as in `migrate_up`.
    """
    rollback_files = await get_rollback_files(
        conn, folder, migration_id=migration_id, nb_migrations=nb_migrations or -1
    )
    nb_files = len(rollback_files)
    if nb_files == 0:
        logs.info("No rollback files to apply")
        return False

    logs.info(f"Found {nb_files} rollback files to apply")
    for _no_transaction, _rollbacks in groupby(rollback_files, key=attrgetter("no_transaction")):
        async with conn.transaction() if use_transaction and not _no_transaction else nullcontext():
            if not _no_transaction:
                await _set_timeouts(conn, lock_timeout=lock_timeout, statement_timeout=statement_timeout)
            for _rollback in _rollbacks:
                logs.info(f"Running {_rollback.path.name}...")
                try:
                    await exec_file(conn, _rollback.path)
                except Exception as e:
                    logs.error(f'Failed to execute migration_id "{_rollback.file_id}"')
                    if _rollback.no_transaction:
                        logs.error(NO_TRANSACTION_FAILURE_MSG)
                    raise e

                migration_data = {
                    "file_ts": _rollback.ts,
                    "file_id": _rollback.file_id,
                    "migration_type": "down",
                    "file_name": _rollback.path.name,
                }
                if metadata:
                    migration_data["meta"] = metadata
                await insert_one(conn, "public.migration", migration_data)

    logs.info("Done!")
    return True


async def get_missing_migrations(conn: asyncpg.Connection, folder: Path, *, chunk_size: int = 500):
    files = get_files(folder, up_only=True)
    not_applied_files = []
    for chunk in get_chunks(files, size=chunk_size, as_list=True):
        file_ids = await conn.fetch(
            """
            SELECT file_id
            from migration
            where migration_type = 'up'
              and file_id = any ($1)
            """,
            [file.file_id for file in chunk],
        )
        file_ids_db = set(file["file_id"] for file in file_ids)
        for _file in chunk:
            if _file.file_id not in file_ids_db:
                not_applied_files.append(_file)

    return not_applied_files


async def verify_migrations(
    conn: asyncpg.Connection, folder: Path, *, chunk_size: int = 500, metadata: dict | None = None
):
    """
    Verify that all migrations inside "folder" have been applied to the database.
    If not, tries to apply them
    """
    not_applied_files = await get_missing_migrations(conn, folder=folder, chunk_size=chunk_size)
    if not not_applied_files:
        logs.info("All migrations have been applied")
        return
    logs.info(f"Found {len(not_applied_files)} missing files")
    _meta = metadata or {"missing": True}
    for _file in not_applied_files:
        await apply_migration(conn, _file, metadata=_meta)
