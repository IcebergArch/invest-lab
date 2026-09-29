#!/usr/bin/env python3
"""Deterministic, read-only broad comparison of a Qlib release and local bars.

SQLite source databases are copied through SQLite's backup API to temporary
snapshots. The originals are opened query-only and are never updated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sqlite3
import sys
import tempfile
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from quant_lab.qlib_local import read_stock


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _snapshot(source: Path, target: Path) -> dict[str, object]:
    if not source.is_file():
        raise FileNotFoundError(source)
    mode = "ro"
    try:
        origin = sqlite3.connect(source.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)
        origin.execute("SELECT 1 FROM daily_bars LIMIT 1").fetchone()
    except sqlite3.OperationalError:
        try:
            origin.close()
        except UnboundLocalError:
            pass
        mode = "rw_query_only"
        origin = sqlite3.connect(source.resolve().as_uri() + "?mode=rw", uri=True, timeout=10)
    try:
        origin.execute("PRAGMA query_only=ON")
        destination = sqlite3.connect(target)
        try:
            origin.backup(destination, pages=1000, sleep=0.1)
        finally:
            destination.close()
    finally:
        origin.close()
    return {"path": str(source.resolve()), "connection_mode": mode,
            "snapshot_size_bytes": target.stat().st_size,
            "snapshot_sha256": _sha256(target)}


def _quantile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * q
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def _distribution(values: list[float]) -> dict[str, float | int | None]:
    return {"count": len(values), "median": _quantile(values, 0.5),
            "p95": _quantile(values, 0.95), "p99": _quantile(values, 0.99),
            "max": max(values) if values else None}


def _year_groups(days: list[str]) -> list[tuple[str, str]]:
    """Keep each reader request below its 5,000-calendar-row safety limit."""
    years = sorted({int(day[:4]) for day in days})
    groups = []
    first = last = years[0]
    for year in years[1:]:
        if year == last + 1 and year - first < 10:
            last = year
        else:
            groups.append((f"{first}-01-01", f"{last}-12-31"))
            first = last = year
    groups.append((f"{first}-01-01", f"{last}-12-31"))
    return groups


def _select_evenly(ids: list[str], count: int) -> list[str]:
    if not ids:
        return []
    if len(ids) <= count:
        return ids
    return sorted({ids[round(index * (len(ids) - 1) / (count - 1))]
                   for index in range(count)})


def _query_rows(connection: sqlite3.Connection, stock: str,
                clause: str = "") -> list[sqlite3.Row]:
    return connection.execute(
        "SELECT instrument_id,trade_date,close,amount,amount_unit,source_id,adjustment,run_id "
        "FROM daily_bars WHERE instrument_id=? " + clause + " ORDER BY trade_date",
        (stock,),
    ).fetchall()


def build_report(qlib_root: Path, manifest: Path, tag: str,
                 historical: sqlite3.Connection, main: sqlite3.Connection,
                 historical_identity: dict[str, object], main_identity: dict[str, object],
                 sampled_stock_count: int) -> dict[str, object]:
    universe = [row[0] for row in historical.execute(
        "SELECT instrument_id FROM daily_bars WHERE source_id='baostock_daily' "
        "AND adjustment='none' AND instrument_id LIKE 'stock:%' "
        "GROUP BY instrument_id HAVING COUNT(*)>=30 ORDER BY instrument_id"
    )]
    selected = set(_select_evenly(universe, sampled_stock_count))
    selected.update(stock for stock in ("stock:000002.SZ", "stock:000003.SZ", "stock:600000.SH")
                    if stock in universe)
    main_ids = [row[0] for row in main.execute(
        "SELECT DISTINCT instrument_id FROM daily_bars "
        "WHERE source_id='eastmoney_kline' AND trade_date>='2026-09-01' "
        "AND instrument_id LIKE 'stock:%' ORDER BY instrument_id"
    )]

    price_abs: list[float] = []
    price_rel: list[float] = []
    amount_abs: list[float] = []
    amount_rel: list[float] = []
    paired = 0
    considered = 0
    no_qlib = 0
    invalid_factor = 0
    no_reference_amount = 0
    amount_unpaired_reason_counts: Counter[str] = Counter()
    years: dict[str, dict[str, object]] = defaultdict(lambda: {
        "reference_rows": 0, "paired": 0, "reference_stocks": set(),
        "paired_stocks": set(), "price_abs": [], "amount_rel": [],
    })
    source_counts: Counter[str] = Counter()
    source_metrics: dict[str, dict[str, object]] = defaultdict(lambda: {
        "reference_rows": 0, "paired": 0, "missing_qlib_close": 0,
        "invalid_qlib_factor_or_price": 0, "amount_unpaired": 0,
        "price_abs": [], "price_rel": [], "amount_abs": [], "amount_rel": [],
        "price_outliers": 0, "amount_outliers": 0,
    })
    run_counts: Counter[str] = Counter()
    price_outliers: list[dict[str, object]] = []
    amount_outliers: list[dict[str, object]] = []
    missing_examples: list[dict[str, object]] = []
    missing_qlib_reason_counts: Counter[str] = Counter()
    missing_reference_status_counts: Counter[str] = Counter()
    largest_price: list[dict[str, object]] = []
    largest_amount: list[dict[str, object]] = []
    stock_summaries: list[dict[str, object]] = []
    factor_jump_count = 0

    def compare_stock(stock: str, source_rows: list[sqlite3.Row], kind: str) -> None:
        nonlocal paired, considered, no_qlib, invalid_factor, no_reference_amount, factor_jump_count
        if not source_rows:
            return
        by_day = {}
        for start, end in _year_groups([row["trade_date"] for row in source_rows]):
            card = read_stock(qlib_root, manifest, tag, stock,
                              start=start, end=end, fields=("close", "factor", "amount"),
                              limit=5000)
            if card["truncated_to_latest"]:
                raise ValueError(f"Qlib stock slice was truncated for {stock}: {start}..{end}")
            by_day.update({row["date"]: row for row in card["rows"]})
        local_pairs = 0
        local_missing = 0
        previous_factor = None
        for reference in source_rows:
            considered += 1
            day = reference["trade_date"]
            yearly = years[day[:4]]
            yearly["reference_rows"] += 1
            yearly["reference_stocks"].add(stock)
            source_key = f"{kind}:{reference['source_id']}:{reference['adjustment']}"
            source_stat = source_metrics[source_key]
            source_stat["reference_rows"] += 1
            qrow = by_day.get(day)
            if qrow is None or qrow["close"] is None:
                no_qlib += 1
                local_missing += 1
                source_stat["missing_qlib_close"] += 1
                missing_reason = ("absent_in_instrument_window" if qrow is None
                                  else "null_close_feature")
                missing_qlib_reason_counts[missing_reason] += 1
                reference_status = None
                if kind == "historical_raw":
                    reference_status = historical.execute(
                        "SELECT tradestatus,is_st,has_valid_bar "
                        "FROM baostock_daily_status WHERE instrument_id=? AND trade_date=?",
                        (stock, day),
                    ).fetchone()
                status_key = ("status_absent" if reference_status is None else
                              f"tradestatus={reference_status['tradestatus']},"
                              f"is_st={reference_status['is_st']},"
                              f"has_valid_bar={reference_status['has_valid_bar']}")
                missing_reference_status_counts[status_key] += 1
                if len(missing_examples) < 50:
                    missing_examples.append({"instrument_id": stock, "date": day,
                                             "reference_source": reference["source_id"],
                                             "reason": missing_reason,
                                             "reference_status": status_key,
                                             "qlib_factor": qrow["factor"] if qrow else None,
                                             "qlib_amount": qrow["amount"] if qrow else None})
                continue
            factor = qrow["factor"]
            if factor is None or not math.isfinite(factor) or factor <= 0:
                invalid_factor += 1
                local_missing += 1
                source_stat["invalid_qlib_factor_or_price"] += 1
                if len(missing_examples) < 50:
                    missing_examples.append({"instrument_id": stock, "date": day,
                                             "reference_source": reference["source_id"],
                                             "reason": "invalid factor"})
                continue
            if previous_factor is not None and abs(factor / previous_factor - 1) > 0.01:
                factor_jump_count += 1
            previous_factor = factor
            reference_close = float(reference["close"])
            candidate_close = float(qrow["close"]) / factor
            if reference_close <= 0 or not math.isfinite(candidate_close):
                invalid_factor += 1
                local_missing += 1
                source_stat["invalid_qlib_factor_or_price"] += 1
                continue
            p_abs = abs(candidate_close - reference_close)
            p_rel = p_abs / reference_close
            price_abs.append(p_abs)
            price_rel.append(p_rel)
            source_stat["price_abs"].append(p_abs)
            source_stat["price_rel"].append(p_rel)
            source_stat["paired"] += 1
            yearly["price_abs"].append(p_abs)
            paired += 1
            local_pairs += 1
            yearly["paired"] += 1
            yearly["paired_stocks"].add(stock)
            source_counts[source_key] += 1
            if reference["run_id"]:
                run_counts[reference["run_id"]] += 1
            evidence = {"instrument_id": stock, "date": day,
                        "reference_source": reference["source_id"],
                        "reference_adjustment": reference["adjustment"],
                        "reference_run_id": reference["run_id"],
                        "candidate_raw_close": candidate_close,
                        "reference_close": reference_close,
                        "price_abs_cny": p_abs, "price_rel": p_rel}
            largest_price.append(evidence)
            if p_abs > 0.01 or p_rel > 0.001:
                price_outliers.append(evidence)
                source_stat["price_outliers"] += 1
            amount = qrow["amount"]
            reference_amount = reference["amount"]
            amount_issue = (
                "null_qlib_amount" if amount is None else
                "null_reference_amount" if reference_amount is None else
                "reference_not_cny" if reference["amount_unit"] != "CNY" else
                "nonpositive_reference_amount" if reference_amount <= 0 else None
            )
            if amount_issue is not None:
                no_reference_amount += 1
                source_stat["amount_unpaired"] += 1
                amount_unpaired_reason_counts[amount_issue] += 1
                continue
            candidate_amount = float(amount) * 1000
            a_abs = abs(candidate_amount - float(reference_amount))
            a_rel = a_abs / float(reference_amount)
            amount_abs.append(a_abs)
            amount_rel.append(a_rel)
            source_stat["amount_abs"].append(a_abs)
            source_stat["amount_rel"].append(a_rel)
            yearly["amount_rel"].append(a_rel)
            amount_evidence = {**evidence,
                               "candidate_amount_cny_if_x1000": candidate_amount,
                               "reference_amount_cny": float(reference_amount),
                               "amount_abs_cny_if_x1000": a_abs,
                               "amount_rel_if_x1000": a_rel}
            largest_amount.append(amount_evidence)
            if a_rel > 0.001 and a_abs > 1000:
                amount_outliers.append(amount_evidence)
                source_stat["amount_outliers"] += 1
        stock_summaries.append({"instrument_id": stock, "reference_kind": kind,
                                "reference_rows": len(source_rows), "paired": local_pairs,
                                "unpaired": local_missing,
                                "first_reference_day": source_rows[0]["trade_date"],
                                "last_reference_day": source_rows[-1]["trade_date"]})

    for stock in sorted(selected):
        compare_stock(stock, _query_rows(historical, stock,
                      "AND source_id='baostock_daily' AND adjustment='none'"), "historical_raw")
    for stock in main_ids:
        compare_stock(stock, _query_rows(main, stock,
                      "AND source_id='eastmoney_kline' AND trade_date>='2026-09-01'"), "main_recent_qfq")

    yearly_summary = {}
    for year, values in sorted(years.items()):
        yearly_summary[year] = {
            "reference_rows": values["reference_rows"], "paired": values["paired"],
            "reference_stock_count": len(values["reference_stocks"]),
            "paired_stock_count": len(values["paired_stocks"]),
            "price_abs_cny": _distribution(values["price_abs"]),
            "amount_rel_if_x1000": _distribution(values["amount_rel"]),
        }
    run_list = sorted(run_counts)
    release = json.loads(manifest.read_text())
    source_summary = {
        key: {
            "reference_rows": values["reference_rows"],
            "paired": values["paired"],
            "missing_qlib_close": values["missing_qlib_close"],
            "invalid_qlib_factor_or_price": values["invalid_qlib_factor_or_price"],
            "amount_unpaired": values["amount_unpaired"],
            "price_abs_cny": _distribution(values["price_abs"]),
            "price_rel": _distribution(values["price_rel"]),
            "amount_abs_cny_if_x1000": _distribution(values["amount_abs"]),
            "amount_rel_if_x1000": _distribution(values["amount_rel"]),
            "price_outlier_count": values["price_outliers"],
            "amount_outlier_count": values["amount_outliers"],
        }
        for key, values in sorted(source_metrics.items())
    }
    outlier_groups: Counter[tuple[str, str, str]] = Counter(
        (str(item["reference_source"]), str(item["instrument_id"]),
         str(item["date"])[:4]) for item in price_outliers
    )
    return {
        "status": "sample_crosscheck_only",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "qlib_release_tag": tag,
        "qlib_archive_sha256": release["archive_sha256"],
        "reference_snapshots": {"historical": historical_identity, "main": main_identity},
        "selection": {
            "method": "evenly spaced code quantiles from BaoStock archived stocks with >=30 bars, plus early 000002/000003/600000 and all recent EastMoney main stocks; all archived dates for selected stocks",
            "requested_historical_quantiles": sampled_stock_count,
            "historical_available_stock_count": len(universe),
            "historical_selected_stock_count": len(selected),
            "historical_selected_ids": sorted(selected),
            "main_recent_selected_ids": main_ids,
            "reference_rows_considered": considered,
        },
        "paired_count": paired,
        "unpaired_reference_count": no_qlib + invalid_factor,
        "missing_qlib_close_count": no_qlib,
        "missing_qlib_reason_counts": dict(sorted(missing_qlib_reason_counts.items())),
        "missing_qlib_reference_status_counts": dict(sorted(missing_reference_status_counts.items())),
        "invalid_qlib_factor_or_price_count": invalid_factor,
        "amount_unpaired_count": no_reference_amount,
        "amount_unpaired_reason_counts": dict(sorted(amount_unpaired_reason_counts.items())),
        "factor_jump_gt_1pct_pair_count": factor_jump_count,
        "price_abs_cny": _distribution(price_abs),
        "price_rel": _distribution(price_rel),
        "amount_abs_cny_if_x1000": _distribution(amount_abs),
        "amount_rel_if_x1000": _distribution(amount_rel),
        "price_outlier_threshold": "absolute > 0.01 CNY OR relative > 0.1%",
        "price_outlier_count": len(price_outliers),
        "price_outlier_examples": sorted(price_outliers,
                                         key=lambda item: item["price_abs_cny"], reverse=True)[:50],
        "amount_outlier_threshold": "absolute > 1000 CNY AND relative > 0.1%",
        "amount_outlier_count": len(amount_outliers),
        "amount_outlier_examples": sorted(amount_outliers,
                                          key=lambda item: item["amount_rel_if_x1000"], reverse=True)[:50],
        "largest_price_deltas": sorted(largest_price,
                                       key=lambda item: item["price_abs_cny"], reverse=True)[:20],
        "largest_amount_deltas": sorted(largest_amount,
                                        key=lambda item: item["amount_abs_cny_if_x1000"], reverse=True)[:20],
        "unpaired_examples": missing_examples,
        "by_year": yearly_summary,
        "by_reference_source": dict(sorted(source_counts.items())),
        "per_source_metrics": source_summary,
        "price_outlier_groups": [
            {"reference_source": source, "instrument_id": stock,
             "year": year, "count": count}
            for (source, stock, year), count in sorted(outlier_groups.items())
        ],
        "reference_run_ids": {"count": len(run_list),
                              "sorted_ids_sha256": "sha256:" + hashlib.sha256("\n".join(run_list).encode()).hexdigest(),
                              "sample": run_list[:20]},
        "per_stock": stock_summaries,
        "limitations": [
            "Main database prices are recent qfq observations, not an independent raw archive; report separates that reference family.",
            "BaoStock historical archive is currently sparse before 2024 and concentrated in low-numbered SZ codes; selection cannot establish all-market correctness.",
            "A Qlib release contains revised history; matching today's archive does not establish point-in-time availability or corporate-action accounting.",
            "Amount conversion is a tested hypothesis for these paired rows, not a universally approved unit contract.",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Broad read-only Qlib/BaoStock comparison")
    parser.add_argument("--qlib-root", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--expected-tag", required=True)
    parser.add_argument("--historical-db", required=True, type=Path)
    parser.add_argument("--main-db", required=True, type=Path)
    parser.add_argument("--historical-quantiles", type=int, default=48)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not 30 <= args.historical_quantiles <= 100:
        parser.error("historical-quantiles must be 30..100")
    with tempfile.TemporaryDirectory(prefix="qlib-crosscheck-") as temporary:
        scratch = Path(temporary)
        historical_file, main_file = scratch / "historical.sqlite3", scratch / "main.sqlite3"
        historical_identity = _snapshot(args.historical_db, historical_file)
        main_identity = _snapshot(args.main_db, main_file)
        historical = sqlite3.connect(historical_file)
        main = sqlite3.connect(main_file)
        historical.row_factory = sqlite3.Row
        main.row_factory = sqlite3.Row
        try:
            report = build_report(args.qlib_root, args.manifest, args.expected_tag,
                                  historical, main, historical_identity, main_identity,
                                  args.historical_quantiles)
        finally:
            historical.close()
            main.close()
    args.out.parent.mkdir(parents=True, exist_ok=True)
    temporary_out = args.out.with_name(args.out.name + ".tmp")
    with temporary_out.open("w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, allow_nan=False, indent=2)
        stream.write("\n")
        stream.flush()
    temporary_out.replace(args.out)
    print(json.dumps({"out": str(args.out), "paired_count": report["paired_count"],
                      "historical_selected_stock_count": report["selection"]["historical_selected_stock_count"],
                      "price_outlier_count": report["price_outlier_count"],
                      "amount_outlier_count": report["amount_outlier_count"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
