"""
업비트 API 래퍼.

공식 SDK(upbit-sdk 0.9.x) 위에 실전 운용에 필요한 계층을 얹는다.
  * 그룹별 토큰버킷 레이트리미터 (Quotation 10rps / Exchange 8rps 제한 대응)
  * 429 / 5xx / 네트워크 오류에 대한 지수 백오프 재시도
  * 캔들 조회 결과 TTL 캐시 (동일 봉을 반복 조회하지 않음)
  * 호가 단위(tick) 자동 산출 - 실제 호가창 간격에서 역산, 실패 시 표준표 사용
  * DRY_RUN 모의계좌(PaperBroker) - 실주문 없이 동일 인터페이스로 동작

주의: API 키는 반드시 "자산조회 + 주문" 권한만 부여하고 출금 권한은 비활성화할 것.
"""
from __future__ import annotations

import json
import math
import threading
import time
import uuid as uuidlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import pandas as pd

from core.logger import get_logger

log = get_logger("upbit")

try:
    from upbit import Upbit
    from upbit import APIConnectionError, APIStatusError, RateLimitError
except ImportError as exc:  # pragma: no cover
    raise SystemExit("upbit-sdk 가 설치되어 있지 않습니다. `pip install -r requirements.txt` 를 실행하세요.") from exc


# --------------------------------------------------------------------------- #
# 공용 자료구조
# --------------------------------------------------------------------------- #
@dataclass
class OrderResult:
    uuid: str
    market: str
    side: str  # "bid" | "ask"
    ord_type: str  # "price" | "market" | "limit"
    state: str  # "wait" | "done" | "cancel"
    price: float = 0.0  # 지정가 주문 가격 (시장가 매수는 주문 금액)
    volume: float = 0.0  # 주문 수량
    executed_volume: float = 0.0
    avg_price: float = 0.0
    paid_fee: float = 0.0
    funds: float = 0.0  # 실제 체결 대금(수수료 제외)
    created_at: str = ""

    @property
    def is_done(self) -> bool:
        return self.state == "done"

    @property
    def is_open(self) -> bool:
        return self.state in ("wait", "watch")


@dataclass
class Balance:
    currency: str
    balance: float = 0.0
    locked: float = 0.0
    avg_buy_price: float = 0.0

    @property
    def total(self) -> float:
        return self.balance + self.locked


# --------------------------------------------------------------------------- #
# 레이트 리미터
# --------------------------------------------------------------------------- #
class _TokenBucket:
    def __init__(self, rate_per_sec: float, capacity: float | None = None) -> None:
        self.rate = rate_per_sec
        self.capacity = capacity if capacity is not None else rate_per_sec
        self._tokens = self.capacity
        self._last = time.monotonic()
        self._lock = threading.Lock()

    def acquire(self, tokens: float = 1.0) -> None:
        with self._lock:
            while True:
                now = time.monotonic()
                self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
                self._last = now
                if self._tokens >= tokens:
                    self._tokens -= tokens
                    return
                time.sleep((tokens - self._tokens) / self.rate)


# --------------------------------------------------------------------------- #
# 호가 단위
# --------------------------------------------------------------------------- #
_KRW_TICK_TABLE: tuple[tuple[float, float], ...] = (
    (2_000_000, 1_000),
    (1_000_000, 1_000),
    (500_000, 500),
    (100_000, 100),
    (10_000, 10),
    (1_000, 1),
    (100, 1),
    (10, 0.01),
    (1, 0.001),
    (0.1, 0.0001),
    (0.01, 0.00001),
    (0.001, 0.000001),
    (0.0001, 0.0000001),
)


def krw_tick_size(price: float) -> float:
    """업비트 KRW 마켓 표준 호가 단위."""
    for threshold, tick in _KRW_TICK_TABLE:
        if price >= threshold:
            return tick
    return 0.00000001


def round_to_tick(price: float, tick: float, mode: str = "nearest") -> float:
    if tick <= 0:
        return price
    ratio = price / tick
    if mode == "floor":
        n = math.floor(ratio + 1e-9)
    elif mode == "ceil":
        n = math.ceil(ratio - 1e-9)
    else:
        n = round(ratio)
    # 부동소수 오차 제거 (호가 단위가 0.00000001 까지 내려가므로 8자리 반올림)
    return round(n * tick, 8)


def fmt_volume(volume: float) -> str:
    return f"{volume:.8f}".rstrip("0").rstrip(".") or "0"


def fmt_price(price: float) -> str:
    if price >= 1:
        text = f"{price:.4f}".rstrip("0").rstrip(".")
    else:
        text = f"{price:.8f}".rstrip("0").rstrip(".")
    return text or "0"


# --------------------------------------------------------------------------- #
# 모의 계좌
# --------------------------------------------------------------------------- #
class PaperBroker:
    """
    DRY_RUN 전용 모의 체결 엔진.

    실주문을 내지 않고 잔고/주문을 로컬 JSON 에 유지한다. 지정가 주문은 매 루프
    현재가를 기준으로 교차 여부를 확인해 체결시키고, 시장가 주문은 슬리피지를
    반영한 가격으로 즉시 체결시킨다. 수수료는 실제와 동일하게 차감한다.
    """

    def __init__(self, path: Path, seed_krw: float, fee_rate: float, slippage: float) -> None:
        self.path = path
        self.fee_rate = fee_rate
        self.slippage = slippage
        self.balances: dict[str, Balance] = {}
        self.open_orders: dict[str, dict[str, Any]] = {}
        self.closed_orders: dict[str, dict[str, Any]] = {}
        self._load(seed_krw)

    # ----- 영속화 -----
    def _load(self, seed_krw: float) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.balances = {
                    c: Balance(**b) for c, b in raw.get("balances", {}).items()
                }
                self.open_orders = raw.get("open_orders", {})
                self.closed_orders = raw.get("closed_orders", {})
                return
            except Exception as exc:
                log.warning("모의계좌 파일 손상(%s) - 새로 시작합니다", exc)
        self.balances = {"KRW": Balance("KRW", balance=seed_krw)}
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "balances": {c: vars(b) for c, b in self.balances.items()},
            "open_orders": self.open_orders,
            "closed_orders": dict(list(self.closed_orders.items())[-500:]),
        }
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ----- 잔고 -----
    def _bal(self, currency: str) -> Balance:
        return self.balances.setdefault(currency, Balance(currency))

    # ----- 주문 -----
    def create(self, market: str, side: str, ord_type: str, price: float, volume: float) -> OrderResult:
        base, quote = market.split("-")[1], market.split("-")[0]
        oid = f"paper-{uuidlib.uuid4().hex[:16]}"
        order = {
            "uuid": oid,
            "market": market,
            "side": side,
            "ord_type": ord_type,
            "price": price,
            "volume": volume,
            "created_at": time.time(),
        }
        if ord_type == "limit":
            if side == "bid":
                need = price * volume
                if self._bal(quote).balance < need:
                    raise ValueError("모의계좌 원화 잔고 부족")
                self._bal(quote).balance -= need
                self._bal(quote).locked += need
            else:
                if self._bal(base).balance < volume:
                    raise ValueError("모의계좌 코인 잔고 부족")
                self._bal(base).balance -= volume
                self._bal(base).locked += volume
            self.open_orders[oid] = order
            self.save()
            return OrderResult(oid, market, side, ord_type, "wait", price=price, volume=volume)

        # 시장가: 즉시 체결
        raise RuntimeError("시장가 주문은 fill_market() 으로 처리합니다")

    def fill_market(self, market: str, side: str, ref_price: float, krw: float = 0.0, volume: float = 0.0) -> OrderResult:
        base, quote = market.split("-")[1], market.split("-")[0]
        oid = f"paper-{uuidlib.uuid4().hex[:16]}"
        if side == "bid":
            fill_price = ref_price * (1 + self.slippage)
            if self._bal(quote).balance < krw:
                raise ValueError("모의계좌 원화 잔고 부족")
            fee = krw * self.fee_rate
            qty = (krw - fee) / fill_price
            self._bal(quote).balance -= krw
            self._apply_buy(base, qty, fill_price)
            res = OrderResult(oid, market, side, "price", "done", price=krw, volume=qty,
                              executed_volume=qty, avg_price=fill_price, paid_fee=fee, funds=krw - fee)
        else:
            fill_price = ref_price * (1 - self.slippage)
            if self._bal(base).balance < volume - 1e-12:
                volume = self._bal(base).balance
            gross = fill_price * volume
            fee = gross * self.fee_rate
            self._bal(base).balance -= volume
            if self._bal(base).balance < 1e-12:
                self._bal(base).balance = 0.0
                self._bal(base).avg_buy_price = 0.0
            self._bal(quote).balance += gross - fee
            res = OrderResult(oid, market, side, "market", "done", volume=volume,
                              executed_volume=volume, avg_price=fill_price, paid_fee=fee, funds=gross - fee)
        self.closed_orders[oid] = {"market": market, "side": side, "avg_price": res.avg_price,
                                   "volume": res.executed_volume, "at": time.time()}
        self.save()
        return res

    def _apply_buy(self, base: str, qty: float, price: float) -> None:
        b = self._bal(base)
        total_cost = b.avg_buy_price * b.total + price * qty
        b.balance += qty
        b.avg_buy_price = total_cost / b.total if b.total > 0 else price

    def poll_limit_fills(self, price_map: dict[str, float]) -> list[OrderResult]:
        """현재가와 교차한 지정가 주문을 체결 처리하고 결과를 돌려준다."""
        filled: list[OrderResult] = []
        for oid, o in list(self.open_orders.items()):
            px = price_map.get(o["market"])
            if px is None:
                continue
            crossed = (o["side"] == "bid" and px <= o["price"]) or (o["side"] == "ask" and px >= o["price"])
            if not crossed:
                continue
            base, quote = o["market"].split("-")[1], o["market"].split("-")[0]
            vol, limit = float(o["volume"]), float(o["price"])
            gross = vol * limit
            fee = gross * self.fee_rate
            if o["side"] == "bid":
                self._bal(quote).locked = max(0.0, self._bal(quote).locked - gross)
                self._apply_buy(base, vol - fee / limit, limit)
                executed = vol - fee / limit
            else:
                self._bal(base).locked = max(0.0, self._bal(base).locked - vol)
                self._bal(quote).balance += gross - fee
                executed = vol
                if self._bal(base).total < 1e-12:
                    self._bal(base).avg_buy_price = 0.0
            del self.open_orders[oid]
            self.closed_orders[oid] = {"market": o["market"], "side": o["side"], "avg_price": limit,
                                       "volume": executed, "at": time.time()}
            filled.append(OrderResult(oid, o["market"], o["side"], "limit", "done", price=limit,
                                      volume=vol, executed_volume=executed, avg_price=limit,
                                      paid_fee=fee, funds=gross - fee))
        if filled:
            self.save()
        return filled

    def cancel(self, order_uuid: str) -> bool:
        o = self.open_orders.pop(order_uuid, None)
        if not o:
            return False
        base, quote = o["market"].split("-")[1], o["market"].split("-")[0]
        if o["side"] == "bid":
            amount = float(o["price"]) * float(o["volume"])
            self._bal(quote).locked = max(0.0, self._bal(quote).locked - amount)
            self._bal(quote).balance += amount
        else:
            vol = float(o["volume"])
            self._bal(base).locked = max(0.0, self._bal(base).locked - vol)
            self._bal(base).balance += vol
        self.save()
        return True


# --------------------------------------------------------------------------- #
# 메인 클라이언트
# --------------------------------------------------------------------------- #
class UpbitClient:
    def __init__(self, settings) -> None:
        self.s = settings
        self.dry_run = settings.dry_run
        self._quotation = _TokenBucket(rate_per_sec=8.0, capacity=8.0)
        self._exchange = _TokenBucket(rate_per_sec=6.0, capacity=6.0)
        self._candle_cache: dict[tuple[str, int | None], tuple[float, pd.DataFrame]] = {}
        self._tick_cache: dict[str, float] = {}

        self._client = Upbit(
            access_key=settings.access_key or "dry-run",
            secret_key=settings.secret_key or "dry-run",
            environment=settings.environment,
            max_retries=0,  # 재시도는 아래 _call() 이 직접 관리한다
        )

        self.paper: PaperBroker | None = None
        if self.dry_run:
            self.paper = PaperBroker(
                path=settings.state_file.parent / "paper_account.json",
                seed_krw=settings.dry_run_seed_krw,
                fee_rate=settings.fee_rate,
                slippage=settings.slippage_pct,
            )
            log.info("DRY_RUN 모드 - 모의계좌로 동작합니다 (원화 잔고 %s원)", f"{self.krw_balance():,.0f}")

    # ------------------------------------------------------------------ #
    # 저수준 호출 (레이트리밋 + 재시도)
    # ------------------------------------------------------------------ #
    def _call(self, group: str, fn, *args, retries: int = 4, **kwargs):
        bucket = self._quotation if group == "quotation" else self._exchange
        delay = 0.5
        last: Exception | None = None
        for attempt in range(retries + 1):
            bucket.acquire()
            try:
                return fn(*args, **kwargs)
            except RateLimitError as exc:
                last = exc
                wait = delay * (2**attempt)
                log.warning("레이트리밋(429) - %.1fs 후 재시도 (%d/%d)", wait, attempt + 1, retries)
                time.sleep(wait)
            except APIConnectionError as exc:
                last = exc
                wait = delay * (2**attempt)
                log.warning("네트워크 오류(%s) - %.1fs 후 재시도 (%d/%d)", exc, wait, attempt + 1, retries)
                time.sleep(wait)
            except APIStatusError as exc:
                status = getattr(exc, "status_code", 0)
                if 500 <= status < 600:
                    last = exc
                    wait = delay * (2**attempt)
                    log.warning("서버 오류(%s) - %.1fs 후 재시도 (%d/%d)", status, wait, attempt + 1, retries)
                    time.sleep(wait)
                    continue
                raise  # 4xx 는 재시도해도 동일하므로 즉시 전파
        raise last if last else RuntimeError("API 호출 실패")

    # ------------------------------------------------------------------ #
    # 시세 조회
    # ------------------------------------------------------------------ #
    def get_candles(self, market: str, unit: int | None, count: int = 200, use_cache: bool = True) -> pd.DataFrame:
        """
        unit 이 정수면 분봉(1/3/5/10/15/30/60/240), None 이면 일봉.
        오름차순(과거 -> 최신) DataFrame 을 반환한다.
        """
        key = (market, unit)
        ttl = self._cache_ttl(unit)
        if use_cache and key in self._candle_cache:
            cached_at, df = self._candle_cache[key]
            if time.time() - cached_at < ttl and len(df) >= count:
                return df.tail(count).copy()

        rows: list[Any] = []
        remaining = count
        to: str | None = None
        while remaining > 0:
            chunk = min(200, remaining)
            kwargs: dict[str, Any] = {"market": market, "count": chunk}
            if to:
                kwargs["to"] = to
            if unit is None:
                data = self._call("quotation", self._client.candles.list_days, **kwargs)
            else:
                data = self._call("quotation", self._client.candles.list_minutes, unit, **kwargs)
            if not data:
                break
            rows.extend(data)
            remaining -= len(data)
            if len(data) < chunk:
                break
            to = data[-1].candle_date_time_utc  # 다음 페이지는 가장 오래된 봉 이전부터
        df = candles_to_df(rows)
        self._candle_cache[key] = (time.time(), df)
        return df.tail(count).copy()

    @staticmethod
    def _cache_ttl(unit: int | None) -> float:
        if unit is None:
            return 600.0
        return max(15.0, unit * 60 / 4)

    def get_tickers(self, markets: Iterable[str]) -> dict[str, dict[str, float]]:
        markets = list(markets)
        if not markets:
            return {}
        out: dict[str, dict[str, float]] = {}
        # 한 번에 너무 많은 마켓을 요청하면 URL 길이 문제가 생기므로 분할한다
        for i in range(0, len(markets), 50):
            batch = markets[i : i + 50]
            data = self._call(
                "quotation", self._client.tickers.list_by_trading_pairs, markets=",".join(batch)
            )
            for t in data:
                out[t.market] = {
                    "price": float(t.trade_price),
                    "change_rate": float(t.signed_change_rate or 0.0),
                    "acc_trade_price_24h": float(t.acc_trade_price_24h or 0.0),
                    "high_52w": float(t.highest_52_week_price or 0.0),
                }
        return out

    def get_krw_tickers(self) -> dict[str, dict[str, float]]:
        """KRW 마켓 전 종목 현재가 (스크리닝용, 1회 호출)."""
        data = self._call("quotation", self._client.tickers.list_by_quote_currencies, quote_currencies="KRW")
        return {
            t.market: {
                "price": float(t.trade_price),
                "change_rate": float(t.signed_change_rate or 0.0),
                "acc_trade_price_24h": float(t.acc_trade_price_24h or 0.0),
            }
            for t in data
        }

    def get_orderbooks(self, markets: Iterable[str]) -> dict[str, dict[str, Any]]:
        markets = list(markets)
        if not markets:
            return {}
        data = self._call("quotation", self._client.orderbooks.list, markets=",".join(markets))
        out: dict[str, dict[str, Any]] = {}
        for ob in data:
            units = ob.orderbook_units or []
            if not units:
                continue
            best_ask = float(units[0].ask_price)
            best_bid = float(units[0].bid_price)
            mid = (best_ask + best_bid) / 2 or 1.0
            out[ob.market] = {
                "best_ask": best_ask,
                "best_bid": best_bid,
                "spread_pct": (best_ask - best_bid) / mid,
                "ask_prices": [float(u.ask_price) for u in units],
                "bid_prices": [float(u.bid_price) for u in units],
            }
            self._learn_tick(ob.market, out[ob.market])
        return out

    def _learn_tick(self, market: str, ob: dict[str, Any]) -> None:
        """실제 호가창의 최소 간격에서 호가 단위를 역산해 캐시한다."""
        diffs = []
        for prices in (ob["ask_prices"], ob["bid_prices"]):
            for a, b in zip(prices, prices[1:]):
                d = round(abs(a - b), 10)
                if d > 0:
                    diffs.append(d)
        if diffs:
            self._tick_cache[market] = min(diffs)

    def tick_size(self, market: str, price: float) -> float:
        return self._tick_cache.get(market) or krw_tick_size(price)

    def get_trading_pairs(self) -> dict[str, dict[str, Any]]:
        """마켓 목록 + 유의/주의 종목 플래그."""
        data = self._call("quotation", self._client.trading_pairs.list, is_details=True)
        out: dict[str, dict[str, Any]] = {}
        for p in data:
            event = getattr(p, "market_event", None)
            caution = getattr(event, "caution", None) if event else None
            caution_flag = False
            if caution is not None:
                try:
                    caution_flag = any(bool(v) for v in caution.model_dump().values())
                except Exception:
                    caution_flag = bool(caution)
            out[p.market] = {
                "korean_name": p.korean_name,
                "warning": bool(getattr(event, "warning", False)) or (p.market_warning == "CAUTION"),
                "caution": caution_flag,
            }
        return out

    # ------------------------------------------------------------------ #
    # 계좌
    # ------------------------------------------------------------------ #
    def get_balances(self) -> dict[str, Balance]:
        if self.paper is not None:
            return {c: Balance(**vars(b)) for c, b in self.paper.balances.items()}
        data = self._call("exchange", self._client.accounts.list)
        out: dict[str, Balance] = {}
        for a in data:
            out[a.currency] = Balance(
                currency=a.currency,
                balance=float(a.balance or 0.0),
                locked=float(a.locked or 0.0),
                avg_buy_price=float(a.avg_buy_price or 0.0),
            )
        return out

    def krw_balance(self) -> float:
        return self.get_balances().get("KRW", Balance("KRW")).balance

    def equity(self, price_map: dict[str, float]) -> float:
        """원화 + 보유코인 평가액 합계."""
        balances = self.get_balances()
        total = balances.get("KRW", Balance("KRW")).total
        for cur, bal in balances.items():
            if cur == "KRW" or bal.total <= 0:
                continue
            px = price_map.get(f"KRW-{cur}")
            if px:
                total += bal.total * px
        return total

    def list_krw_cash_flows(self, seen_uuids: set[str]) -> list[tuple[str, float, str]]:
        """
        아직 반영하지 않은 KRW 입출금 내역을 (uuid, 순증감액(+입금/-출금), 완료시각) 로 반환한다.

        입출금은 매매 손익이 아니므로 자본 기준선(initial_equity/equity_hwm/day_start_equity)에
        그대로 반영해 두지 않으면, 입금 직후 수익률이 왜곡되고 출금 직후에는 MDD 서킷브레이커가
        오발동한다. 페이퍼 모드는 입출금이 없으므로 빈 리스트를 반환한다.
        """
        if self.paper is not None:
            return []
        flows: list[tuple[str, float, str]] = []
        try:
            deposits = self._call(
                "exchange", self._client.deposits.list, currency="KRW", state="ACCEPTED", limit=100
            )
            for d in deposits:
                if d.uuid in seen_uuids:
                    continue
                flows.append((d.uuid, float(d.amount), d.done_at or d.created_at))
        except Exception as exc:
            log.warning("입금 내역 조회 실패: %s", exc)
        try:
            withdraws = self._call(
                "exchange", self._client.withdraws.list, currency="KRW", state="DONE", limit=100
            )
            for w in withdraws:
                if w.uuid in seen_uuids:
                    continue
                flows.append((w.uuid, -(float(w.amount) + float(w.fee or 0.0)), w.done_at or w.created_at))
        except Exception as exc:
            log.warning("출금 내역 조회 실패: %s", exc)
        return flows

    # ------------------------------------------------------------------ #
    # 주문
    # ------------------------------------------------------------------ #
    def buy_market(self, market: str, krw: float, ref_price: float) -> OrderResult:
        krw = math.floor(krw)
        if krw < self.s.min_order_krw:
            raise ValueError(f"주문 금액 {krw}원이 최소 주문금액({self.s.min_order_krw:.0f}원) 미만입니다")
        if self.paper is not None:
            return self.paper.fill_market(market, "bid", ref_price, krw=krw)
        order = self._call(
            "exchange", self._client.orders.create,
            market=market, side="bid", ord_type="price", price=str(krw),
            identifier=self._identifier(),
        )
        return self._wait_fill(order, fallback_price=ref_price)

    def sell_market(self, market: str, volume: float, ref_price: float) -> OrderResult:
        if volume <= 0:
            raise ValueError("매도 수량이 0 이하입니다")
        if self.paper is not None:
            return self.paper.fill_market(market, "ask", ref_price, volume=volume)
        order = self._call(
            "exchange", self._client.orders.create,
            market=market, side="ask", ord_type="market", volume=fmt_volume(volume),
            identifier=self._identifier(),
        )
        return self._wait_fill(order, fallback_price=ref_price)

    def buy_limit(self, market: str, price: float, volume: float) -> OrderResult:
        price = round_to_tick(price, self.tick_size(market, price), "floor")
        if self.paper is not None:
            return self.paper.create(market, "bid", "limit", price, volume)
        order = self._call(
            "exchange", self._client.orders.create,
            market=market, side="bid", ord_type="limit",
            price=fmt_price(price), volume=fmt_volume(volume),
            identifier=self._identifier(),
        )
        return _to_result(order)

    def sell_limit(self, market: str, price: float, volume: float) -> OrderResult:
        price = round_to_tick(price, self.tick_size(market, price), "ceil")
        if self.paper is not None:
            return self.paper.create(market, "ask", "limit", price, volume)
        order = self._call(
            "exchange", self._client.orders.create,
            market=market, side="ask", ord_type="limit",
            price=fmt_price(price), volume=fmt_volume(volume),
            identifier=self._identifier(),
        )
        return _to_result(order)

    def cancel_order(self, order_uuid: str) -> bool:
        if self.paper is not None:
            return self.paper.cancel(order_uuid)
        try:
            self._call("exchange", self._client.orders.cancel, uuid=order_uuid)
            return True
        except APIStatusError as exc:
            log.warning("주문 취소 실패 %s: %s", order_uuid, exc)
            return False

    def get_order(self, order_uuid: str) -> OrderResult | None:
        if self.paper is not None:
            o = self.paper.open_orders.get(order_uuid)
            if o:
                return OrderResult(order_uuid, o["market"], o["side"], o["ord_type"], "wait",
                                   price=float(o["price"]), volume=float(o["volume"]))
            c = self.paper.closed_orders.get(order_uuid)
            if c:
                return OrderResult(order_uuid, c["market"], c["side"], "limit", "done",
                                   price=float(c["avg_price"]), volume=float(c["volume"]),
                                   executed_volume=float(c["volume"]), avg_price=float(c["avg_price"]))
            return None
        try:
            return _to_result(self._call("exchange", self._client.orders.retrieve, uuid=order_uuid))
        except APIStatusError:
            return None

    def list_open_orders(self, market: str | None = None) -> list[OrderResult]:
        if self.paper is not None:
            return [
                OrderResult(oid, o["market"], o["side"], o["ord_type"], "wait",
                            price=float(o["price"]), volume=float(o["volume"]))
                for oid, o in self.paper.open_orders.items()
                if market is None or o["market"] == market
            ]
        kwargs: dict[str, Any] = {"limit": 100}
        if market:
            kwargs["market"] = market
        page = self._call("exchange", self._client.orders.list_open, **kwargs)
        return [_to_result(o) for o in page]

    def cancel_all(self, market: str, side: str | None = None) -> int:
        count = 0
        for o in self.list_open_orders(market):
            if side and o.side != side:
                continue
            if self.cancel_order(o.uuid):
                count += 1
        return count

    def poll_paper_fills(self, price_map: dict[str, float]) -> list[OrderResult]:
        """DRY_RUN 에서만 의미가 있는 지정가 체결 시뮬레이션."""
        if self.paper is None:
            return []
        return self.paper.poll_limit_fills(price_map)

    # ------------------------------------------------------------------ #
    # 내부 유틸
    # ------------------------------------------------------------------ #
    @staticmethod
    def _identifier() -> str:
        # 업비트 identifier 는 계정 내 고유해야 하며 중복 주문 방지용으로 쓰인다
        return f"bot-{uuidlib.uuid4().hex[:24]}"

    def _wait_fill(self, order, fallback_price: float, timeout: float = 5.0) -> OrderResult:
        """시장가 주문 직후 체결 내역이 채워질 때까지 짧게 폴링한다."""
        res = _to_result(order)
        deadline = time.time() + timeout
        while res.executed_volume <= 0 and time.time() < deadline:
            time.sleep(0.4)
            try:
                res = _to_result(self._call("exchange", self._client.orders.retrieve, uuid=res.uuid))
            except APIStatusError:
                break
        if res.executed_volume > 0 and res.avg_price <= 0:
            res.avg_price = fallback_price
        elif res.executed_volume <= 0:
            log.warning("주문 %s 체결 확인 실패 - 다음 루프에서 잔고로 동기화합니다", res.uuid)
            res.avg_price = fallback_price
        return res


# --------------------------------------------------------------------------- #
# 변환 헬퍼
# --------------------------------------------------------------------------- #
def _to_result(order) -> OrderResult:
    executed = float(order.executed_volume or 0.0)
    funds = 0.0
    trades = getattr(order, "trades", None) or []
    for t in trades:
        funds += float(getattr(t, "funds", 0.0) or 0.0)
    avg = funds / executed if executed > 0 and funds > 0 else 0.0
    return OrderResult(
        uuid=order.uuid,
        market=order.market,
        side=order.side,
        ord_type=order.ord_type,
        state=order.state,
        price=float(order.price or 0.0),
        volume=float(order.volume or 0.0),
        executed_volume=executed,
        avg_price=avg,
        paid_fee=float(order.paid_fee or 0.0),
        funds=funds,
        created_at=str(order.created_at or ""),
    )


def candles_to_df(rows: list[Any]) -> pd.DataFrame:
    """업비트 캔들 응답(최신순)을 오름차순 OHLCV DataFrame 으로 변환."""
    if not rows:
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume", "value"])
    records = []
    for c in rows:
        records.append(
            {
                "datetime": c.candle_date_time_kst,
                "open": float(c.opening_price),
                "high": float(c.high_price),
                "low": float(c.low_price),
                "close": float(c.trade_price),
                "volume": float(c.candle_acc_trade_volume),
                "value": float(c.candle_acc_trade_price),
            }
        )
    df = pd.DataFrame(records)
    df["datetime"] = pd.to_datetime(df["datetime"])
    df = df.drop_duplicates(subset="datetime").set_index("datetime").sort_index()
    return df
