# GSA Contract Tracker — Enhancement Roadmap

> Last updated: 2026-05-13

This document catalogs prioritized improvements, bug fixes, performance optimizations, and new data source opportunities for the GSA Contract Tracker dashboard and pipeline.

---

## Priority Definitions

| Level | Meaning | Target |
|-------|---------|--------|
| **P0** | Critical — blocks core functionality or causes incorrect data | Immediate |
| **P1** | High — degrades user experience or hides important data | Next sprint |
| **P2** | Medium — quality-of-life or performance improvement | 1–2 months |
| **P3** | Low — nice-to-have, future consideration | Backlog |

---

## 1. Bug Fixes

### P0 — Critical

| ID | Description | File |
|----|-------------|------|
| BUG-1 | Missing 'N' (Legal Contract Cancellation) code in Terminations page filter dropdown — users cannot filter by this termination type | `app/pages/2_Terminations.py` |
| BUG-4 | Plotly imported inside conditional blocks on every rerender — adds ~200ms per page load | `app/main.py` |

### P1 — High

| ID | Description | File |
|----|-------------|------|
| BUG-2 | `get_cancellation_pending()` has no vehicle filter in SQL — Python post-filter instead of SQL WHERE, wasting memory and CPU | `app/db.py` |

### P2 — Medium

| ID | Description | File |
|----|-------------|------|
| BUG-3 | `highlight_terminated()` uses `.get()` on Pandas Series — fragile with NaN values, can throw on edge cases | `app/pages/3_Vendor_Roster.py` |
| BUG-5 | Hard-coded `LIMIT 5000` on vendor roster query silently hides data beyond that threshold | `app/db.py` |
| BUG-6 | No `STYLE_LIMIT` check on Vendor Roster `Styler.apply()` — expensive rendering for large result sets | `app/pages/3_Vendor_Roster.py` |

---

## 2. Performance Optimizations

### P0

| ID | Description | File | Impact |
|----|-------------|------|--------|
| PERF-1 | `split_vendor_sins()` uses `.iterrows()` — replace with vectorized Pandas `str.split()` + `explode()` | `pipeline/fetch_elib.py` | 5–10× faster SIN expansion on ~51k MAS rows |

### P1

| ID | Description | File | Impact |
|----|-------------|------|--------|
| PERF-2 | Add composite index on `(vehicle_id, status)` to `contracts` table for filtered dashboard queries | `pipeline/bootstrap.py` | Faster Cancellation Pending and Terminations page loads |

### P2

| ID | Description | Impact |
|----|-------------|--------|
| PERF-3 | Move Plotly imports to module top-level instead of inside render blocks | Eliminates repeated import overhead on rerenders |
| PERF-4 | Add pagination to Vendor Roster instead of `LIMIT 5000` cap | Removes data hiding while keeping render times bounded |
| PERF-5 | Evaluate replacing SQLite WAL reads with read-only connections (`?mode=ro`) for dashboard queries | Reduces lock contention during concurrent pipeline writes |

---

## 3. Dashboard UI/UX Enhancements

### P1 — Data Freshness & Clarity

| ID | Description | Priority |
|----|-------------|----------|
| UI-1 | Add data freshness indicators to every page header — show last refresh timestamp and stale-data warnings (>24h for eLib, >35d for USASpending) | P1 |
| UI-2 | Add "Last Updated" badge to Overview KPI cards sourced from `refresh_log` | P1 |
| UI-3 | Annotate Plotly charts with axis labels, units, and data source attribution | P1 |

### P2 — Visual Polish

| ID | Description | Priority |
|----|-------------|----------|
| UI-4 | Replace raw contract status codes with human-readable labels in tables (e.g., 'E' → 'Terminated for Default') | P2 |
| UI-5 | Add sparkline trend indicators to Overview KPI metrics | P2 |
| UI-6 | Implement consistent color scheme for termination types across all charts | P2 |
| UI-7 | Add tooltip explanations for acronyms (UEI, SIN, BPA, MAS) | P2 |

### P3 — Advanced Features

| ID | Description | Priority |
|----|-------------|----------|
| UI-8 | Add CSV/Excel export buttons to all data tables | P3 |
| UI-9 | Implement saved filter presets per page | P3 |
| UI-10 | Add a "Compare Vehicles" view for side-by-side MAS vs BPA analysis | P3 |

---

## 4. Infrastructure & Automation

### P0 — CI/CD

| ID | Description | Priority |
|----|-------------|----------|
| INFRA-1 | Create GitHub Actions workflow for daily eLibrary refresh (`scheduling.daily_cron: "0 9 * * *"`) | P0 |
| INFRA-2 | Create GitHub Actions workflow for monthly USASpending bulk refresh (`scheduling.monthly_cron: "0 8 1 * *"`) | P0 |

### P1 — Reliability

| ID | Description | Priority |
|----|-------------|----------|
| INFRA-3 | Add retry logic with exponential backoff to `fetch_elib.py` HTTP requests | P1 |
| INFRA-4 | Add health-check endpoint or script that validates DB freshness + row counts | P1 |
| INFRA-5 | Create `tests/` directory with unit tests for `app/db.py` query functions and `pipeline/detect_changes.py` | P1 |

### P2 — Monitoring

| ID | Description | Priority |
|----|-------------|----------|
| INFRA-6 | Add GitHub Actions failure notifications (email or Slack webhook) | P2 |
| INFRA-7 | Log pipeline run durations to `refresh_log` for performance trending | P2 |
| INFRA-8 | Add `pre-commit` hooks for linting (`ruff`) and type checking (`mypy`) | P2 |

---

## 5. New Data Sources

### P1 — SAM.gov Integration

| ID | Description | Priority |
|----|-------------|----------|
| DATA-1 | **SAM.gov Entity API** — cross-reference vendor UEI numbers to get entity status (active, inactive, excluded). Enables flagging vendors with SAM registration issues before contract impact. API: `https://api.sam.gov/entity-information/v3/entities` | P1 |
| DATA-2 | **SAM.gov Exclusions API** — check if vendors appear on the SAM exclusions list (debarment, suspension). Critical for compliance monitoring. API: `https://api.sam.gov/entity-information/v3/exclusions` | P1 |

### P2 — Enrichment Sources

| ID | Description | Priority |
|----|-------------|----------|
| DATA-3 | **FPDS-NG (Federal Procurement Data System)** — pull award history and modification records per contract for richer termination context. Available via USASpending bulk files or direct FPDS API. | P2 |
| DATA-4 | **GSA D2D (Data to Decisions)** — integrate actual reported GSA sales data for revenue impact analysis of cancellations. Access method TBD (config `d2d.enabled: false`). | P2 |
| DATA-5 | **SBA Dynamic Small Business Search** — validate socioeconomic certifications (8(a), HUBZone, SDVOSB) against SBA records to detect certification lapses that precede cancellations. | P2 |

### P3 — Future Expansion

| ID | Description | Priority |
|----|-------------|----------|
| DATA-6 | **OASIS SB / Alliant 3 vehicle data** — enable additional contract vehicles already stubbed in `config.yaml` (`elib.vehicles.OASIS_SB`, `elib.vehicles.ALLIANT_3`) | P3 |
| DATA-7 | **USASpending Sub-Awards API** — track subcontract terminations cascading from prime contract cancellations | P3 |
| DATA-8 | **GSA Advantage pricing data** — correlate pricing changes with cancellation patterns | P3 |

---

## 6. Implementation Sequence

### Phase 1 — Stability (Weeks 1–2)
1. Fix all P0 bugs (BUG-1, BUG-4)
2. Deploy GitHub Actions workflows (INFRA-1, INFRA-2)
3. Apply PERF-1 vectorization fix
4. Add data freshness indicators (UI-1, UI-2)

### Phase 2 — Quality (Weeks 3–4)
1. Fix P1 bugs (BUG-2)
2. Add composite DB indexes (PERF-2)
3. Add unit test foundation (INFRA-5)
4. Improve chart clarity (UI-3, UI-4)
5. Begin SAM.gov API integration (DATA-1, DATA-2)

### Phase 3 — Enrichment (Months 2–3)
1. Fix P2 bugs (BUG-3, BUG-5, BUG-6)
2. Add pagination to Vendor Roster (PERF-4)
3. Visual polish items (UI-5 through UI-7)
4. FPDS-NG integration (DATA-3)
5. Pipeline monitoring (INFRA-6, INFRA-7)

### Phase 4 — Expansion (Months 3+)
1. D2D integration when access confirmed (DATA-4)
2. Enable additional vehicles (DATA-6)
3. Advanced UI features (UI-8 through UI-10)
4. Sub-award tracking (DATA-7)
