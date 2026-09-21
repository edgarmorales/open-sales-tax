# Daily state sales tax audit — 2026-09-21 (rotation day 5: DE + FL)

> **Rotation note.** This run executes the **day-5 pair (DE + FL)** during the
> days-27–31 catch-up window, per the rotation debt recorded in the 2026-09-05
> handoff. The session opened under a 2026-09-05 clock and the host date
> advanced to 2026-09-21 mid-run; the pair was completed rather than restarted
> (same precedent as the AR + AZ run). The report is filed under the date it
> was written so one date maps to one report. See "Rotation debt" below.

## TL;DR

- **2 jurisdictions audited. 1 repo rate defect found and fixed; 2 further
  live wrong rates confirmed that are already fixed in the repo but have
  never been deployed.**
- **Florida — 3 live over-collections, all in the same module.**
  - **Okeechobee County 1.5% → 1.0%** — a genuine repo defect. `34972` returns
    **7.500%**, should be **7.000%**. Introduced by iter-142 from a
    **SalesTaxHandbook** figure; the DOR table has never agreed. **Fixed this
    run.**
  - **Palm Beach County** — `33401` / `33432` return **7.000%**, should be
    **6.500%**. Repo is already correct (fixed 2026-07-05, commit `32a61f6`).
  - **Collier County** — `34102` returns **7.000%**, should be **6.000%**.
    Repo already correct, same commit.
  - The last two are **not** drift: prod's FL rates were loaded **2026-05-11**
    (`data_versions.id=496`), which predates the 2026-07-05 fix. **The fix is a
    prod reload, not a code change.** Chipped.
- **Delaware — fully clean.** No state or local sales tax; the engine returns
  **0.000%** with zero jurisdictions at every ZIP probed. Nothing to do.
- **Method change worth keeping:** Florida was audited by diffing **all 67
  counties** against the parsed DR-15DSS, not by spot-checking pinned ZIPs.
  The pinned-ZIP method would have missed Okeechobee entirely — no FL pin
  covered a wrong county until this run.

## DE — Delaware

- **Source:** Delaware Division of Revenue —
  <https://revenue.delaware.gov/business-tax-forms/doing-business-in-delaware/step-4-gross-receipts-taxes/>
  and the Gross Receipts Tax FAQ.
- **Last loaded on prod:** `DE-ZCTA-2020` (`data_versions.id=565`, fetched
  2026-05-11). No rate file exists or is needed.
- **Latest available:** n/a — Delaware publishes no sales tax rate table
  because it levies no sales tax.
- **Drift summary:** **None.** Delaware imposes **no state or local sales
  tax**. It instead levies a **gross receipts tax on the seller** (0.0945% –
  1.9914%, up to 2.4218% on petroleum products), which is a tax on the
  vendor's receipts rather than a transaction tax collected from the buyer —
  correctly outside the scope of a sales tax engine.
- **Recommended action:** None. `no_tax.py` models DE correctly.
- **2026 legislative check:** no sales tax bill is pending. A March 2026
  Delaware House release is titled "Sales Tax Not Under Consideration."

| ZIP | Expected (DE DOR) | Actual (engine) | Delta |
|---|---|---|---|
| 19801 Wilmington | 0.000% | 0.000% (no jurisdictions) | — |
| 19901 Dover | 0.000% | 0.000% (no jurisdictions) | — |
| 19702 Newark | 0.000% | 0.000% (no jurisdictions) | — |
| 19958 Lewes | 0.000% | 0.000% (no jurisdictions) | — |

The single DE `DOR_GRID` pin (`19801` → 0.000) passes.

## FL — Florida

- **Source:** FL DOR Form **DR-15DSS**, "Discretionary Sales Surtax Information
  for Calendar Year 2026" (**R. 11/25**), plus the 2026 Tax Information
  Publication series.
- **Last loaded on prod:** `FL-SST-2026Q2APR15` (`data_versions.id=496`,
  **fetched 2026-05-11**).
- **Latest available:** CY2026 DR-15DSS (R. 11/25). No CY2027 reissue yet.
- **Drift summary:** 1 repo defect (Okeechobee) + 2 counties where prod is
  serving pre-fix rates (Palm Beach, Collier).
- **Recommended action:** Okeechobee fixed in repo this run; **reload FL on
  prod** to land all three.

### Source-URL trap — read this before the next FL audit

`https://floridarevenue.com/Forms_library/current/dr15dss.pdf` — the obvious
"current" path, and the one recorded in this task's own procedure — **still
serves the CY2025 revision (R. 11/24)** as of 2026-09. The live CY2026 table is
at the **year-suffixed** URL:

```
https://floridarevenue.com/Forms_library/current/dr15dss_26.pdf
```

Both were downloaded and diffed to establish this. An audit that trusts the
unsuffixed path validates against **last year's** rates and reports "clean"
while Palm Beach, Martin and Jackson are all wrong. A note is now in the
`fl_data.py` docstring. (`dr15dss_27.pdf` 404s — CY2027 normally posts in
November.)

### Method: full 67-county diff

Prior FL audits spot-checked pinned ZIPs. This run parsed the CY2026 DR-15DSS
with `pypdf` and diffed **every one of the 67 counties** against
`FL_COUNTY_SURTAX_PCT`. Result: **66 exact, 1 wrong.**

### Finding 1 — Okeechobee County 1.500% → 1.000% (repo defect, FIXED)

The DR-15DSS row, identical in **both** the CY2026 and CY2025 tables:

```
Okeechobee 1% Oct 1, 1995    None
```

One small county surtax, effective 1995-10-01, **no expiration, no second
component**. The module carried **1.500%**, attributed by iter-142 to
"SalesTaxHandbook 2026 shows 1.5 (Small County Surtax + School Capital
Outlay)". **Okeechobee levies no school capital outlay surtax** — the county
appears in neither the CY2026 nor the CY2025 change lists, so this was never a
rate change the engine was tracking late. It was wrong the day it landed and
has over-collected **0.5pp** on every Okeechobee County transaction since.

| City | Expected (DOR) | Actual (engine) | Delta |
|---|---|---|---|
| Okeechobee `34972` | 7.000% | **7.500%** | **+0.50pp over** |

Fixed in `fl_data.py`; the `34972` pin moved 7.500 → 7.000 with the DOR
citation. **The pin fails under `-m liveapi` until prod reloads FL** — the
same convention used for the AK / GA / WA / NM rows already waiting on a
deploy. `liveapi` is excluded from CI, so the pipeline is unaffected.

**This is the AZ pattern again.** Of the FL county rates ever sourced from an
aggregator, one (Hamilton 2.0%, iter-145) happens to match the DOR and one
(Okeechobee) did not. A `grep -rniE "salestaxhandbook|avalara" src/` sweep
remains owed project-wide; the AZ audit counted 130 hits.

### Finding 2 — Palm Beach + Collier are right in the repo and wrong in prod

| City | Expected (DOR CY2026) | Actual (engine) | Repo value | Delta |
|---|---|---|---|---|
| West Palm Beach `33401` | 6.500% | **7.000%** | 6.500 (correct) | **+0.50pp over** |
| Boca Raton `33432` | 6.500% | **7.000%** | 6.500 (correct) | **+0.50pp over** |
| Naples `34102` | 6.000% | **7.000%** | 6.000 (correct) | **+1.00pp over** |

Per the CY2026 DR-15DSS change notes:

- **Palm Beach — .5% total.** The 1% local government infrastructure surtax was
  **repealed effective 12/31/2025**; a new **.5% school capital outlay surtax
  began 1/1/2026** and expires 12/31/2035.
- **Collier — None.** Listed "None" in both CY2025 and CY2026 (the 1%
  infrastructure surtax hit its $490M / 7-year cap and ended).

Both were corrected in the repo on **2026-07-05** (commit `32a61f6`). Prod's FL
rates date from **2026-05-11**. So these have been **live and wrong for the
whole of 2026**, and wrong-despite-a-fix for **78 days**. Palm Beach County is
Florida's third-most-populous county; this is the largest-exposure item in the
report.

**No code change is possible or appropriate** — the repo is already right.
Chipped as a prod reload.

### Verified correct (no action)

All other tier-1 FL cities match the DR-15DSS exactly:

| City | ZIP | Expected | Actual |
|---|---|---|---|
| Jacksonville (Duval 1.5) | 32202 | 7.500% | 7.500% |
| Miami (Miami-Dade 1.0) | 33130 | 7.000% | 7.000% |
| Tampa (Hillsborough 1.5) | 33602 | 7.500% | 7.500% |
| Orlando (Orange 0.5) | 32801 | 6.500% | 6.500% |
| St. Petersburg (Pinellas 1.0) | 33701 | 7.000% | 7.000% |
| Hialeah (Miami-Dade 1.0) | 33010 | 7.000% | 7.000% |
| Tallahassee (Leon 1.5) | 32301 | 7.500% | 7.500% |
| Fort Myers (Lee 0.5) | 33901 | 6.500% | 6.500% |
| Fort Lauderdale (Broward 1.0) | 33301 | 7.000% | 7.000% |
| Hollywood (Broward 1.0) | 33020 | 7.000% | 7.000% |
| Gainesville (Alachua 1.5) | 32601 | 7.500% | 7.500% |
| Clearwater (Pinellas 1.0) | 33755 | 7.000% | 7.000% |
| Lakeland (Polk 1.0) | 33801 | 7.000% | 7.000% |
| Miami Beach (Miami-Dade 1.0) | 33139 | 7.000% | 7.000% |
| Key West (Monroe 1.5) | 33040 | 7.500% | 7.500% |
| Pensacola (Escambia 1.5) | 32501 | 7.500% | 7.500% |

**Martin County** deserves a note as a near-miss: it changed **1.0% → 0.5%**
effective 2026-01-01 (its .5% school capital outlay surtax expired
12/31/2025). The module already carries 0.500, so this one was caught by the
2026-07-05 audit. **Jackson County** appears in the CY2026 change list but its
*total* stayed 1.5% — an extension, not a rate move.

### Offline pin-consistency check (the AZ follow-up, applied to FL)

The 2026-09-05 AZ audit named a CI-time `DOR_GRID` consistency test as the
highest-value open item in the project — AZ had a pin asserting one rate and a
second pin on the same ZIP asserting another, and neither ever ran because
`DOR_GRID` is gated behind `-m liveapi`, which CI deselects.

That check was run ad hoc against Florida this session: all **29** FL
`DOR_GRID` rows were evaluated against `FL_COUNTY_SURTAX_PCT` plus the
ZIP→county bindings, with no network and no prod. **Result after the
Okeechobee fix: 0 pins contradict the module, no duplicate ZIPs.** Florida is
internally coherent. This was a one-off script, not a committed test — **the
general CI-time version is still owed** and remains the top follow-up.

### Non-drift observations

- **ZCTA `34974` binds to Glades County, but it is Okeechobee city's main
  ZIP.** The ZCTA spans five counties (Martin, Highlands, Okeechobee, Glades,
  Hendry) and the area-majority rule picks Glades, which has the most rural
  land area while the populated part is in Okeechobee. **Rate-neutral** — both
  counties are 1.0% — so the returned *rate* is right and only the returned
  county *name* is wrong. It becomes a live rate defect only if either county
  changes. Recorded, not fixed: hand-pinning it would paper over the
  area-majority rule rather than address it, exactly the objection raised in
  `specs/findings/multi-county-zip-fips-first-tiebreak-2026-08.md`.
- **Brevard and Charlotte counties both expire 12/31/2026.** Brevard's two
  .5% surtaxes and Charlotte's 1% all carry that expiration date. If neither
  renews, both drop to **0.0%** (combined 6.0%) on **2027-01-01**. **Brevard
  covers the Palm Bay `32905` tier-1 pin.** The CY2027 DR-15DSS (normally
  published in November) will settle it; **the day-5 rotation next lands
  2026-10-05 and 2026-11-05, so the November run must check this.**
- **No 2026 TIP changes any surtax rate.** All twelve 2026 sales-and-use TIPs
  are exemptions, holidays, indexed fuel/asphalt rates or reporting changes.
  Florida surtaxes change on January 1 by statute, so mid-year drift is not
  expected — the annual DR-15DSS reissue is the thing to watch.

## Actions taken

1. **Committed:** `fl_data.py` Okeechobee County 1.500 → 1.000; the `34972`
   `DOR_GRID` pin 7.500 → 7.000 with the DOR citation and a
   fails-until-prod-reload note; docstring updated with the full-67-county
   re-verification, the `dr15dss_26.pdf` URL trap, and the Brevard/Charlotte
   2027 watch item.
2. **Chipped:** reload FL on prod — lands Okeechobee, Palm Beach and Collier
   together (3 live over-collections, one `data load`).
3. **Handoff:** open follow-ups updated.

## Rotation debt

This run cleared **day 5 (DE + FL)**. Still owed from the September cycle, per
the 2026-09-05 handoff plus the dates that elapsed during this session:

| Day | Pair | Status |
|----:|------|--------|
| 4 | CT, DC | **owed** |
| 5 | DE, FL | done this run |
| 6 | GA, HI | **owed** — HI Maui 0.5pp under-collection open since 2026-07-06 |
| 7 | IA, ID | owed |
| 8 | IL, IN | owed |
| 9 | KS, KY | owed |
| 10 | LA, MA | owed |
| 11 | MD, ME | owed |
| 12 | MI, MN | owed |
| 13 | MO, MS | owed |
| 14 | MT, NC | owed |
| 15 | ND, NE | owed |
| 16 | NH, NJ | owed |
| 17 | NM, NV | owed |
| 18 | NY, OH | owed |
| 19 | OK, OR | owed |
| 20 | PA, PR | owed |
| 21 | RI, SC | owed (today's nominal pair) |

**The rotation is not keeping up.** Since 2026-08-06 the audit has run on 5 of
46 days. This needs a decision from Eric on whether the daily task is actually
firing (the Claude Code app must be open when the cron fires) — the gap
pattern looks like missed fires rather than skipped work.

## Standing systemic item

**The prod-deploy backlog is now the single largest source of live wrong
rates.** This report adds Palm Beach (two ZIPs) and Collier to a list that
already held HI Maui, GA Madison, ND Scranton + Drayton, NE Edgar, WA (6),
AK (7), AZ Florence and the multi-county tiebreak fix. **Every one of these is
fixed in the repo and wrong in production.** The engine's correctness is no
longer gated on finding drift — it is gated on deploying.
