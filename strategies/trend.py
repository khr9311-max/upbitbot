"""
추세 국면 전략 - 변동성 돌파 진입 + 샹들리에 출구(Chandelier Exit) 트레일링 청산.

전략서 3.2절:
  * 진입: 상위 타임프레임 시가 + k x (직전 봉 레인지) 를 상향 돌파할 때 모멘텀 추종
  * 청산: CE_long = max(High_{t-n..t}) - k x ATR(n),  기본 n=22, k=3.0
          ADX 가 임계치를 넘는 강추세에서는 k 를 상향해 눌림목 조기 이탈을 방지
  * 부분 익절: +1.5R 도달 시 일부 청산하고 잔여 포지션 손절선을 본전으로 이동
"""
from __future__ import annotations

from core.indicators import atr as atr_fn
from core.logger import get_logger
from core.regime import STRONG_BEAR, STRONG_BULL
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

log = get_logger("trend")


class TrendBreakoutStrategy(Strategy):
    name = "trend"
    regimes = (STRONG_BULL,)

    # ------------------------------------------------------------------ #
    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        if not view.ready:
            return []
        if position and position.is_open:
            return self._manage(view, position, ctx)
        return self._entry(view, ctx)

    # ------------------------------------------------------------------ #
    # 진입
    # ------------------------------------------------------------------ #
    def _entry(self, view: MarketView, ctx: Context) -> list[Action]:
        if view.regime.regime != STRONG_BULL:
            return []

        macro = view.macro
        if len(macro) < 3:
            return []

        # 4시간봉 구조 확인: 정배열이어야 한다
        if not (view.mac("ema50", 0) > view.mac("ema200", 0)):
            return []

        adx = view.sig("adx", 0.0)
        if adx < self.s.trend_adx_min:
            return []
        if view.sig("plus_di", 0.0) <= view.sig("minus_di", 0.0):
            return []

        rsi = view.sig("rsi14", 50.0)
        if rsi >= self.s.trend_rsi_max:
            return []  # 과열 구간 추격매수 금지

        # 변동성 돌파 목표가: 현재 상위봉 시가 + k x 직전 상위봉 레인지
        cur = macro.iloc[-1]
        prev = macro.iloc[-2]
        prev_range = float(prev["high"]) - float(prev["low"])
        if prev_range <= 0:
            return []

        k = self.s.trend_breakout_k
        if adx >= self.s.trend_adx_strong:
            k *= 0.8  # 강추세에서는 더 이른 진입을 허용
        target = float(cur["open"]) + k * prev_range

        if view.price < target:
            return []
        if view.price <= view.sig("ema20", view.price):
            return []  # 돌파했더라도 단기 추세가 살아있어야 한다

        atr = view.atr_macro
        stop = view.price - self.s.trend_init_stop_atr * atr
        stop_dist = (view.price - stop) / view.price
        if stop_dist <= 0:
            return []

        krw, reason = ctx.size_krw(stop_dist, self.name, view.market)
        if krw <= 0:
            log.debug("%s 추세 진입 사이징 불가: %s", view.market, reason)
            return []

        return [
            Action(
                kind=BUY_MARKET,
                market=view.market,
                krw=krw,
                price=stop,
                reason=(
                    f"변동성 돌파 진입 | 목표가 {target:,.0f} <= 현재가 {view.price:,.0f} | "
                    f"ADX {adx:.1f} RSI {rsi:.1f} | {reason}"
                ),
                meta={
                    "strategy": self.name,
                    "stop": stop,
                    "init_stop": stop,
                    "breakout_target": target,
                    "k": k,
                },
            )
        ]

    # ------------------------------------------------------------------ #
    # 보유 중 관리
    # ------------------------------------------------------------------ #
    def _manage(self, view: MarketView, pos: Position, ctx: Context) -> list[Action]:
        actions: list[Action] = []
        price = view.price
        pos.highest = max(pos.highest or pos.avg_price, price)

        # 1) 샹들리에 출구 계산
        n = self.s.chandelier_n
        adx = view.sig("adx", 0.0)
        k = self.s.chandelier_k_strong if adx >= self.s.trend_adx_strong else self.s.chandelier_k
        chandelier = self._chandelier(view, n, k)

        new_stop = pos.stop_price
        if chandelier > 0:
            new_stop = max(new_stop, chandelier)

        # 2) 부분 익절(+R 배수) 후 손절선을 본전으로 상향
        init_risk = pos.avg_price - (pos.init_stop or pos.stop_price)
        if not pos.partial_taken and init_risk > 0:
            tp_price = pos.avg_price + self.s.trend_partial_tp_r * init_risk
            if price >= tp_price:
                ratio = min(0.9, max(0.1, self.s.trend_partial_tp_ratio))
                actions.append(
                    Action(
                        kind=SELL_MARKET,
                        market=view.market,
                        ratio=ratio,
                        reason=f"부분 익절 +{self.s.trend_partial_tp_r:.1f}R (목표 {tp_price:,.0f}원)",
                        meta={"partial": True},
                    )
                )
                breakeven = pos.avg_price * (1 + 2 * self.s.fee_rate)
                new_stop = max(new_stop, breakeven)

        if new_stop > pos.stop_price:
            actions.append(
                Action(
                    kind=SET_STOP,
                    market=view.market,
                    price=new_stop,
                    reason=f"샹들리에 출구 상향 {pos.stop_price:,.0f} -> {new_stop:,.0f} (k={k})",
                )
            )

        # 3) 손절선 이탈 시 전량 청산
        stop_line = max(new_stop, pos.stop_price)
        if stop_line > 0 and price <= stop_line:
            return [
                Action(
                    kind=SELL_MARKET,
                    market=view.market,
                    ratio=1.0,
                    reason=f"샹들리에 출구 이탈 청산 (현재가 {price:,.0f} <= 손절선 {stop_line:,.0f})",
                )
            ]

        # 4) 하락 국면 전환에서만 강제 청산한다.
        #
        #    전략서 3.2절: "진입 이후의 수익을 조기 마감하지 않고 추세의 끝까지 포지션을
        #    유지하는 것이 복리 수익 극대화의 핵심". 국면이 상승에서 횡보/조정으로만
        #    바뀌어도 털어버리면 정상적인 눌림목마다 승자를 작게 끊어내는 반면 패자는
        #    손절선까지 그대로 가므로 손익비가 무너진다. 추세 포지션의 출구는 원칙적으로
        #    샹들리에 트레일링 스탑 하나이고, 현금 보유가 강제되는 하락 국면만 예외다.
        if (
            view.regime.regime == STRONG_BEAR
            and self.s.bear_force_exit
            and view.regime.confidence >= self.s.regime_min_confidence
        ):
            return actions + [
                Action(
                    kind=SELL_MARKET,
                    market=view.market,
                    ratio=1.0,
                    reason=f"하락 국면 전환 청산 ({STRONG_BULL} -> {view.regime.regime})",
                )
            ]
        return actions

    # ------------------------------------------------------------------ #
    def _chandelier(self, view: MarketView, period: int, k: float) -> float:
        # 전략서의 n=22, k=3.0 은 일봉 기준 파라미터다. 상위 타임프레임(4시간봉)에서
        # 계산해야 22봉 = 약 3.7일로 원래 의도한 시간축을 유지한다.
        df = view.macro
        if len(df) < period + 1:
            return 0.0
        highest = float(df["high"].tail(period).max())
        atr_series = atr_fn(df, period).dropna()
        if atr_series.empty:
            return 0.0
        return highest - k * float(atr_series.iloc[-1])
