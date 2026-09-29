# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eric Osterberg and OpenSalesTax contributors
"""Coverage counts are stored, not recomputed per request.

``tax_authorities.zip5_count`` replaced a per-request
``count(distinct zip5) group by authority_id`` over the whole boundary
table. In production that statement measured p50 3,370 ms against
8.86 million rows and ran on every request that matched an authority, which
was enough on its own to hold the database at 100% CPU.

These tests pin the two things that matter:

* the loader keeps the stored count in step with the boundary rows,
  including shrinking it back to zero when coverage goes away, and
* the lookup path no longer aggregates the boundary table at all.

DB-backed; skipped automatically when ``OPENSALESTAX_DATABASE_URL``
is unset.
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from opensalestax.data.loader import refresh_zip5_counts
from opensalestax.db.models import Boundary, DataVersion, State, TaxAuthority

pytestmark = pytest.mark.asyncio


async def _seed(session: AsyncSession, zips: list[str]) -> tuple[int, int]:
    """One state, one authority, one boundary row per ZIP. Returns ids."""
    state = State(abbrev="ZZ", name="Testland", sst_member=False, has_sales_tax=True)
    session.add(state)
    await session.flush()

    version = DataVersion(
        state_id=state.id,
        source="test",
        version_label="TEST-0001",
    )
    authority = TaxAuthority(
        state_id=state.id, name="Testville", authority_type="city"
    )
    session.add_all([version, authority])
    await session.flush()

    for zip5 in zips:
        session.add(
            Boundary(
                authority_id=authority.id,
                zip5=zip5,
                data_version_id=version.id,
            )
        )
    await session.flush()
    return authority.id, version.id


async def test_refresh_counts_distinct_zips(async_session: AsyncSession) -> None:
    # Two rows share a ZIP: the count is DISTINCT ZIPs, not boundary rows.
    authority_id, _ = await _seed(async_session, ["10001", "10002", "10002"])

    written = await refresh_zip5_counts(async_session, {authority_id})

    assert written == 1
    stored = await async_session.scalar(
        select(TaxAuthority.zip5_count).where(TaxAuthority.id == authority_id)
    )
    assert stored == 2


async def test_refresh_resets_an_authority_that_lost_all_coverage(
    async_session: AsyncSession,
) -> None:
    """A shrinking data version must not leave a stale count behind.

    This is the failure the denormalisation invites: if the refresh only
    wrote authorities that still have boundary rows, one whose coverage
    went to zero would keep its old number and go on winning
    city/county tie-breaks it should now lose.
    """
    authority_id, _ = await _seed(async_session, ["10001", "10002"])
    await refresh_zip5_counts(async_session, {authority_id})

    await async_session.execute(
        Boundary.__table__.delete().where(Boundary.authority_id == authority_id)
    )
    await refresh_zip5_counts(async_session, {authority_id})

    stored = await async_session.scalar(
        select(TaxAuthority.zip5_count).where(TaxAuthority.id == authority_id)
    )
    assert stored == 0


async def test_refresh_without_ids_rebuilds_every_authority(
    async_session: AsyncSession,
) -> None:
    """The no-argument form is what a restore or a backfill wants."""
    authority_id, _ = await _seed(async_session, ["20001"])

    await refresh_zip5_counts(async_session)

    stored = await async_session.scalar(
        select(TaxAuthority.zip5_count).where(TaxAuthority.id == authority_id)
    )
    assert stored == 1


async def test_lookup_no_longer_aggregates_the_boundary_table() -> None:
    """The expensive statement is gone from the request path.

    Asserted against the source rather than by timing, because a timing
    assertion would pass on a small test dataset even if the aggregation
    came back -- the cost only shows up at production row counts.
    """
    from pathlib import Path

    import opensalestax.core.lookup as lookup_module

    source = Path(lookup_module.__file__).read_text(encoding="utf-8")
    assert "zip5.distinct()" not in source


async def test_counts_survive_a_restore_that_carried_none(
    async_session: AsyncSession,
) -> None:
    """A dump predating the column restores zeros; the rebuild repairs them.

    Dumps are data-only, so a COPY produced before ``tax_authorities``
    grew ``zip5_count`` carries no value for it and every row lands on the
    column default. The schema check does not catch this -- our dumps
    deliberately exclude ``alembic_version`` -- and the migration backfill
    has already run by then, so the CLI rebuilds the counts after applying
    a dump. This pins that the rebuild is what repairs it.
    """
    authority_id, _ = await _seed(async_session, ["30301", "30302", "30303"])

    # Simulate the post-restore state: boundary rows present, count at the
    # column default because the dump had no value to supply.
    await async_session.execute(
        TaxAuthority.__table__.update()
        .where(TaxAuthority.id == authority_id)
        .values(zip5_count=0)
    )

    await refresh_zip5_counts(async_session)

    stored = await async_session.scalar(
        select(TaxAuthority.zip5_count).where(TaxAuthority.id == authority_id)
    )
    assert stored == 3
