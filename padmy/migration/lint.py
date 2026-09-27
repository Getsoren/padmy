import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path

from pglast import ast, parse_sql
from pglast.enums import AlterTableType, ConstrType, ObjectType

from .utils import Header, PREFIXES

__all__ = ("Finding", "lint_sql", "lint_file")

IGNORE_RE = re.compile(r"--\s*padmy-lint:\s*ignore\s+([\w\-, ]+)")

# ponytail: allowlist of non-volatile functions, any other call in a default is assumed to rewrite the table
NON_VOLATILE_FUNCS = {"now", "statement_timestamp", "transaction_timestamp"}

IN_TRANSACTION_MSG = (
    f"cannot run in a transaction, move it to its own migration with a {PREFIXES['no-transaction']!r} header"
)


@dataclasses.dataclass(frozen=True)
class Finding:
    rule: str
    message: str
    line: int


def lint_file(path: Path) -> list[Finding]:
    return lint_sql(path.read_text())


def lint_sql(sql: str) -> list[Finding]:
    """Returns the operations of `sql` that lock or rewrite existing tables, minus the rules
    ignored with a `-- padmy-lint: ignore rule-a, rule-b` comment."""
    ignored = {rule.strip() for m in IGNORE_RE.finditer(sql) for rule in m.group(1).split(",")}
    no_transaction = Header.from_text(sql).no_transaction
    if no_transaction:
        ignored.add("in-transaction")
    new_tables: set[str] = set()
    findings = []
    for i, raw_stmt in enumerate(parse_sql(sql)):
        stmt = raw_stmt.stmt
        if isinstance(stmt, ast.CreateStmt) and stmt.relation:
            new_tables.add(_rel_name(stmt.relation))
        line = sql[: raw_stmt.stmt_location].count("\n") + 1
        if no_transaction and i == 1:
            # Postgres runs a multi-statement query in an implicit transaction
            findings.append(
                Finding("no-transaction-statements", "a no-transaction migration holds one statement", line)
            )
        findings += [Finding(rule, msg, line) for rule, msg in _check(stmt, new_tables) if rule not in ignored]
    return findings


def _check(stmt: ast.Node | None, new_tables: set[str]) -> Iterator[tuple[str, str]]:
    match stmt:
        case ast.IndexStmt(concurrent=True) | ast.DropStmt(concurrent=True) | ast.VacuumStmt():
            yield "in-transaction", IN_TRANSACTION_MSG
        case ast.ReindexStmt(params=params) if any(p.defname == "concurrently" for p in params or ()):
            yield "in-transaction", IN_TRANSACTION_MSG
        case ast.IndexStmt(relation=ast.RangeVar() as rel) if _rel_name(rel) not in new_tables:
            yield "create-index", f"blocks writes on {_rel_name(rel)} while the index builds (build it CONCURRENTLY)"
        case ast.ReindexStmt():
            yield "reindex", "blocks writes on the table while the index is rebuilt"
        case ast.ClusterStmt():
            yield "table-rewrite", "rewrites the table under an ACCESS EXCLUSIVE lock"
        case ast.TruncateStmt():
            yield "truncate", "deletes every row under an ACCESS EXCLUSIVE lock"
        case ast.RenameStmt(
            renameType=ObjectType.OBJECT_TABLE | ObjectType.OBJECT_COLUMN, relation=ast.RangeVar() as rel
        ) if _rel_name(rel) not in new_tables:
            yield "rename", "breaks running code still using the old name"
        case ast.AlterTableStmt(relation=ast.RangeVar() as rel, cmds=cmds) if _rel_name(rel) not in new_tables:
            for cmd in cmds or ():
                yield from _check_alter_cmd(cmd, _rel_name(rel))


def _check_alter_cmd(cmd: ast.AlterTableCmd, table: str) -> Iterator[tuple[str, str]]:
    match cmd:
        case ast.AlterTableCmd(subtype=AlterTableType.AT_AddColumn, def_=ast.ColumnDef(constraints=constraints)):
            if any(c.contype == ConstrType.CONSTR_DEFAULT and _is_volatile(c.raw_expr) for c in constraints or ()):
                yield "add-column-volatile-default", f"volatile default rewrites {table}"
        case ast.AlterTableCmd(subtype=AlterTableType.AT_AlterColumnType):
            yield "alter-column-type", f"may rewrite {table} and its indexes under an ACCESS EXCLUSIVE lock"
        case ast.AlterTableCmd(subtype=AlterTableType.AT_SetNotNull):
            yield "set-not-null", f"scans {table} under an ACCESS EXCLUSIVE lock (use a NOT VALID check first)"
        case ast.AlterTableCmd(subtype=AlterTableType.AT_DropColumn):
            yield "drop-column", "breaks running code still using the column"
        case ast.AlterTableCmd(subtype=AlterTableType.AT_AddConstraint, def_=ast.Constraint() as c):
            if c.contype in (ConstrType.CONSTR_FOREIGN, ConstrType.CONSTR_CHECK) and not c.skip_validation:
                yield "constraint-not-valid", f"validates every row of {table} under lock (add it NOT VALID)"
            elif c.contype in (ConstrType.CONSTR_PRIMARY, ConstrType.CONSTR_UNIQUE) and not c.indexname:
                yield "add-unique-constraint", f"builds an index blocking writes on {table} (use USING INDEX)"


def _rel_name(rel: ast.RangeVar) -> str:
    return f"{rel.schemaname}.{rel.relname}" if rel.schemaname else str(rel.relname)


def _is_volatile(node: object) -> bool:
    match node:
        case ast.FuncCall(funcname=(*_, ast.String(sval=name))) if name not in NON_VOLATILE_FUNCS:
            return True
        case ast.Node():
            return any(_is_volatile(getattr(node, attr)) for attr in node)
        case list() | tuple():
            return any(_is_volatile(n) for n in node)
    return False
