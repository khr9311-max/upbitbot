"""
전역 설정 로더.

.env 파일과 환경변수에서 모든 운용 파라미터를 읽어들인다.
기본값은 "시드 30만원 미만 / 거래대금 상위 자동선정 / 공격형 리스크" 프로파일 기준.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _str(key: str, default: str = "") -> str:
    return (os.getenv(key) or default).strip()


def _bool(key: str, default: bool = False) -> bool:
    return _str(key, str(default)).lower() in ("1", "true", "t", "yes", "y", "on")


def _int(key: str, default: int) -> int:
    try:
        return int(float(_str(key, str(default))))
    except ValueError:
        return default


def _float(key: str, default: float) -> float:
    try:
        return float(_str(key, str(default)))
    except ValueError:
        return default


def _list(key: str, default: str = "") -> list[str]:
    return [x.strip().upper() for x in _str(key, default).split(",") if x.strip()]


def _floats(key: str, default: str) -> list[float]:
    out: list[float] = []
    for x in _str(key, default).split(","):
        x = x.strip()
        if not x:
            continue
        try:
            out.append(float(x))
        except ValueError:
            pass
    return out


@dataclass
class Settings:
    # ---------- 인증 / 실행 모드 ----------
    access_key: str = field(default_factory=lambda: _str("UPBIT_ACCESS_KEY"))
    secret_key: str = field(default_factory=lambda: _str("UPBIT_SECRET_KEY"))
    environment: str = field(default_factory=lambda: _str("UPBIT_ENVIRONMENT", "kr"))
    dry_run: bool = field(default_factory=lambda: _bool("DRY_RUN", True))
    dry_run_seed_krw: float = field(default_factory=lambda: _float("DRY_RUN_SEED_KRW", 300_000))

    # ---------- 매매 유니버스 ----------
    universe_mode: str = field(default_factory=lambda: _str("UNIVERSE_MODE", "auto").lower())
    universe_size: int = field(default_factory=lambda: _int("UNIVERSE_SIZE", 3))
    universe_fixed: list[str] = field(
        default_factory=lambda: _list("UNIVERSE_FIXED", "KRW-BTC,KRW-ETH,KRW-SOL")
    )
    universe_exclude: list[str] = field(default_factory=lambda: _list("UNIVERSE_EXCLUDE"))
    # 스테이블코인은 변동성이 없어 어떤 전략도 수익을 낼 수 없으므로 기본 제외
    universe_exclude_stablecoins: bool = field(
        default_factory=lambda: _bool("UNIVERSE_EXCLUDE_STABLECOINS", True)
    )
    # 업비트 "주의" 표시 종목(급등락/입금량 급증 등) 제외 여부. 유의(warning) 종목은 항상 제외
    universe_exclude_caution: bool = field(
        default_factory=lambda: _bool("UNIVERSE_EXCLUDE_CAUTION", True)
    )
    universe_min_trade_price_24h: float = field(
        default_factory=lambda: _float("UNIVERSE_MIN_TRADE_PRICE_24H", 30_000_000_000)
    )
    universe_refresh_hours: float = field(default_factory=lambda: _float("UNIVERSE_REFRESH_HOURS", 6))
    universe_max_spread_pct: float = field(default_factory=lambda: _float("UNIVERSE_MAX_SPREAD_PCT", 0.002))

    # ---------- 자본 / 포지션 ----------
    max_concurrent_positions: int = field(default_factory=lambda: _int("MAX_CONCURRENT_POSITIONS", 2))
    max_asset_alloc_pct: float = field(default_factory=lambda: _float("MAX_ASSET_ALLOC_PCT", 0.5))
    min_order_krw: float = field(default_factory=lambda: _float("MIN_ORDER_KRW", 5000))
    cash_reserve_pct: float = field(default_factory=lambda: _float("CASH_RESERVE_PCT", 0.05))

    # ---------- 리스크 관리 ----------
    risk_per_trade_pct: float = field(default_factory=lambda: _float("RISK_PER_TRADE_PCT", 0.02))
    kelly_fraction: float = field(default_factory=lambda: _float("KELLY_FRACTION", 0.33))
    kelly_min_trades: int = field(default_factory=lambda: _int("KELLY_MIN_TRADES", 20))
    kelly_lookback_trades: int = field(default_factory=lambda: _int("KELLY_LOOKBACK_TRADES", 60))
    max_drawdown_pct: float = field(default_factory=lambda: _float("MAX_DRAWDOWN_PCT", 0.30))
    daily_loss_limit_pct: float = field(default_factory=lambda: _float("DAILY_LOSS_LIMIT_PCT", 0.08))
    consecutive_loss_limit: int = field(default_factory=lambda: _int("CONSECUTIVE_LOSS_LIMIT", 4))
    cooldown_minutes: float = field(default_factory=lambda: _float("COOLDOWN_MINUTES", 120))

    # ---------- 1계층: 국면 분류 ----------
    regime_timeframe: int = field(default_factory=lambda: _int("REGIME_TIMEFRAME", 240))
    regime_candles: int = field(default_factory=lambda: _int("REGIME_CANDLES", 600))
    regime_n_states: int = field(default_factory=lambda: _int("REGIME_N_STATES", 4))
    regime_refit_hours: float = field(default_factory=lambda: _float("REGIME_REFIT_HOURS", 24))
    regime_min_confidence: float = field(default_factory=lambda: _float("REGIME_MIN_CONFIDENCE", 0.55))
    regime_use_hmm: bool = field(default_factory=lambda: _bool("REGIME_USE_HMM", True))

    # ---------- 2계층: 미시 실행 ----------
    signal_timeframe: int = field(default_factory=lambda: _int("SIGNAL_TIMEFRAME", 15))
    signal_candles: int = field(default_factory=lambda: _int("SIGNAL_CANDLES", 200))
    loop_interval_sec: float = field(default_factory=lambda: _float("LOOP_INTERVAL_SEC", 30))
    fee_rate: float = field(default_factory=lambda: _float("FEE_RATE", 0.0005))
    slippage_pct: float = field(default_factory=lambda: _float("SLIPPAGE_PCT", 0.0007))

    # ---------- 추세 국면: 변동성 돌파 + 샹들리에 출구 ----------
    trend_breakout_k: float = field(default_factory=lambda: _float("TREND_BREAKOUT_K", 0.5))
    trend_adx_min: float = field(default_factory=lambda: _float("TREND_ADX_MIN", 20))
    trend_adx_strong: float = field(default_factory=lambda: _float("TREND_ADX_STRONG", 30))
    trend_rsi_max: float = field(default_factory=lambda: _float("TREND_RSI_MAX", 80))
    chandelier_n: int = field(default_factory=lambda: _int("TREND_CHANDELIER_N", 22))
    chandelier_k: float = field(default_factory=lambda: _float("TREND_CHANDELIER_K", 3.0))
    chandelier_k_strong: float = field(default_factory=lambda: _float("TREND_CHANDELIER_K_STRONG", 4.0))
    trend_init_stop_atr: float = field(default_factory=lambda: _float("TREND_INIT_STOP_ATR", 2.0))
    trend_partial_tp_r: float = field(default_factory=lambda: _float("TREND_PARTIAL_TP_R", 1.5))
    trend_partial_tp_ratio: float = field(default_factory=lambda: _float("TREND_PARTIAL_TP_RATIO", 0.4))

    # ---------- 횡보 국면: ATR 적응형 동적 그리드 ----------
    grid_levels: int = field(default_factory=lambda: _int("GRID_LEVELS", 3))
    grid_alpha: float = field(default_factory=lambda: _float("GRID_ALPHA", 1.0))
    grid_alpha_min: float = field(default_factory=lambda: _float("GRID_ALPHA_MIN", 0.6))
    grid_alpha_max: float = field(default_factory=lambda: _float("GRID_ALPHA_MAX", 2.0))
    grid_min_spacing_pct: float = field(default_factory=lambda: _float("GRID_MIN_SPACING_PCT", 0.004))
    grid_order_ttl_min: float = field(default_factory=lambda: _float("GRID_ORDER_TTL_MIN", 180))
    grid_break_atr: float = field(default_factory=lambda: _float("GRID_BREAK_ATR", 1.5))

    # ---------- 조정 국면: 한도 제어형 스마트 DCA ----------
    dca_max_steps: int = field(default_factory=lambda: _int("DCA_MAX_STEPS", 3))
    dca_step_atr: float = field(default_factory=lambda: _float("DCA_STEP_ATR", 1.0))
    dca_size_multipliers: list[float] = field(
        default_factory=lambda: _floats("DCA_SIZE_MULTIPLIERS", "1.0,1.3,1.6")
    )
    dca_rsi_max: float = field(default_factory=lambda: _float("DCA_RSI_MAX", 32))
    dca_tp_pct: float = field(default_factory=lambda: _float("DCA_TP_PCT", 0.03))
    dca_sl_atr: float = field(default_factory=lambda: _float("DCA_SL_ATR", 2.5))
    dca_time_stop_hours: float = field(default_factory=lambda: _float("DCA_TIME_STOP_HOURS", 48))

    # ---------- 하락 국면 ----------
    bear_force_exit: bool = field(default_factory=lambda: _bool("BEAR_FORCE_EXIT", True))

    # ---------- 운영 ----------
    telegram_bot_token: str = field(default_factory=lambda: _str("TELEGRAM_BOT_TOKEN"))
    telegram_chat_id: str = field(default_factory=lambda: _str("TELEGRAM_CHAT_ID"))
    log_level: str = field(default_factory=lambda: _str("LOG_LEVEL", "INFO").upper())
    state_file: Path = field(default_factory=lambda: BASE_DIR / _str("STATE_FILE", "data/state.json"))
    kill_switch_file: Path = field(default_factory=lambda: BASE_DIR / _str("KILL_SWITCH_FILE", "data/STOP"))
    log_dir: Path = field(default_factory=lambda: BASE_DIR / _str("LOG_DIR", "logs"))
    heartbeat_minutes: float = field(default_factory=lambda: _float("HEARTBEAT_MINUTES", 240))

    # ---------- 국면별 자본 배분 비중 (전략서 2장 표) ----------
    alloc_by_regime: dict = field(
        default_factory=lambda: {
            "STRONG_BULL": _float("ALLOC_STRONG_BULL", 0.70),
            "LOW_VOL_RANGE": _float("ALLOC_LOW_VOL_RANGE", 0.80),
            "VOLATILE_PULLBACK": _float("ALLOC_VOLATILE_PULLBACK", 0.45),
            "STRONG_BEAR": _float("ALLOC_STRONG_BEAR", 0.0),
        }
    )

    def validate(self) -> list[str]:
        """치명적인 설정 오류 목록을 반환한다 (빈 리스트면 정상)."""
        errs: list[str] = []
        if not self.dry_run and (not self.access_key or not self.secret_key):
            errs.append("실거래 모드인데 UPBIT_ACCESS_KEY / UPBIT_SECRET_KEY 가 비어 있습니다.")
        if self.min_order_krw < 5000:
            errs.append("업비트 KRW 마켓 최소 주문금액은 5,000원입니다. MIN_ORDER_KRW >= 5000 으로 설정하세요.")
        if not 0 < self.kelly_fraction <= 1:
            errs.append("KELLY_FRACTION 은 0 초과 1 이하여야 합니다.")
        if not 0 < self.max_drawdown_pct < 1:
            errs.append("MAX_DRAWDOWN_PCT 는 0 과 1 사이여야 합니다.")
        if self.max_concurrent_positions < 1:
            errs.append("MAX_CONCURRENT_POSITIONS 는 1 이상이어야 합니다.")
        if len(self.dca_size_multipliers) < self.dca_max_steps:
            errs.append("DCA_SIZE_MULTIPLIERS 개수가 DCA_MAX_STEPS 보다 적습니다.")
        if self.universe_size < 1:
            errs.append("UNIVERSE_SIZE 는 1 이상이어야 합니다.")
        if self.universe_mode not in ("auto", "fixed"):
            errs.append("UNIVERSE_MODE 는 auto 또는 fixed 여야 합니다.")
        return errs


settings = Settings()
