"""
단타 레이어 - 30분봉 과매도 역추세 (소액 별도 자본).

실측 백테스트 근거(30분봉, ATR 백분위 상위 30%, 지정가 왕복 수수료 0.10%만 가정):
    KRW-XRP  RSI<30 반등     표본 634건  순수익 +0.040%/건  승률 54.4%
    KRW-XRP  BB하단 이탈      표본 730건  순수익 +0.064%/건  승률 54.4%
    KRW-SUI  BB하단 이탈      표본 382건  순수익 +0.022%/건  승률 51.6%
    KRW-ONDO BB하단 이탈      표본 315건  순수익 +0.071%/건  승률 53.7%
같은 조건에서 TRUMP(스프레드는 좁지만 이벤트성 급등 몇 건이 평균을 왜곡 -
중앙값은 마이너스)와 ENA(스프레드 0.9%로 지정가 체결 가정 자체가 비현실적)는
가짜 엣지로 판정되어 제외했다. 즉 특정 종목을 하드코딩하지 않고, 매 갱신마다
"스프레드가 좁고 유동성이 충분한 종목"을 스크리닝해서 담아야 이 함정을 피한다
(core.screener.UniverseScreener.select_scalp_watchlist 가 담당).

경고: 검증 표본이 약 2주로 매우 짧다. SCALP_ALLOC_PCT 로 노출을 강하게
제한하고, 실거래 투입 전 더 긴 기간으로 재검증할 것.
"""
from __future__ import annotations

from core.logger import get_logger
from core.regime import STRONG_BEAR
from core.state import Position
from strategies.base import (
    BUY_LIMIT,
    CANCEL,
    SELL_MARKET,
    Action,
    Context,
    MarketView,
    Strategy,
)

log = get_logger("scalp")


class ScalpMeanReversionStrategy(Strategy):
    name = "scalp"
    regimes = ()  # 국면이 아니라 엔진의 단타 워치리스트 소속 여부로 직접 라우팅된다

    # ------------------------------------------------------------------ #
    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        if not view.ready:
            return []

        pending = position.meta.get("scalp_pending_uuid") if position else None
        has_coin = bool(position and position.volume > 0)

        if position is not None and pending and not has_coin:
            return self._await_fill(view, position)
        if has_coin:
            return self._manage(view, position, ctx)
        return self._entry(view, ctx)

    # ------------------------------------------------------------------ #
    # 진입
    # ------------------------------------------------------------------ #
    def _entry(self, view: MarketView, ctx: Context) -> list[Action]:
        if view.regime.regime == STRONG_BEAR:
            return []  # 떨어지는 칼 방지 - 구조적 하락에서는 반등을 노리지 않는다

        atr_pct = view.sig("atr_pct", 0.0)
        if atr_pct < self.s.scalp_atr_percentile_min:
            return []  # 변동성이 낮으면 비용을 이길 만큼의 움직임 자체가 없다

        rsi = view.sig("rsi14", 50.0)
        close = view.sig("close", view.price)
        bb_lower = view.sig("bb_lower", 0.0)
        oversold = rsi <= self.s.scalp_rsi_max or (bb_lower > 0 and close < bb_lower)
        if not oversold:
            return []

        krw, reason = ctx.size_krw(self.s.scalp_stop_loss_pct, self.name, view.market)
        if krw <= 0:
            log.debug("%s 단타 진입 사이징 불가: %s", view.market, reason)
            return []

        return [
            Action(
                kind=BUY_LIMIT,
                market=view.market,
                price=view.price,
                volume=krw / view.price,
                reason=f"단타 진입 (RSI {rsi:.1f}, 변동성 백분위 {atr_pct * 100:.0f}%) | {reason}",
                meta={"strategy": self.name, "entry_ts": view.ts},
            )
        ]

    def _await_fill(self, view: MarketView, pos: Position) -> list[Action]:
        """지정가 진입이 체결 대기 중일 때 - TTL 넘으면 추격 없이 그냥 취소."""
        placed_at = float(pos.meta.get("scalp_pending_at", view.ts))
        ttl = self.s.scalp_timeframe * 60  # 한 신호봉만큼만 기다린다
        if view.ts - placed_at > ttl:
            return [
                Action(
                    kind=CANCEL,
                    market=view.market,
                    uuid=pos.meta["scalp_pending_uuid"],
                    reason="단타 진입 미체결 취소 (추격 매수 금지)",
                )
            ]
        return []

    # ------------------------------------------------------------------ #
    # 보유 중 관리 - 익절 / 손절 / 타임스톱
    # ------------------------------------------------------------------ #
    def _manage(self, view: MarketView, pos: Position, ctx: Context) -> list[Action]:
        price = view.price

        if view.regime.regime == STRONG_BEAR and view.regime.source == "override":
            return [Action(SELL_MARKET, view.market, ratio=1.0, reason="구조적 하락 오버라이드 - 단타 포지션 정리")]

        tp = pos.avg_price * (1 + self.s.scalp_take_profit_pct)
        sl = pos.avg_price * (1 - self.s.scalp_stop_loss_pct)
        if price >= tp:
            return [Action(SELL_MARKET, view.market, ratio=1.0,
                           reason=f"단타 익절 +{self.s.scalp_take_profit_pct * 100:.1f}%")]
        if price <= sl:
            return [Action(SELL_MARKET, view.market, ratio=1.0,
                           reason=f"단타 손절 -{self.s.scalp_stop_loss_pct * 100:.1f}%")]

        elapsed_bars = (view.ts - pos.opened_at) / (self.s.scalp_timeframe * 60)
        if elapsed_bars >= self.s.scalp_max_hold_bars:
            return [Action(SELL_MARKET, view.market, ratio=1.0,
                           reason=f"단타 타임스톱 ({self.s.scalp_max_hold_bars}봉 경과, "
                                  f"평가손익 {pos.unrealized_pct(price) * 100:+.2f}%)")]
        return []

    # ------------------------------------------------------------------ #
    # 엔진 콜백
    # ------------------------------------------------------------------ #
    def on_order_placed(self, pos: Position, action: Action, order, ts: float | None = None) -> None:
        if action.kind != BUY_LIMIT:
            return
        pos.meta["scalp_pending_uuid"] = order.uuid
        pos.meta["scalp_pending_at"] = ts if ts is not None else action.meta.get("entry_ts", 0.0)

    def on_order_cancelled(self, pos: Position, order_uuid: str) -> None:
        if pos.meta.get("scalp_pending_uuid") == order_uuid:
            pos.meta.pop("scalp_pending_uuid", None)
            pos.meta.pop("scalp_pending_at", None)

    def on_buy_filled(
        self, pos: Position, order_uuid: str, price: float, volume: float, ts: float | None = None
    ) -> None:
        if pos.meta.get("scalp_pending_uuid") == order_uuid:
            pos.meta.pop("scalp_pending_uuid", None)
            pos.meta.pop("scalp_pending_at", None)
