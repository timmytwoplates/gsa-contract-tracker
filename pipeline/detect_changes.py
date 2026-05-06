"""
detect_changes.py — Change detection for eLibrary contract data.

Compares an incoming DataFrame (new CSV) against the current DB snapshot and
produces a structured list of change records for the mas_change_log table.

Change types:
    ADD          — contract_number not in DB (new vendor)
    REMOVE       — contract_number in DB but not in new CSV (vendor dropped)
    FIELD_UPDATE — field value changed above Levenshtein threshold
    SIN_ADD      — SIN added to a contract
    SIN_REMOVE   — SIN removed from a contract

Levenshtein filtering:
    For fuzzy_fields (addresses, URLs, etc.), a change is only logged if BOTH:
      1. Absolute edit distance > levenshtein_threshold
      2. Normalized ratio > levenshtein_ratio_threshold
    This suppresses noise like "St" vs "Street" or "http" vs "https".
    High-priority fields bypass this filter entirely.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pandas as pd
from loguru import logger
from rapidfuzz.distance import Levenshtein


@dataclass
class ChangeRecord:
    """A single detected change, ready to insert into mas_change_log."""

    run_id: str
    contract_number: str
    vehicle_id: int
    change_type: str  # ADD | REMOVE | FIELD_UPDATE | SIN_ADD | SIN_REMOVE
    field_name: str | None = None
    old_value: str | None = None
    new_value: str | None = None
    levenshtein_distance: int | None = None


def _normalize(value: Any) -> str:
    """Coerce any value to a stripped string for comparison."""
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value).strip()


def _levenshtein_distance(a: str, b: str) -> int:
    return Levenshtein.distance(a, b)


def _is_significant_change(
    old: str,
    new: str,
    threshold: int,
    ratio_threshold: float,
) -> tuple[bool, int]:
    """
    Return (is_significant, distance).

    A change is significant if:
      - absolute distance > threshold
      AND
      - normalized distance ratio > ratio_threshold

    Both conditions must hold. This prevents short strings from being
    overly sensitive and long strings from being overly permissive.
    """
    if old == new:
        return False, 0

    dist = _levenshtein_distance(old, new)
    max_len = max(len(old), len(new), 1)
    ratio = dist / max_len

    significant = dist > threshold and ratio > ratio_threshold
    return significant, dist


def detect_field_changes(
    contract_number: str,
    vehicle_id: int,
    run_id: str,
    db_row: dict,
    csv_row: dict,
    high_priority_fields: list[str],
    fuzzy_fields: list[str],
    levenshtein_threshold: int,
    levenshtein_ratio_threshold: float,
) -> list[ChangeRecord]:
    """
    Compare a single vendor row from DB vs incoming CSV.
    Returns a list of ChangeRecord for each significant field change.
    """
    changes: list[ChangeRecord] = []
    all_fields = set(high_priority_fields) | set(fuzzy_fields)

    for field in all_fields:
        old = _normalize(db_row.get(field))
        new = _normalize(csv_row.get(field))

        if old == new:
            continue

        if field in high_priority_fields:
            # Always log — no fuzzy filtering
            changes.append(
                ChangeRecord(
                    run_id=run_id,
                    contract_number=contract_number,
                    vehicle_id=vehicle_id,
                    change_type="FIELD_UPDATE",
                    field_name=field,
                    old_value=old or None,
                    new_value=new or None,
                    levenshtein_distance=_levenshtein_distance(old, new),
                )
            )
        else:
            # Fuzzy field — apply Levenshtein gate
            significant, dist = _is_significant_change(
                old, new, levenshtein_threshold, levenshtein_ratio_threshold
            )
            if significant:
                changes.append(
                    ChangeRecord(
                        run_id=run_id,
                        contract_number=contract_number,
                        vehicle_id=vehicle_id,
                        change_type="FIELD_UPDATE",
                        field_name=field,
                        old_value=old or None,
                        new_value=new or None,
                        levenshtein_distance=dist,
                    )
                )

    return changes


def detect_sin_changes(
    contract_number: str,
    vehicle_id: int,
    run_id: str,
    db_sins: set[str],
    csv_sins: set[str],
) -> list[ChangeRecord]:
    """
    Diff SIN sets for a single contract.
    SIN changes are always high-priority — no fuzzy filtering.
    """
    changes: list[ChangeRecord] = []

    for sin in csv_sins - db_sins:
        changes.append(
            ChangeRecord(
                run_id=run_id,
                contract_number=contract_number,
                vehicle_id=vehicle_id,
                change_type="SIN_ADD",
                field_name="sin",
                new_value=sin,
            )
        )

    for sin in db_sins - csv_sins:
        changes.append(
            ChangeRecord(
                run_id=run_id,
                contract_number=contract_number,
                vehicle_id=vehicle_id,
                change_type="SIN_REMOVE",
                field_name="sin",
                old_value=sin,
            )
        )

    return changes


def diff_vendor_snapshot(
    run_id: str,
    vehicle_id: int,
    db_df: pd.DataFrame,
    csv_df: pd.DataFrame,
    db_sins: dict[str, set[str]],
    csv_sins: dict[str, set[str]],
    high_priority_fields: list[str],
    fuzzy_fields: list[str],
    levenshtein_threshold: int,
    levenshtein_ratio_threshold: float,
) -> list[ChangeRecord]:
    """
    Full diff between the current DB snapshot and the incoming CSV.

    Parameters
    ----------
    db_df : DataFrame indexed by contract_number, columns = vendor fields
    csv_df : DataFrame indexed by contract_number, columns = vendor fields
    db_sins : {contract_number: {sin, ...}} from DB
    csv_sins : {contract_number: {sin, ...}} from CSV
    """
    changes: list[ChangeRecord] = []

    db_contracts = set(db_df.index)
    csv_contracts = set(csv_df.index)

    # --- NEW contracts ---
    for cn in csv_contracts - db_contracts:
        changes.append(
            ChangeRecord(
                run_id=run_id,
                contract_number=cn,
                vehicle_id=vehicle_id,
                change_type="ADD",
            )
        )
        # Log SINs for new contracts as SIN_ADD
        for sin in csv_sins.get(cn, set()):
            changes.append(
                ChangeRecord(
                    run_id=run_id,
                    contract_number=cn,
                    vehicle_id=vehicle_id,
                    change_type="SIN_ADD",
                    field_name="sin",
                    new_value=sin,
                )
            )

    # --- REMOVED contracts ---
    for cn in db_contracts - csv_contracts:
        changes.append(
            ChangeRecord(
                run_id=run_id,
                contract_number=cn,
                vehicle_id=vehicle_id,
                change_type="REMOVE",
            )
        )

    # --- EXISTING contracts — field + SIN diffs ---
    for cn in db_contracts & csv_contracts:
        db_row = db_df.loc[cn].to_dict()
        csv_row = csv_df.loc[cn].to_dict()

        # Field-level diff
        field_changes = detect_field_changes(
            contract_number=cn,
            vehicle_id=vehicle_id,
            run_id=run_id,
            db_row=db_row,
            csv_row=csv_row,
            high_priority_fields=high_priority_fields,
            fuzzy_fields=fuzzy_fields,
            levenshtein_threshold=levenshtein_threshold,
            levenshtein_ratio_threshold=levenshtein_ratio_threshold,
        )
        changes.extend(field_changes)

        # SIN diff
        sin_changes = detect_sin_changes(
            contract_number=cn,
            vehicle_id=vehicle_id,
            run_id=run_id,
            db_sins=db_sins.get(cn, set()),
            csv_sins=csv_sins.get(cn, set()),
        )
        changes.extend(sin_changes)

    adds = sum(1 for c in changes if c.change_type == "ADD")
    removes = sum(1 for c in changes if c.change_type == "REMOVE")
    field_updates = sum(1 for c in changes if c.change_type == "FIELD_UPDATE")
    sin_adds = sum(1 for c in changes if c.change_type == "SIN_ADD")
    sin_removes = sum(1 for c in changes if c.change_type == "SIN_REMOVE")

    logger.info(
        f"Change summary | vehicle_id={vehicle_id} | "
        f"ADD={adds} REMOVE={removes} FIELD_UPDATE={field_updates} "
        f"SIN_ADD={sin_adds} SIN_REMOVE={sin_removes}"
    )

    return changes
