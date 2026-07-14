from __future__ import annotations

import contextlib
import json
import os
import re
import ssl
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from functools import partial
from importlib import reload
from pathlib import Path
from typing import Sequence, AsyncGenerator, AsyncIterator, Callable, TypeVar, cast, Literal

import asyncpg
from asyncpg import Connection
from asyncpg.pool import PoolConnectionProxy
from piou import Option, Password

from padmy import env
from .env import (
    PG_HOST,
    PG_PORT,
    PG_USER,
    PG_DATABASE,
    PG_PASSWORD,
    PG_SSL_MODE,
    PG_SSL_CA,
    PG_SSL_CERT,
    PG_SSL_KEY,
    PG_SSL_PASSWORD,
)
from .logs import logs

type PgConnection = Connection | PoolConnectionProxy

PgHost = Option(PG_HOST, "--host", help="PG Host")
PgPort = Option(PG_PORT, "--port", help="PG Port")
PgUser = Option(PG_USER, "--user", help="PG User")
PgPassword = Option(PG_PASSWORD, "-p", help="PG Password")
PgDatabase = Option(PG_DATABASE, "--db", help="PG Database")

OnStdErrorFn = Callable[[str], None]


def exec_cmd(
    cmd: Sequence[str | int],
    env: dict | None = None,
    *,
    on_stderr: OnStdErrorFn | None = None,
) -> str:
    _cmd = cmd if isinstance(cmd, str) else " ".join(str(x) for x in cmd)
    logs.debug(f"Executing cmd: {_cmd}")
    stdout, stderr = subprocess.Popen(
        _cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env
    ).communicate()
    if stderr:
        _msg = stderr.decode("utf-8")
        if on_stderr is not None:
            on_stderr(_msg)
        else:
            logs.warning(_msg)
    return stdout.decode("utf-8")


def has_cmd(cmd: str) -> bool:
    return exec_cmd(f'command -v "{cmd}"') != ""


class CommandNotFound(Exception):
    def __init__(self, cmd: str):
        super().__init__()
        self.cmd = cmd


_COMMANDS = {}


def check_cmd(cmd: str):
    global _COMMANDS
    path = exec_cmd(f'command -v "{cmd}"')
    if path == "":
        raise CommandNotFound(cmd=cmd)
    _COMMANDS[cmd] = path.strip()


def _get_check_cmd(cmd: str):
    if cmd not in _COMMANDS:
        check_cmd(cmd)
    return _COMMANDS[cmd]


def create_ssl_context(
    ssl_mode: str | None = None,
    ssl_ca: Path | None = None,
    ssl_cert: Path | None = None,
    ssl_key: Path | None = None,
    ssl_password: str | None = None,
) -> ssl.SSLContext | None:
    """
    Creates an SSL context for PostgreSQL connections with mTLS support.

    Args:
        ssl_mode: SSL mode - "require", "verify-ca", or "verify-full"
        ssl_ca: Path to CA certificate file
        ssl_cert: Path to client certificate file
        ssl_key: Path to client private key file
        ssl_password: Password for encrypted private key file

    Returns:
        SSLContext configured for mTLS, or None if SSL is not configured
    """
    # If no SSL configuration is provided, return None (no SSL)
    if not ssl_mode and not ssl_ca and not ssl_cert and not ssl_key:
        return None

    # Create SSL context
    ctx = ssl.create_default_context(ssl.Purpose.SERVER_AUTH)

    # Load CA certificate if provided
    if ssl_ca:
        if not ssl_ca.exists():
            raise FileNotFoundError(f"CA certificate not found: {ssl_ca.absolute()!r}")
        logs.debug(f"Loading CA certificate from {ssl_ca.absolute()!r}")
        ctx.load_verify_locations(cafile=str(ssl_ca))

    # Load client certificate and key for mTLS if provided
    if ssl_cert and ssl_key:
        if not ssl_cert.exists():
            raise FileNotFoundError(f"Client certificate not found: {ssl_cert.absolute()}")
        if not ssl_key.exists():
            raise FileNotFoundError(f"Client key not found: {ssl_key.absolute()}")
        logs.debug(f"Loading client certificate from {ssl_cert.absolute()} and key from {ssl_key.absolute()}")

        # Load certificate chain with optional password for encrypted private key
        password_bytes = ssl_password.encode() if ssl_password else None
        ctx.load_cert_chain(certfile=str(ssl_cert), keyfile=str(ssl_key), password=password_bytes)
    elif ssl_cert or ssl_key:
        raise ValueError("Both ssl_cert and ssl_key must be provided together for mTLS")

    # Configure SSL verification mode
    if ssl_mode == "require":
        # Require SSL but don't verify the server certificate
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        logs.debug("SSL mode: require (no certificate verification)")
    elif ssl_mode == "verify-ca":
        # Verify server certificate against CA
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_REQUIRED
        logs.debug("SSL mode: verify-ca (verify certificate authority)")
    elif ssl_mode == "verify-full":
        # Verify server certificate and hostname
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        logs.debug("SSL mode: verify-full (verify certificate and hostname)")
    elif ssl_mode:
        raise ValueError(f"Invalid SSL mode: {ssl_mode}. Use 'require', 'verify-ca', or 'verify-full'")
    else:
        # Default to verify-full if SSL is configured but mode is not specified
        ctx.check_hostname = True
        ctx.verify_mode = ssl.CERT_REQUIRED
        logs.debug("SSL mode: verify-full (default)")

    return ctx


def get_pg_envs():
    """Build the libpq env dict passed to subprocess calls (pg_dump, createdb, …).

    Prefer values already set in os.environ — `PGConnectionInfo.temp_env`
    populates PGHOST/PGPORT/PGUSER/PGPASSWORD inside its `with` block so that
    `--host-to` / `PG_PORT_TO` overrides flow through. Falling back to the
    `env` module defaults (PG_HOST / PG_PORT) only when libpq vars aren't set
    keeps the standalone CLI path working.
    """
    reload(env)
    envs = {
        "PGPASSWORD": os.environ.get("PGPASSWORD", env.PG_PASSWORD),
        "PGUSER": os.environ.get("PGUSER", env.PG_USER),
        "PGHOST": os.environ.get("PGHOST", env.PG_HOST),
        "PGPORT": os.environ.get("PGPORT", str(env.PG_PORT)),
    }
    # Add SSL environment variables if configured
    if PG_SSL_MODE:
        envs["PGSSLMODE"] = PG_SSL_MODE
    if PG_SSL_CA:
        envs["PGSSLROOTCERT"] = str(PG_SSL_CA)
    if PG_SSL_CERT:
        envs["PGSSLCERT"] = str(PG_SSL_CERT)
    if PG_SSL_KEY:
        envs["PGSSLKEY"] = str(PG_SSL_KEY)
    if PG_SSL_PASSWORD:
        envs["PGSSLPASSWORD"] = PG_SSL_PASSWORD
    return envs


class PGError(Exception):
    def __init__(self, msg: str, cmd: str | None = None):
        super().__init__(msg)
        self.msg = msg
        self.cmd = cmd
        self.errors = extract_pg_error(msg)

    @property
    def msg_fmt(self):
        return "=== ERROR ====\n\n" + "\n\n=== ERROR ====\n\n".join(self.errors)


def _on_pg_error(msg: str, cmd: list[str] | str):
    if "ERROR" in msg or "FATAL" in msg:
        _cmd = cmd if isinstance(cmd, str) else " ".join(str(x) for x in cmd)
        raise PGError(msg, cmd=_cmd)


def _get_conn_infos(
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
):
    infos = {"-U": user, "-W": password, "-h": host, "-p": port}
    cmd = []
    for k, v in infos.items():
        if v is not None:
            cmd += [k, v]
    return cmd


def pg_dump(
    database: str,
    schemas: list[str],
    dump_path: str | None = None,
    options: list[str] | None = None,
    with_grants: bool = True,
    with_comments: bool = True,
    restrict_key: str | None = None,
    *,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
    get_env: bool = True,
) -> str | None:
    _schemas = "|".join(x for x in schemas)
    cmd = [
        _get_check_cmd("pg_dump"),
        "-n",
        f"'({_schemas})'",
        "-d",
        database,
    ] + _get_conn_infos(user, password, host, port)
    if not with_grants:
        cmd += ["--no-owner", "--no-privileges"]
    if not with_comments:
        cmd += ["--no-comments"]
    if restrict_key is not None:
        cmd += [f"--restrict-key={restrict_key}"]
    if options:
        cmd += options

    if dump_path:
        cmd += [">", dump_path]
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    return exec_cmd(cmd, env=get_pg_envs() if get_env else None, on_stderr=_on_stderr)


def pg_restore(
    database: str,
    dump_path: str,
    options: list[str] | None = None,
    *,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
):
    cmd = [_get_check_cmd("pg_restore"), "-d", database, dump_path] + _get_conn_infos(user, password, host, port)
    if options is not None:
        cmd += options
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    exec_cmd(cmd, env=get_pg_envs(), on_stderr=_on_stderr)


def create_db(
    database: str,
    *,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
):
    cmd = [_get_check_cmd("createdb"), database] + _get_conn_infos(user, password, host, port)
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    exec_cmd(cmd, env=get_pg_envs(), on_stderr=_on_stderr)


def drop_db(
    database: str,
    *,
    if_exists: bool = True,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
):
    cmd = [_get_check_cmd("dropdb")] + _get_conn_infos(user, password, host, port)

    if if_exists:
        cmd.append("--if-exists")
    cmd.append(database)
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    exec_cmd(cmd, env=get_pg_envs(), on_stderr=_on_stderr)


def exec_psql_file(
    database: str,
    sql_file: str,
    *,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
):
    cmd = [_get_check_cmd("psql"), "-f", sql_file, "-d", database] + _get_conn_infos(user, password, host, port)
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    exec_cmd(cmd, env=get_pg_envs(), on_stderr=_on_stderr)


def exec_psql(
    database: str,
    query: str,
    *,
    user: str | None = None,
    password: str | None = None,
    host: str | None = None,
    port: int | None = None,
    on_stderr: OnStdErrorFn | None = None,
):
    cmd = [_get_check_cmd("psql"), "-c", f"'{query}'", "-d", database] + _get_conn_infos(user, password, host, port)
    _on_stderr = on_stderr or partial(_on_pg_error, cmd=cmd)
    exec_cmd(cmd, env=get_pg_envs(), on_stderr=_on_stderr)


async def exec_file(conn: asyncpg.Connection, file: Path):
    await conn.execute(file.read_text())


async def iterate_pg(
    conn: asyncpg.Connection,
    query: str,
    *args,
    from_offset: int = 0,
    chunk_size: int = 500,
    timeout: int | None = None,
) -> AsyncGenerator[list[asyncpg.Record], None]:
    async with conn.transaction():
        cur: asyncpg.connection.cursor.Cursor = await conn.cursor(query, *args)
        if from_offset:
            await cur.forward(from_offset, timeout=timeout)
        while data := await cur.fetch(chunk_size, timeout=timeout):
            yield cast(list[asyncpg.Record], data)


_EXT_EXISTS_QUERY = """
                    SELECT EXISTS(SELECT
                                  FROM pg_extension
                                  WHERE extname = $1) \
                    """


async def check_extension_exists(conn: asyncpg.Connection, extension: str) -> bool:
    _exists = await conn.fetchval(_EXT_EXISTS_QUERY, extension)
    if _exists is None:
        raise NotImplementedError()
    return _exists


_TMP_TABLE_EXISTS_QUERY = """
                          SELECT EXISTS (SELECT
                                         FROM information_schema.tables
                                         WHERE table_schema LIKE 'pg_temp_%'
                                           AND table_name = $1) \
                          """


async def check_tmp_table_exists(conn: asyncpg.Connection, table: str) -> bool:
    exists = await conn.fetchval(_TMP_TABLE_EXISTS_QUERY, table)
    if exists is None:
        raise NotImplementedError()
    return exists


X = TypeVar("X")


def get_first(*it: X, fn: Callable) -> X | None:
    for v in it:
        if fn(v):
            return v
    return None


async def get_conn(pool: asyncpg.Pool, fn: Callable):
    async with pool.acquire() as conn:
        await fn(conn=conn)


@dataclass
class PGConnectionInfo:
    """Connection information for PostgreSQL including DSN and SSL context."""

    pg_user: str
    pg_password: str
    pg_host: str
    pg_port: int
    ssl_mode: str | None = None
    ssl_ca: Path | None = None
    ssl_cert: Path | None = None
    ssl_key: Path | None = None
    ssl_password: str | None = None
    database: str | None = None

    @classmethod
    def from_uri(cls, uri: str) -> "PGConnectionInfo":
        """
        Parse a PostgreSQL URI and create a PGConnectionInfo instance.

        Supports URIs in the format:
        postgresql://user:password@host:port/dbname?sslmode=...&sslrootcert=...&sslcert=...&sslkey=...

        Args:
            uri: PostgreSQL connection URI

        Returns:
            PGConnectionInfo instance

        Raises:
            ValueError: If the URI format is invalid
        """
        import re
        from urllib.parse import parse_qs

        pattern = re.compile(
            r"postgresql://(?P<user>[^:]+):(?P<password>[^@]+)@(?P<host>[^:]+):(?P<port>\d+)"
            r"(/(?P<dbname>[^?]+))?(\?(?P<params>.+))?"
        )

        if match := pattern.match(uri):
            group = match.groupdict()

            # Parse query parameters for SSL settings
            ssl_mode = None
            ssl_ca = None
            ssl_cert = None
            ssl_key = None

            if params_str := group.get("params"):
                params = parse_qs(params_str)
                ssl_mode = params.get("sslmode", [None])[0]
                ssl_ca = params.get("sslrootcert", [None])[0]
                ssl_cert = params.get("sslcert", [None])[0]
                ssl_key = params.get("sslkey", [None])[0]

            return cls(
                pg_user=group["user"],
                pg_password=group["password"],
                pg_host=group["host"],
                pg_port=int(group["port"]),
                database=group["dbname"],
                ssl_mode=ssl_mode,
                ssl_ca=Path(ssl_ca) if ssl_ca else None,
                ssl_cert=Path(ssl_cert) if ssl_cert else None,
                ssl_key=Path(ssl_key) if ssl_key else None,
            )
        raise ValueError(f"Invalid PG uri: {uri}")

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.pg_user}:{self.pg_password}@{self.pg_host}:{self.pg_port}"

    @property
    def obfuscated_dsn(self) -> str:
        return f"postgresql://{self.pg_user}:****@{self.pg_host}:{self.pg_port}"

    @property
    def ssl_context(self) -> ssl.SSLContext | None:
        ssl_context = create_ssl_context(
            ssl_mode=self.ssl_mode,
            ssl_ca=self.ssl_ca,
            ssl_cert=self.ssl_cert,
            ssl_key=self.ssl_key,
            ssl_password=self.ssl_password or PG_SSL_PASSWORD,
        )
        return ssl_context

    def get_env_vars(self) -> dict[str, str]:
        """Get environment variables for CLI tools based on SSL context."""
        envs = {}
        if self.ssl_mode:
            envs["PGSSLMODE"] = self.ssl_mode
        if self.ssl_ca:
            envs["PGSSLROOTCERT"] = str(self.ssl_ca)
        if self.ssl_cert:
            envs["PGSSLCERT"] = str(self.ssl_cert)
        if self.ssl_key:
            envs["PGSSLKEY"] = str(self.ssl_key)
        return envs

    @property
    def pg_url(self) -> str:
        """Returns the PostgreSQL connection URL with SSL parameters appended."""
        if not self.ssl_mode and not self.ssl_ca and not self.ssl_cert and not self.ssl_key:
            return self.dsn

        # Build SSL query parameters for asyncpg DSN format
        ssl_params = []
        if self.ssl_mode:
            ssl_params.append(f"sslmode={self.ssl_mode}")
        if self.ssl_ca:
            ssl_params.append(f"sslrootcert={self.ssl_ca}")
        if self.ssl_cert:
            ssl_params.append(f"sslcert={self.ssl_cert}")
        if self.ssl_key:
            ssl_params.append(f"sslkey={self.ssl_key}")

        # Append parameters to URI
        separator = "&" if "?" in self.dsn else "?"
        return f"{self.dsn}{separator}{'&'.join(ssl_params)}"

    @contextmanager
    def temp_env(self, *, include_database: bool = True):
        """
        Context manager that sets PostgreSQL environment variables.

        Args:
            include_database: Whether to include PGDATABASE in the environment variables
        """
        _env = {
            "PGHOST": self.pg_host,
            "PGPORT": str(self.pg_port),
            "PGUSER": self.pg_user,
            "PGPASSWORD": self.pg_password,
        }

        if include_database and self.database:
            _env["PGDATABASE"] = self.database

        # Add SSL environment variables
        _env.update(self.get_env_vars())

        # Save current environment
        old_env = os.environ.copy()
        os.environ.update(_env)
        try:
            yield
        finally:
            # Restore original environment
            os.environ.clear()
            os.environ.update(old_env)

    @contextlib.asynccontextmanager
    async def get_conn(self, database: str | None = None, timeout: int = 5):
        _database = database if database is not None else self.database
        if _database is None:
            raise ValueError("Either database parameter or PGConnectionInfo.database must be set")
        dsn = f"{self.dsn}/{_database}"
        conn = await asyncpg.connect(dsn=dsn, ssl=self.ssl_context, timeout=timeout)
        await init_connection(conn)
        try:
            yield conn
        finally:
            await conn.close()

    @contextlib.asynccontextmanager
    async def get_pool(self, database: str | None = None, timeout: int = 5) -> AsyncIterator[asyncpg.Pool]:
        if database is not None:
            _database = database
        else:
            _database = self.database
        if _database is None:
            raise ValueError("Either database parameter or PGConnectionInfo.database must be set")
        dsn = f"{self.dsn}/{_database}"
        async with asyncpg.create_pool(dsn=dsn, ssl=self.ssl_context, init=init_connection, timeout=timeout) as pool:
            yield pool


def get_pg_infos(
    pg_host: str = PgHost,
    pg_port: int = PgPort,
    pg_user: str = PgUser,
    pg_password: str = PgPassword,
    ssl_mode: str | None = Option(
        PG_SSL_MODE,
        "--ssl-mode",
        help="SSL mode for (require, verify-ca, verify-full)",
    ),
    ssl_ca: Path | None = Option(
        PG_SSL_CA,
        "--ssl-ca",
        help="Path to CA certificate",
    ),
    ssl_cert: Path | None = Option(
        PG_SSL_CERT,
        "--ssl-cert",
        help="Path to client certificate",
    ),
    ssl_key: Path | None = Option(
        PG_SSL_KEY,
        "--ssl-key",
        help="Path to client private key",
    ),
) -> PGConnectionInfo:
    return PGConnectionInfo(
        pg_user=pg_user,
        pg_password=pg_password,
        pg_host=pg_host,
        pg_port=pg_port,
        ssl_ca=ssl_ca,
        ssl_cert=ssl_cert,
        ssl_key=ssl_key,
        ssl_mode=ssl_mode,
    )


def get_pg_infos_from(source: Literal["from", "to"]):
    _source, _source_lower = source.upper(), source.lower()

    _source_ssl_ca = os.getenv(f"PGSSLROOTCERT_{_source}")
    _source_ssl_cert = os.getenv(f"PGSSLCERT_{_source}")
    _source_ssl_key = os.getenv(f"PGSSLKEY_{_source}")

    def _get_pg_root(
        pg_host: str = Option(
            os.getenv(f"PG_HOST_{_source}", "localhost"),
            f"--host-{_source_lower}",
            help=f"PG Host {_source}",
            arg_name=f"pg_host_{_source}",
        ),
        pg_port: int = Option(
            int(os.getenv(f"PG_PORT_{_source}", 5432)),
            f"--port-{_source_lower}",
            help=f"PG Port {_source}",
            arg_name=f"pg_port_{_source}",
        ),
        pg_user: str = Option(
            os.getenv(f"PG_USER_{_source}", "postgres"),
            f"--user-{_source_lower}",
            help=f"PG User {_source}",
            arg_name=f"pg_user_{_source}",
        ),
        pg_password: Password = Option(
            os.getenv(f"PG_PASSWORD_{_source}", "postgres"),
            f"--password-{_source_lower}",
            help=f"PG Password {_source}",
            arg_name=f"pg_password_{_source}",
        ),
        ssl_mode: str | None = Option(
            os.getenv(f"PGSSLMODE_{_source}"),
            f"--ssl-mode-{_source_lower}",
            help=f"SSL mode for {_source} (require, verify-ca, verify-full)",
            arg_name=f"ssl_mode_{_source}",
        ),
        ssl_ca: Path | None = Option(
            (Path(_source_ssl_ca) if _source_ssl_ca and _source_ssl_ca != "system" else None),
            f"--ssl-ca-{_source_lower}",
            help=f"Path to CA certificate for {_source}",
            arg_name=f"ssl_ca_{_source}",
        ),
        ssl_cert: Path | None = Option(
            Path(_source_ssl_cert) if _source_ssl_cert else None,
            f"--ssl-cert-{_source_lower}",
            help=f"Path to client certificate for {_source}",
            arg_name=f"ssl_cert_{_source}",
        ),
        ssl_key: Path | None = Option(
            Path(_source_ssl_key) if _source_ssl_key else None,
            f"--ssl-key-{_source_lower}",
            help=f"Path to client private key for {_source}",
            arg_name=f"ssl_key_{_source}",
        ),
    ) -> PGConnectionInfo:
        info = PGConnectionInfo(
            pg_user=pg_user,
            pg_password=pg_password,
            pg_host=pg_host,
            pg_port=pg_port,
            ssl_mode=ssl_mode,
            ssl_ca=ssl_ca,
            ssl_cert=ssl_cert,
            ssl_key=ssl_key,
        )

        logs.debug(f"PG url {source}: {info.obfuscated_dsn}")
        return info

    return _get_pg_root


async def init_connection(conn: asyncpg.Connection):
    await conn.set_type_codec("jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")
    await conn.set_type_codec("json", encoder=json.dumps, decoder=json.loads, schema="pg_catalog")


_IS_ERROR_REG = re.compile(r".*(?P<error_type>ERROR|FATAL):(?P<msg>.*)")
_IS_INFO_REG = re.compile(r".*NOTICE:(?P<msg>.*)")
_CLEAN_STR_REG = re.compile(r"^E\s+|\s{2,}", flags=re.MULTILINE)


def _clean_str(msg: str) -> str:
    # Replace 'E' prefix and multiple spaces, then strip leading/trailing whitespace
    cleaned = _CLEAN_STR_REG.sub(" ", msg)
    # Remove leading spaces from each line
    lines = [line.strip() for line in cleaned.split("\n")]
    return "\n".join(lines).strip()


def extract_pg_error(msg: str) -> list[str]:
    """
    Extracts the errors from a pg error message
    """
    errors = []
    current_error = None
    for line in msg.splitlines():
        if _match_error := _IS_ERROR_REG.match(line):
            if current_error is not None:
                errors.append(_clean_str(current_error))
            current_error = f"{_match_error.group('error_type')}: {_match_error.group('msg')}"
        elif _match_info := _IS_INFO_REG.match(line):
            if current_error is not None:
                errors.append(_clean_str(current_error))
            current_error = None
        else:
            if current_error is not None:
                current_error += f"\n{_clean_str(line)}"

    if current_error is not None:
        errors.append(_clean_str(current_error))

    return errors


def remove_restrict_clauses(dump_path: Path) -> None:
    r"""Remove \restrict and \unrestrict clauses from a pg_dump file."""
    content = dump_path.read_text()
    filtered_lines = [
        line
        for line in content.splitlines()
        if not (line.strip().startswith(r"\restrict") or line.strip().startswith(r"\unrestrict"))
    ]
    result = "\n".join(filtered_lines)
    if content.endswith("\n"):
        result += "\n"
    dump_path.write_text(result)
