"""Fixed Qlib cohort policy-event report, with separately sourced CSI 300.

Only verified Qlib adjusted CLOSE observations are imported.  Qlib volume and
amount remain unavailable here because their source units are not validated.
The main-store index is compared solely through its own dimensionless return.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Mapping

import quant_lab.event_study as event_study_module
import quant_lab.forecast_qlib as forecast_qlib_module
from quant_lab.canonical_data import MarketStoreAdapter
from quant_lab.event_study import (
    EventDailyBar, evaluate_event_study, load_event_registry,
)
from quant_lab.forecast_qlib import (
    COHORT_VERSION, _read_series, _stratum, select_stratified_cohort,
)
from quant_lab.qlib_archive import (
    QlibArchiveError, SOURCE_ID, _stock_id, parse_calendar, parse_instruments,
)
from quant_lab.qlib_local import _load_index, _verified_file


REPORT_VERSION = "qlib-fixed-cohort-macro-events-v1"
FIXED_PER_STRATUM = 3
DEFAULT_BENCHMARK = "index:000300.SH"


def _sha256_json(value: object) -> str:
    return hashlib.sha256(json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")).hexdigest()


def evaluate_qlib_event_cohort(
    root: str | Path,
    manifest: str | Path,
    expected_tag: str,
    main_database: str | Path,
    event_registry: str | Path,
    *,
    benchmark_id: str = DEFAULT_BENCHMARK,
) -> dict[str, Any]:
    """Evaluate the forecast benchmark's *same deterministic* 40-stock rule.

    The selected count is release-dependent; the 2026-09-28 release selects
    40.  This function rejects a missing selected close feature instead of
    quietly substituting a different stock.  An all-NaN feature is recorded
    as selected but not observed, and its absence is explicit in coverage.
    """
    root_path = Path(root).resolve()
    index = _load_index(root_path, Path(manifest), expected_tag)
    if index["source_id"] != SOURCE_ID:
        raise QlibArchiveError("unexpected Qlib source")
    with _verified_file(root_path, index, "qlib_bin/calendars/day.txt") as stream:
        qlib_calendar = parse_calendar(stream.read(), "day calendar")
    with _verified_file(root_path, index, "qlib_bin/instruments/all.txt") as stream:
        instruments = parse_instruments(stream.read(), "all instruments")
    target = date.fromisoformat(index["release"]["target_trade_date"])
    if qlib_calendar[-1] != target:
        raise QlibArchiveError("published calendar target differs from release")
    symbols, population = select_stratified_cohort(
        instruments, target, per_stratum=FIXED_PER_STRATUM,
    )
    if not symbols:
        raise QlibArchiveError("fixed cohort selection is empty")
    events = load_event_registry(event_registry)
    stock_bars: list[EventDailyBar] = []
    cohort_members: dict[str, list[str]] = defaultdict(list)
    selected: list[dict[str, Any]] = []
    dataset_id = ("qlib-release:" + index["release"]["tag"] + ":"
                  + index["release"]["archive_sha256"])
    for symbol in symbols:
        spans = instruments[symbol]
        days, closes, evidence = _read_series(
            root_path, index, symbol, spans, qlib_calendar,
        )
        if evidence["status"] == "missing_close_feature" or not evidence["file_sha256"]:
            raise QlibArchiveError(f"selected close feature is absent: {symbol}")
        file_hash = str(evidence["file_sha256"])
        if not file_hash.startswith("sha256:") or len(file_hash) != 71:
            raise QlibArchiveError(f"invalid selected close feature hash: {symbol}")
        instrument_id = _stock_id(symbol)
        stratum = _stratum(symbol, spans, target)
        if days:
            cohort_members[stratum].append(instrument_id)
        selected.append({
            "symbol": symbol, "instrument_id": instrument_id,
            "stratum": stratum,
            "intervals": [{"start": begin.isoformat(), "end": end.isoformat()}
                          for begin, end in spans],
            **evidence,
        })
        for day, close in zip(days, closes):
            stock_bars.append(EventDailyBar(
                instrument_id=instrument_id, asset_type="stock",
                trade_date=day, close=close,
                price_basis="qlib_adjusted",
                price_unit="Qlib adjusted close feature",
                source_id=SOURCE_ID, dataset_id=dataset_id,
                payload_hash=file_hash[len("sha256:"):],
                volume_shares=None, amount_cny=None,
                tradability="unknown", observed_at=None,
            ))
    benchmark_bars = list(MarketStoreAdapter(main_database).iter_bars(
        instrument_ids=[benchmark_id], end=target,
    ))
    if not benchmark_bars:
        raise ValueError("main-store benchmark index is absent")
    if any(bar.asset_type != "index" or bar.price_basis != "index_points"
           for bar in benchmark_bars):
        raise ValueError("main-store benchmark must contain only index point bars")
    if benchmark_bars[-1].trade_date < max(event.announcement_day for event in events):
        raise ValueError("benchmark ends before the registered events")
    index_dates = {bar.trade_date for bar in benchmark_bars}
    qlib_dates = {day for day in qlib_calendar if benchmark_bars[0].trade_date <= day <= target}
    missing_index_dates = qlib_dates - index_dates
    index_lineage = {
        "instrument_id": benchmark_id,
        "price_basis": "index_points",
        "source_ids": sorted({bar.source_id for bar in benchmark_bars}),
        "run_ids": sorted({bar.run_id for bar in benchmark_bars if bar.run_id}),
        "bar_count": len(benchmark_bars),
        "first_trade_date": benchmark_bars[0].trade_date.isoformat(),
        "last_trade_date": benchmark_bars[-1].trade_date.isoformat(),
        "qlib_calendar_sessions_missing_in_index": len(missing_index_dates),
        "qlib_calendar_missing_dates_sample": [day.isoformat()
                                                for day in sorted(missing_index_dates)[:10]],
    }
    analysis = evaluate_event_study(
        events, [*stock_bars, *benchmark_bars], benchmark_id=benchmark_id,
        asof=target, cohorts=cohort_members,
    )
    observed_ids = {bar.instrument_id for bar in stock_bars}
    selection = {
        "cohort_version": COHORT_VERSION,
        "per_stratum": FIXED_PER_STRATUM,
        "strata_population": population,
        "selected_count": len(selected),
        "observed_stock_count": len(observed_ids),
        "selected_without_valid_close": [item["instrument_id"] for item in selected
                                         if item["instrument_id"] not in observed_ids],
        "selected": selected,
    }
    release = {
        "source_id": SOURCE_ID,
        "release": index["release"],
        "calendar_sha256": index["files"]["qlib_bin/calendars/day.txt"]["sha256"],
        "instruments_sha256": index["files"]["qlib_bin/instruments/all.txt"]["sha256"],
    }
    analysis["qlib_cohort_report_version"] = REPORT_VERSION
    analysis["qlib_selection"] = selection
    analysis["qlib_release"] = release
    analysis["benchmark_lineage"] = index_lineage
    analysis["event_registry_file_sha256"] = hashlib.sha256(
        Path(event_registry).read_bytes()
    ).hexdigest()
    analysis["implementation_sha256"] = {
        "event_study": hashlib.sha256(Path(event_study_module.__file__).read_bytes()).hexdigest(),
        "event_study_qlib": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "cohort_selection_module": hashlib.sha256(
            Path(forecast_qlib_module.__file__).read_bytes()
        ).hexdigest(),
    }
    analysis["qlib_context_key_sha256"] = _sha256_json({
        "event_experiment_key": analysis["experiment_key_sha256"],
        "selection": selection,
        "release": release,
        "benchmark_lineage": index_lineage,
        "event_registry_file_sha256": analysis["event_registry_file_sha256"],
        "implementation_sha256": analysis["implementation_sha256"],
    })
    analysis["liquidity_observation_status"] = (
        "Qlib volume and amount source units unverified; excluded from normalized ratio metrics"
    )
    analysis["cohort_scope"] = (
        "Fixed retrospective strata from the release's recorded instrument intervals; "
        "not an A-share market-representative or contemporaneously known universe"
    )
    analysis["limitations"].extend([
        "40 股按固定 Qlib Release 的交易所、首次区间年代和区间是否结束分层，抽样使用了发布时的回看信息。",
        "Qlib 复权收盘价的单位与主库前复权价不同；仅各自计算无量纲收益，主库沪深 300 只作收益背景。",
        "Qlib 量额源单位尚未验证，未计算该 40 股的成交量比和成交额比。",
        "指数历史来自当前主库快照，与固定 Qlib Release 不是同一冻结数据版本。",
    ])
    return analysis
