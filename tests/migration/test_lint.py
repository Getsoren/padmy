from contextlib import nullcontext

import pytest

from padmy.migration.lint import lint_sql


@pytest.mark.parametrize(
    "sql, expected",
    [
        pytest.param("CREATE INDEX i ON t (a)", ["create-index"], id="index on existing table"),
        pytest.param("CREATE TABLE s.t (a int); CREATE INDEX i ON s.t (a)", [], id="index on new table"),
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
            "ALTER TABLE t ADD a int DEFAULT -1, ADD b timestamptz DEFAULT now(), ADD c text DEFAULT 'x'::text",
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
        pytest.param("CREATE TABLE t (a int); ALTER TABLE t RENAME a TO b", [], id="rename on new table"),
        pytest.param(
            "CREATE TABLE s.t (a int); ALTER TABLE t ADD CHECK (a > 0)",
            ["constraint-not-valid"],
            id="new table in another schema",
        ),
        pytest.param(
            "-- padmy-lint: ignore drop-column, rename\nALTER TABLE t DROP a, ALTER b TYPE int; ALTER TABLE t RENAME c TO d",
            ["alter-column-type"],
            id="ignored rules",
        ),
    ],
)
def test_lint_sql(sql: str, expected: list[str]):
    assert [f.rule for f in lint_sql(sql)] == expected


def test_lint_sql_line():
    sql = "-- Author: foo\n\nCREATE TABLE t (a int);\n/* é */\nALTER TABLE u\n  DROP a;"
    assert [f.line for f in lint_sql(sql)] == [5]


@pytest.mark.parametrize(
    "sql, expected, output",
    [
        pytest.param("ALTER TABLE t ADD a int;", nullcontext(), "No unsafe operations", id="safe"),
        pytest.param("ALTER TABLE t DROP a;", pytest.raises(SystemExit), "Found 1 unsafe", id="unsafe"),
        pytest.param("ALTER TABL t;", pytest.raises(SystemExit), "Failed to parse", id="invalid sql"),
    ],
)
def test_lint_cmd(tmp_path, capsys, sql: str, expected, output: str):
    from padmy.run import cli

    (tmp_path / "1-00000000-up.sql").write_text(sql)
    with expected:
        cli.run_with_args("migrate", "lint", "--sql-dir", str(tmp_path))
    assert output in capsys.readouterr().out
