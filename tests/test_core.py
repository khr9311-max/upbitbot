"""
핵심 로직 단위 테스트.

    python tests/test_core.py        (pytest 없이 그대로 실행 가능)
    python -m pytest tests -q        (pytest 가 있으면 이쪽도 동작)

네트워크를 타지 않고 합성 시계열로만 검증하므로 CI 나 EC2 에서도 즉시 돌릴 수 있다.
"""
from __future__ import annotations

import copy
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config import settings  # noqa: E402
from core.indicators import adx, atr, bollinger, build_features, chandelier_exit_long, rsi  # noqa: E402
from core.regime import (  # noqa: E402
    LOW_VOL_RANGE,
    STRONG_BEAR,
    STRONG_BULL,
    RegimeClassifier,
)
from core.sizing import PositionSizer  # noqa: E402
from core.state import BotState, Position, Trade  # noqa: E402
from core.upbit_client import fmt_volume, krw_tick_size, round_to_tick  # noqa: E402
from strategies.base import BUY_LIMIT, CANCEL, MarketView  # noqa: E402
from strategies.daily_trend import DailyTrendStrategy, compute_daily_ma  # noqa: E402
from strategies.dca import SmartDcaStrategy  # noqa: E402
from strategies.grid import AtrGridStrategy  # noqa: E402
from strategies.scalp import ScalpMeanReversionStrategy  # noqa: E402
from strategies.trend import TrendBreakoutStrategy  # noqa: E402


# --------------------------------------------------------------------------- #
# 합성 캔들 생성기
# --------------------------------------------------------------------------- #
def make_candles(n: int = 400, drift: float = 0.0, vol: float = 0.01, seed: int = 7) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    steps = rng.normal(drift, vol, n)
    close = 100.0 * np.exp(np.cumsum(steps))
    high = close * (1 + np.abs(rng.normal(0, vol / 2, n)))
    low = close * (1 - np.abs(rng.normal(0, vol / 2, n)))
    open_ = np.concatenate([[close[0]], close[:-1]])
    idx = pd.date_range("2025-01-01", periods=n, freq="15min")
    return pd.DataFrame(
        {"open": open_, "high": np.maximum(high, np.maximum(open_, close)),
         "low": np.minimum(low, np.minimum(open_, close)), "close": close,
         "volume": rng.lognormal(3, 0.4, n), "value": rng.lognormal(10, 0.4, n)},
        index=idx,
    )


# --------------------------------------------------------------------------- #
# 지표
# --------------------------------------------------------------------------- #
def test_rsi_bounds_and_extremes():
    df = make_candles(300, vol=0.02)
    r = rsi(df["close"], 14).dropna()
    assert r.between(0, 100).all(), "RSI 는 0~100 범위를 벗어날 수 없다"

    # 단조 상승 시계열은 RSI 100 에 수렴해야 한다
    up = pd.Series(np.linspace(100, 200, 100))
    assert rsi(up, 14).iloc[-1] > 99.9
    down = pd.Series(np.linspace(200, 100, 100))
    assert rsi(down, 14).iloc[-1] < 0.1
    # 완전 평탄한 시계열은 NaN 이나 예외 없이 중립값을 준다
    flat = pd.Series([100.0] * 60)
    assert abs(rsi(flat, 14).iloc[-1] - 50.0) < 1e-9


def test_atr_positive_and_scales_with_volatility():
    calm = atr(make_candles(300, vol=0.003, seed=1), 14).dropna()
    wild = atr(make_candles(300, vol=0.03, seed=1), 14).dropna()
    assert (calm > 0).all()
    assert wild.iloc[-1] > calm.iloc[-1], "변동성이 크면 ATR 도 커져야 한다"


def test_adx_range():
    a, plus, minus = adx(make_candles(400, drift=0.002, vol=0.01), 14)
    a = a.dropna()
    assert a.between(0, 100).all()
    assert plus.dropna().ge(0).all() and minus.dropna().ge(0).all()


def test_bollinger_ordering():
    mid, upper, lower, width = bollinger(make_candles(200)["close"], 20, 2.0)
    valid = mid.notna()
    assert (upper[valid] >= mid[valid]).all()
    assert (lower[valid] <= mid[valid]).all()
    assert (width.dropna() >= 0).all()


def test_chandelier_below_recent_high():
    df = make_candles(300, drift=0.002)
    ce = chandelier_exit_long(df, 22, 3.0).dropna()
    recent_high = df["high"].rolling(22).max().dropna()
    assert (ce < recent_high.loc[ce.index]).all(), "샹들리에 출구는 항상 최근 고점 아래에 있어야 한다"


def test_build_features_has_no_lookahead():
    """t 시점 지표는 t 이후 데이터에 영향을 받지 않아야 한다 (백테스트 신뢰성의 전제)."""
    df = make_candles(400, drift=0.001)
    full = build_features(df)
    sliced = build_features(df.iloc[:300])
    for col in ("rsi14", "atr14", "adx", "ema50", "bb_upper", "vol_z"):
        a = float(full[col].iloc[299])
        b = float(sliced[col].iloc[299])
        assert np.isclose(a, b, rtol=1e-9, equal_nan=True), f"{col} 에 미래 정보가 새고 있다"


# --------------------------------------------------------------------------- #
# 호가 단위 / 수량 포맷
# --------------------------------------------------------------------------- #
def test_tick_rounding():
    assert krw_tick_size(150_000_000) == 1000
    assert krw_tick_size(50_000) == 10
    assert krw_tick_size(1_500) == 1
    assert round_to_tick(109_617_432, 1000, "floor") == 109_617_000
    assert round_to_tick(109_617_432, 1000, "ceil") == 109_618_000
    # 소수 호가에서 부동소수 오차가 남지 않아야 한다
    assert round_to_tick(0.123456789, 0.0001, "floor") == 0.1234


def test_fmt_volume():
    assert fmt_volume(0.00010000) == "0.0001"
    assert fmt_volume(1.0) == "1"
    assert fmt_volume(0.123456789) == "0.12345679"


# --------------------------------------------------------------------------- #
# 켈리 사이징
# --------------------------------------------------------------------------- #
def _trade(pnl: float, strategy: str = "trend") -> Trade:
    return Trade("KRW-BTC", strategy, STRONG_BULL, 100, 101, 1, pnl, pnl / 100, 0, 0, "")


def test_kelly_formula():
    sizer = PositionSizer(settings)
    # 승률 60%, 평균이익 2 / 평균손실 1 -> f* = 0.6 - 0.4/2 = 0.4
    trades = [_trade(2.0) for _ in range(30)] + [_trade(-1.0) for _ in range(20)]
    k = sizer.kelly(trades)
    assert k.usable
    assert abs(k.win_rate - 0.6) < 1e-9
    assert abs(k.payoff_ratio - 2.0) < 1e-9
    assert abs(k.f_star - 0.4) < 1e-9


def test_kelly_needs_minimum_sample():
    sizer = PositionSizer(settings)
    assert not sizer.kelly([_trade(1.0) for _ in range(5)]).usable
    # 승리만 있고 패배가 없으면 손익비를 정의할 수 없으므로 사용 불가
    assert not sizer.kelly([_trade(1.0) for _ in range(40)]).usable


def test_sizing_respects_caps_and_minimum():
    sizer = PositionSizer(settings)
    res = sizer.size(
        equity=300_000, cash_available=300_000, regime_weight=0.7,
        stop_distance_pct=0.03, trades=[], n_slots=2,
    )
    assert res.ok
    # 슬롯 상한(30만 x 0.7 / 2 = 105,000) 을 넘을 수 없다
    assert res.krw <= 105_000 + 1
    # 손절 시 손실이 거래당 리스크 한도 부근을 크게 벗어나지 않아야 한다
    assert res.krw * 0.03 <= 300_000 * settings.risk_per_trade_pct * 1.01

    # 현금이 최소 주문금액에 못 미치면 주문하지 않는다
    tiny = sizer.size(
        equity=300_000, cash_available=4_000, regime_weight=0.7,
        stop_distance_pct=0.03, trades=[], n_slots=2,
    )
    assert not tiny.ok and "최소주문" in tiny.reason


def test_sizing_blocked_in_bear_regime():
    sizer = PositionSizer(settings)
    res = sizer.size(
        equity=300_000, cash_available=300_000, regime_weight=0.0,
        stop_distance_pct=0.03, trades=[], n_slots=2,
    )
    assert not res.ok


# --------------------------------------------------------------------------- #
# 국면 분류
# --------------------------------------------------------------------------- #
def test_rule_classifier_detects_trend_and_bear():
    clf = RegimeClassifier(settings)
    bull = build_features(make_candles(400, drift=0.004, vol=0.006, seed=3))
    bear = build_features(make_candles(400, drift=-0.004, vol=0.006, seed=3))
    assert clf._classify_rule(bull).regime == STRONG_BULL
    assert clf._classify_rule(bear).regime == STRONG_BEAR


def test_structural_bear_override_blocks_longs():
    """HMM 이 상승이라 우겨도 4시간봉 구조가 무너졌으면 하락 국면으로 강제 전환된다."""
    cfg = copy.deepcopy(settings)
    cfg.regime_use_hmm = False
    clf = RegimeClassifier(cfg)
    bear = build_features(make_candles(400, drift=-0.005, vol=0.008, seed=11))
    assert clf._structural_bear(bear)
    res = clf.classify("KRW-TEST", bear)
    assert res.regime == STRONG_BEAR
    assert clf.alloc_weight(res.regime) == 0.0, "하락 국면 자본 배분은 0 이어야 한다"


def test_regime_alloc_weights_match_strategy_doc():
    clf = RegimeClassifier(settings)
    assert clf.alloc_weight(STRONG_BULL) > 0
    assert clf.alloc_weight(LOW_VOL_RANGE) > 0
    assert clf.alloc_weight(STRONG_BEAR) == 0.0


# --------------------------------------------------------------------------- #
# 전략
# --------------------------------------------------------------------------- #
def _view(regime_name: str, drift: float = 0.0, vol: float = 0.01, price: float | None = None) -> MarketView:
    from core.regime import RegimeResult

    macro = build_features(make_candles(400, drift=drift * 4, vol=vol * 2, seed=5))
    signal = build_features(make_candles(400, drift=drift, vol=vol, seed=5))
    px = price if price is not None else float(signal["close"].iloc[-1])
    return MarketView(
        market="KRW-TEST", price=px,
        regime=RegimeResult(regime_name, 0.9, "test"),
        macro=macro, signal=signal,
    )


class _Ctx:
    """전략 테스트용 최소 컨텍스트."""

    def __init__(self, krw: float = 100_000, equity: float = 300_000):
        self.settings = settings
        self.client = None
        self.state = BotState()
        self.equity = equity
        self.cash = equity
        self.regime_weight = 0.7
        self.n_slots = 2
        self._krw = krw

    def size_krw(self, stop_distance_pct, strategy, market):
        return self._krw, "테스트"


def test_macro_atr_gives_realistic_stop_distance():
    """15분봉 ATR 로 손절선을 잡으면 수수료 노이즈에 털린다 - 상위 ATR 을 쓰는지 검증."""
    view = _view(STRONG_BULL, drift=0.002)
    assert view.atr_macro > view.atr, "상위 타임프레임 ATR 이 실행 타임프레임보다 커야 한다"
    stop_dist = settings.trend_init_stop_atr * view.atr_macro / view.price
    assert stop_dist > 4 * settings.fee_rate, "손절폭이 왕복 수수료의 4배보다는 커야 한다"


def test_trend_does_not_enter_outside_bull_regime():
    strat = TrendBreakoutStrategy(settings)
    for regime_name in (STRONG_BEAR, LOW_VOL_RANGE):
        assert strat.plan(_view(regime_name, drift=0.002), None, _Ctx()) == []


def test_trend_exits_when_price_breaks_stop():
    strat = TrendBreakoutStrategy(settings)
    view = _view(STRONG_BULL, drift=0.002)
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 1.2, invested_krw=100_000,
                   stop_price=view.price * 1.1, init_stop=view.price * 1.1)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and a.ratio == 1.0 for a in actions), "손절선 이탈 시 전량 청산해야 한다"


def test_dca_refuses_to_average_down_in_downtrend():
    """구조적 약세장 물타기 금지 - 정액 DCA 봇의 치명적 결함을 막는 필터."""
    strat = SmartDcaStrategy(settings)
    view = _view("VOLATILE_PULLBACK", drift=-0.004)
    assert not strat._uptrend_intact(view)
    assert strat.plan(view, None, _Ctx()) == []


def test_dca_step_cap_enforced():
    strat = SmartDcaStrategy(settings)
    view = _view("VOLATILE_PULLBACK", drift=0.002)
    pos = Position(market="KRW-TEST", strategy="dca", volume=1.0,
                   avg_price=view.price, invested_krw=50_000,
                   steps=settings.dca_max_steps,
                   meta={"dca_unit": 20_000, "last_entry_price": view.price * 2})
    assert strat._maybe_add(view, pos, _Ctx()) is None, "최대 진입 횟수를 넘겨 추가매수하면 안 된다"


def test_dca_time_stop_triggers():
    strat = SmartDcaStrategy(settings)
    view = _view("VOLATILE_PULLBACK", drift=0.002)
    pos = Position(market="KRW-TEST", strategy="dca", volume=1.0,
                   avg_price=view.price * 1.05, invested_krw=50_000, steps=1,
                   stop_price=view.price * 0.5, init_stop=view.price * 0.5,
                   meta={"dca_unit": 20_000, "last_entry_price": view.price})
    pos.last_add_at = time.time() - (settings.dca_time_stop_hours + 1) * 3600
    actions = strat.plan(view, pos, _Ctx())
    assert any("타임스톱" in a.reason for a in actions)


def test_grid_spacing_covers_round_trip_cost():
    strat = AtrGridStrategy(settings)
    view = _view(LOW_VOL_RANGE, drift=0.0, vol=0.004)
    spacing = strat.spacing_pct(view)
    assert spacing >= 4 * settings.fee_rate, "그리드 간격이 왕복 수수료를 넘지 못하면 거래할수록 손해다"
    assert spacing >= settings.grid_min_spacing_pct


def test_grid_lower_bound_sits_below_deepest_level():
    strat = AtrGridStrategy(settings)
    view = _view(LOW_VOL_RANGE, drift=0.0, vol=0.004)
    spacing = strat.spacing_pct(view)
    levels = settings.grid_levels
    lower = strat._lower_bound(view, spacing, levels)
    deepest = view.price * (1 - levels * spacing)
    assert lower < deepest, "하단 이탈선이 최저 매수 레벨보다 위면 체결 즉시 손절된다"


def test_grid_liquidates_on_band_break():
    strat = AtrGridStrategy(settings)
    view = _view(LOW_VOL_RANGE, drift=0.0, vol=0.004)
    pos = Position(market="KRW-TEST", strategy="grid", volume=1.0,
                   avg_price=view.price, invested_krw=50_000,
                   grid={"spacing_pct": 0.01, "lower": view.price * 1.05,
                         "upper": view.price * 1.10, "buys": {"u1": {"price": 1, "volume": 1, "level": 1}},
                         "lots": []})
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "cancel" for a in actions), "하단 이탈 시 미체결 매수를 전량 취소해야 한다"
    assert any(a.kind == "sell_market" and a.ratio == 1.0 for a in actions)


# --------------------------------------------------------------------------- #
# 상태 / 리스크
# --------------------------------------------------------------------------- #
def test_position_average_price_math():
    pos = Position(market="KRW-BTC", strategy="dca")
    pos.add_fill(100.0, 1.0, 100.0)
    pos.add_fill(50.0, 1.0, 50.0)
    assert abs(pos.avg_price - 75.0) < 1e-9
    assert pos.steps == 2
    assert abs(pos.unrealized_pct(150.0) - 1.0) < 1e-9


def test_state_roundtrip(tmp_path=None):
    import tempfile

    root = Path(tmp_path) if tmp_path else Path(tempfile.mkdtemp())
    path = root / "state.json"
    st = BotState()
    st.positions["KRW-BTC"] = Position(market="KRW-BTC", strategy="grid", volume=0.5,
                                       avg_price=100.0, grid={"lots": [{"id": "a", "price": 1, "volume": 2}]})
    st.record_trade(_trade(-5.0))
    st.equity_hwm = 310_000
    st.save(path)

    loaded = BotState.load(path)
    assert loaded.positions["KRW-BTC"].avg_price == 100.0
    assert loaded.positions["KRW-BTC"].grid["lots"][0]["id"] == "a"
    assert loaded.equity_hwm == 310_000
    assert loaded.consecutive_losses == 1


def test_risk_circuit_breakers():
    from core.risk import RiskManager

    cfg = copy.deepcopy(settings)
    cfg.kill_switch_file = Path("__no_such_file__")
    rm = RiskManager(cfg)
    st = BotState()
    st.equity_hwm = 300_000
    st.day_start_equity = 300_000
    st.initial_equity = 300_000

    assert rm.evaluate(st, 295_000).can_enter, "정상 구간에서는 진입이 허용돼야 한다"

    # 일일 손실 한도 초과
    st2 = BotState()
    st2.equity_hwm = 300_000
    st2.day_start_equity = 300_000
    v = rm.evaluate(st2, 300_000 * (1 - cfg.daily_loss_limit_pct - 0.001))
    assert not v.can_enter and "일일 손실" in v.reason

    # MDD 초과 -> 강제 청산
    st3 = BotState()
    st3.equity_hwm = 300_000
    st3.day_start_equity = 300_000
    v = rm.evaluate(st3, 300_000 * (1 - cfg.max_drawdown_pct - 0.001))
    assert not v.can_enter and v.force_liquidate and "MDD" in v.reason


def test_daily_reset_clears_halt():
    st = BotState()
    st.day_key = "2000-01-01"
    st.halted = True
    st.halt_reason = "일일 손실 한도"
    assert st.roll_day_if_needed(300_000)
    assert not st.halted and st.day_start_equity == 300_000


# --------------------------------------------------------------------------- #
# 일봉 추세 엔진 (daily_trend)
# --------------------------------------------------------------------------- #
def _daily_view(drift: float, regime_name: str = STRONG_BEAR, seed: int = 9, source: str = "override") -> MarketView:
    """
    regime_name 은 구조적 하락 오버라이드를 테스트하기 위한 값이며 실제 4시간봉과 무관하게 주입한다.
    source 는 "override"(기본값, _structural_bear 규칙에서 나온 강제 청산) 여야 STRONG_BEAR 가
    daily_trend 의 진입 차단/강제 청산을 유발한다 - 일반 HMM/규칙 STRONG_BEAR(source="hmm"/"rule")는
    노이즈로 확인돼 더 이상 강제 청산을 유발하지 않는다.
    """
    from core.regime import RegimeResult

    daily_raw = make_candles(150, drift=drift, vol=0.01, seed=seed)
    daily = compute_daily_ma(build_features(daily_raw), settings.trend_ma_len)
    macro = build_features(make_candles(300, drift=drift, vol=0.008, seed=seed))
    return MarketView(
        market="KRW-TEST", price=float(daily["close"].iloc[-1]),
        regime=RegimeResult(regime_name, 0.9, source),
        macro=macro, signal=macro, daily=daily,
    )


def test_daily_trend_enters_when_above_ma():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BULL)
    actions = strat.plan(view, None, _Ctx())
    assert any(a.kind == "buy_market" for a in actions), "종가가 MA 위에 있으면 진입해야 한다"


def test_daily_trend_no_entry_below_ma():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=-0.006, regime_name="LOW_VOL_RANGE")
    assert strat.plan(view, None, _Ctx()) == []


def test_daily_trend_no_entry_in_structural_bear():
    """추세만 보면 진입 조건이어도, 구조적 하락 오버라이드가 떠 있으면 진입하면 안 된다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BEAR)
    assert strat.plan(view, None, _Ctx()) == []


def test_daily_trend_exits_on_ma_breakdown():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=-0.006, regime_name="LOW_VOL_RANGE")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 1.1, invested_krw=100_000)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and a.ratio == 1.0 for a in actions)


def test_daily_trend_exits_on_structural_bear_even_if_above_ma():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BEAR)
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.9, invested_krw=100_000)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and a.ratio == 1.0 for a in actions)


def test_daily_trend_holds_through_noisy_non_override_bear():
    """
    source 가 "override"(_structural_bear 규칙) 가 아닌 일반 HMM/규칙 STRONG_BEAR 판정은
    노이즈로 뒤집히는 경우가 실측에서 확인됐으므로 더 이상 강제 청산을 유발하면 안 된다.
    """
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BEAR, source="hmm")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.9, invested_krw=100_000)
    # 추적 손절선 상향(set_stop)은 나올 수 있다 - 청산만 하지 않으면 된다
    assert not any(a.kind == "sell_market" for a in strat.plan(view, pos, _Ctx()))


def test_daily_trend_entry_sets_protective_stop():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BULL)
    actions = strat.plan(view, None, _Ctx())
    buy = next(a for a in actions if a.kind == "buy_market")
    assert 0 < buy.price < view.price, "매수 액션에 진입가보다 낮은 손절가가 실려야 한다"


def test_daily_trend_hard_stop_forces_exit_even_above_ma():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 1.5, invested_krw=100_000,
                   stop_price=view.price * 1.01)  # 현재가가 이미 손절선 아래
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and a.ratio == 1.0 for a in actions)


def test_daily_trend_holds_when_above_ma_no_bear():
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.9, invested_krw=100_000)
    assert not any(a.kind == "sell_market" for a in strat.plan(view, pos, _Ctx()))


# ---- 추적 손절(샹들리에) / 부분 익절 ---- #
def test_daily_trend_trailing_stop_ratchets_up():
    """보유 중 손절선은 샹들리에 출구까지 올라가야 한다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.9, invested_krw=100_000,
                   stop_price=view.price * 0.5, init_stop=view.price * 0.5)
    actions = strat.plan(view, pos, _Ctx())
    stops = [a for a in actions if a.kind == "set_stop"]
    assert stops, "샹들리에 출구가 기존 손절선보다 높으면 상향해야 한다"
    assert stops[0].price > pos.stop_price


def test_daily_trend_trailing_stop_never_lowers():
    """이미 높은 손절선을 샹들리에가 끌어내리면 안 된다 (래칫)."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    high_stop = view.price * 0.995
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.9, invested_krw=100_000,
                   stop_price=high_stop, init_stop=view.price * 0.5)
    for a in strat.plan(view, pos, _Ctx()):
        if a.kind == "set_stop":
            assert a.price >= high_stop


def test_daily_trend_trailing_uses_position_high_not_market_high():
    """
    시장 22일 최고가를 기준으로 삼으면, 고점에서 밀린 종목에 이 로직을 처음 붙이는
    순간 손절선이 현재가 위로 올라가 즉시 전량 청산된다. 진입 이후 고가(pos.highest)
    기준이어야 기존 포지션에 안전하게 얹을 수 있다.
    """
    from core.indicators import atr as atr_fn

    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    n, k = settings.chandelier_n, settings.chandelier_k
    market_high = float(view.daily["high"].tail(n).max())
    atr_now = float(atr_fn(view.daily, n).dropna().iloc[-1])

    # 진입 이후 고가가 시장 고가보다 낮은 상황(= 고점에서 밀린 뒤 들어온 포지션)
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price, invested_krw=100_000,
                   stop_price=view.price * 0.9, init_stop=view.price * 0.9,
                   highest=view.price)
    assert pos.highest < market_high, "테스트 전제: 진입 이후 고가 < 시장 고가"

    chandelier = strat._chandelier(view, pos)
    market_based = market_high - k * atr_now
    assert chandelier < market_based, "시장 고가 기준보다 낮은(= 더 여유 있는) 손절선이어야 한다"
    assert chandelier < view.price, "추적 손절선이 현재가 위로 올라가면 즉시 청산된다"


def test_daily_trend_partial_take_profit_at_r_multiple():
    """+R 배수 도달 시 부분 익절하고 손절선을 본전 위로 올려야 한다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    init_stop = view.price * 0.8
    # 현재가가 평단 + 1.5R 을 넘도록 평단을 낮게 잡는다
    avg = view.price / (1 + settings.trend_partial_tp_r * 0.2) * 0.98
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=avg, invested_krw=100_000,
                   stop_price=init_stop, init_stop=avg * 0.8)
    actions = strat.plan(view, pos, _Ctx())
    partials = [a for a in actions if a.kind == "sell_market" and a.meta.get("partial")]
    assert partials, "1.5R 도달 시 부분 익절이 나와야 한다"
    assert 0 < partials[0].ratio < 1.0


def test_daily_trend_stop_cooldown_blocks_same_day_reentry():
    """
    손절선 이탈 청산 직후 같은 일봉 안에서는 재진입하면 안 된다 - 손절선 부근
    등락으로 청산/재진입이 반복되는 휩쏘 방지(실측: 백테스트에서 한 종목이
    하루 안에 7회 왕복 청산됨).
    """
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BULL)
    ctx = _Ctx()
    today = str(view.daily.index[-1].date())
    ctx.state.trend_stop_cooldown[view.market] = today

    actions = strat.plan(view, None, ctx)
    assert actions == [], "같은 일봉에 손절 이력이 있으면 재진입을 보류해야 한다"


def test_daily_trend_stop_breach_sets_cooldown():
    """손절선 이탈 청산이 발생하면 그 일봉 날짜로 쿨다운을 기록해야 한다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    ctx = _Ctx()
    pos = Position(market=view.market, strategy="trend", volume=1.0,
                   avg_price=view.price * 0.5, invested_krw=100_000,
                   stop_price=view.price * 1.01, init_stop=view.price * 0.4)
    actions = strat.plan(view, pos, ctx)
    assert any(a.kind == "sell_market" for a in actions)
    assert ctx.state.trend_stop_cooldown.get(view.market) == str(view.daily.index[-1].date())


def test_daily_trend_cooldown_clears_on_new_daily_bar():
    """쿨다운은 그 날짜에만 적용된다 - 다른 날짜로 기록돼 있으면 재진입을 막지 않는다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name=STRONG_BULL)
    ctx = _Ctx()
    ctx.state.trend_stop_cooldown[view.market] = "2000-01-01"  # 오래된 날짜

    actions = strat.plan(view, None, ctx)
    assert any(a.kind == "buy_market" for a in actions), "다른(과거) 날짜의 쿨다운은 오늘 진입을 막으면 안 된다"


def test_daily_trend_stop_breach_beats_partial_take_profit():
    """손절선을 이미 이탈했다면 부분 익절이 아니라 전량 청산이어야 한다."""
    strat = DailyTrendStrategy(settings)
    view = _daily_view(drift=0.006, regime_name="LOW_VOL_RANGE")
    pos = Position(market="KRW-TEST", strategy="trend", volume=1.0,
                   avg_price=view.price * 0.5, invested_krw=100_000,
                   stop_price=view.price * 1.01, init_stop=view.price * 0.4)
    actions = strat.plan(view, pos, _Ctx())
    assert len(actions) == 1
    assert actions[0].kind == "sell_market" and actions[0].ratio == 1.0


# --------------------------------------------------------------------------- #
# 단타 레이어 (scalp)
# --------------------------------------------------------------------------- #
def _scalp_view(oversold: bool, high_vol: bool, regime_name: str = "LOW_VOL_RANGE",
                 ts: float = 1_700_000_000.0, source: str = "override"):
    from core.regime import RegimeResult

    n = 300
    rng = np.random.default_rng(21)
    steps = rng.normal(0.0002, 0.003, n)
    if oversold:
        steps[-3:] = [-0.02, -0.015, -0.01]
    if not high_vol:
        steps = rng.normal(0.0, 0.0005, n)  # 저변동성으로 덮어써 atr_pct 를 낮춘다
    close = 100 * np.exp(np.cumsum(steps))
    high, low = close * 1.002, close * 0.998
    open_ = np.r_[close[0], close[:-1]]
    idx = pd.date_range("2025-01-01", periods=n, freq="30min")
    df = pd.DataFrame(
        {"open": open_, "high": np.maximum(high, close), "low": np.minimum(low, close),
         "close": close, "volume": rng.lognormal(3, 0.3, n), "value": rng.lognormal(10, 0.3, n)},
        index=idx,
    )
    signal = build_features(df)
    macro = build_features(make_candles(300, drift=0.0, vol=0.006, seed=4))
    return MarketView(
        market="KRW-TEST", price=float(signal["close"].iloc[-1]),
        regime=RegimeResult(regime_name, 0.9, source),
        macro=macro, signal=signal, ts=ts,
    )


def test_scalp_enters_on_oversold_high_vol():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=True, high_vol=True)
    actions = strat.plan(view, None, _Ctx())
    assert any(a.kind == BUY_LIMIT for a in actions), "과매도 + 고변동성이면 지정가 진입해야 한다"


def test_scalp_no_entry_low_volatility():
    """전략서와 무관하게 실측: 저변동성 구간은 비용을 이길 움직임 자체가 없어 진입 금지."""
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=True, high_vol=False)
    assert strat.plan(view, None, _Ctx()) == []


def test_scalp_no_entry_not_oversold():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True)
    assert strat.plan(view, None, _Ctx()) == []


def test_scalp_blocks_entry_in_structural_bear():
    """실측 근거: TRUMP 처럼 급락 중 반등을 노리다 물리는 함정을 막는 필수 안전장치."""
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=True, high_vol=True, regime_name=STRONG_BEAR)
    assert strat.plan(view, None, _Ctx()) == []


def test_scalp_pending_entry_cancelled_after_ttl():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=True, high_vol=True, ts=1_700_100_000.0)
    pos = Position(market="KRW-TEST", strategy="scalp",
                   meta={"scalp_pending_uuid": "u1", "scalp_pending_at": view.ts - settings.scalp_timeframe * 60 - 1})
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == CANCEL and a.uuid == "u1" for a in actions)


def test_scalp_pending_entry_waits_within_ttl():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=True, high_vol=True, ts=1_700_100_000.0)
    pos = Position(market="KRW-TEST", strategy="scalp",
                   meta={"scalp_pending_uuid": "u1", "scalp_pending_at": view.ts - 60})
    assert strat.plan(view, pos, _Ctx()) == []


def test_scalp_take_profit():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True)
    entry = view.price / (1 + settings.scalp_take_profit_pct * 2)  # 익절선을 확실히 넘긴 진입가
    pos = Position(market="KRW-TEST", strategy="scalp", volume=1.0, avg_price=entry,
                   invested_krw=entry, opened_at=view.ts)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and "익절" in a.reason for a in actions)


def test_scalp_stop_loss():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True)
    entry = view.price / (1 - settings.scalp_stop_loss_pct * 2)  # 손절선을 확실히 넘긴 진입가
    pos = Position(market="KRW-TEST", strategy="scalp", volume=1.0, avg_price=entry,
                   invested_krw=entry, opened_at=view.ts)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and "손절" in a.reason for a in actions)


def test_scalp_time_stop():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True, ts=1_700_000_000.0)
    pos = Position(market="KRW-TEST", strategy="scalp", volume=1.0, avg_price=view.price,
                   invested_krw=view.price, opened_at=view.ts - (settings.scalp_max_hold_bars + 1) * settings.scalp_timeframe * 60)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" and "타임스톱" in a.reason for a in actions)


def test_scalp_forced_exit_on_structural_bear():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True, regime_name=STRONG_BEAR)
    pos = Position(market="KRW-TEST", strategy="scalp", volume=1.0, avg_price=view.price,
                   invested_krw=view.price, opened_at=view.ts)
    actions = strat.plan(view, pos, _Ctx())
    assert any(a.kind == "sell_market" for a in actions)


def test_scalp_holds_through_noisy_non_override_bear():
    strat = ScalpMeanReversionStrategy(settings)
    view = _scalp_view(oversold=False, high_vol=True, regime_name=STRONG_BEAR, source="hmm")
    pos = Position(market="KRW-TEST", strategy="scalp", volume=1.0, avg_price=view.price,
                   invested_krw=view.price, opened_at=view.ts)
    actions = strat.plan(view, pos, _Ctx())
    assert not any(a.kind == "sell_market" and "하락" in a.reason for a in actions)


# --------------------------------------------------------------------------- #
# 입출금 자본 기준선 동기화 (_sync_cash_flows)
# --------------------------------------------------------------------------- #
class _FakeCashFlowClient:
    def __init__(self, flows):
        self._flows = flows

    def list_krw_cash_flows(self, seen):
        return [f for f in self._flows if f[0] not in seen]


class _FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []

    def send(self, text):
        self.sent.append(text)


def test_sync_cash_flows_first_run_only_seeds_uuids():
    """
    최초 동기화 시점의 initial_equity/equity_hwm 은 이미 계좌의 실제 잔고(=과거 모든
    입출금이 녹아든 값) 기준이므로, 조회된 과거 입출금을 다시 순증감액으로 반영하면
    기준선이 이중으로 틀어진다 (실거래에서 이 버그로 MDD 서킷브레이커가 오발동해
    포지션이 강제청산된 사고가 있었다). uuid만 기록하고 기준선은 그대로여야 한다.
    """
    from core.engine import TradingEngine

    fake = type("Fake", (), {})()
    fake.client = _FakeCashFlowClient([("u1", 100_000.0, "t1"), ("u2", -50_000.0, "t2")])
    fake.notifier = _FakeNotifier()
    fake.state = BotState(initial_equity=100_000.0, equity_hwm=120_000.0, day_start_equity=110_000.0)

    TradingEngine._sync_cash_flows(fake)

    assert fake.state.initial_equity == 100_000.0
    assert fake.state.equity_hwm == 120_000.0
    assert fake.state.day_start_equity == 110_000.0
    assert set(fake.state.seen_cash_flow_uuids) == {"u1", "u2"}
    assert fake.notifier.sent == []


def test_sync_cash_flows_applies_only_new_flows_after_first_run():
    from core.engine import TradingEngine

    fake = type("Fake", (), {})()
    fake.client = _FakeCashFlowClient([("u1", 100_000.0, "t1"), ("u3", 30_000.0, "t3")])
    fake.notifier = _FakeNotifier()
    fake.state = BotState(
        initial_equity=100_000.0, equity_hwm=120_000.0, day_start_equity=110_000.0,
        seen_cash_flow_uuids=["u1"],
    )

    TradingEngine._sync_cash_flows(fake)

    assert fake.state.initial_equity == 130_000.0
    assert fake.state.equity_hwm == 150_000.0
    assert fake.state.day_start_equity == 140_000.0
    assert set(fake.state.seen_cash_flow_uuids) == {"u1", "u3"}
    assert len(fake.notifier.sent) == 1


# --------------------------------------------------------------------------- #
def _run_all() -> int:
    tests = [(n, f) for n, f in sorted(globals().items()) if n.startswith("test_") and callable(f)]
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"  PASS  {name}")
        except Exception as exc:
            failed += 1
            print(f"  FAIL  {name}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} 통과")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(_run_all())
