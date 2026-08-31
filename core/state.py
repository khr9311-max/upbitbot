"""
봇 상태 영속화.

포지션 / 체결이력 / 자본 고점(HWM) / 일일 손익 기준선 / 서킷브레이커 상태를
단일 JSON 파일에 원자적으로 저장한다. EC2 컨테이너가 재시작되어도 포지션과
리스크 카운터가 그대로 이어지도록 하는 것이 목적이다.
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from core.logger import get_logger

log = get_logger("state")

KST = timezone(timedelta(hours=9))
MAX_TRADE_HISTORY = 500


def kst_day_key(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), KST).strftime("%Y-%m-%d")


def kst_now_str(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), KST).strftime("%Y-%m-%d %H:%M:%S")


@dataclass
class Position:
    market: str
    strategy: str  # "trend" | "grid" | "dca"
    regime_at_entry: str = ""
    volume: float = 0.0
    avg_price: float = 0.0
    invested_krw: float = 0.0
    realized_krw: float = 0.0  # 분할 익절로 이미 회수한 손익
    opened_at: float = field(default_factory=time.time)
    last_add_at: float = field(default_factory=time.time)
    steps: int = 0  # DCA 진입 차수 / 그리드 체결 단수
    stop_price: float = 0.0
    init_stop: float = 0.0
    highest: float = 0.0
    partial_taken: bool = False
    grid: dict[str, Any] = field(default_factory=dict)
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.volume > 0

    def value(self, price: float) -> float:
        return self.volume * price

    def unrealized_pct(self, price: float) -> float:
        if self.avg_price <= 0:
            return 0.0
        return price / self.avg_price - 1.0

    def add_fill(self, price: float, volume: float, krw: float) -> None:
        """
        체결 반영. 평단은 "실제로 쓴 원화 / 보유 수량"으로 계산하므로 매수 수수료가
        원가에 포함된다. 체결가만으로 평단을 잡으면 거래마다 매수 수수료(0.05%)가
        손익 계산에서 누락돼 성과와 켈리 통계가 낙관적으로 부풀려진다.
        """
        self.volume += volume
        self.invested_krw += krw
        self.avg_price = self.invested_krw / self.volume if self.volume > 0 else price
        self.steps += 1
        self.last_add_at = time.time()
        self.highest = max(self.highest, price)


@dataclass
class Trade:
    market: str
    strategy: str
    regime: str
    entry_price: float
    exit_price: float
    volume: float
    pnl_krw: float
    pnl_pct: float
    opened_at: float
    closed_at: float
    reason: str = ""

    @property
    def is_win(self) -> bool:
        return self.pnl_krw > 0


@dataclass
class BotState:
    positions: dict[str, Position] = field(default_factory=dict)
    trades: list[Trade] = field(default_factory=list)
    equity_hwm: float = 0.0
    day_key: str = field(default_factory=kst_day_key)
    day_start_equity: float = 0.0
    consecutive_losses: int = 0
    cooldown_until: float = 0.0
    halted: bool = False
    halt_reason: str = ""
    universe: list[str] = field(default_factory=list)
    universe_updated_at: float = 0.0
    # 일봉 추세 엔진 / 단타 레이어가 각자 독립적으로 관리하는 유니버스
    trend_universe: list[str] = field(default_factory=list)
    trend_universe_updated_at: float = 0.0
    scalp_watchlist: list[str] = field(default_factory=list)
    scalp_watchlist_updated_at: float = 0.0
    last_heartbeat_at: float = 0.0
    started_at: float = field(default_factory=time.time)
    initial_equity: float = 0.0
    # market -> 이 시각(epoch seconds) 전까지는 그리드 세션 재개설 금지
    grid_cooldowns: dict[str, float] = field(default_factory=dict)
    # 이미 자본 기준선에 반영한 입출금 uuid (중복 반영 방지)
    seen_cash_flow_uuids: list[str] = field(default_factory=list)

    # ------------------------------------------------------------------ #
    # 직렬화
    # ------------------------------------------------------------------ #
    def to_dict(self) -> dict[str, Any]:
        return {
            "positions": {m: asdict(p) for m, p in self.positions.items()},
            "trades": [asdict(t) for t in self.trades[-MAX_TRADE_HISTORY:]],
            "equity_hwm": self.equity_hwm,
            "day_key": self.day_key,
            "day_start_equity": self.day_start_equity,
            "consecutive_losses": self.consecutive_losses,
            "cooldown_until": self.cooldown_until,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "universe": self.universe,
            "universe_updated_at": self.universe_updated_at,
            "trend_universe": self.trend_universe,
            "trend_universe_updated_at": self.trend_universe_updated_at,
            "scalp_watchlist": self.scalp_watchlist,
            "scalp_watchlist_updated_at": self.scalp_watchlist_updated_at,
            "last_heartbeat_at": self.last_heartbeat_at,
            "started_at": self.started_at,
            "initial_equity": self.initial_equity,
            "grid_cooldowns": self.grid_cooldowns,
            "seen_cash_flow_uuids": self.seen_cash_flow_uuids[-2000:],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "BotState":
        st = cls()
        for market, p in (raw.get("positions") or {}).items():
            st.positions[market] = Position(**p)
        st.trades = [Trade(**t) for t in (raw.get("trades") or [])]
        st.equity_hwm = float(raw.get("equity_hwm", 0.0))
        st.day_key = raw.get("day_key") or kst_day_key()
        st.day_start_equity = float(raw.get("day_start_equity", 0.0))
        st.consecutive_losses = int(raw.get("consecutive_losses", 0))
        st.cooldown_until = float(raw.get("cooldown_until", 0.0))
        st.halted = bool(raw.get("halted", False))
        st.halt_reason = raw.get("halt_reason", "")
        st.universe = list(raw.get("universe") or [])
        st.universe_updated_at = float(raw.get("universe_updated_at", 0.0))
        st.trend_universe = list(raw.get("trend_universe") or [])
        st.trend_universe_updated_at = float(raw.get("trend_universe_updated_at", 0.0))
        st.scalp_watchlist = list(raw.get("scalp_watchlist") or [])
        st.scalp_watchlist_updated_at = float(raw.get("scalp_watchlist_updated_at", 0.0))
        st.last_heartbeat_at = float(raw.get("last_heartbeat_at", 0.0))
        st.started_at = float(raw.get("started_at", time.time()))
        st.initial_equity = float(raw.get("initial_equity", 0.0))
        st.grid_cooldowns = {k: float(v) for k, v in (raw.get("grid_cooldowns") or {}).items()}
        st.seen_cash_flow_uuids = list(raw.get("seen_cash_flow_uuids") or [])
        return st

    # ------------------------------------------------------------------ #
    # 파일 입출력
    # ------------------------------------------------------------------ #
    @classmethod
    def load(cls, path: Path) -> "BotState":
        if not path.exists():
            log.info("상태 파일이 없어 새 상태로 시작합니다: %s", path)
            return cls()
        try:
            return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except Exception as exc:
            backup = path.with_suffix(f".corrupt.{int(time.time())}.json")
            path.rename(backup)
            log.error("상태 파일 파손(%s) - %s 로 백업하고 새로 시작합니다", exc, backup.name)
            return cls()

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    # ------------------------------------------------------------------ #
    # 편의 메서드
    # ------------------------------------------------------------------ #
    def open_positions(self) -> dict[str, Position]:
        return {m: p for m, p in self.positions.items() if p.is_open}

    def record_trade(self, trade: Trade) -> None:
        self.trades.append(trade)
        if len(self.trades) > MAX_TRADE_HISTORY:
            self.trades = self.trades[-MAX_TRADE_HISTORY:]
        if trade.pnl_krw > 0:
            self.consecutive_losses = 0
        else:
            self.consecutive_losses += 1

    def roll_day_if_needed(self, equity: float) -> bool:
        """KST 날짜가 바뀌면 일일 손익 기준선을 갱신한다. 갱신 시 True."""
        today = kst_day_key()
        if today != self.day_key:
            self.day_key = today
            self.day_start_equity = equity
            self.halted = False
            self.halt_reason = ""
            return True
        if self.day_start_equity <= 0:
            self.day_start_equity = equity
        return False
