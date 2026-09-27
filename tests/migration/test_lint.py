from contextlib import nullcontext

import pytest

from padmy.migration.lint import lint_sql

# timeouts are covered by test_lint_sql_timeouts
IGNORE_TIMEOUTS = "-- padmy-lint: ignore require-lock-timeout, require-statement-timeout\n"


@pytest.mark.parametrize(
    "sql, expected",
    [
        pytest.param("CREATE INDEX i ON t (a)", ["create-index"], id="index on existing table"),
        pytest.param("CREATE TABLE s.t (a text); CREATE INDEX i ON s.t (a)", [], id="index on new table"),
        pytest.param("CREATE INDEX CONCURRENTLY i ON t (a)", ["in-transaction"], id="index concurrently"),
        pytest.param("DROP INDEX CONCURRENTLY i", ["in-transaction"], id="drop index concurrently"),
        pytest.param("REINDEX INDEX CONCURRENTLY i", ["in-transaction"], id="reindex concurrently"),
        pytest.param("REINDEX INDEX i", ["reindex"], id="reindex"),
        pytest.param(
            "-- No-transaction: true\nCREATE INDEX CONCURRENTLY i ON t (a)", [], id="no-transaction concurrently"
        ),
        pytest.param(
            "-- No-transaction: true\nCREATE INDEX CONCURRENTLY i ON t (a); CREATE INDEX CONCURRENTLY j ON t (b)",
            ["no-transaction-statements"],
            id="no-transaction multiple statements",
        ),
        pytest.param("VACUUM FULL t", ["in-transaction"], id="vacuum"),
        pytest.param("CLUSTER t USING i", ["table-rewrite"], id="cluster"),
        pytest.param("TRUNCATE t", ["truncate"], id="truncate"),
        pytest.param(
            "ALTER TABLE t ADD a bigint DEFAULT -1, ADD b timestamptz DEFAULT now(), ADD c text DEFAULT 'x'::text",
            [],
            id="non-volatile defaults",
        ),
        pytest.param(
            "ALTER TABLE t ADD a uuid DEFAULT gen_random_uuid()", ["add-column-volatile-default"], id="volatile default"
        ),
        pytest.param(
            "ALTER TABLE t ADD a text DEFAULT md5(random()::text)",
            ["add-column-volatile-default"],
            id="nested volatile default",
        ),
        pytest.param("ALTER TABLE t ALTER a TYPE bigint", ["alter-column-type"], id="alter column type"),
        pytest.param("ALTER TABLE t ALTER a SET NOT NULL", ["set-not-null"], id="set not null"),
        pytest.param("ALTER TABLE t DROP COLUMN a", ["drop-column"], id="drop column"),
        pytest.param(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id)",
            ["constraint-not-valid"],
            id="validated fk",
        ),
        pytest.param("ALTER TABLE t ADD CHECK (a > 0)", ["constraint-not-valid"], id="validated check"),
        pytest.param(
            "ALTER TABLE t ADD CONSTRAINT fk FOREIGN KEY (a) REFERENCES u (id) NOT VALID; "
            "ALTER TABLE t VALIDATE CONSTRAINT fk",
            [],
            id="not valid fk then validate",
        ),
        pytest.param("ALTER TABLE t ADD UNIQUE (a)", ["add-unique-constraint"], id="unique constraint"),
        pytest.param("ALTER TABLE t ADD CONSTRAINT u UNIQUE USING INDEX i", [], id="unique using index"),
        pytest.param("ALTER TABLE t RENAME a TO b", ["rename"], id="rename column"),
        pytest.param("CREATE TABLE t (a text); ALTER TABLE t RENAME a TO b", [], id="rename on new table"),
        pytest.param(
            "CREATE TABLE s.t (a bigint); ALTER TABLE t ADD CHECK (a > 0)",
            ["constraint-not-valid"],
            id="new table in another schema",
        ),
        pytest.param(
            "CREATE TABLE t (a int, b serial, c varchar(10), d char(2), e timestamp, f bigint, g text, h timestamptz)",
            ["prefer-bigint", "prefer-bigint", "prefer-text", "prefer-text", "prefer-timestamptz"],
            id="column types",
        ),
        pytest.param("CREATE TABLE t (a text); ALTER TABLE t ADD b int", ["prefer-bigint"], id="type on new table"),
        pytest.param(
            "ALTER TABLE t ALTER a TYPE varchar(20)", ["alter-column-type", "prefer-text"], id="alter to varchar"
        ),
        pytest.param("ALTER TABLE t ADD a bigserial", ["add-column-volatile-default"], id="add serial"),
        pytest.param(
            "ALTER TABLE t ADD a bigint GENERATED ALWAYS AS IDENTITY",
            ["add-column-volatile-default"],
            id="add identity",
        ),
        pytest.param(
            "ALTER TABLE t ADD a bigint GENERATED ALWAYS AS (b * 2) STORED",
            ["add-column-generated"],
            id="add stored generated",
        ),
        pytest.param("ALTER TABLE t ADD a bigint GENERATED ALWAYS AS (b * 2) VIRTUAL", [], id="add virtual generated"),
        pytest.param("ALTER TABLE t ADD a text NOT NULL", ["add-column-not-null"], id="add not null"),
        pytest.param("ALTER TABLE t ADD a text NOT NULL DEFAULT ''", [], id="add not null with default"),
        pytest.param("DROP INDEX i", ["drop-index"], id="drop index"),
        pytest.param("DROP TABLE t", ["drop-table"], id="drop table"),
        pytest.param(
            "BEGIN; ALTER TABLE t ADD a text; COMMIT",
            ["transaction-nesting", "transaction-nesting"],
            id="transaction nesting",
        ),
        pytest.param(
            "-- padmy-lint: ignore drop-column, rename\nALTER TABLE t DROP a, ALTER b TYPE bigint; ALTER TABLE t RENAME c TO d",
            ["alter-column-type"],
            id="ignored rules",
        ),
    ],
)
def test_lint_sql(sql: str, expected: list[str]):
    assert [f.rule for f in lint_sql(IGNORE_TIMEOUTS + sql)] == expected


@pytest.mark.parametrize(
    "sql, expected",
    [
        pytest.param(
            "ALTER TABLE t DROP a; ALTER TABLE t DROP b",
            ["drop-column", "require-lock-timeout", "require-statement-timeout", "drop-column"],
            id="missing, reported once",
        ),
        pytest.param(
            "SET lock_timeout = '1s'; SET LOCAL statement_timeout = '1min'; ALTER TABLE t DROP a",
            ["drop-column"],
            id="set",
        ),
        pytest.param(
            "SET lock_timeout = '1s'; ALTER TABLE t DROP a",
            ["drop-column", "require-statement-timeout"],
            id="partially set",
        ),
        pytest.param("CREATE TABLE t (a text); CREATE INDEX i ON t (a)", [], id="new table"),
        pytest.param("-- No-transaction: true\nCREATE INDEX CONCURRENTLY i ON t (a)", [], id="concurrently"),
    ],
)
def test_lint_sql_timeouts(sql: str, expected: list[str]):
    assert [f.rule for f in lint_sql(sql)] == expected


def test_lint_sql_line():
    sql = "-- Author: foo\n\nCREATE TABLE t (a text);\n/* é */\nALTER TABLE u\n  DROP a;"
    assert [f.line for f in lint_sql(sql)] == [5, 5, 5]


@pytest.mark.parametrize(
    "sql, expected, output",
    [
        pytest.param("CREATE TABLE t (a text);", nullcontext(), "No unsafe operations", id="safe"),
        pytest.param("CREATE TABLE t (a int);", pytest.raises(SystemExit), "Found 1 unsafe", id="unsafe"),
        pytest.param("ALTER TABL t;", pytest.raises(SystemExit), "Failed to parse", id="invalid sql"),
    ],
)
def test_lint_cmd(tmp_path, capsys, sql: str, expected, output: str):
    from padmy.run import cli

    (tmp_path / "1-00000000-up.sql").write_text(sql)
    with expected:
        cli.run_with_args("migrate", "lint", "--sql-dir", str(tmp_path))
    assert output in capsys.readouterr().out
