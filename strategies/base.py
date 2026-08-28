"""
전략 공통 인터페이스.

전략은 직접 주문을 내지 않는다. 시장 상태(MarketView)와 실행 컨텍스트(Context)를
보고 "무엇을 하고 싶은지"를 Action 목록으로 반환하면, 엔진이 리스크 검증을 거쳐
실제 주문으로 옮긴다. 덕분에 백테스트와 실거래가 동일한 전략 코드를 공유한다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from core.regime import RegimeResult
from core.state import BotState, Position

# Action.kind 종류
BUY_MARKET = "buy_market"
SELL_MARKET = "sell_market"
BUY_LIMIT = "buy_limit"
SELL_LIMIT = "sell_limit"
CANCEL = "cancel"
SET_STOP = "set_stop"


@dataclass
class Action:
    kind: str
    market: str
    krw: float = 0.0  # buy_market 주문 금액
    volume: float = 0.0  # 수량 (sell_market / limit)
    price: float = 0.0  # 지정가 / 손절선
    ratio: float = 0.0  # sell_market 시 보유수량 대비 비율 (volume 대신 사용 가능)
    uuid: str = ""  # cancel 대상
    reason: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class MarketView:
    market: str
    price: float
    regime: RegimeResult
    macro: pd.DataFrame  # 상위 타임프레임(기본 4시간봉) 지표 포함
    signal: pd.DataFrame  # 실행 타임프레임(기본 15분봉) 지표 포함
    open_orders: list = field(default_factory=list)
    spread_pct: float = 0.0

    # ---- 안전한 값 접근 헬퍼 ----
    def sig(self, column: str, default: float = float("nan")) -> float:
        return _last(self.signal, column, default)

    def mac(self, column: str, default: float = float("nan")) -> float:
        return _last(self.macro, column, default)

    @property
    def atr(self) -> float:
        """실행 타임프레임(15분봉) ATR - 그리드 간격처럼 미시 구조에만 쓴다."""
        v = self.sig("atr14")
        return v if v == v and v > 0 else self.price * 0.01  # NaN 방어

    @property
    def atr_macro(self) -> float:
        """
        상위 타임프레임(4시간봉) ATR - 손절선/추적청산/DCA 스텝의 기준.

        15분봉 ATR 은 가격의 0.1~0.3% 수준이라 손절선을 그 배수로 잡으면 호가 노이즈와
        왕복 수수료(0.1%) 안에 손절선이 들어가 버려 거의 100% 털린다. 포지션의 생존
        시간축(수 시간~수 일)에 맞는 변동성 척도를 써야 한다.
        """
        v = self.mac("atr14")
        if v == v and v > 0:
            return v
        return max(self.atr * 4, self.price * 0.015)

    @property
    def ready(self) -> bool:
        return len(self.signal) >= 60 and len(self.macro) >= 60 and self.price > 0


@dataclass
class Context:
    settings: Any
    client: Any
    sizer: Any
    state: BotState
    equity: float
    cash: float
    regime_weight: float
    n_slots: int

    def size_krw(self, stop_distance_pct: float, strategy: str, market: str) -> tuple[float, str]:
        pos = self.state.positions.get(market)
        exposure = pos.invested_krw if pos else 0.0
        res = self.sizer.size(
            equity=self.equity,
            cash_available=self.cash,
            regime_weight=self.regime_weight,
            stop_distance_pct=stop_distance_pct,
            trades=self.state.trades,
            strategy=strategy,
            current_exposure_krw=exposure,
            n_slots=self.n_slots,
        )
        return res.krw, res.reason


class Strategy:
    """모든 실행 전략의 베이스."""

    name: str = "base"
    regimes: tuple[str, ...] = ()

    def __init__(self, settings) -> None:
        self.s = settings

    def handles(self, regime: str) -> bool:
        return regime in self.regimes

    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        """현재 상황에서 수행할 액션 목록을 반환한다."""
        raise NotImplementedError

    # 공통 유틸 ------------------------------------------------------- #
    @staticmethod
    def _pct(a: float, b: float) -> float:
        return (a / b - 1.0) if b else 0.0


def _last(df: pd.DataFrame, column: str, default: float) -> float:
    if df is None or df.empty or column not in df.columns:
        return default
    series = df[column]
    if series.empty:
        return default
    val = series.iloc[-1]
    try:
        val = float(val)
    except (TypeError, ValueError):
        return default
    return default if val != val else val  # NaN 체크
