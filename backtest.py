"""
전향적 롤링 검증(Walk-Forward Validation) 백테스터.

실거래와 동일한 국면 분류기 · 전략 · 포지션 사이징 코드를 그대로 돌린다.
차이는 주문 체결을 과거 캔들로 시뮬레이션한다는 점뿐이다.

    python backtest.py --markets KRW-BTC,KRW-ETH --days 365
    python backtest.py --markets KRW-BTC --days 730 --refit-hours 168
    python backtest.py --markets KRW-SOL --days 180 --no-hmm --seed 300000

전략서 6장: 실자본 투입 전 최소 1~2년치 데이터로 워크포워드 검증을 거치고,
이후 2~4주 이상 DRY_RUN 실시간 검증을 병행할 것.
"""
from __future__ import annotations

import argparse
import bisect
import copy
import time
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from config import BASE_DIR, settings
from core.indicators import build_features
from core.logger import force_utf8, get_logger, setup_logging
from core.regime import RegimeClassifier
from core.screener import STABLECOINS
from core.sizing import PositionSizer
from core.state import BotState, Position, Trade
from core.upbit_client import UpbitClient
from strategies.base import (
    BUY_LIMIT,
    BUY_MARKET,
    CANCEL,
    SELL_LIMIT,
    SELL_MARKET,
    SET_STOP,
    Context,
    MarketView,
)
from strategies.dca import SmartDcaStrategy
from strategies.grid import AtrGridStrategy
from strategies.trend import TrendBreakoutStrategy

force_utf8()
log = get_logger("backtest")

CACHE_DIR = BASE_DIR / "data" / "candles"


# --------------------------------------------------------------------------- #
# 데이터
# --------------------------------------------------------------------------- #
def load_candles(client: UpbitClient, market: str, unit: int | None, count: int, refresh: bool) -> pd.DataFrame:
    tag = f"{unit}m" if unit else "1d"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"{market}_{tag}.csv"
    if path.exists() and not refresh:
        df = pd.read_csv(path, parse_dates=["datetime"]).set_index("datetime").sort_index()
        if len(df) >= count:
            return df.tail(count)
    log.info("%s %s %d개 수집 중 ... (업비트 API)", market, tag, count)
    df = client.get_candles(market, unit, count, use_cache=False)
    if not df.empty:
        df.reset_index().to_csv(path, index=False)
    return df


# --------------------------------------------------------------------------- #
# 동적 유니버스 (실거래 봇의 자동선정을 백테스트에서 재현)
# --------------------------------------------------------------------------- #
def build_universe_schedule(
    client: UpbitClient, cfg, start_ts: pd.Timestamp, end_ts: pd.Timestamp, refresh: bool
) -> tuple[dict[pd.Timestamp, list[str]], list[str]]:
    """
    실거래 스크리너(core/screener.py)와 같은 필터(유의/주의/스테이블코인 제외,
    거래대금 하한, 최소 상장 경과일)를 과거 시점에 재현해 6시간마다 유니버스를
    갱신하는 스케줄을 만든다.

    호가 스프레드 필터는 과거 호가창 이력이 없어 재현할 수 없으므로 생략한다.
    또한 업비트 API 는 유의/주의 종목의 과거 이력을 제공하지 않으므로, 현재
    시점의 플래그를 전체 백테스트 기간에 일괄 적용하는 근사치를 쓴다 - 즉 지금
    유의종목인 코인은 과거에도 항상 제외되고, 그 반대는 재현하지 못한다.

    반환값: (refresh_ts -> 선정 종목 리스트) 스케줄, 그리고 기간 내 한 번이라도
    선정된 종목의 합집합(실행 캔들을 받아야 할 대상).
    """
    pairs = client.get_trading_pairs()
    tickers = client.get_krw_tickers()
    candidates = []
    for market in tickers:
        if not market.startswith("KRW-"):
            continue
        if market.split("-")[1] in STABLECOINS:
            continue
        info = pairs.get(market)
        if info is None:
            continue
        if info["warning"] or (cfg.universe_exclude_caution and info["caution"]):
            continue
        candidates.append(market)

    log.info(
        "유니버스 후보 %d개 (KRW %d종목 중 유의/주의/스테이블코인 제외, 현재 시점 플래그 기준)",
        len(candidates), sum(1 for m in tickers if m.startswith("KRW-")),
    )

    now = pd.Timestamp.now().normalize()
    span_days = int((end_ts - start_ts).days) + max(0, (now - end_ts).days) + 10
    daily: dict[str, pd.Series] = {}
    for i, market in enumerate(candidates, 1):
        df = load_candles(client, market, None, span_days, refresh)
        if df.empty:
            continue
        daily[market] = df["value"]
        if i % 50 == 0:
            log.info("일봉 수집 진행 %d/%d", i, len(candidates))

    if not daily:
        raise SystemExit("유니버스 후보의 일봉 데이터를 하나도 받지 못했습니다.")

    # 상장일 판정은 클리핑 전 원본 이력에서 첫 유효 봉 시점으로 잡는다. 클리핑된
    # wide 로만 계산하면 실제로는 오래된 코인도 백테스트 시작 시점 근처에서는
    # "방금 상장한 것"처럼 보여 초반 구간을 잘못 걸러내게 된다.
    first_date = {m: s.dropna().index.min() for m, s in daily.items() if s.dropna().size > 0}

    wide = pd.DataFrame(daily).sort_index()
    wide = wide.loc[(wide.index >= start_ts - pd.Timedelta(days=3)) & (wide.index <= end_ts)]

    refresh_points = pd.date_range(start_ts, end_ts, freq=f"{int(cfg.universe_refresh_hours)}h")
    schedule: dict[pd.Timestamp, list[str]] = {}
    skipped_new_total = 0
    for ts in refresh_points:
        # 룩어헤드 방지: ts 이전에 이미 마감된 일봉까지만 사용한다
        past = wide.loc[wide.index < ts.normalize()]
        if past.empty:
            schedule[ts] = []
            continue
        latest = past.iloc[-1].dropna()
        latest = latest[latest >= cfg.universe_min_trade_price_24h]
        if cfg.universe_min_listing_days > 0:
            seasoned = [
                m for m in latest.index
                if m in first_date and (ts.normalize() - first_date[m]).days >= cfg.universe_min_listing_days
            ]
            skipped_new_total += len(latest) - len(seasoned)
            latest = latest[seasoned]
        top = latest.sort_values(ascending=False).head(cfg.universe_size)
        schedule[ts] = list(top.index)

    if cfg.universe_min_listing_days > 0:
        log.info("신규상장(경과 %d일 미만) 제외로 후보에서 걸러진 연인원: %d회",
                 cfg.universe_min_listing_days, skipped_new_total)

    union = sorted({m for markets in schedule.values() for m in markets})
    log.info(
        "유니버스 스케줄 생성 완료 | 갱신주기 %.0f시간 | 갱신횟수 %d회 | 실행 기간 중 선정된 종목 %d개: %s",
        cfg.universe_refresh_hours, len(schedule), len(union), ", ".join(union),
    )
    return schedule, union


class UniverseSchedule:
    """시각을 넣으면 그 시점에 적용 중인 유니버스(신규 진입 허용 종목)를 돌려준다."""

    def __init__(self, schedule: dict[pd.Timestamp, list[str]]) -> None:
        self._keys = sorted(schedule)
        self._values = {k: set(v) for k, v in schedule.items()}

    def at(self, ts: pd.Timestamp) -> set[str]:
        if not self._keys:
            return set()
        idx = bisect.bisect_right(self._keys, ts) - 1
        if idx < 0:
            idx = 0
        return self._values[self._keys[idx]]


# --------------------------------------------------------------------------- #
# 모의 브로커
# --------------------------------------------------------------------------- #
@dataclass
class _LimitOrder:
    uuid: str
    market: str
    side: str
    price: float
    volume: float
    placed_bar: int


@dataclass
class BacktestBroker:
    fee: float
    slippage: float
    cash: float
    holdings: dict[str, float] = field(default_factory=dict)
    # 미체결 지정가에 묶인 자산. 평가자산에서 빠지면 가짜 낙폭이 잡히므로 별도 추적한다
    locked_krw: float = 0.0
    locked_coin: dict[str, float] = field(default_factory=dict)
    open_orders: dict[str, _LimitOrder] = field(default_factory=dict)
    _seq: int = 0

    def next_uuid(self) -> str:
        self._seq += 1
        return f"bt-{self._seq:07d}"

    def equity(self, price_map: dict[str, float]) -> float:
        coins = sum(
            (v + self.locked_coin.get(m, 0.0)) * price_map.get(m, 0.0)
            for m, v in self.holdings.items()
        )
        return self.cash + self.locked_krw + coins

    # ---- 시장가 ----
    def buy_market(self, market: str, krw: float, price: float) -> tuple[float, float]:
        fill = price * (1 + self.slippage)
        krw = min(krw, self.cash)
        if krw <= 0:
            return 0.0, 0.0
        fee = krw * self.fee
        qty = (krw - fee) / fill
        self.cash -= krw
        self.holdings[market] = self.holdings.get(market, 0.0) + qty
        return fill, qty

    def sell_market(self, market: str, volume: float, price: float) -> tuple[float, float, float]:
        volume = min(volume, self.holdings.get(market, 0.0))
        if volume <= 0:
            return 0.0, 0.0, 0.0
        fill = price * (1 - self.slippage)
        gross = fill * volume
        fee = gross * self.fee
        self.cash += gross - fee
        self.holdings[market] -= volume
        return fill, volume, fee

    # ---- 지정가 ----
    def place_limit(self, market: str, side: str, price: float, volume: float, bar: int) -> str:
        uid = self.next_uuid()
        if side == "bid":
            need = price * volume
            if need > self.cash:
                return ""
            self.cash -= need
            self.locked_krw += need
        else:
            if volume > self.holdings.get(market, 0.0) + 1e-12:
                return ""
            self.holdings[market] = self.holdings.get(market, 0.0) - volume
            self.locked_coin[market] = self.locked_coin.get(market, 0.0) + volume
        self.open_orders[uid] = _LimitOrder(uid, market, side, price, volume, bar)
        return uid

    def cancel(self, uid: str) -> bool:
        o = self.open_orders.pop(uid, None)
        if not o:
            return False
        if o.side == "bid":
            self.cash += o.price * o.volume
            self.locked_krw -= o.price * o.volume
        else:
            self.holdings[o.market] = self.holdings.get(o.market, 0.0) + o.volume
            self.locked_coin[o.market] = self.locked_coin.get(o.market, 0.0) - o.volume
        return True

    def match(self, market: str, bar_low: float, bar_high: float) -> list[tuple[_LimitOrder, float]]:
        """해당 봉의 고저가를 지나간 지정가 주문을 체결 처리한다."""
        filled = []
        for uid, o in list(self.open_orders.items()):
            if o.market != market:
                continue
            if o.side == "bid" and bar_low <= o.price:
                gross = o.price * o.volume
                fee = gross * self.fee
                self.locked_krw -= gross
                self.holdings[market] = self.holdings.get(market, 0.0) + o.volume - fee / o.price
                filled.append((o, o.volume - fee / o.price))
                del self.open_orders[uid]
            elif o.side == "ask" and bar_high >= o.price:
                gross = o.price * o.volume
                self.locked_coin[market] = self.locked_coin.get(market, 0.0) - o.volume
                self.cash += gross - gross * self.fee
                filled.append((o, o.volume))
                del self.open_orders[uid]
        return filled


# --------------------------------------------------------------------------- #
# 백테스트 엔진
# --------------------------------------------------------------------------- #
class Backtester:
    def __init__(
        self, cfg, markets: list[str], seed: float, refit_hours: float,
        universe_schedule: "UniverseSchedule | None" = None,
    ) -> None:
        self.s = cfg
        self.markets = markets
        self.refit_hours = refit_hours
        # 동적 유니버스 모드에서만 채워진다. None 이면 --markets 로 받은 종목 전체가
        # 항상 신규 진입 가능하다 (기존 고정 유니버스 동작과 동일).
        self.universe_schedule = universe_schedule
        self.broker = BacktestBroker(fee=cfg.fee_rate, slippage=cfg.slippage_pct, cash=seed)
        self.state = BotState()
        self.state.initial_equity = seed
        self.state.equity_hwm = seed
        self.sizer = PositionSizer(cfg)
        self.clf = RegimeClassifier(cfg)
        self.strategies = {
            s.name: s
            for s in (TrendBreakoutStrategy(cfg), AtrGridStrategy(cfg), SmartDcaStrategy(cfg))
        }
        self.equity_curve: list[tuple[pd.Timestamp, float]] = []
        self.regime_bars: dict[str, int] = {}
        self._last_refit_bar: dict[str, int] = {}
        # 상위 타임프레임 봉이 그대로면 국면도 그대로다. 15분봉마다 HMM 추론을
        # 다시 돌리는 것은 순수한 낭비이므로 (마켓, 상위봉 인덱스) 로 캐시한다.
        self._regime_cache: dict[str, tuple[int, object]] = {}

    # ------------------------------------------------------------------ #
    def run(self, data: dict[str, tuple[pd.DataFrame, pd.DataFrame]]) -> dict:
        # 모든 종목의 15분봉 인덱스를 합쳐 공통 타임라인을 만든다
        timeline = sorted(set().union(*[set(sig.index) for _, sig in data.values()]))
        warmup = 220  # 지표 워밍업 구간
        if len(timeline) <= warmup:
            raise SystemExit("백테스트에 필요한 캔들이 부족합니다. --days 를 늘리세요.")

        last_price: dict[str, float] = {}
        refit_bars = max(1, int(self.refit_hours * 60 / self.s.signal_timeframe))

        for i, ts in enumerate(timeline):
            if i < warmup:
                continue

            price_map: dict[str, float] = {}
            for market in self.markets:
                macro, sig = data[market]
                if ts not in sig.index:
                    continue
                j = sig.index.get_loc(ts)
                bar = sig.iloc[j]
                price_map[market] = last_price[market] = float(bar["close"])

                # 1) 지정가 체결 매칭 (이번 봉의 고저 범위)
                for order, qty in self.broker.match(market, float(bar["low"]), float(bar["high"])):
                    self._on_limit_fill(market, order, qty, ts)

                # 2) 이 시점까지 완성된 상위 타임프레임 봉만 노출 (룩어헤드 차단)
                k = macro.index.searchsorted(ts, side="right") - 1
                if k < 60:
                    continue
                macro_slice = macro.iloc[: k + 1]
                sig_slice = sig.iloc[: j + 1]

                # 3) 워크포워드 재적합
                if i - self._last_refit_bar.get(market, -10**9) >= refit_bars:
                    self.clf._models.pop(market, None)
                    self._regime_cache.pop(market, None)
                    self._last_refit_bar[market] = i

                cached = self._regime_cache.get(market)
                if cached is not None and cached[0] == k:
                    regime = cached[1]
                else:
                    regime = self.clf.classify(market, macro_slice)
                    self._regime_cache[market] = (k, regime)
                self.regime_bars[regime.regime] = self.regime_bars.get(regime.regime, 0) + 1

                view = MarketView(
                    market=market,
                    price=price_map[market],
                    regime=regime,
                    macro=macro_slice,
                    signal=sig_slice,
                    ts=ts.timestamp(),
                )
                self._step(view, ts, i, last_price)

            equity = self.broker.equity(last_price)
            self.state.equity_hwm = max(self.state.equity_hwm, equity)
            self.equity_curve.append((ts, equity))

        # 종료 시 잔여 포지션 청산
        last_prices = {m: float(data[m][1]["close"].iloc[-1]) for m in self.markets}
        for market, pos in list(self.state.positions.items()):
            self._cancel_all_for(market)
            if pos.volume > 0:
                fill, vol, fee = self.broker.sell_market(market, pos.volume, last_prices[market])
                if vol > 0:
                    self._book_sell(pos, fill, vol, fee, "백테스트 종료 청산",
                                    pd.Timestamp(self.equity_curve[-1][0]))
        if self.equity_curve:
            # 곡선의 마지막 점은 청산 전 평가액이라 청산 수수료/슬리피지가 빠져 있다.
            # 그대로 두면 "종료자본"(곡선 끝값)과 "실현손익 합계"(거래이력 합)가
            # 강제청산 비용만큼 어긋나 장부 오차로 잡힌다. 청산 후 값으로 갱신한다.
            final_ts = self.equity_curve[-1][0]
            self.equity_curve[-1] = (final_ts, self.broker.equity(last_prices))
        if self.equity_curve:
            self._benchmark = self.buy_and_hold(data, self.equity_curve[0][0], self.equity_curve[-1][0])
        return self.report(last_prices)

    # ------------------------------------------------------------------ #
    def _step(self, view: MarketView, ts, bar_idx: int, price_map: dict[str, float]) -> None:
        market = view.market
        pos = self.state.positions.get(market)
        strategy = self._strategy_for(view, pos)
        if strategy is None:
            return

        equity = self.broker.equity(price_map)
        active = {m for m, p in self.state.positions.items() if p.volume > 0 or p.grid.get("buys")}

        ctx = Context(
            settings=self.s,
            client=None,
            sizer=self.sizer,
            state=self.state,
            equity=equity,
            cash=self.broker.cash,
            regime_weight=self.clf.alloc_weight(view.regime.regime),
            n_slots=max(1, self.s.max_concurrent_positions),
        )

        try:
            actions = strategy.plan(view, pos, ctx)
        except Exception as exc:
            log.debug("%s 전략 예외: %s", market, exc)
            return

        allowed = self.universe_schedule.at(ts) if self.universe_schedule is not None else None

        for action in actions:
            if action.kind in (BUY_MARKET, BUY_LIMIT):
                if market not in active and len(active) >= self.s.max_concurrent_positions:
                    continue
                # 실거래 스크리너처럼, 보유 중인 종목은 유니버스에서 빠져도 계속 관리하되
                # 유니버스 밖 종목에 새 포지션을 여는 것만 막는다.
                if allowed is not None and market not in active and market not in allowed:
                    continue
            self._apply(action, view, strategy, ts, bar_idx)
            active = {m for m, p in self.state.positions.items() if p.volume > 0 or p.grid.get("buys")}

    def _apply(self, action, view: MarketView, strategy, ts, bar_idx: int) -> None:
        market = action.market
        pos = self.state.positions.get(market)

        if action.kind == SET_STOP:
            if pos:
                pos.stop_price = action.price
                if pos.init_stop <= 0:
                    pos.init_stop = action.price
            return

        if action.kind == CANCEL:
            if self.broker.cancel(action.uuid) and pos and hasattr(strategy, "on_order_cancelled"):
                strategy.on_order_cancelled(pos, action.uuid)
            return

        if action.kind == BUY_MARKET:
            krw = float(int(action.krw))
            if krw < self.s.min_order_krw or krw > self.broker.cash:
                return
            fill, qty = self.broker.buy_market(market, krw, view.price)
            if qty <= 0:
                return
            if pos is None:
                pos = Position(market=market, strategy=action.meta.get("strategy", strategy.name),
                               regime_at_entry=view.regime.regime)
                pos.opened_at = ts.timestamp()
                self.state.positions[market] = pos
            pos.add_fill(fill, qty, krw)
            pos.last_add_at = ts.timestamp()
            pos.meta.update({k: v for k, v in action.meta.items() if k != "strategy"})
            if action.price > 0:
                pos.stop_price = action.price if pos.steps <= 1 else max(pos.stop_price, action.price)
                if pos.init_stop <= 0:
                    pos.init_stop = action.price
            return

        if action.kind == SELL_MARKET:
            if pos is None or pos.volume <= 0:
                return
            self._cancel_open_asks(market, pos, strategy)
            volume = action.volume or pos.volume * (action.ratio or 1.0)
            volume = min(volume, pos.volume)
            remainder = (pos.volume - volume) * view.price
            if 0 < remainder < self.s.min_order_krw:
                volume = pos.volume
            if volume * view.price < self.s.min_order_krw:
                return
            fill, vol, fee = self.broker.sell_market(market, volume, view.price)
            if vol <= 0:
                return
            self._book_sell(pos, fill, vol, fee, action.reason, ts)
            if hasattr(strategy, "drop_lots_for_volume"):
                strategy.drop_lots_for_volume(pos, vol)
            if action.meta.get("grid_upper_take"):
                pos.grid["upper_taken"] = True
            if action.meta.get("partial"):
                pos.partial_taken = True
            return

        if action.kind in (BUY_LIMIT, SELL_LIMIT):
            if action.price * action.volume < self.s.min_order_krw:
                return
            side = "bid" if action.kind == BUY_LIMIT else "ask"
            uid = self.broker.place_limit(market, side, action.price, action.volume, bar_idx)
            if not uid:
                return
            if pos is None:
                pos = Position(market=market, strategy=action.meta.get("strategy", strategy.name),
                               regime_at_entry=view.regime.regime)
                pos.opened_at = ts.timestamp()
                self.state.positions[market] = pos
            if hasattr(strategy, "on_order_placed"):
                strategy.on_order_placed(pos, action, type("O", (), {"uuid": uid})(), ts=ts.timestamp())

    def _cancel_open_asks(self, market: str, pos: Position, strategy) -> None:
        """시장가 청산 전에 묶여 있는 지정가 매도를 전부 회수한다."""
        for uid, o in list(self.broker.open_orders.items()):
            if o.market != market or o.side != "ask":
                continue
            if self.broker.cancel(uid) and hasattr(strategy, "on_order_cancelled"):
                strategy.on_order_cancelled(pos, uid)

    def _cancel_all_for(self, market: str) -> None:
        for uid, o in list(self.broker.open_orders.items()):
            if o.market == market:
                self.broker.cancel(uid)

    def _on_limit_fill(self, market: str, order: _LimitOrder, qty: float, ts) -> None:
        pos = self.state.positions.get(market)
        if pos is None:
            return
        strategy = self.strategies.get(pos.strategy)
        if order.side == "bid":
            pos.add_fill(order.price, qty, order.price * order.volume)
            pos.last_add_at = ts.timestamp()
            if strategy and hasattr(strategy, "on_buy_filled"):
                strategy.on_buy_filled(pos, order.uuid, order.price, qty, ts=ts.timestamp())
        else:
            self._book_sell(pos, order.price, qty, order.price * qty * self.s.fee_rate,
                            "그리드 지정가 익절", ts)
            if strategy and hasattr(strategy, "on_sell_filled"):
                strategy.on_sell_filled(pos, order.uuid)

    def _book_sell(self, pos: Position, price: float, volume: float, fee: float, reason: str, ts) -> None:
        cost = pos.avg_price * volume
        pnl = price * volume - fee - cost
        pnl_pct = (price / pos.avg_price - 1.0) if pos.avg_price > 0 else 0.0
        pos.volume = max(0.0, pos.volume - volume)
        pos.realized_krw += pnl
        pos.invested_krw = max(0.0, pos.invested_krw - cost)
        self.state.record_trade(
            Trade(pos.market, pos.strategy, pos.regime_at_entry, pos.avg_price, price, volume,
                  pnl, pnl_pct, pos.opened_at, ts.timestamp(), reason)
        )
        if pos.volume <= 1e-12 and not pos.grid.get("buys"):
            self._cancel_all_for(pos.market)
            self.state.positions.pop(pos.market, None)

    def _strategy_for(self, view: MarketView, pos: Position | None):
        if pos is not None and (pos.volume > 0 or pos.grid.get("buys")):
            return self.strategies.get(pos.strategy)
        for strat in self.strategies.values():
            if strat.handles(view.regime.regime):
                return strat
        return None

    # ------------------------------------------------------------------ #
    def buy_and_hold(self, data: dict, start_ts, end_ts) -> dict[str, float]:
        """
        같은 기간 같은 자본을 종목에 균등 분산해 그냥 들고만 있었을 때의 성과.

        봇 수익률은 이 값과 비교해야 의미가 있다. 하락장에서 -5% 는 훌륭한 성과이고
        상승장에서 -5% 는 실패다. 절대 수익률만 보면 둘을 구분할 수 없다.
        """
        rets, mdds = [], []
        for _, sig in data.values():
            window = sig.loc[(sig.index >= start_ts) & (sig.index <= end_ts), "close"]
            if len(window) < 2:
                continue
            first, last = float(window.iloc[0]), float(window.iloc[-1])
            if first <= 0:
                continue
            # 매수 수수료와 청산 수수료를 동일하게 반영해 공정 비교
            rets.append((last / first) * (1 - self.s.fee_rate) ** 2 - 1.0)
            peak = window.cummax()
            mdds.append(float(((window - peak) / peak).min()))
        if not rets:
            return {"수익률": 0.0, "MDD": 0.0}
        return {"수익률": sum(rets) / len(rets), "MDD": min(mdds) if mdds else 0.0}

    def report(self, last_prices: dict[str, float]) -> dict:
        if not self.equity_curve:
            return {}
        curve = pd.Series(
            [e for _, e in self.equity_curve], index=pd.DatetimeIndex([t for t, _ in self.equity_curve])
        )
        start, end = float(curve.iloc[0]), float(curve.iloc[-1])
        days = max(1.0, (curve.index[-1] - curve.index[0]).total_seconds() / 86400)

        total_ret = end / start - 1.0
        cagr = (end / start) ** (365.0 / days) - 1.0 if start > 0 else 0.0
        running_max = curve.cummax()
        mdd = float(((curve - running_max) / running_max).min())

        rets = curve.pct_change().dropna()
        bars_per_year = 365 * 24 * 60 / self.s.signal_timeframe
        sharpe = float(rets.mean() / rets.std() * np.sqrt(bars_per_year)) if rets.std() > 0 else 0.0

        trades = self.state.trades
        wins = [t for t in trades if t.pnl_krw > 0]
        losses = [t for t in trades if t.pnl_krw <= 0]
        gross_win = sum(t.pnl_krw for t in wins)
        gross_loss = -sum(t.pnl_krw for t in losses)
        kelly = self.sizer.kelly(trades)

        by_strategy: dict[str, dict] = {}
        for t in trades:
            b = by_strategy.setdefault(t.strategy, {"n": 0, "pnl": 0.0, "win": 0})
            b["n"] += 1
            b["pnl"] += t.pnl_krw
            b["win"] += 1 if t.pnl_krw > 0 else 0

        # 장부 정합성: 거래 손익 합계와 실제 자산 변화가 크게 어긋나면 회계 누락이 있다
        realized = sum(t.pnl_krw for t in trades)
        leak = (end - start) - realized

        return {
            "장부오차": leak,
            "벤치마크": getattr(self, "_benchmark", {"수익률": 0.0, "MDD": 0.0}),
            "기간": f"{curve.index[0]:%Y-%m-%d} ~ {curve.index[-1]:%Y-%m-%d} ({days:.0f}일)",
            "시작자본": start,
            "종료자본": end,
            "총수익률": total_ret,
            "CAGR": cagr,
            "MDD": mdd,
            "샤프지수": sharpe,
            "거래수": len(trades),
            "승률": len(wins) / len(trades) if trades else 0.0,
            "손익비": (gross_win / len(wins)) / (gross_loss / len(losses)) if wins and losses else 0.0,
            "손익팩터": gross_win / gross_loss if gross_loss > 0 else float("inf"),
            "켈리f*": kelly.f_star,
            "전략별": by_strategy,
            "국면분포": self.regime_bars,
        }


# --------------------------------------------------------------------------- #
def print_report(rep: dict, markets: list[str]) -> None:
    if not rep:
        print("리포트를 생성할 수 없습니다.")
        return
    print("\n" + "=" * 72)
    print(f"  백테스트 결과  |  {', '.join(markets)}")
    print("=" * 72)
    print(f"  기간          {rep['기간']}")
    print(f"  자본          {rep['시작자본']:,.0f}원 -> {rep['종료자본']:,.0f}원")
    print(f"  총수익률      {rep['총수익률'] * 100:+.2f}%      연환산(CAGR) {rep['CAGR'] * 100:+.2f}%")
    print(f"  최대낙폭(MDD) {rep['MDD'] * 100:.2f}%       샤프지수 {rep['샤프지수']:.2f}")
    bh = rep.get("벤치마크", {})
    if bh:
        alpha = rep["총수익률"] - bh["수익률"]
        print(f"  단순보유 대비  단순보유 {bh['수익률'] * 100:+.2f}% (MDD {bh['MDD'] * 100:.2f}%)"
              f"  ->  초과성과 {alpha * 100:+.2f}%p")
    print(f"  거래수        {rep['거래수']}건        승률 {rep['승률'] * 100:.1f}%")
    print(f"  손익비(R)     {rep['손익비']:.2f}         손익팩터 {rep['손익팩터']:.2f}")
    print(f"  켈리 f*       {rep['켈리f*']:.4f}       -> 적용 베팅비율 "
          f"{max(0.0, rep['켈리f*']) * settings.kelly_fraction * 100:.2f}%")
    if rep["전략별"]:
        print("\n  전략별 성과")
        for name, b in rep["전략별"].items():
            wr = b["win"] / b["n"] * 100 if b["n"] else 0
            print(f"    {name:6s} {b['n']:4d}건  승률 {wr:5.1f}%  손익 {b['pnl']:>+12,.0f}원")
    if rep["국면분포"]:
        total = sum(rep["국면분포"].values())
        print("\n  국면 분포")
        for k, v in sorted(rep["국면분포"].items(), key=lambda x: -x[1]):
            print(f"    {k:20s} {v / total * 100:5.1f}%  ({v}봉)")
    print("=" * 72)
    mdd_limit = settings.max_drawdown_pct
    if abs(rep["MDD"]) > mdd_limit:
        print(f"  ⚠️ MDD {abs(rep['MDD']) * 100:.1f}% 가 설정 한도 {mdd_limit * 100:.0f}% 를 초과합니다.")
        print("     RISK_PER_TRADE_PCT / KELLY_FRACTION 을 낮추고 재검증하세요.")
    leak = rep.get("장부오차", 0.0)
    if abs(leak) > max(500.0, abs(rep["시작자본"]) * 0.002):
        print(f"  ⚠️ 장부 오차 {leak:+,.0f}원 - 거래 손익 합계와 자산 변화가 어긋납니다 (회계 누락 의심).")
    if rep["거래수"] < 30:
        print("  ⚠️ 거래 표본이 30건 미만입니다. 통계적 신뢰도가 낮으니 기간을 늘려 재검증하세요.")
    print()


def main() -> int:
    p = argparse.ArgumentParser(description="업비트 자동매매 봇 워크포워드 백테스터")
    p.add_argument("--markets", default="KRW-BTC",
                   help="쉼표 구분 마켓 코드 (기본 KRW-BTC). --dynamic-universe 와 함께 쓸 수 없음")
    p.add_argument("--dynamic-universe", action="store_true",
                   help="실거래 봇처럼 6시간마다 거래대금 상위 종목을 자동 선정해 검증 (--markets 무시)")
    p.add_argument("--days", type=int, default=365, help="검증 기간(일)")
    p.add_argument("--seed", type=float, default=None, help="시작 자본(원). 기본값은 DRY_RUN_SEED_KRW")
    p.add_argument("--refit-hours", type=float, default=168, help="HMM 롤링 재적합 주기(시간)")
    p.add_argument("--no-hmm", action="store_true", help="규칙 기반 국면 분류만 사용 (빠름)")
    p.add_argument("--refresh", action="store_true", help="캔들 캐시를 무시하고 새로 수집")
    p.add_argument("--end", default=None,
                   help="검증 종료일 (YYYY-MM-DD). 과거 특정 구간(예: 상승장)만 잘라서 검증할 때 사용")
    args = p.parse_args()

    setup_logging(settings.log_level, None)
    seed = args.seed if args.seed is not None else settings.dry_run_seed_krw

    cfg = copy.deepcopy(settings)
    if args.no_hmm:
        cfg.regime_use_hmm = False

    client = UpbitClient(settings)
    end_ts = pd.Timestamp(args.end) if args.end else pd.Timestamp.now()
    start_ts = end_ts - pd.Timedelta(days=args.days)

    raw_schedule = None
    if args.dynamic_universe:
        log.info("동적 유니버스 모드 - 거래대금 상위 %d종목을 %.0f시간마다 자동 선정합니다.",
                 cfg.universe_size, cfg.universe_refresh_hours)
        raw_schedule, markets = build_universe_schedule(client, cfg, start_ts, end_ts, args.refresh)
        if not markets:
            raise SystemExit("동적 유니버스 스케줄에서 선정된 종목이 없습니다. "
                             "UNIVERSE_MIN_TRADE_PRICE_24H 를 낮춰보세요.")
    else:
        markets = [m.strip().upper() for m in args.markets.split(",") if m.strip()]

    span_days = args.days + max(0, (pd.Timestamp.now().normalize() - end_ts).days)
    sig_count = int(span_days * 24 * 60 / cfg.signal_timeframe) + 250
    macro_count = int(span_days * 24 * 60 / cfg.regime_timeframe) + 250

    data: dict[str, tuple[pd.DataFrame, pd.DataFrame]] = {}
    for m in markets:
        macro_raw = load_candles(client, m, cfg.regime_timeframe, macro_count, args.refresh)
        sig_raw = load_candles(client, m, cfg.signal_timeframe, sig_count, args.refresh)
        if len(macro_raw) < 100 or len(sig_raw) < 300:
            log.error("%s 캔들 부족 (상위 %d / 실행 %d) - 제외", m, len(macro_raw), len(sig_raw))
            continue
        macro_f, sig_f = build_features(macro_raw), build_features(sig_raw)
        if args.end:
            # 지표 워밍업을 위해 시작 이전 구간도 일부 남겨둔 뒤 잘라낸다
            macro_f = macro_f.loc[macro_f.index <= end_ts]
            sig_f = sig_f.loc[(sig_f.index <= end_ts) & (sig_f.index >= start_ts)]
            if len(sig_f) < 400:
                log.error("%s 지정 구간(%s 이전 %d일) 캔들 부족 - 제외", m, args.end, args.days)
                continue
        data[m] = (macro_f, sig_f)
    if not data:
        raise SystemExit("사용 가능한 마켓 데이터가 없습니다.")

    universe_sched_obj = None
    if raw_schedule is not None:
        # 데이터 수집에 실패해 제외된 종목은 스케줄에서도 걸러낸다
        raw_schedule = {ts: [m for m in ms if m in data] for ts, ms in raw_schedule.items()}
        universe_sched_obj = UniverseSchedule(raw_schedule)

    started = time.time()
    bt = Backtester(cfg, list(data.keys()), seed, args.refit_hours, universe_sched_obj)
    report = bt.run(data)
    log.info("백테스트 소요 %.1f초", time.time() - started)
    label = f"동적 유니버스({len(data)}종목 순환)" if args.dynamic_universe else ", ".join(data.keys())
    print_report(report, [label])
    if args.dynamic_universe:
        print(f"동적 유니버스 대상 종목: {', '.join(sorted(data.keys()))}")
        print("주의: 유의/주의종목 필터는 현재 시점 플래그를 과거 전체 기간에 일괄 적용한 근사치이며,")
        print("      호가 스프레드 필터는 과거 이력이 없어 이 백테스트에서는 재현되지 않습니다.\n")

    out = BASE_DIR / "data" / "backtest_equity.csv"
    pd.DataFrame(bt.equity_curve, columns=["datetime", "equity"]).to_csv(out, index=False)
    print(f"자산 곡선 저장: {out}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
