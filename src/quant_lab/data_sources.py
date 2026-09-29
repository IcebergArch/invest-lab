from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import time
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any, Mapping, Sequence, Tuple
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from quant_lab.models import DailyBar, Instrument
from quant_lab.universe import sw_industry_instrument


@dataclass(frozen=True)
class SourceSpec:
    source_id: str
    owner: str
    role: str
    trust_tier: int
    endpoint: str
    notes: str


SOURCE_CATALOG: Sequence[SourceSpec] = (
    SourceSpec(
        source_id="sse_official",
        owner="上海证券交易所 / 中证指数有限公司",
        role="指数代码、名称与编制方案",
        trust_tier=1,
        endpoint="https://www.sse.com.cn/market/sseindex/indexlist/",
        notes="官方口径源；本版本不从该页面抓行情。",
    ),
    SourceSpec(
        source_id="csi_official",
        owner="中证指数有限公司",
        role="中证系列指数元数据与编制方案",
        trust_tier=1,
        endpoint="https://www.csindex.com.cn/",
        notes="官方口径源；本版本不从该页面抓行情。",
    ),
    SourceSpec(
        source_id="sw_research",
        owner="申万宏源研究",
        role="申万一级行业清单与日线",
        trust_tier=1,
        endpoint="https://www.swsresearch.com/institute-sw/api/index_publish/",
        notes="官方发布接口；行业清单每次同步时动态核对。",
    ),
    SourceSpec(
        source_id="eastmoney_kline",
        owner="东方财富",
        role="A 股与宽基指数公开历史日线",
        trust_tier=2,
        endpoint="https://push2his.eastmoney.com/api/qt/stock/kline/get",
        notes="公开、免密但无 SLA；生产交易前应接入授权行情并做双源对账。",
    ),
    SourceSpec(
        source_id="tencent_kline",
        owner="腾讯证券",
        role="A 股与宽基指数历史日线回退源",
        trust_tier=2,
        endpoint="https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
        notes="东方财富连接限流时自动回退；返回无成交额，库内明确标为 unavailable。",
    ),
    SourceSpec(
        source_id="baostock_daily",
        owner="BaoStock",
        role="可选沪深 A 股前复权日线、人民币成交额及交易/ST状态",
        trust_tier=2,
        endpoint="https://pypi.org/project/baostock/",
        notes="已做独立试点对照；不含北交所，试点必须使用独立数据库。",
    ),
    SourceSpec(
        source_id="baostock_stock_basic",
        owner="BaoStock",
        role="可选当前沪深在市股票清单",
        trust_tier=2,
        endpoint="https://pypi.org/project/baostock/",
        notes="当前列表不是历史时点股票池；不含北交所及已退市历史成分。",
    ),
    SourceSpec(
        source_id="akshare_reference",
        owner="AKShare contributors / GitHub",
        role="接口字段与采集实现交叉验证",
        trust_tier=3,
        endpoint="https://github.com/akfamily/akshare",
        notes="只作实现参考，不作为原始行情来源。",
    ),
)


class HttpJsonClient:
    def __init__(self, timeout: float = 30.0, retries: int = 3):
        self.timeout = timeout
        self.retries = retries

    def get(self, endpoint: str, params: Mapping[str, Any]) -> Tuple[Mapping[str, Any], str]:
        # Both public quote endpoints accept comma-delimited composite params.
        # Keeping commas literal avoids intermittent gateway/TLS rejection seen
        # with percent-encoded commas on the Tencent endpoint.
        url = f"{endpoint}?{urlencode(params, safe=',')}"
        curl = shutil.which("curl")
        if curl:
            try:
                completed = subprocess.run(
                    [
                        curl,
                        "--fail",
                        "--silent",
                        "--show-error",
                        "--location",
                        "--retry",
                        str(max(0, self.retries - 1)),
                        "--retry-all-errors",
                        "--connect-timeout",
                        str(int(self.timeout)),
                        "--max-time",
                        str(int(self.timeout)),
                        "--user-agent",
                        "Mozilla/5.0 Chrome/124 Safari/537.36",
                        url,
                    ],
                    check=True,
                    capture_output=True,
                    timeout=self.timeout * self.retries + 5,
                )
                parsed = json.loads(completed.stdout.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("JSON root must be an object")
                return parsed, hashlib.sha256(completed.stdout).hexdigest()
            except (
                subprocess.SubprocessError,
                UnicodeError,
                json.JSONDecodeError,
                ValueError,
            ) as exc:
                # urllib remains a portable fallback when curl is absent or rejected.
                curl_error = exc
        else:
            curl_error = None
        request = Request(
            url,
            headers={
                "Accept": "application/json,text/plain,*/*",
                "Referer": "https://quote.eastmoney.com/",
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 Chrome/124 Safari/537.36"
                ),
            },
        )
        last_error: Exception | None = None
        for attempt in range(self.retries):
            try:
                with urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                parsed = json.loads(body.decode("utf-8"))
                if not isinstance(parsed, dict):
                    raise ValueError("JSON root must be an object")
                return parsed, hashlib.sha256(body).hexdigest()
            except Exception as exc:  # network errors differ between Python versions
                last_error = exc
                if attempt + 1 < self.retries:
                    time.sleep(0.5 * (2**attempt))
        details = f"curl={curl_error}; urllib={last_error}"
        raise RuntimeError(f"GET failed after {self.retries} attempts: {url}: {details}")


class EastMoneyDailySource:
    endpoint = "https://push2his.eastmoney.com/api/qt/stock/kline/get"

    def __init__(self, http: HttpJsonClient | None = None):
        self.http = http or HttpJsonClient()

    def fetch(self, instrument: Instrument, start: date, end: date) -> list[DailyBar]:
        if instrument.source_id != "eastmoney_kline":
            raise ValueError(f"unsupported instrument source: {instrument.source_id}")
        adjustment_code = {"none": "0", "qfq": "1", "hfq": "2"}[instrument.adjustment]
        payload, payload_hash = self.http.get(
            self.endpoint,
            {
                "secid": instrument.provider_code,
                "fields1": "f1,f2,f3,f4,f5,f6",
                "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
                "klt": "101",
                "fqt": adjustment_code,
                "beg": start.strftime("%Y%m%d"),
                "end": end.strftime("%Y%m%d"),
                "lmt": "5000",
            },
        )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError(f"{instrument.instrument_id}: provider returned no data")
        provider_code = str(data.get("code") or "")
        expected_code = instrument.symbol.split(".", 1)[0]
        if provider_code and provider_code != expected_code:
            raise ValueError(
                f"{instrument.instrument_id}: provider code {provider_code!r} "
                f"does not match {expected_code!r}"
            )
        provider_name = str(data.get("name") or "")
        normalized_name = lambda value: "".join(unicodedata.normalize("NFKC", value).split()).casefold()
        if provider_name and normalized_name(provider_name) != normalized_name(instrument.name):
            raise ValueError(
                f"{instrument.instrument_id}: provider name {provider_name!r} "
                f"does not match configured name {instrument.name!r}"
            )
        rows = data.get("klines")
        if not isinstance(rows, list):
            raise ValueError(f"{instrument.instrument_id}: missing klines")
        bars = [
            parse_eastmoney_kline(
                instrument=instrument,
                raw=str(raw),
                payload_hash=payload_hash,
            )
            for raw in rows
        ]
        return [bar for bar in bars if start <= bar.trade_date <= end]


def parse_eastmoney_kline(
    instrument: Instrument, raw: str, payload_hash: str
) -> DailyBar:
    fields = raw.split(",")
    if len(fields) < 7:
        raise ValueError(f"{instrument.instrument_id}: malformed kline: {raw!r}")
    bar = DailyBar(
        instrument_id=instrument.instrument_id,
        trade_date=date.fromisoformat(fields[0]),
        open=float(fields[1]),
        close=float(fields[2]),
        high=float(fields[3]),
        low=float(fields[4]),
        volume=float(fields[5]),
        amount=float(fields[6]),
        volume_unit="lot",
        amount_unit="CNY",
        adjustment=instrument.adjustment,
        source_id=instrument.source_id,
        payload_hash=payload_hash,
    )
    bar.validate()
    return bar


class TencentDailySource:
    endpoint = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"

    def __init__(self, http: HttpJsonClient | None = None):
        self.http = http or HttpJsonClient(timeout=30.0, retries=2)

    def fetch(self, instrument: Instrument, start: date, end: date) -> list[DailyBar]:
        symbol = self._symbol(instrument.provider_code)
        cursor_end = end
        collected: dict[date, DailyBar] = {}
        previous_earliest: date | None = None
        for _ in range(20):
            payload, payload_hash = self.http.get(
                self.endpoint,
                {
                    "param": (
                        f"{symbol},day,{start.isoformat()},{cursor_end.isoformat()},640,qfq"
                    )
                },
            )
            if payload.get("code") not in (0, "0"):
                raise ValueError(
                    f"{instrument.instrument_id}: Tencent error: {payload.get('msg')}"
                )
            data = payload.get("data")
            container = data.get(symbol) if isinstance(data, dict) else None
            if not isinstance(container, dict):
                raise ValueError(f"{instrument.instrument_id}: Tencent returned no data")
            rows = container.get("qfqday") or container.get("day") or []
            if not rows:
                break
            earliest = date.fromisoformat(str(rows[0][0]))
            if earliest == previous_earliest:
                raise ValueError(f"{instrument.instrument_id}: Tencent pagination stalled")
            previous_earliest = earliest
            for row in rows:
                trade_date = date.fromisoformat(str(row[0]))
                if trade_date < start or trade_date > end:
                    continue
                bar = DailyBar(
                    instrument_id=instrument.instrument_id,
                    trade_date=trade_date,
                    open=float(row[1]),
                    close=float(row[2]),
                    high=float(row[3]),
                    low=float(row[4]),
                    volume=float(row[5]),
                    amount=None,
                    volume_unit="lot",
                    amount_unit="unavailable",
                    adjustment=instrument.adjustment,
                    source_id="tencent_kline",
                    payload_hash=payload_hash,
                )
                bar.validate()
                collected[trade_date] = bar
            if earliest <= start:
                break
            cursor_end = earliest - timedelta(days=1)
        return [collected[key] for key in sorted(collected)]

    @staticmethod
    def _symbol(provider_code: str) -> str:
        market, code = provider_code.split(".", 1)
        if market == "0":
            return f"sz{code}"
        if market == "1":
            return f"sh{code}"
        raise ValueError(f"unsupported Tencent market code: {provider_code}")


class PublicMarketDailySource:
    """EastMoney primary with an explicitly attributed Tencent fallback."""

    def __init__(self) -> None:
        self.primary = EastMoneyDailySource(HttpJsonClient(timeout=15.0, retries=1))
        self.fallback = TencentDailySource()
        self.primary_available = True

    def fetch(self, instrument: Instrument, start: date, end: date) -> list[DailyBar]:
        primary_error: Exception | None = None
        if self.primary_available:
            try:
                return self.primary.fetch(instrument, start, end)
            except Exception as exc:
                primary_error = exc
                # Connection-level failures tend to affect the whole batch.
                # Fuse the primary after one failure instead of waiting on every item.
                self.primary_available = False
        try:
            return self.fallback.fetch(instrument, start, end)
        except Exception as fallback_error:
            raise RuntimeError(
                f"{instrument.instrument_id}: all available market sources failed; "
                f"eastmoney={primary_error}; tencent={fallback_error}"
            ) from fallback_error


class SwResearchDailySource:
    listing_endpoint = "https://www.swsresearch.com/institute-sw/api/index_publish/current/"
    trend_endpoint = "https://www.swsresearch.com/institute-sw/api/index_publish/trend/"

    def __init__(self, http: HttpJsonClient | None = None):
        self.http = http or HttpJsonClient(timeout=45.0)

    def list_level_one(self) -> list[Instrument]:
        payload, _ = self.http.get(
            self.listing_endpoint,
            {"page": "1", "page_size": "100", "indextype": "一级行业"},
        )
        data = payload.get("data")
        if not isinstance(data, dict) or not isinstance(data.get("results"), list):
            raise ValueError("SW Research returned no level-one industry list")
        count = int(data.get("count") or 0)
        results = data["results"]
        if count != len(results):
            raise ValueError(f"SW Research industry list incomplete: expected {count}, got {len(results)}")
        instruments = [
            sw_industry_instrument(str(row["swindexcode"]), str(row["swindexname"]))
            for row in results
        ]
        if len(instruments) < 25:
            raise ValueError(f"unexpectedly small SW level-one universe: {len(instruments)}")
        return sorted(instruments, key=lambda item: item.instrument_id)

    def fetch(self, instrument: Instrument, start: date, end: date) -> list[DailyBar]:
        if instrument.source_id != "sw_research":
            raise ValueError(f"unsupported instrument source: {instrument.source_id}")
        payload, payload_hash = self.http.get(
            self.trend_endpoint,
            {"swindexcode": instrument.provider_code, "period": "DAY"},
        )
        rows = payload.get("data")
        if not isinstance(rows, list):
            raise ValueError(f"{instrument.instrument_id}: SW Research returned no history")
        bars = []
        for row in rows:
            trade_date = date.fromisoformat(str(row["bargaindate"])[:10])
            if trade_date < start or trade_date > end:
                continue
            bar = DailyBar(
                instrument_id=instrument.instrument_id,
                trade_date=trade_date,
                open=float(row["openindex"]),
                high=float(row["maxindex"]),
                low=float(row["minindex"]),
                close=float(row["closeindex"]),
                volume=float(row.get("bargainamount") or 0),
                amount=float(row.get("bargainsum") or 0),
                volume_unit="source_native",
                amount_unit="source_native",
                adjustment="none",
                source_id=instrument.source_id,
                payload_hash=payload_hash,
            )
            bar.validate()
            bars.append(bar)
        return sorted(bars, key=lambda item: item.trade_date)
