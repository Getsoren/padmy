from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import yaml

FieldType = Literal[
    "EMAIL",
    "NULL",
    "FIRST_NAME",
    "LAST_NAME",
    "NAME",
    "PHONE_NUMBER",
    "DATE_OF_BIRTH",
    "TEXT",
    "WORD",
]

SampleType = float | int


def _check_sample_size(sample: SampleType | None):
    if sample is None:
        return
    if sample < 0 or sample > 100:
        raise ValueError(f"Sample must be a value between 0 and 100 (got {sample})")


@dataclass
class AnoFields:
    column: str
    type: FieldType
    extra_args: dict | None = None
    # Generate values through faker.unique (for columns with a unique constraint)
    unique: bool = False

    @classmethod
    def load(cls, data: dict):
        if len(data) == 1:
            column = next(iter(data))
            _type = data[column]
            extra_args = None
            unique = False
        else:
            column = data.pop("column")
            _type = data.pop("type")
            unique = data.pop("unique", False)
            extra_args = data if data else None

        return AnoFields(column=column, type=_type, extra_args=extra_args, unique=unique)


@dataclass
class ConfigTable:
    schema: str
    table: str
    sample: SampleType | None = None

    fields: list[AnoFields] = field(default_factory=list)

    ignore: bool = False

    def __post_init__(self):
        _check_sample_size(self.sample)

    @property
    def full_name(self):
        return f"{self.schema}.{self.table}"

    @property
    def has_ano_fields(self):
        return bool(self.fields)

    @classmethod
    def load(cls, table: dict):
        _fields_any: dict | list = table.pop("fields", [])
        _fields: list = [_fields_any] if isinstance(_fields_any, dict) else _fields_any

        return cls(**table, fields=[AnoFields.load(_field) for _field in _fields])


@dataclass
class ConfigSchema:
    schema: str
    sample: SampleType | None = None

    def __post_init__(self):
        _check_sample_size(self.sample)

    @classmethod
    def load(cls, v: str | dict):
        if isinstance(v, str):
            return cls(v)
        else:
            return cls(v["name"], v.get("sample"))


@dataclass
class Config:
    # Sample size in percentage
    sample: SampleType | None = None
    schemas: list[ConfigSchema] = field(default_factory=list)
    tables: list[ConfigTable] = field(default_factory=list)

    def __post_init__(self):
        _check_sample_size(self.sample)

    @classmethod
    def load(cls, sample: SampleType, schemas: list[str]):
        _schemas = [ConfigSchema(x) for x in schemas]
        return cls(sample=sample, schemas=_schemas)

    @classmethod
    def load_from_file(cls, path: Path):
        with path.open("r") as f:
            config = yaml.load(f, Loader=yaml.Loader)
        schemas = [ConfigSchema.load(schema) for schema in config.pop("schemas", [])]
        tables = [ConfigTable.load(table) for table in config.pop("tables", [])]

        return cls(**config, schemas=schemas, tables=tables)
