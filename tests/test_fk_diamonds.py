from contextlib import AbstractContextManager, nullcontext

import pytest

from padmy.db import FKConstraint
from padmy.fk_diamonds import FKDiamond, find_fk_diamonds, parse_ignore


def _fk(name: str, table: str, cols: list[str], target: str) -> FKConstraint:
    return FKConstraint(
        column_names=cols,
        constraint_name=name,
        foreign_schema="s",
        foreign_table=target,
        foreign_column_names=["id"] * len(cols),
        table=table,
        schema="s",
    )


_BASE = [
    _fk("items_article_fkey", "items", ["article_id"], "articles"),
    _fk("lines_article_fkey", "lines", ["article_id"], "articles"),
    _fk("lines_grid_fkey", "lines", ["grid_id"], "grids"),
]


@pytest.mark.parametrize(
    "fks, expected",
    [
        pytest.param(
            [
                *_BASE,
                _fk("prices_item_fkey", "prices", ["item_id"], "items"),
                _fk("prices_line_fkey", "prices", ["line_id"], "lines"),
            ],
            [
                FKDiamond(
                    "s.prices",
                    "s.articles",
                    ("prices_item_fkey", "items_article_fkey"),
                    ("prices_line_fkey", "lines_article_fkey"),
                )
            ],
            id="stored price line can point to another article",
        ),
        pytest.param(
            [
                *_BASE,
                _fk("prices_item_fkey", "prices", ["item_id", "article_id"], "items"),
                _fk("prices_line_fkey", "prices", ["line_id", "article_id"], "lines"),
            ],
            [],
            id="shared column pins both paths",
        ),
        pytest.param(
            [
                *_BASE,
                _fk("prices_item_fkey", "prices", ["item_id"], "items"),
                _fk("prices_option_fkey", "prices", ["option_id"], "options"),
            ],
            [],
            id="paths that never meet",
        ),
        pytest.param(
            [_fk("a_parent_fkey", "a", ["parent_id"], "a"), _fk("a_b_fkey", "a", ["b_id"], "b")],
            [],
            id="self reference skipped",
        ),
        pytest.param(
            [
                _fk("bill_items_bill_fkey", "bill_items", ["bill_id"], "bills"),
                _fk("bill_items_event_fkey", "bill_items", ["event_id"], "events"),
                _fk("bills_booking_fkey", "bills", ["booking_id"], "bookings"),
                _fk("events_booking_fkey", "events", ["booking_id"], "bookings"),
                _fk("bookings_contract_fkey", "bookings", ["contract_id"], "contracts"),
            ],
            [
                FKDiamond(
                    "s.bill_items",
                    "s.bookings",
                    ("bill_items_bill_fkey", "bills_booking_fkey"),
                    ("bill_items_event_fkey", "events_booking_fkey"),
                )
            ],
            id="only the first meeting point",
        ),
    ],
)
def test_find_fk_diamonds(fks: list[FKConstraint], expected: list[FKDiamond]) -> None:
    assert find_fk_diamonds(fks) == expected


def test_find_fk_diamonds_max_depth() -> None:
    fks = [
        _fk("a_b_fkey", "a", ["b_id"], "b"),
        _fk("b_c_fkey", "b", ["c_id"], "c"),
        _fk("a_c_fkey", "a", ["c_id"], "c"),
    ]
    assert [d.key for d in find_fk_diamonds(fks, max_depth=2)] == ["s.a>s.c"]
    assert find_fk_diamonds(fks, max_depth=1) == []


@pytest.mark.parametrize(
    "lines, expected",
    [
        pytest.param(
            ["s.a>s.c  # tenant pinned by code", "", "# comment"], nullcontext({"s.a>s.c"}), id="entries and comments"
        ),
        pytest.param(["s.a"], pytest.raises(ValueError, match="expected"), id="missing separator"),
    ],
)
def test_parse_ignore(lines: list[str], expected: AbstractContextManager) -> None:
    with expected as e:
        assert parse_ignore(lines) == e
