"""
리스크 통제 계층 - 서킷브레이커.

전략서 4장의 "손실 복구 비대칭성" 원리에 따라, 수익 극대화보다 우선하는
방어선을 둔다. 어떤 전략 신호가 나오든 이 계층이 거부하면 주문은 나가지 않는다.

  1) 킬 스위치 파일 존재       -> 전량 청산 후 정지
  2) 고점 대비 낙폭 > MDD 한도 -> 전량 청산 + 신규 진입 영구 중단(수동 해제 필요)
  3) 당일 손실 > 일일 한도     -> 당일 신규 진입 중단 (KST 자정에 자동 해제)
  4) 연속 손실 N회             -> 쿨다운 시간 동안 신규 진입 중단
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.logger import get_logger
from core.state import BotState

log = get_logger("risk")


@dataclass
class RiskVerdict:
    can_enter: bool
    force_liquidate: bool
    shutdown: bool
    reason: str = ""

    @property
    def blocked(self) -> bool:
        return not self.can_enter


class RiskManager:
    def __init__(self, settings) -> None:
        self.s = settings

    def evaluate(self, state: BotState, equity: float) -> RiskVerdict:
        # 0) 기준선 갱신
        if equity > state.equity_hwm:
            state.equity_hwm = equity
        if state.initial_equity <= 0:
            state.initial_equity = equity
        state.roll_day_if_needed(equity)

        # 1) 킬 스위치
        if self.s.kill_switch_file.exists():
            return RiskVerdict(False, True, True, f"킬 스위치 파일 감지: {self.s.kill_switch_file}")

        # 2) 이미 영구 중단된 상태
        if state.halted and state.halt_reason.startswith("MDD"):
            return RiskVerdict(False, False, False, state.halt_reason)

        # 3) 최대 낙폭(MDD) 서킷브레이커
        if state.equity_hwm > 0:
            drawdown = 1.0 - equity / state.equity_hwm
            if drawdown >= self.s.max_drawdown_pct:
                reason = (
                    f"MDD 서킷브레이커 발동: 고점 {state.equity_hwm:,.0f}원 대비 "
                    f"{drawdown * 100:.1f}% 하락 (한도 {self.s.max_drawdown_pct * 100:.0f}%)"
                )
                if not state.halted:
                    log.critical(reason)
                state.halted = True
                state.halt_reason = reason
                return RiskVerdict(False, True, False, reason)

        # 4) 일일 손실 한도
        if state.day_start_equity > 0:
            day_pnl = equity / state.day_start_equity - 1.0
            if day_pnl <= -self.s.daily_loss_limit_pct:
                reason = (
                    f"일일 손실 한도 도달: {day_pnl * 100:.2f}% "
                    f"(한도 -{self.s.daily_loss_limit_pct * 100:.0f}%) - KST 자정까지 신규 진입 중단"
                )
                if not state.halted:
                    log.warning(reason)
                state.halted = True
                state.halt_reason = reason
                return RiskVerdict(False, False, False, reason)

        # 5) 연속 손실 쿨다운
        if state.consecutive_losses >= self.s.consecutive_loss_limit:
            if state.cooldown_until <= time.time():
                state.cooldown_until = time.time() + self.s.cooldown_minutes * 60
                state.consecutive_losses = 0
                log.warning(
                    "연속 손실 %d회 - %.0f분 쿨다운 시작", self.s.consecutive_loss_limit, self.s.cooldown_minutes
                )
        if state.cooldown_until > time.time():
            remain = (state.cooldown_until - time.time()) / 60
            return RiskVerdict(False, False, False, f"연속 손실 쿨다운 중 (잔여 {remain:.0f}분)")

        if state.halted:
            state.halted = False
            state.halt_reason = ""
        return RiskVerdict(True, False, False, "")

    # ------------------------------------------------------------------ #
    def drawdown(self, state: BotState, equity: float) -> float:
        if state.equity_hwm <= 0:
            return 0.0
        return max(0.0, 1.0 - equity / state.equity_hwm)

    def day_pnl_pct(self, state: BotState, equity: float) -> float:
        if state.day_start_equity <= 0:
            return 0.0
        return equity / state.day_start_equity - 1.0
