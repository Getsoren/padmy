import datetime as dt
import sys
import uuid
from typing import Literal, get_args, TypeGuard

if sys.version_info >= (3, 12):
    UTC = dt.UTC
else:
    from pytz import UTC  # pyright: ignore[reportMissingModuleSource]

import dataclasses
import textwrap
from itertools import groupby
from operator import attrgetter
from pathlib import Path
from padmy.logs import logs

from padmy.utils import exec_cmd

__all__ = (
    "Header",
    "MigrationFile",
    "get_files",
    "iter_migration_files",
    "verify_migration_files",
    "get_git_email",
    "utc_now",
    "MigrationFileError",
    "MigrationErrorType",
    "parse_filename",
)


PREFIXES = {
    "author": "-- Author:",
    "prev-file": "-- Prev-file:",
    "version": "-- Version:",
    "skip-verify": "-- Skip-verify:",
    "no-transaction": "-- No-transaction:",
}


def get_git_email():
    return exec_cmd(["git", "config", "user.email"]).strip()


@dataclasses.dataclass
class Header:
    prev_file: str | None
    author: str | None
    version: str | None
    skip_verify: bool = False
    skip_reason: str | None = None
    # runs outside the migration transaction (eg: CREATE INDEX CONCURRENTLY)
    no_transaction: bool = False

    def __post_init__(self):
        if self.skip_verify and self.skip_reason is None:
            self.skip_reason = "no reason provided"

    @property
    def is_empty(self):
        return not any([self.prev_file, self.author, self.version, self.no_transaction])

    @classmethod
    def from_text(cls, text: str):
        prev_file = None
        author = None
        version = None
        skip_verify = False
        skip_reason = None
        no_transaction = False
        for line in text.split("\n"):
            if line.startswith(PREFIXES["prev-file"]):
                prev_file = line.split(":")[1].strip()
            elif line.startswith(PREFIXES["author"]):
                author = line.split(":")[1].strip()
            elif line.startswith(PREFIXES["version"]):
                version = line.split(":")[1].strip()
            elif line.startswith(PREFIXES["skip-verify"]):
                skip_reason = line.split(":")[1].strip().lower()
                skip_verify = True
            elif line.startswith(PREFIXES["no-transaction"]):
                no_transaction = True
        return cls(
            prev_file,
            author,
            version,
            skip_verify=skip_verify,
            skip_reason=skip_reason,
            no_transaction=no_transaction,
        )

    def as_text(self):
        _header = [
            f"{PREFIXES['prev-file']} {self.prev_file or ''}",
            f"{PREFIXES['author']} {self.author or ''}",
        ]
        if self.version is not None:
            _header.append(f"{PREFIXES['version']} {self.version}")
        if self.skip_verify:
            _header.append(f"{PREFIXES['skip-verify']} {self.skip_reason or 'no reason provided'}")
        if self.no_transaction:
            _header.append(f"{PREFIXES['no-transaction']} true")

        file_header = textwrap.dedent("\n".join(_header)).strip()
        return file_header


@dataclasses.dataclass
class MigrationFile:
    ts: dt.datetime
    file_id: str
    file_type: str
    path: Path
    header: Header | None = None

    @property
    def name(self):
        return f"{int(self.ts.timestamp())}-{self.file_id}-{self.file_type}.sql"

    @property
    def skip_verify(self) -> bool:
        return self.header.skip_verify if self.header else False

    @property
    def no_transaction(self) -> bool:
        return self.header.no_transaction if self.header else False

    def replace_ts(self, ts: dt.datetime):
        self.ts = ts
        new_path = self.path.with_name(self.name)
        self.path = self.path.rename(new_path)

    def write_header(self):
        if not self.header:
            return
        # Remove the first lines starting with --
        if self.path.exists():
            lines = [
                _line for _line in self.path.read_text().split("\n") if not _line.startswith(tuple(PREFIXES.values()))
            ]
            _lines = "\n" + "\n".join(lines)
        else:
            _lines = "\n"
        new_text = self.header.as_text() + _lines
        self.path.write_text(new_text)

    @staticmethod
    def generate_base_name(ts: int | None = None, file_id: str | None = None) -> str:
        """
        Get a base name based on the timestamp and file_id
        to be used to generated file name later ({base_name}-{file_type}.sql)
        """
        _file_id = str(uuid.uuid4())[:8] if file_id is None else file_id
        _ts = ts if ts is not None else int(utc_now().timestamp())
        return f"{_ts}-{_file_id}"

    @classmethod
    def from_file(cls, path: Path):
        filename_infos = parse_filename(path.name)
        header = Header.from_text(path.read_text())
        return cls(
            ts=filename_infos["file_ts"],
            file_id=filename_infos["file_id"],
            file_type=filename_infos["migration_type"],
            path=path,
            header=header if not header.is_empty else None,
        )


def parse_filename(filename: str) -> dict:
    ts, file_id, file_type = filename.split("-")
    infos = {
        "file_ts": dt.datetime.fromtimestamp(int(ts), tz=UTC).replace(tzinfo=None),
        "file_id": file_id,
        "migration_type": file_type.replace(".sql", ""),
    }
    return infos


def get_files(folder: Path, reverse: bool = False, up_only: bool = False) -> list[MigrationFile]:
    """Returns the migration files in ascending order"""
    files = []
    pattern = "*.sql" if not up_only else "*-up.sql"
    for file in folder.glob(pattern):
        files.append(MigrationFile.from_file(file))

    return sorted(files, key=attrgetter("ts", "file_id"), reverse=reverse)


def iter_migration_files(files: list[MigrationFile]):
    for _file_id, _files_it in groupby(files, lambda x: x.file_id):
        _files: list[MigrationFile] = list(_files_it)

        _up_files = [x for x in _files if x.file_type == "up"]
        _down_files = [x for x in _files if x.file_type == "down"]

        if len(_up_files) != 1:
            raise ValueError(
                f'Found {len(_up_files)} "up" files (file_id: {_up_files[0].file_id})'
                if _up_files
                else "No up file found"
            )
        if len(_down_files) != 1:
            raise ValueError(
                f'Found {len(_down_files)} "down" files (file_id: {_down_files[0].file_id})'
                if _down_files
                else "No down file found"
            )

        yield _up_files[0], _down_files[0]


MigrationErrorType = Literal["order", "header", "duplicate"]


def is_migration_error_type(value: str) -> TypeGuard[MigrationErrorType]:
    return value in get_args(MigrationErrorType)


class MigrationFileError(Exception):
    def __init__(self, error_type: MigrationErrorType, message: str, file_id: str):
        self._error_type = error_type
        self.message = message
        self.file_id = file_id

    @property
    def error_type(self) -> MigrationErrorType:
        if is_migration_error_type(self._error_type):
            return self._error_type
        raise ValueError(f"Invalid error type {self._error_type!r}")


def verify_migration_files(
    migration_dir: Path, *, raise_error: bool = True
) -> list[tuple[MigrationFile, MigrationFile, MigrationFileError]]:
    """
    Verifies that the migration files are in order.
    If raise_error is set to False, it will return weather or not the files are in order
    and a list of files that are not correct (with the associated error message)
    """
    prev_files = None
    error_files = []
    _ids = set()
    for _file in iter_migration_files(get_files(migration_dir)):
        if prev_files is None:
            prev_files = _file
            continue
        prev_up, prev_down = prev_files
        up, down = _file

        if up.file_id in _ids:
            raise MigrationFileError("duplicate", f"Duplicate file_id: {up.file_id}", file_id=up.file_id)
        _ids.add(up.file_id)

        try:
            if prev_up.ts > up.ts:
                raise MigrationFileError(
                    "order", f"Files are not in order: {prev_up.path.name} > {up.path.name}", file_id=up.file_id
                )
            if prev_down.ts > down.ts:
                raise MigrationFileError(
                    "order", f"Files are not in order: {prev_down.path.name} > {down.path.name}", file_id=down.file_id
                )

            if up.header is not None and up.header.prev_file != prev_up.path.name:
                raise MigrationFileError(
                    "header",
                    f"Invalid header for up file {up.path.name} : "
                    f"(expected {prev_up.path.name!r} got {up.header.prev_file!r})",
                    file_id=up.file_id,
                )
            if down.header is not None and down.header.prev_file != prev_down.path.name:
                raise MigrationFileError(
                    "header",
                    f"Invalid header for down file {down.path.name} : "
                    f"(expected {prev_down.path.name!r} got {down.header.prev_file!r})",
                    file_id=down.file_id,
                )
        except MigrationFileError as e:
            if raise_error:
                raise e
            logs.warning(e.message)
            error_files.append((up, down, e))
        else:
            logs.debug(f"Valid header for file {up.path.name}")

        prev_files = _file

    return error_files


def utc_now():
    return dt.datetime.now(UTC).replace(tzinfo=None)
