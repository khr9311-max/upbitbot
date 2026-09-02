"""
일봉 추세 엔진 - 메인 자본 (전략서 재검토 후 도입).

15분봉/4시간봉 HMM 기반 추세 판별은 실측 검증 결과 예측력이 없었다
(STRONG_BULL 판정 이후 24시간 수익률이 STRONG_BEAR 판정보다 낮은 경우까지 있었음).
반면 일봉 이동평균 필터는 2.7년 백테스트에서 5종목 전부 단순보유를 이겼고
(MA50 기준 평균 초과성과 +122%p), 매매 빈도도 연 10~20회 수준으로 낮아
소액 계좌의 마찰비용 문제에서 자유롭다.

규칙은 단순하다:
    종가 > MA(N)  ->  보유
    종가 <= MA(N) ->  청산 후 현금

여기에 기존에 검증된 유일한 방어 장치인 "4시간봉 구조적 하락 오버라이드"
(core.regime._structural_bear, STRONG_BEAR 판정)를 얹어 급락 국면에서는
일봉 신호를 기다리지 않고 즉시 청산한다.

보유 중에는 일봉 샹들리에 출구로 손절선을 끌어올린다. 진입 시 정한 손절선만
쓰던 기존 동작은 MA 에서 멀리 떨어진 자리에서 진입할수록 손절폭이 벌어져
(실측: XRP 평단 1,894 / 손절선 1,729 = -8.7%) 되돌림에서 수익을 전부 반납한
뒤에야 털리는 구조였다. 기준 고가는 pos.highest(진입 이후 고가)를 쓴다 -
이유는 _chandelier() 주석 참고.

경고: MA 길이에 대한 강건성 검증에서 MA50(+122%p)과 MA200(-27%p)이 정반대
결과를 보였다. 이는 2.7년 표본 안의 특정 상승장/하락장 구간에 대한 과적합
위험 신호이므로, 실거래 투입 전 반드시 별도 기간으로 재검증할 것.
"""
from __future__ import annotations

from core.indicators import atr as atr_fn, sma
from core.logger import get_logger
from core.regime import STRONG_BEAR
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

log = get_logger("daily_trend")


class DailyTrendStrategy(Strategy):
    name = "trend"
    regimes = ()  # 국면 분류기가 아니라 엔진에서 "추세 유니버스" 소속 여부로 직접 라우팅된다

    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        if not view.daily_ready:
            return []

        close = view.day("close", view.price)
        ma = view.day(f"ma{self.s.trend_ma_len}")
        above_ma = close > ma if ma == ma else False  # NaN 방어

        # 구조적 하락 오버라이드 - 일봉 신호와 무관하게 즉시 청산.
        # source == "override" 인 경우만 인정한다: 일반 HMM/규칙 기반 STRONG_BEAR 판정은
        # (hysteresis 를 거쳤어도) 4시간봉 노이즈에 뒤집히는 경우가 실측에서 확인됐고,
        # 실측에서 방어 효과가 검증된 것은 _structural_bear() 규칙(EMA200 붕괴) 하나뿐이다.
        structural_bear = view.regime.regime == STRONG_BEAR and view.regime.source == "override"

        daily_bar_date = str(view.daily.index[-1].date())

        if position and position.is_open:
            return self._manage(view, position, close, ma, above_ma, structural_bear, ctx, daily_bar_date)

        if structural_bear or not above_ma:
            return []

        # 당일 손절 쿨다운 - 손절선 부근에서 등락하면 청산 직후 같은 일봉으로 바로
        # 재진입해 손절만 반복하는 휩쏘가 발생한다(실측: 백테스트에서 한 종목이
        # 하루 안에 7회 왕복). 이 일봉(daily_bar_date)에 이미 손절이 있었다면
        # 다음 일봉이 나올 때까지 재진입을 보류한다.
        if ctx.state.trend_stop_cooldown.get(view.market) == daily_bar_date:
            return []

        # 진입 - 손절 기준은 일봉 ATR 이 아니라 MA 자체까지의 거리(추세 이탈 = 청산 조건이므로)
        stop_dist = max(0.02, (close - ma) / close * 0.5 + 0.02)
        krw, reason = ctx.size_krw(stop_dist, self.name, view.market)
        if krw <= 0:
            log.debug("%s 일봉 추세 진입 사이징 불가: %s", view.market, reason)
            return []

        return [
            Action(
                kind=BUY_MARKET,
                market=view.market,
                krw=krw,
                price=close * (1 - stop_dist),
                reason=f"일봉 추세 진입 (종가 {close:,.0f} > MA{self.s.trend_ma_len} {ma:,.0f}) | {reason}",
                meta={"strategy": self.name},
            )
        ]


    # ------------------------------------------------------------------ #
    # 보유 중 관리 - 추적 손절(샹들리에) / 부분 익절 / 청산
    # ------------------------------------------------------------------ #
    def _manage(
        self, view: MarketView, pos: Position, close: float, ma: float,
        above_ma: bool, structural_bear: bool, ctx: Context, daily_bar_date: str,
    ) -> list[Action]:
        price = view.price
        pos.highest = max(pos.highest or pos.avg_price, price)

        # 1) 추세 이탈은 트레일링보다 우선한다 - 이 엔진의 1차 청산 규칙이다.
        if structural_bear or not above_ma:
            reason = (
                "구조적 하락 오버라이드" if structural_bear
                else f"종가 {close:,.0f} <= MA{self.s.trend_ma_len} {ma:,.0f}"
            )
            return [Action(kind=SELL_MARKET, market=view.market, ratio=1.0,
                           reason=f"일봉 추세 이탈 청산 ({reason})")]

        actions: list[Action] = []
        new_stop = pos.stop_price

        # 2) 샹들리에 출구로 손절선을 끌어올린다 (내려가지는 않는 래칫)
        chandelier = self._chandelier(view, pos)
        if chandelier > 0:
            new_stop = max(new_stop, chandelier)

        # 3) +R 배수 도달 시 부분 익절하고 손절선을 본전(왕복 수수료 포함)으로 올린다
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
                        reason=f"일봉 추세 부분 익절 +{self.s.trend_partial_tp_r:.1f}R "
                               f"(목표 {tp_price:,.0f}원)",
                        meta={"partial": True},
                    )
                )
                new_stop = max(new_stop, pos.avg_price * (1 + 2 * self.s.fee_rate))

        if new_stop > pos.stop_price:
            actions.append(
                Action(
                    kind=SET_STOP,
                    market=view.market,
                    price=new_stop,
                    reason=f"추적 손절선 상향 {pos.stop_price:,.0f} -> {new_stop:,.0f}",
                )
            )

        # 4) 손절선 이탈 시 전량 청산 (부분 익절보다 우선한다)
        stop_line = max(new_stop, pos.stop_price)
        if stop_line > 0 and price <= stop_line:
            ctx.state.trend_stop_cooldown[view.market] = daily_bar_date
            return [Action(kind=SELL_MARKET, market=view.market, ratio=1.0,
                           reason=f"일봉 추세 손절선 이탈 청산 (현재가 {price:,.0f} <= "
                                  f"손절선 {stop_line:,.0f})")]

        return actions

    def _chandelier(self, view: MarketView, pos: Position) -> float:
        """
        일봉 샹들리에 출구: (진입 이후 고가) - k * ATR(n).

        기준 고가로 "최근 n봉의 시장 최고가"가 아니라 pos.highest(진입 이후 고가)를
        쓴다. 시장 고가를 쓰면 이미 고점에서 한참 밀린 종목에 이 로직을 처음 붙이는
        순간 손절선이 현재가보다 위에 잡혀 즉시 전량 청산된다(실측: XRP 진입가 1,894
        보유 중 22일 최고가 2,331 기준 샹들리에 2,066 > 현재가 1,820). 열려 있는
        포지션에 대한 추적 손절의 의미대로 "내가 들고 있는 동안의 고점"을 기준으로
        삼아야 기존 포지션에 안전하게 얹을 수 있다.
        """
        df = view.daily
        n = self.s.chandelier_n
        if len(df) < n + 1:
            return 0.0
        series = atr_fn(df, n).dropna()
        if series.empty:
            return 0.0
        adx = view.day("adx", 0.0)
        k = self.s.chandelier_k_strong if adx >= self.s.trend_adx_strong else self.s.chandelier_k
        high = max(pos.highest or pos.avg_price, view.price)
        return high - k * float(series.iloc[-1])


def compute_daily_ma(df, length: int):
    """일봉 DataFrame 에 MA(length) 컬럼을 붙인다. build_features() 뒤에 호출."""
    out = df.copy()
    out[f"ma{length}"] = sma(out["close"], length)
    return out
