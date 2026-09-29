# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Eric Osterberg and OpenSalesTax contributors
"""Denormalised ZIP coverage count on tax_authorities

Adds ``tax_authorities.zip5_count`` -- the number of distinct ZIP5s an
authority covers, derived from ``boundaries``.

The lookup path needs this to break city/county ties and to spot
single-ZIP districts. It was computed per request as
``count(distinct zip5) group by authority_id`` over the whole boundary
table, which in production measured p50 3,370 ms against 8.86 million rows
on every request that matched an authority -- enough on its own to hold
the database at 100% CPU. No index helps, because the statement
aggregates rather than looks up.

The value only changes when a data version is loaded, so the loader
refreshes it there. Constitution §6 makes a data refresh a deliberate
version-bumping operation, which is the right moment for derived data;
a runtime cache would instead serve counts from one data version beside
rates from another, breaking the §5 guarantee that identical inputs
yield identical output for a given data version.

Backfilled in this migration so existing deployments are correct
immediately rather than after the next load.

The backfill is set-based and therefore dialect-specific. The portable
correlated-subquery form runs one aggregate per authority and measured
over two minutes against 8.86 million boundary rows on an idle local
database -- unusable on a production one, where it would hold a lock on
tax_authorities for the duration. The set-based form does a single
grouped pass. Branching on the dialect is fine here: constitution §10
rule 1 constrains business logic, not schema migrations.

Revision ID: 0005_authority_zip5_count
Revises: 0004_taxability_thresholds
Create Date: 2026-09-27
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_authority_zip5_count"
down_revision: str | Sequence[str] | None = "0004_taxability_thresholds"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "tax_authorities",
        sa.Column("zip5_count", sa.Integer(), nullable=False, server_default="0"),
    )
    # Backfill from the boundary table -- the expensive aggregation the
    # column exists to avoid, run once here instead of on every request.
    # Authorities with no boundary rows keep the server_default of 0.
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            """
            UPDATE tax_authorities ta
               SET zip5_count = c.n
              FROM (
                    SELECT authority_id, COUNT(DISTINCT zip5) AS n
                      FROM boundaries
                     GROUP BY authority_id
                   ) c
             WHERE c.authority_id = ta.id
            """
        )
    else:
        op.execute(
            """
            UPDATE tax_authorities ta
              JOIN (
                    SELECT authority_id, COUNT(DISTINCT zip5) AS n
                      FROM boundaries
                     GROUP BY authority_id
                   ) c ON c.authority_id = ta.id
               SET ta.zip5_count = c.n
            """
        )


def downgrade() -> None:
    op.drop_column("tax_authorities", "zip5_count")
