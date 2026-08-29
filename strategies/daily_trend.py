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

경고: MA 길이에 대한 강건성 검증에서 MA50(+122%p)과 MA200(-27%p)이 정반대
결과를 보였다. 이는 2.7년 표본 안의 특정 상승장/하락장 구간에 대한 과적합
위험 신호이므로, 실거래 투입 전 반드시 별도 기간으로 재검증할 것.
"""
from __future__ import annotations

from core.indicators import sma
from core.logger import get_logger
from core.regime import STRONG_BEAR
from core.state import Position
from strategies.base import BUY_MARKET, SELL_MARKET, Action, Context, MarketView, Strategy

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

        # 구조적 하락 오버라이드 - 일봉 신호와 무관하게 즉시 청산
        structural_bear = view.regime.regime == STRONG_BEAR

        if position and position.is_open:
            if structural_bear or not above_ma:
                reason = "구조적 하락 오버라이드" if structural_bear else f"종가 {close:,.0f} <= MA{self.s.trend_ma_len} {ma:,.0f}"
                return [Action(kind=SELL_MARKET, market=view.market, ratio=1.0,
                               reason=f"일봉 추세 이탈 청산 ({reason})")]
            return []

        if structural_bear or not above_ma:
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
                reason=f"일봉 추세 진입 (종가 {close:,.0f} > MA{self.s.trend_ma_len} {ma:,.0f}) | {reason}",
                meta={"strategy": self.name},
            )
        ]


def compute_daily_ma(df, length: int):
    """일봉 DataFrame 에 MA(length) 컬럼을 붙인다. build_features() 뒤에 호출."""
    out = df.copy()
    out[f"ma{length}"] = sma(out["close"], length)
    return out
