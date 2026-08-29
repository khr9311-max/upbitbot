"""실행 전략 모음.

daily_trend / scalp 이 현재 엔진(core/engine.py, backtest.py)이 실제로 쓰는 전략이다.
trend(변동성돌파) / grid / dca 는 4시간봉 HMM 국면 라우팅 기반의 이전 세대 전략으로,
실측 검증 결과(국면 판별에 예측력 부족)에 따라 엔진 배선에서는 제외됐지만 코드와
테스트는 참고/비교용으로 남겨둔다.
"""

from strategies.base import Action, Context, MarketView, Strategy
from strategies.daily_trend import DailyTrendStrategy
from strategies.dca import SmartDcaStrategy
from strategies.grid import AtrGridStrategy
from strategies.scalp import ScalpMeanReversionStrategy
from strategies.trend import TrendBreakoutStrategy

__all__ = [
    "Action",
    "Context",
    "MarketView",
    "Strategy",
    "DailyTrendStrategy",
    "ScalpMeanReversionStrategy",
    "TrendBreakoutStrategy",
    "AtrGridStrategy",
    "SmartDcaStrategy",
]
