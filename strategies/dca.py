"""
조정 국면 전략 - 한도 제어형 스마트 DCA (Bounded Smart DCA).

전략서 3.3절:
  * 4시간봉 상승 추세가 유지되는 상황에서만 진입 (구조적 약세장 물타기 금지)
  * 15분봉 RSI <= 30 과매도 또는 볼린저 밴드 하단 하회 시에만 단계적 분할 매수
  * 최대 진입 횟수 3~5회로 엄격 제한, 단일 자산 노출 50% 상한
  * 마지막 주문 후 정해진 시간(기본 48시간) 내 반등이 없으면 시간 기반 청산

정액 DCA 봇이 하락장에서 안전주문을 조기 소진해 최저점에서 최대 노출을 갖는
구조적 결함을, "진입 필터 + 횟수 캡 + 타임스톱" 세 겹으로 차단한다.
"""
from __future__ import annotations

import time

from core.logger import get_logger
from core.regime import STRONG_BEAR, VOLATILE_PULLBACK
from core.state import Position
from strategies.base import (
    BUY_MARKET,
    SELL_MARKET,
    SET_STOP,
    Action,
    Context,
    MarketView,
    Strategy,
)

log = get_logger("dca")


class SmartDcaStrategy(Strategy):
    name = "dca"
    regimes = (VOLATILE_PULLBACK,)

    # ------------------------------------------------------------------ #
    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        if not view.ready:
            return []
        if position and position.is_open:
            return self._manage(view, position, ctx)
        return self._entry(view, ctx, position)

    # ------------------------------------------------------------------ #
    # 진입 / 추가 진입
    # ------------------------------------------------------------------ #
    def _entry(self, view: MarketView, ctx: Context, position: Position | None) -> list[Action]:
        if view.regime.regime != VOLATILE_PULLBACK:
            return []
        if not self._uptrend_intact(view):
            return []
        if not self._oversold(view):
            return []

        atr = view.atr_macro
        stop = view.price - self.s.dca_sl_atr * atr
        stop_dist = (view.price - stop) / view.price
        if stop_dist <= 0:
            return []

        krw, reason = ctx.size_krw(stop_dist, self.name, view.market)
        if krw <= 0:
            log.debug("%s DCA 1차 진입 사이징 불가: %s", view.market, reason)
            return []

        # 1차 진입은 전체 배정액을 배수 합계로 나눈 기본 단위
        unit = krw / sum(self.s.dca_size_multipliers[: self.s.dca_max_steps])
        first = unit * self.s.dca_size_multipliers[0]
        if first < self.s.min_order_krw:
            first = self.s.min_order_krw
        if first > krw:
            return []

        return [
            Action(
                kind=BUY_MARKET,
                market=view.market,
                krw=first,
                price=stop,
                reason=(
                    f"스마트 DCA 1차 진입 | RSI {view.sig('rsi14', 50):.1f} | "
                    f"배정 {krw:,.0f}원 중 {first:,.0f}원 | {reason}"
                ),
                meta={
                    "strategy": self.name,
                    "stop": stop,
                    "init_stop": stop,
                    "dca_unit": unit,
                    "dca_budget": krw,
                    "last_entry_price": view.price,
                },
            )
        ]

    # ------------------------------------------------------------------ #
    # 보유 중 관리
    # ------------------------------------------------------------------ #
    def _manage(self, view: MarketView, pos: Position, ctx: Context) -> list[Action]:
        price = view.price
        atr = view.atr_macro
        actions: list[Action] = []

        # 1) 익절
        tp_price = pos.avg_price * (1 + self.s.dca_tp_pct)
        if price >= tp_price:
            return [
                Action(
                    SELL_MARKET, view.market, ratio=1.0,
                    reason=f"DCA 목표 익절 +{self.s.dca_tp_pct * 100:.1f}% (평단 {pos.avg_price:,.0f} -> {price:,.0f})",
                )
            ]

        # 2) 하드 손절 - 평단 기준 ATR 배수
        hard_stop = pos.avg_price - self.s.dca_sl_atr * atr
        if pos.stop_price <= 0 or abs(pos.stop_price - hard_stop) / max(hard_stop, 1) > 0.002:
            actions.append(Action(SET_STOP, view.market, price=hard_stop, reason="DCA 손절선 갱신"))
        if price <= hard_stop:
            return [
                Action(
                    SELL_MARKET, view.market, ratio=1.0,
                    reason=f"DCA 하드 손절 (현재가 {price:,.0f} <= 손절선 {hard_stop:,.0f})",
                )
            ]

        # 3) 하락 국면 전환 - 물타기 근거 소멸 (BEAR_FORCE_EXIT=False 면 추가매수만 중단)
        if view.regime.regime == STRONG_BEAR and self.s.bear_force_exit:
            return [
                Action(SELL_MARKET, view.market, ratio=1.0, reason="하락 국면 전환 - DCA 포지션 정리")
            ]

        # 4) 시간 기반 청산 - 마지막 매수 후 반등이 없으면 손실 확정
        elapsed_h = (time.time() - pos.last_add_at) / 3600
        if elapsed_h >= self.s.dca_time_stop_hours and price < pos.avg_price:
            return [
                Action(
                    SELL_MARKET, view.market, ratio=1.0,
                    reason=f"타임스톱 청산 ({elapsed_h:.0f}시간 무반등, 평가손익 {pos.unrealized_pct(price) * 100:+.2f}%)",
                )
            ]

        # 5) 추가 분할 매수
        add = self._maybe_add(view, pos, ctx)
        if add:
            actions.append(add)
        return actions

    def _maybe_add(self, view: MarketView, pos: Position, ctx: Context) -> Action | None:
        if pos.steps >= self.s.dca_max_steps:
            return None
        if view.regime.regime == STRONG_BEAR:
            return None
        if not self._uptrend_intact(view):
            return None
        if not self._oversold(view):
            return None

        last_price = float(pos.meta.get("last_entry_price") or pos.avg_price)
        trigger = last_price - self.s.dca_step_atr * view.atr_macro
        if view.price > trigger:
            return None

        unit = float(pos.meta.get("dca_unit") or 0.0)
        if unit <= 0:
            return None
        idx = min(pos.steps, len(self.s.dca_size_multipliers) - 1)
        krw = unit * self.s.dca_size_multipliers[idx]
        krw = max(krw, self.s.min_order_krw)

        # 단일 자산 노출 상한 재확인 (전략서: 전체 자본의 50% 초과 금지)
        cap = ctx.equity * self.s.max_asset_alloc_pct - pos.invested_krw
        available = ctx.cash - ctx.equity * self.s.cash_reserve_pct
        krw = min(krw, cap, available)
        if krw < self.s.min_order_krw:
            return None

        return Action(
            kind=BUY_MARKET,
            market=view.market,
            krw=float(int(krw)),
            reason=(
                f"DCA {pos.steps + 1}차 추가매수 (트리거 {trigger:,.0f} >= 현재가 {view.price:,.0f}, "
                f"RSI {view.sig('rsi14', 50):.1f})"
            ),
            meta={"strategy": self.name, "dca_add": True, "last_entry_price": view.price},
        )

    # ------------------------------------------------------------------ #
    # 진입 필터
    # ------------------------------------------------------------------ #
    def _uptrend_intact(self, view: MarketView) -> bool:
        """4시간봉 기준 상승 추세가 살아 있는가 - 구조적 약세장 물타기 차단."""
        close = view.mac("close", 0.0)
        ema200 = view.mac("ema200", 0.0)
        ema50 = view.mac("ema50", 0.0)
        if close <= 0 or ema200 <= 0:
            return False
        return close > ema200 and ema50 > ema200 * 0.98

    def _oversold(self, view: MarketView) -> bool:
        rsi = view.sig("rsi14", 50.0)
        close = view.sig("close", view.price)
        bb_lower = view.sig("bb_lower", 0.0)
        return rsi <= self.s.dca_rsi_max or (bb_lower > 0 and close < bb_lower)
