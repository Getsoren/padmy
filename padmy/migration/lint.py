import dataclasses
import re
from collections.abc import Iterator
from pathlib import Path

from pglast import ast, parse_sql
from pglast.enums import AlterTableType, ConstrType, ObjectType, TransactionStmtKind

from .utils import Header, PREFIXES

__all__ = ("Finding", "lint_sql", "lint_file")

IGNORE_RE = re.compile(r"--\s*padmy-lint:\s*ignore\s+([\w\-, ]+)")

# ponytail: allowlist of non-volatile functions, any other call in a default is assumed to rewrite the table
NON_VOLATILE_FUNCS = {"now", "statement_timestamp", "transaction_timestamp"}

TIMEOUTS = ("lock_timeout", "statement_timeout")

# type name -> advice, pglast gives the canonical name (int -> int4, char -> bpchar, ...)
TYPE_RULES = {
    **dict.fromkeys(
        ("int2", "int4", "serial", "serial2", "serial4", "smallserial"),
        ("prefer-bigint", "int can overflow, use bigint"),
    ),
    "varchar": ("prefer-text", "changing varchar(n) locks the table, use text with a length CHECK"),
    "bpchar": ("prefer-text", "char(n) pads values with spaces, use text"),
    "timestamp": ("prefer-timestamptz", "timestamp drops the time zone, use timestamptz"),
}
SERIAL_TYPES = {"serial", "serial2", "serial4", "serial8", "smallserial", "bigserial"}
TRANSACTION_KINDS = (
    TransactionStmtKind.TRANS_STMT_BEGIN,
    TransactionStmtKind.TRANS_STMT_START,
    TransactionStmtKind.TRANS_STMT_COMMIT,
    TransactionStmtKind.TRANS_STMT_ROLLBACK,
)

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
    """Returns the unsafe operations of `sql`, minus the rules ignored with a `-- padmy-lint: ignore rule-a` comment."""
    ignored = {rule.strip() for m in IGNORE_RE.finditer(sql) for rule in m.group(1).split(",")}
    no_transaction = Header.from_text(sql).no_transaction
    if no_transaction:
        ignored.add("in-transaction")
    new_tables: set[str] = set()
    timeouts: set[str] = set()
    findings = []
    for i, raw_stmt in enumerate(parse_sql(sql)):
        stmt = raw_stmt.stmt
        line = sql[: raw_stmt.stmt_location].count("\n") + 1
        rules = list(_check(stmt, new_tables))
        match stmt:
            case ast.CreateStmt(relation=ast.RangeVar() as rel):
                new_tables.add(_rel_name(rel))
            case ast.VariableSetStmt(name=str(name)):
                timeouts.add(name)
        if no_transaction and i == 1:
            # Postgres runs a multi-statement query in an implicit transaction
            rules.append(("no-transaction-statements", "a no-transaction migration holds one statement"))
        if _locks_table(stmt, new_tables):
            for timeout in set(TIMEOUTS) - timeouts:
                rules.append((f"require-{timeout.replace('_', '-')}", f"set {timeout} before locking a table"))
                timeouts.add(timeout)
        findings += [Finding(rule, msg, line) for rule, msg in sorted(rules) if rule not in ignored]
    return findings


def _locks_table(stmt: ast.Node | None, new_tables: set[str]) -> bool:
    match stmt:
        case ast.IndexStmt(concurrent=True) | ast.DropStmt(concurrent=True):
            return False
        case ast.IndexStmt(relation=ast.RangeVar() as rel) | ast.AlterTableStmt(relation=ast.RangeVar() as rel):
            return _rel_name(rel) not in new_tables
        case ast.RenameStmt(relation=ast.RangeVar() as rel):
            return _rel_name(rel) not in new_tables
        case ast.DropStmt(removeType=ObjectType.OBJECT_TABLE | ObjectType.OBJECT_INDEX):
            return True
        case ast.TruncateStmt() | ast.ClusterStmt() | ast.ReindexStmt():
            return True
    return False


def _check(stmt: ast.Node | None, new_tables: set[str]) -> Iterator[tuple[str, str]]:
    match stmt:
        case ast.TransactionStmt(kind=kind) if kind in TRANSACTION_KINDS:
            yield "transaction-nesting", "padmy already runs migrations in a transaction"
        case ast.CreateStmt(tableElts=elts):
            for col in elts or ():
                if isinstance(col, ast.ColumnDef):
                    yield from _check_type(col)
        case ast.DropStmt(removeType=ObjectType.OBJECT_INDEX, concurrent=False):
            yield "drop-index", "locks the table, use DROP INDEX CONCURRENTLY"
        case ast.DropStmt(removeType=ObjectType.OBJECT_TABLE):
            yield "drop-table", "breaks running code still using the table"
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
        case ast.AlterTableStmt(relation=ast.RangeVar() as rel, cmds=cmds):
            for cmd in cmds or ():
                if isinstance(cmd.def_, ast.ColumnDef):
                    yield from _check_type(cmd.def_)
                if _rel_name(rel) not in new_tables:
                    yield from _check_alter_cmd(cmd, _rel_name(rel))


def _check_alter_cmd(cmd: ast.AlterTableCmd, table: str) -> Iterator[tuple[str, str]]:
    match cmd:
        case ast.AlterTableCmd(subtype=AlterTableType.AT_AddColumn, def_=ast.ColumnDef() as col):
            constraints = {c.contype: c for c in col.constraints or ()}
            default = constraints.get(ConstrType.CONSTR_DEFAULT)
            generated = constraints.get(ConstrType.CONSTR_GENERATED)
            if (
                (default and _is_volatile(default.raw_expr))
                or _type_name(col) in SERIAL_TYPES
                or ConstrType.CONSTR_IDENTITY in constraints
            ):
                yield "add-column-volatile-default", f"volatile default rewrites {table}"
            if generated and generated.generated_kind == "s":
                yield "add-column-generated", f"stored generated column rewrites {table}"
            if ConstrType.CONSTR_NOTNULL in constraints and not default:
                yield "add-column-not-null", f"NOT NULL without a default fails if {table} has rows"
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


def _check_type(col: ast.ColumnDef) -> Iterator[tuple[str, str]]:
    if (name := _type_name(col)) and (rule := TYPE_RULES.get(name)):
        yield rule


def _type_name(col: ast.ColumnDef) -> str | None:
    match col.typeName:
        case ast.TypeName(names=(*_, ast.String(sval=name))):
            return name
    return None


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
