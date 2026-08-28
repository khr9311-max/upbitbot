"""
포지션 사이징 - 부분 켈리(Fractional Kelly) + 고정비율 리스크 이중 상한.

전략서 4장:
    f* = W - (1 - W) / R          (W = 승률, R = 손익비)
소액 계좌는 팻테일 구간에서 풀 켈리가 곧 파산이므로 f* 의 25~33% 만 반영하는
쿼터 켈리를 기본으로 하고, 여기에 "거래당 원금의 N%" 고정 리스크 상한을
교차 적용해 둘 중 작은 값을 최종 주문 금액으로 삼는다.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from core.logger import get_logger
from core.state import Trade

log = get_logger("sizing")


@dataclass
class KellyStats:
    win_rate: float = 0.0
    payoff_ratio: float = 0.0
    f_star: float = 0.0
    sample: int = 0
    usable: bool = False


@dataclass
class SizingResult:
    krw: float
    reason: str = ""
    kelly: KellyStats = field(default_factory=KellyStats)
    fraction: float = 0.0

    @property
    def ok(self) -> bool:
        return self.krw > 0


class PositionSizer:
    def __init__(self, settings) -> None:
        self.s = settings

    # ------------------------------------------------------------------ #
    def kelly(self, trades: Sequence[Trade], strategy: str | None = None) -> KellyStats:
        """최근 거래 이력에서 켈리 최적 베팅비율 f* 를 추정한다."""
        hist = [t for t in trades if strategy is None or t.strategy == strategy]
        hist = hist[-self.s.kelly_lookback_trades :]
        if len(hist) < self.s.kelly_min_trades:
            return KellyStats(sample=len(hist), usable=False)

        wins = [t.pnl_krw for t in hist if t.pnl_krw > 0]
        losses = [-t.pnl_krw for t in hist if t.pnl_krw <= 0]
        if not wins or not losses:
            # 한쪽만 존재하면 통계적 의미가 없다
            return KellyStats(sample=len(hist), usable=False)

        w = len(wins) / len(hist)
        avg_win = sum(wins) / len(wins)
        avg_loss = sum(losses) / len(losses)
        if avg_loss <= 0:
            return KellyStats(win_rate=w, sample=len(hist), usable=False)

        r = avg_win / avg_loss
        f_star = w - (1.0 - w) / r
        return KellyStats(win_rate=w, payoff_ratio=r, f_star=f_star, sample=len(hist), usable=True)

    # ------------------------------------------------------------------ #
    def size(
        self,
        *,
        equity: float,
        cash_available: float,
        regime_weight: float,
        stop_distance_pct: float,
        trades: Sequence[Trade],
        strategy: str | None = None,
        current_exposure_krw: float = 0.0,
        n_slots: int = 1,
    ) -> SizingResult:
        """
        equity            : 총 평가자산 (원화 + 코인)
        cash_available    : 즉시 주문 가능한 원화
        regime_weight     : 국면별 자본 배분 비중 (0~1)
        stop_distance_pct : 진입가 대비 손절선까지의 거리 (예: 0.03 = 3%)
        current_exposure_krw : 해당 종목에 이미 투입된 금액 (DCA 추가매수 시)
        n_slots           : 동시 운용 포지션 수 (슬롯당 자본을 나눈다)
        """
        stats = self.kelly(trades, strategy)

        if regime_weight <= 0:
            return SizingResult(0.0, "국면 배분 비중 0 (현금 보유 국면)", stats)

        # 1) 이번 거래에서 감수할 자본 비율을 정한다.
        #    켈리 f* 는 "베팅 규모"가 아니라 "잃을 각오를 한 자본 비율"로 해석해야
        #    손절폭이 다른 전략들 사이에서 리스크가 일관된다.
        if stats.usable and stats.f_star > 0:
            risk_fraction = stats.f_star * self.s.kelly_fraction
        else:
            # 표본 부족 구간에서는 전략서의 초보자 기준선(거래당 고정 리스크)을 쓴다
            risk_fraction = self.s.risk_per_trade_pct
        # 켈리 추정이 튀어도 고정 리스크의 0.25~3배를 벗어나지 않게 묶는다
        risk_fraction = max(
            0.25 * self.s.risk_per_trade_pct,
            min(risk_fraction, 3.0 * self.s.risk_per_trade_pct),
        )

        # 2) 손절선까지의 거리로 나눠 실제 주문 금액으로 환산
        stop_distance_pct = max(stop_distance_pct, 0.005)  # 0.5% 미만은 수수료에 묻힌다
        risk_krw = equity * risk_fraction / stop_distance_pct

        # 3) 종목당 최대 노출 상한 (전략서: 단일 자산 50% 초과 금지)
        asset_cap = equity * self.s.max_asset_alloc_pct - current_exposure_krw

        # 4) 국면별 배분 비중을 슬롯 수로 나눈 상한
        slot_cap = equity * regime_weight / max(1, n_slots)

        # 5) 현금 여력 (예비 현금 제외)
        cash_cap = cash_available - equity * self.s.cash_reserve_pct

        krw = min(risk_krw, asset_cap, slot_cap, cash_cap)
        binding = min(
            (risk_krw, f"리스크{risk_fraction * 100:.1f}%"), (asset_cap, "종목상한"),
            (slot_cap, "슬롯상한"), (cash_cap, "현금여력"), key=lambda x: x[0],
        )[1]
        fraction = risk_fraction

        if krw < self.s.min_order_krw:
            return SizingResult(
                0.0,
                f"산출 주문금액 {max(krw, 0):,.0f}원 < 최소주문 {self.s.min_order_krw:,.0f}원 (제약: {binding})",
                stats,
                fraction,
            )
        return SizingResult(float(int(krw)), f"제약: {binding}", stats, fraction)
