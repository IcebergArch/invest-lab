from __future__ import annotations

from typing import Iterable, Sequence

from quant_lab.models import Instrument


CORE_INSTRUMENTS: Sequence[Instrument] = (
    Instrument(
        instrument_id="stock:002475.SZ",
        symbol="002475.SZ",
        name="立讯精密",
        asset_type="stock",
        family="focus_stock",
        source_id="eastmoney_kline",
        provider_code="0.002475",
        exchange="SZSE",
        adjustment="qfq",
    ),
    Instrument(
        instrument_id="stock:000338.SZ",
        symbol="000338.SZ",
        name="潍柴动力",
        asset_type="stock",
        family="focus_stock",
        source_id="eastmoney_kline",
        provider_code="0.000338",
        exchange="SZSE",
        adjustment="qfq",
    ),
    Instrument(
        instrument_id="stock:002714.SZ",
        symbol="002714.SZ",
        name="牧原股份",
        asset_type="stock",
        family="focus_stock",
        source_id="eastmoney_kline",
        provider_code="0.002714",
        exchange="SZSE",
        adjustment="qfq",
    ),
    Instrument(
        instrument_id="index:000688.SH",
        symbol="000688.SH",
        name="科创50",
        asset_type="index",
        family="star_market",
        source_id="eastmoney_kline",
        provider_code="1.000688",
        exchange="SSE",
    ),
    Instrument(
        instrument_id="index:000001.SH",
        symbol="000001.SH",
        name="上证指数",
        asset_type="index",
        family="sse",
        source_id="eastmoney_kline",
        provider_code="1.000001",
        exchange="SSE",
    ),
    Instrument(
        instrument_id="index:000300.SH",
        symbol="000300.SH",
        name="沪深300",
        asset_type="index",
        family="csi",
        source_id="eastmoney_kline",
        provider_code="1.000300",
        exchange="CSI",
    ),
    Instrument(
        instrument_id="index:000905.SH",
        symbol="000905.SH",
        name="中证500",
        asset_type="index",
        family="csi",
        source_id="eastmoney_kline",
        provider_code="1.000905",
        exchange="CSI",
    ),
    Instrument(
        instrument_id="index:000852.SH",
        symbol="000852.SH",
        name="中证1000",
        asset_type="index",
        family="csi",
        source_id="eastmoney_kline",
        provider_code="1.000852",
        exchange="CSI",
    ),
)


def sw_industry_instrument(code: str, name: str) -> Instrument:
    return Instrument(
        instrument_id=f"industry:{code}.SW",
        symbol=f"{code}.SW",
        name=name,
        asset_type="industry_index",
        family="sw_level_1",
        source_id="sw_research",
        provider_code=code,
        exchange="SW",
    )


def select_core(groups: Iterable[str]) -> list[Instrument]:
    selected = set(groups)
    if not selected or "all" in selected or "core" in selected:
        return list(CORE_INSTRUMENTS)
    result = []
    for item in CORE_INSTRUMENTS:
        if "stocks" in selected and item.asset_type == "stock":
            result.append(item)
        elif "broad" in selected and item.asset_type == "index":
            result.append(item)
    return result
