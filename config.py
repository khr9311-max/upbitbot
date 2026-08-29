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
    # 신규 상장 코인은 상장 직후 며칠간 거래대금이 일시적으로 폭증했다가 급락하는
    # 경우가 흔하다. 순수 거래대금 순위만 쓰면 이런 "상장빨" 코인이 상위권에 잡혀
    # 소액 계좌가 검증되지 않은 변동성에 노출된다. 최소 경과일 미만은 제외한다.
    universe_min_listing_days: int = field(default_factory=lambda: _int("UNIVERSE_MIN_LISTING_DAYS", 14))

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
    # 국면 판정이 몇 봉 연속으로 같은 라벨을 내야 실제 전환으로 인정할지.
    # 1이면 확인 절차 없이 매 봉 판정을 그대로 반영한다(휩소에 취약).
    regime_confirm_bars: int = field(default_factory=lambda: _int("REGIME_CONFIRM_BARS", 2))

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
    # 그리드가 청산된 직후 같은 봉/다음 봉에서 바로 재개설되는 것을 막는다. 밴드
    # 경계가 매 세션 재계산되는데 가격이 그 경계 근처에서 오르내리면 체결 없이
    # 청산-재개설만 반복하는 "세션 스팸"이 발생하기 때문이다.
    grid_reopen_cooldown_min: float = field(default_factory=lambda: _float("GRID_REOPEN_COOLDOWN_MIN", 240))

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

    # ---------- 일봉 추세 엔진 (메인 자본) ----------
    # 백테스트 실측: 2.7년, 5종목 평균 - MA50 초과성과 +122%p(5/5종목 승리),
    # MA200 -27%p(1/5종목 승리). 짧을수록 반응은 빠르지만 휩소 위험도 커지므로
    # 50~75 구간을 기본값으로 둔다. 종목/기간에 따라 최적값이 크게 갈리므로
    # (과적합 위험) 실거래 투입 전 별도 기간으로 반드시 재검증할 것.
    trend_enabled: bool = field(default_factory=lambda: _bool("TREND_ENABLED", True))
    trend_ma_len: int = field(default_factory=lambda: _int("TREND_MA_LEN", 60))
    trend_universe_size: int = field(default_factory=lambda: _int("TREND_UNIVERSE_SIZE", 3))
    trend_alloc_pct: float = field(default_factory=lambda: _float("TREND_ALLOC_PCT", 0.80))

    # ---------- 단타 레이어 (소액 별도 자본) ----------
    # 백테스트 실측(30분봉, 변동성 상위 30%, 지정가 가정): XRP/SUI/ONDO 의
    # RSI 과매도·볼린저 하단 이탈 반등에서만 비용 차감 후 순양(+)이 확인됐다.
    # 표본이 약 2주로 짧아 신뢰도가 낮으므로 SCALP_ALLOC_PCT 로 노출을 강하게 제한한다.
    scalp_enabled: bool = field(default_factory=lambda: _bool("SCALP_ENABLED", True))
    scalp_alloc_pct: float = field(default_factory=lambda: _float("SCALP_ALLOC_PCT", 0.15))
    scalp_timeframe: int = field(default_factory=lambda: _int("SCALP_TIMEFRAME", 30))
    scalp_watchlist_size: int = field(default_factory=lambda: _int("SCALP_WATCHLIST_SIZE", 3))
    scalp_max_spread_pct: float = field(default_factory=lambda: _float("SCALP_MAX_SPREAD_PCT", 0.0015))
    scalp_min_trade_price_24h: float = field(
        default_factory=lambda: _float("SCALP_MIN_TRADE_PRICE_24H", 5_000_000_000)
    )
    scalp_rsi_max: float = field(default_factory=lambda: _float("SCALP_RSI_MAX", 30))
    scalp_atr_percentile_min: float = field(default_factory=lambda: _float("SCALP_ATR_PERCENTILE_MIN", 0.7))
    scalp_hold_bars: int = field(default_factory=lambda: _int("SCALP_HOLD_BARS", 1))
    scalp_max_hold_bars: int = field(default_factory=lambda: _int("SCALP_MAX_HOLD_BARS", 4))
    scalp_take_profit_pct: float = field(default_factory=lambda: _float("SCALP_TAKE_PROFIT_PCT", 0.006))
    scalp_stop_loss_pct: float = field(default_factory=lambda: _float("SCALP_STOP_LOSS_PCT", 0.008))
    scalp_watchlist_refresh_hours: float = field(
        default_factory=lambda: _float("SCALP_WATCHLIST_REFRESH_HOURS", 6)
    )

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
