import tempfile
from dataclasses import asdict
from pathlib import Path

import pytest

from padmy.config import Config, ConfigSchema, ConfigTable, AnoFields
from .utils import pprint_dataclass_diff

CONFIG_LITE = """
sample: 20
schemas:
  - schema_1
"""


def get_config_lite_expected():
    schemas = [
        ConfigSchema(schema="schema_1"),
    ]
    expected = Config(
        sample=20,
        schemas=schemas,
    )
    return expected


CONFIG_FULL = """
sample: 20
schemas:
  - schema_1
  - name: schema_2
    sample: 30

tables:
  - schema: public
    table: table_1
    sample: 10
    fields:
      bar: EMAIL
  - schema: public
    table: table_2
"""


def get_config_full_expected():
    schemas = [
        ConfigSchema(schema="schema_1"),
        ConfigSchema(schema="schema_2", sample=30),
    ]
    tables = [
        ConfigTable(
            schema="public",
            table="table_1",
            sample=10,
            fields=[AnoFields.load({"bar": "EMAIL"})],
        ),
        ConfigTable(schema="public", table="table_2"),
    ]
    expected = Config(
        sample=20,
        schemas=schemas,
        tables=tables,
    )
    return expected


CONFIG_CUSTOM_TABLE_FIELDS = """
tables:
  - schema: public
    table: table_1
    sample: 10
    fields:
      - column: bar
        type: EMAIL
        # Extra args
        domain_name: soren.fr
"""


def get_custom_fields_expected():
    expected = Config(
        tables=[
            ConfigTable(
                schema="public",
                table="table_1",
                sample=10,
                fields=[
                    AnoFields(
                        column="bar",
                        type="EMAIL",
                        extra_args={"domain_name": "soren.fr"},
                    )
                ],
            ),
        ],
    )
    return expected


CONFIG_UNIQUE_FIELDS = """
tables:
  - schema: public
    table: table_1
    fields:
      - column: bar
        type: EMAIL
        unique: true
        domain_name: soren.fr
"""


def get_unique_fields_expected():
    expected = Config(
        tables=[
            ConfigTable(
                schema="public",
                table="table_1",
                fields=[
                    AnoFields(
                        column="bar",
                        type="EMAIL",
                        extra_args={"domain_name": "soren.fr"},
                        unique=True,
                    )
                ],
            ),
        ],
    )
    return expected


CONFIG_IGNORE = """
tables:
  - schema: public
    table: table_1
    ignore: true
"""


def get_ignore_table_expected():
    expected = Config(
        tables=[
            ConfigTable(schema="public", table="table_1", ignore=True),
        ],
    )
    return expected


@pytest.mark.parametrize(
    "config, expected",
    [
        pytest.param(CONFIG_LITE, get_config_lite_expected(), id="Config lite"),
        pytest.param(CONFIG_FULL, get_config_full_expected(), id="Config full"),
        pytest.param(CONFIG_CUSTOM_TABLE_FIELDS, get_custom_fields_expected(), id="Custom fields"),
        pytest.param(CONFIG_UNIQUE_FIELDS, get_unique_fields_expected(), id="Unique fields"),
        pytest.param(CONFIG_IGNORE, get_ignore_table_expected(), id="Config ignore"),
    ],
)
def test_load_config_file(config, expected):
    from padmy.config import Config

    with tempfile.TemporaryDirectory() as dir:
        _file = Path(dir) / "test.yml"
        _file.write_text(config)
        config = Config.load_from_file(_file)

    assert asdict(config) == asdict(expected), pprint_dataclass_diff(config, expected)


def test_load_config():
    from padmy.config import Config, ConfigSchema

    schemas = [
        ConfigSchema(schema="schema_1"),
        ConfigSchema(schema="schema_2"),
    ]

    expected = Config(
        sample=20,
        schemas=schemas,
    )
    config = Config.load(sample=20, schemas=["schema_1", "schema_2"])
    assert asdict(config) == asdict(expected), pprint_dataclass_diff(config, expected)


def test_has_ano_fields_false_when_no_fields():
    from padmy.config import ConfigTable

    assert ConfigTable(schema="public", table="t").has_ano_fields is False
    assert ConfigTable(schema="public", table="t", fields=[]).has_ano_fields is False


def test_has_ano_fields_true_when_fields_present():
    from padmy.config import AnoFields, ConfigTable

    t = ConfigTable(schema="public", table="t", fields=[AnoFields(column="email", type="EMAIL")])
    assert t.has_ano_fields is True
