"""Lazy, budgeted, single-use auto-pagination.

Iterators stop when ``page * limit >= total``, when a page comes back empty, at
``max_pages`` (default 50) or ``max_items``. **Short pages are not a stop
signal** — Deep Search pages can be short when catalogue rows were deleted
since the candidate set was cached.

Every page is charged. With ``max_credits`` set, each next page's cost is
projected from your live rate card and :class:`CreditBudgetExceededError` is
raised *before* a page that would exceed the budget is sent.

Iterators are **single-use**: iterating one a second time raises
:class:`IteratorConsumedError` instead of silently re-sending (and re-paying
for) every page. Call ``tracks.iter(...)`` again for a fresh walk.
"""

from __future__ import annotations

from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Generator, Iterator
from typing import Callable, Optional

from ._errors import CreditBudgetExceededError, IteratorConsumedError, ValidationError
from ._models import Track, TrackPage

__all__ = ["AsyncPageIterator", "SyncPageIterator"]


def _check_limits(max_pages: int, max_items: Optional[int], max_credits: Optional[int]) -> None:
    if isinstance(max_pages, bool) or not isinstance(max_pages, int) or max_pages < 1:
        raise ValidationError("max_pages must be a positive integer")
    if max_items is not None and (isinstance(max_items, bool) or not isinstance(max_items, int) or max_items < 1):
        raise ValidationError("max_items must be a positive integer")
    if max_credits is not None and (
        isinstance(max_credits, bool) or not isinstance(max_credits, int) or max_credits < 0
    ):
        raise ValidationError("max_credits must be a non-negative integer")


class _BudgetState:
    __slots__ = ("max_credits", "operation", "spent")

    def __init__(self, operation: str, max_credits: Optional[int]) -> None:
        self.operation = operation
        self.max_credits = max_credits
        self.spent = 0

    def check(self, page: int, cost: Optional[int], cost_key: Optional[str]) -> None:
        if self.max_credits is None:
            return
        if cost is None:
            # guard_failure_mode='open' already logged; count nothing we cannot price
            return
        if self.spent + cost > self.max_credits:
            raise CreditBudgetExceededError(
                f"{self.operation}: page {page} is projected to cost {cost} credits ({cost_key}); "
                f"{self.spent} already spent of max_credits={self.max_credits}. Nothing was sent.",
                operation=self.operation,
                spent_projected=self.spent,
                next_cost=cost,
                cap=self.max_credits,
                cost_key=cost_key,
            )

    def add(self, cost: Optional[int]) -> None:
        if cost:
            self.spent += cost


class _SingleUse:
    __slots__ = ("_claimed", "_operation")

    def __init__(self, operation: str) -> None:
        self._operation = operation
        self._claimed = False

    def claim(self) -> None:
        if self._claimed:
            raise IteratorConsumedError(
                f"this {self._operation} iterator has already been iterated; iterators are single-use "
                "because every page is charged. Call the .iter(...) method again for a fresh walk.",
                operation=self._operation,
            )
        self._claimed = True


def _done(page: TrackPage, fetched: int, max_pages: int) -> bool:
    if not page.data:
        return True
    if page.page * page.limit >= page.total:
        return True
    return fetched >= max_pages


class SyncPageIterator:
    """``for track in it`` iterates tracks; ``for page in it.pages()`` iterates pages."""

    def __init__(
        self,
        *,
        operation: str,
        fetch: Callable[[int], TrackPage],
        cost_key_for: Callable[[int], Optional[str]],
        project: Callable[[Optional[str]], Optional[int]],
        start_page: int = 1,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
    ) -> None:
        _check_limits(max_pages, max_items, max_credits)
        self._operation = operation
        self._fetch = fetch
        self._cost_key_for = cost_key_for
        self._project = project
        self._start_page = start_page
        self.max_pages = max_pages
        self.max_items = max_items
        self.max_credits = max_credits
        self.spent_projected = 0
        self._use = _SingleUse(operation)

    def pages(self) -> Generator[TrackPage, None, None]:
        """Iterate whole pages. Single-use, shared with iterating the tracks directly."""
        self._use.claim()
        return self._pages()

    def _pages(self) -> Generator[TrackPage, None, None]:
        budget = _BudgetState(self._operation, self.max_credits)
        page_number = self._start_page
        fetched = 0
        while True:
            cost_key = self._cost_key_for(page_number)
            cost: Optional[int] = None
            if self.max_credits is not None:
                cost = self._project(cost_key)
                budget.check(page_number, cost, cost_key)
            page = self._fetch(page_number)
            fetched += 1
            budget.add(cost)
            self.spent_projected = budget.spent
            yield page
            if _done(page, fetched, self.max_pages):
                return
            page_number += 1

    def __iter__(self) -> Iterator[Track]:
        self._use.claim()
        return self._tracks()

    def _tracks(self) -> Iterator[Track]:
        count = 0
        pages = self._pages()
        try:
            for page in pages:
                for track in page.data:
                    yield track
                    count += 1
                    if self.max_items is not None and count >= self.max_items:
                        return
        finally:
            pages.close()


class AsyncPageIterator:
    """``async for track in it`` iterates tracks; ``async for page in it.pages()`` iterates pages."""

    def __init__(
        self,
        *,
        operation: str,
        fetch: Callable[[int], Awaitable[TrackPage]],
        cost_key_for: Callable[[int], Optional[str]],
        project: Callable[[Optional[str]], Awaitable[Optional[int]]],
        start_page: int = 1,
        max_pages: int = 50,
        max_items: Optional[int] = None,
        max_credits: Optional[int] = None,
    ) -> None:
        _check_limits(max_pages, max_items, max_credits)
        self._operation = operation
        self._fetch = fetch
        self._cost_key_for = cost_key_for
        self._project = project
        self._start_page = start_page
        self.max_pages = max_pages
        self.max_items = max_items
        self.max_credits = max_credits
        self.spent_projected = 0
        self._use = _SingleUse(operation)

    def pages(self) -> AsyncGenerator[TrackPage, None]:
        """Iterate whole pages. Single-use, shared with iterating the tracks directly."""
        self._use.claim()
        return self._pages()

    async def _pages(self) -> AsyncGenerator[TrackPage, None]:
        budget = _BudgetState(self._operation, self.max_credits)
        page_number = self._start_page
        fetched = 0
        while True:
            cost_key = self._cost_key_for(page_number)
            cost: Optional[int] = None
            if self.max_credits is not None:
                cost = await self._project(cost_key)
                budget.check(page_number, cost, cost_key)
            page = await self._fetch(page_number)
            fetched += 1
            budget.add(cost)
            self.spent_projected = budget.spent
            yield page
            if _done(page, fetched, self.max_pages):
                return
            page_number += 1

    async def _items(self) -> AsyncIterator[Track]:
        count = 0
        pages = self._pages()
        try:
            async for page in pages:
                for track in page.data:
                    yield track
                    count += 1
                    if self.max_items is not None and count >= self.max_items:
                        return
        finally:
            await pages.aclose()

    def __aiter__(self) -> AsyncIterator[Track]:
        self._use.claim()
        return self._items()
