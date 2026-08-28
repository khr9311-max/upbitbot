"""
기술적 지표 계산 모듈.

모든 함수는 오름차순(과거 -> 최신) 정렬된 pandas 객체를 입력으로 받고
같은 인덱스의 Series/DataFrame 을 돌려준다. 외부 TA 라이브러리 의존성이 없어
AWS ARM 인스턴스에서도 추가 빌드 없이 동작한다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# --------------------------------------------------------------------------- #
# 이동평균 / 모멘텀
# --------------------------------------------------------------------------- #
def sma(series: pd.Series, period: int) -> pd.Series:
    return series.rolling(period, min_periods=period).mean()


def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False, min_periods=period).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder RSI (지수가중 평균 방식)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = (-delta).clip(lower=0.0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - (100.0 / (1.0 + rs))
    # 평균손실 0 -> 상승만 있었으므로 100, 상승/하락 모두 0 -> 중립 50
    out = out.mask((avg_loss == 0.0) & (avg_gain > 0.0), 100.0)
    out = out.mask((avg_loss == 0.0) & (avg_gain == 0.0), 50.0)
    return out


# --------------------------------------------------------------------------- #
# 변동성
# --------------------------------------------------------------------------- #
def true_range(df: pd.DataFrame) -> pd.Series:
    prev_close = df["close"].shift(1)
    ranges = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    """Wilder ATR."""
    tr = true_range(df)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def bollinger(close: pd.Series, period: int = 20, std_mult: float = 2.0):
    """(중심선, 상단, 하단, 밴드폭비율) 을 반환."""
    mid = sma(close, period)
    std = close.rolling(period, min_periods=period).std(ddof=0)
    upper = mid + std_mult * std
    lower = mid - std_mult * std
    width = (upper - lower) / mid.replace(0.0, np.nan)
    return mid, upper, lower, width


def bb_width_percentile(width: pd.Series, lookback: int = 120) -> pd.Series:
    """현재 밴드폭이 최근 lookback 구간에서 차지하는 백분위(0~1). 스퀴즈 판정용."""
    return width.rolling(lookback, min_periods=max(20, lookback // 4)).rank(pct=True)


def adx(df: pd.DataFrame, period: int = 14):
    """(ADX, +DI, -DI) 를 반환하는 Wilder ADX."""
    up_move = df["high"].diff()
    down_move = -df["low"].diff()

    plus_dm = np.where((up_move > down_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((down_move > up_move) & (down_move > 0), down_move, 0.0)

    plus_dm = pd.Series(plus_dm, index=df.index)
    minus_dm = pd.Series(minus_dm, index=df.index)

    atr_ = true_range(df).ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    safe_atr = atr_.replace(0.0, np.nan)

    plus_di = 100.0 * plus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / safe_atr
    minus_di = 100.0 * minus_dm.ewm(alpha=1 / period, adjust=False, min_periods=period).mean() / safe_atr

    dx = 100.0 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0.0, np.nan)
    adx_ = dx.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    return adx_, plus_di, minus_di


def realized_vol(close: pd.Series, period: int = 20) -> pd.Series:
    """로그 수익률 기준 실현 변동성(표준편차)."""
    return log_returns(close).rolling(period, min_periods=period).std(ddof=0)


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


# --------------------------------------------------------------------------- #
# 청산 기준선
# --------------------------------------------------------------------------- #
def chandelier_exit_long(df: pd.DataFrame, period: int = 22, k: float = 3.0) -> pd.Series:
    """롱 포지션용 샹들리에 출구: max(High, n) - k * ATR(n)."""
    highest = df["high"].rolling(period, min_periods=period).max()
    return highest - k * atr(df, period)


# --------------------------------------------------------------------------- #
# 통합 피처 생성
# --------------------------------------------------------------------------- #
def build_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    캔들 DataFrame(open/high/low/close/volume/value) 에 지표 컬럼을 붙여 돌려준다.
    HMM 국면 분류와 각 실행 전략이 공통으로 사용하는 피처 집합이다.
    """
    out = df.copy()
    close = out["close"]

    out["ret"] = log_returns(close)
    out["ema20"] = ema(close, 20)
    out["ema50"] = ema(close, 50)
    out["ema200"] = ema(close, 200)
    out["rsi14"] = rsi(close, 14)
    out["atr14"] = atr(out, 14)
    out["atr_ratio"] = out["atr14"] / close.replace(0.0, np.nan)

    mid, upper, lower, width = bollinger(close, 20, 2.0)
    out["bb_mid"] = mid
    out["bb_upper"] = upper
    out["bb_lower"] = lower
    out["bb_width"] = width
    out["bb_pct"] = bb_width_percentile(width, 120)

    adx_, plus_di, minus_di = adx(out, 14)
    out["adx"] = adx_
    out["plus_di"] = plus_di
    out["minus_di"] = minus_di

    out["vol20"] = realized_vol(close, 20)
    out["ret_ma20"] = out["ret"].rolling(20, min_periods=20).mean()
    out["ema_gap"] = (close - out["ema50"]) / out["ema50"].replace(0.0, np.nan)

    vol_mean = out["volume"].rolling(20, min_periods=20).mean()
    vol_std = out["volume"].rolling(20, min_periods=20).std(ddof=0)
    out["vol_z"] = (out["volume"] - vol_mean) / vol_std.replace(0.0, np.nan)

    return out


def last_valid(df: pd.DataFrame, column: str, default: float = float("nan")) -> float:
    """마지막 유효값을 float 로 안전하게 꺼낸다."""
    if column not in df.columns or df.empty:
        return default
    series = df[column].dropna()
    if series.empty:
        return default
    return float(series.iloc[-1])
