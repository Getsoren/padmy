import asyncio
import logging
import sys
import tempfile
from pathlib import Path


from piou import Cli, Derived, Option

try:
    from faker import Faker
except ImportError:
    Faker = None  # type: ignore[assignment,misc]


from padmy.anonymize import anonymize_db
from padmy.config import Config
from padmy.db import pretty_print_stats, pprint_compared_dbs, Database
from padmy.logs import setup_logging, logs
from padmy.migration import migration
from padmy.sampling import sample_database, copy_database
from padmy.utils import get_pg_infos, get_pg_infos_from, PGConnectionInfo
from padmy.env import CONSOLE

cli = Cli("Padmy utility commands")

cli.add_option("-v", "--verbose", help="Verbosity")
cli.add_option("-vv", "--verbose2", help="Increased verbosity")

cli.add_command_group(migration)


def on_process(verbose: bool = False, verbose2: bool = False):
    setup_logging(logging.DEBUG if verbose2 else logging.INFO if verbose else logging.WARNING)


cli.set_options_processor(on_process)


async def get_explored_db(pg_info: PGConnectionInfo, db_name: str, schemas: list[str]) -> Database:
    """Explore a database and return the Database object."""
    db = Database(name=db_name)
    logs.info(f"Gathering information about {db_name!r}...")
    async with pg_info.get_pool(db_name) as pool:
        await db.explore(pool, schemas)
    return db


@cli.command(cmd="anonymize", help="Run anonymization")
async def ano_main(
    pg_infos: PGConnectionInfo = Derived(get_pg_infos),
    db_name: str = Option(..., "--db", help="Database to anonymize"),
    config_path: Path = Option(..., "-f", "--file", help="Path to the configuration file"),
    locale: str | None = Option(None, "--locale", help="Faker locale used to generate fake data (eg. fr_FR)"),
):
    if Faker is None:
        raise ImportError('Please install faker or padmy with "anonymize" to use this module')
    faker = Faker(locale)

    config = Config.load_from_file(config_path)
    async with pg_infos.get_pool(db_name) as pool:
        await anonymize_db(pool, config, faker)


@cli.command(cmd="sample", help="Sample database")
async def sample_main(
    from_db: str = Option(..., "--db", help="Database to migrate from"),
    to_db: str = Option(..., "--db-to", help="Database to migrate to"),
    schemas: list[str] | None = Option(None, "--schemas", help="Schemas to samples"),
    sample: int | None = Option(None, "--sample", help="Sample size"),
    config_path: Path | None = Option(None, "-f", "--file", help="Path to the configuration file"),
    copy_db: bool = Option(False, "--copy-db", help="Copy database before sampling"),
    pg_from_info: PGConnectionInfo = Derived(get_pg_infos_from("from")),
    pg_to_info: PGConnectionInfo = Derived(get_pg_infos_from("to")),
    progress: bool = Option(False, "--progress", help="Show sampling progress"),
):
    """
    Create a copy of a given database and inserts a sample to a new database
    """

    if config_path:
        config = Config.load_from_file(config_path)
    elif sample is not None and schemas is not None:
        config = Config.load(sample, schemas=schemas)
    else:
        raise NotImplementedError("Please specify either a sample size and schemas or a config path")

    _schemas = [x.schema for x in config.schemas]
    if copy_db:
        logs.info(f"Copying {from_db!r} to {to_db!r}")
        try:
            copy_database(
                from_pg_uri=pg_from_info,
                to_pg_uri=pg_to_info,
                from_db=from_db,
                to_db=to_db,
                schemas=_schemas,
            )
        except Exception as e:
            logs.error(e)
            sys.exit(1)
        else:
            logs.info("Done!")

    # Explore source database
    db = await get_explored_db(pg_from_info, from_db, _schemas)
    db.load_config(config)
    pretty_print_stats(db)

    async with pg_from_info.get_conn(database=from_db) as conn:
        async with pg_to_info.get_conn(database=to_db) as target_conn:
            await sample_database(conn, target_conn, db, show_progress=progress)

    # Explore target database
    new_db = await get_explored_db(pg_to_info, to_db, _schemas)
    pprint_compared_dbs(db, new_db)


@cli.command(cmd="copy-db", help="Copy a database schema to a new database")
def copy_main(
    from_db: str = Option(..., "--db", help="Database to migrate from"),
    to_db: str = Option(..., "--db-to", help="Database to migrate to"),
    schemas: list[str] = Option(..., "--schemas", help="Schemas to samples"),
    pg_from_info: PGConnectionInfo = Derived(get_pg_infos_from("from")),
    pg_to_info: PGConnectionInfo = Derived(get_pg_infos_from("to")),
):
    from padmy.sampling import copy_database

    logs.info(f"Copying {from_db!r} to {to_db!r}")
    copy_database(
        from_pg_uri=pg_from_info,
        to_pg_uri=pg_to_info,
        from_db=from_db,
        to_db=to_db,
        schemas=schemas,
        drop_public=False,
    )
    logs.info("Done!")


@cli.command(cmd="analyze", help="Analyze database")
async def analyze_main(
    db_name: str = Option(..., "--db", help="Database to explore"),
    pg_infos: PGConnectionInfo = Derived(get_pg_infos),
    schemas: list[str] = Option(..., "--schemas", help="Schemas to analyze"),
    show_graphs: bool = Option(False, "--show-graphs", help="Show graphs of the database"),
    port: int = Option(5555, "--graph-port", help="Port for the graph"),
):
    """
    Show information about a database
    """
    from padmy.db import pretty_print_stats

    db = await get_explored_db(pg_infos, db_name, schemas)

    if not show_graphs:
        pretty_print_stats(db)
        return

    from padmy.sampling.viz import run_simple_app

    run_simple_app(db, port=port)


@cli.command(cmd="compare", help="Compare 2 databases")
async def compare_db_main(
    from_db: str = Option(..., "--db", help="Database to explore"),
    to_db: str = Option(..., "--db-to", help="Database to explore"),
    pg_url: PGConnectionInfo = Derived(get_pg_infos),
    schemas: list[str] = Option(..., "--schemas", help="Schemas to analyze"),
):
    from padmy.db import pprint_compared_dbs

    db1, db2 = await asyncio.gather(
        get_explored_db(pg_url, from_db, schemas),
        get_explored_db(pg_url, to_db, schemas),
    )

    pprint_compared_dbs(db1, db2)


@cli.command(cmd="dump", help="Dump a database using pg_dump")
def dump_main(
    pg_infos: PGConnectionInfo = Derived(get_pg_infos),
    output: Path | None = Option(
        None, "-o", "--output", help="Output file path (prints to stdout if not set)", raise_path_does_not_exist=False
    ),
    database: str = Option(..., "--db", help="Database to dump"),
    schemas: list[str] = Option(..., "--schemas", help="Schemas to dump"),
    with_grants: bool = Option(False, "--with-grants", help="Include owner and privileges"),
    with_comments: bool = Option(False, "--with-comments", help="Include comments"),
    schema_only: bool = Option(False, "--schema-only", help="Dump only schema, no data"),
    no_owner: bool = Option(False, "--no-owner", help="Exclude ownership statements"),
    restrict_key: str | None = Option(None, "--restrict-key", help="Restrict key to be written in \restrict"),
):
    """
    Convenience wrapper around pg_dump. Dumps schema only without owner/privileges by default.
    """
    from padmy.utils import pg_dump

    options = []
    if schema_only:
        options.append("--schema-only")
    if no_owner:
        options.append("--no-owner")

    with pg_infos.temp_env(include_database=False):
        result = pg_dump(
            database=database,
            schemas=schemas,
            dump_path=str(output) if output else None,
            options=options if options else None,
            with_grants=with_grants,
            with_comments=with_comments,
            restrict_key=restrict_key,
            on_stderr=lambda x: logs.error(x),
            get_env=False,
        )

    if output:
        logs.info(f"Dump written to {output}")
    elif result:
        print(result)


@cli.command(cmd="schema-diff", help="Compare the schemas of 2 databases")
def schema_diff(
    pg_from_info: PGConnectionInfo = Derived(get_pg_infos_from("from")),
    pg_to_info: PGConnectionInfo = Derived(get_pg_infos_from("to")),
    database: str = Option(..., "--db", help="Database to compare"),
    schemas: list[str] = Option(..., "--schemas", help="Schemas to analyze"),
    no_privileges: bool = Option(False, "-x", "--no-privileges", help="Exclude privileges from the dump"),
):
    from padmy.compare import compare_databases

    with tempfile.TemporaryDirectory() as dump_dir:
        _dump_dir = Path(dump_dir)
        diff = compare_databases(
            from_pg_url=pg_from_info,
            to_pg_url=pg_to_info,
            schemas=schemas,
            database=database,
            dump_dir=_dump_dir,
            no_privileges=no_privileges,
        )
    if diff is not None:
        CONSOLE.print("[orange]Differences found[/orange]")
        CONSOLE.print("\n".join(diff))
    else:
        CONSOLE.print("[green]No differences found[/green]")


def run():
    cli.run()


if __name__ == "__main__":
    run()
