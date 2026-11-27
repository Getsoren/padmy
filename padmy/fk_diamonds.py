from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from padmy.db import FKConstraint

__all__ = ("FKDiamond", "find_fk_diamonds", "parse_ignore")


@dataclass(frozen=True)
class FKDiamond:
    """Two foreign-key paths that leave `table` and meet at `meets_at` without sharing a column."""

    table: str
    meets_at: str
    path_1: tuple[str, ...]
    path_2: tuple[str, ...]

    @property
    def key(self) -> str:
        return f"{self.table}>{self.meets_at}"


@dataclass(frozen=True)
class _Route:
    constraints: tuple[str, ...]
    tables: tuple[str, ...]  # tables passed through, the target excluded


def _routes(first: FKConstraint, edges: dict[str, list[FKConstraint]], max_depth: int) -> dict[str, _Route]:
    """Shortest route from `first` to every table it reaches, at most `max_depth` hops."""
    best = {first.foreign_full_name: _Route((first.constraint_name,), ())}
    frontier = [(first.foreign_full_name, best[first.foreign_full_name], {first.full_name, first.foreign_full_name})]
    for _ in range(max_depth - 1):
        nxt = []
        for table, route, seen in frontier:
            for fk in edges.get(table, []):
                target = fk.foreign_full_name
                if target in seen:
                    continue
                new_route = _Route((*route.constraints, fk.constraint_name), (*route.tables, table))
                best.setdefault(target, new_route)
                nxt.append((target, new_route, seen | {target}))
        frontier = nxt
    return best


def find_fk_diamonds(fks: Sequence[FKConstraint], max_depth: int = 4) -> list[FKDiamond]:
    """Pairs of FK paths from one table that first meet at another, their first constraints sharing no column.

    Such paths can point to two different rows of the table they meet at: nothing in the schema
    makes them agree unless a column (tenant_id, article_id…) is part of both first constraints.
    Only the first meeting point is reported, one pair per (table, meets_at); self-references are skipped.
    """
    edges: dict[str, list[FKConstraint]] = defaultdict(list)
    for fk in fks:
        if fk.full_name != fk.foreign_full_name:
            edges[fk.full_name].append(fk)

    diamonds: list[FKDiamond] = []
    for table in sorted(edges):
        firsts = sorted(edges[table], key=lambda x: x.constraint_name)
        routes = [(fk, _routes(fk, edges, max_depth)) for fk in firsts]
        found: dict[str, FKDiamond] = {}
        for i, (fk_a, routes_a) in enumerate(routes):
            for fk_b, routes_b in routes[i + 1 :]:
                if set(fk_a.column_names) & set(fk_b.column_names):
                    continue
                for meets_at in sorted(routes_a.keys() & routes_b.keys()):
                    a, b = routes_a[meets_at], routes_b[meets_at]
                    if set(a.tables) & set(b.tables):
                        continue  # they met earlier: that table is the diamond
                    current = found.get(meets_at)
                    if current is None or len(a.constraints) + len(b.constraints) < len(current.path_1) + len(
                        current.path_2
                    ):
                        found[meets_at] = FKDiamond(table, meets_at, a.constraints, b.constraints)
        diamonds.extend(found[k] for k in sorted(found))
    return diamonds


def parse_ignore(values: Iterable[str]) -> set[str]:
    """Ignore entries as `schema.table>schema.meets_at`; blank lines and `#` comments are skipped."""
    out = set()
    for value in values:
        value = value.split("#", 1)[0].strip()
        if not value:
            continue
        if ">" not in value:
            raise ValueError(f"Invalid ignore entry {value!r}: expected 'schema.table>schema.meets_at'")
        out.add(value)
    return out
