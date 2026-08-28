"""
1계층 - 시장 국면(Market Regime) 분류기.

전략서 2.1절에 따라 은닉 마르코프 모델(HMM)로 관측 불가능한 시장 체제를
로그수익률 / 변동성비율 / 실현변동성 / 거래량 z-score 분포로부터 역추론한다.

hmmlearn 이 없거나 적합에 실패하면 규칙 기반 분류기로 자동 폴백하므로
봇이 국면 판별 실패로 멈추는 일은 없다. 또한 HMM 결과와 무관하게
"4시간봉 구조적 하락" 조건이 성립하면 강제로 하락 국면으로 덮어써서
모델 오분류로 하락장에 롱을 잡는 사고를 차단한다.
"""
from __future__ import annotations

import time
import warnings
from dataclasses import dataclass, field
from typing import Any

import numpy as np
import pandas as pd

from core.logger import get_logger

log = get_logger("regime")

STRONG_BULL = "STRONG_BULL"
STRONG_BEAR = "STRONG_BEAR"
LOW_VOL_RANGE = "LOW_VOL_RANGE"
VOLATILE_PULLBACK = "VOLATILE_PULLBACK"

ALL_REGIMES = (STRONG_BULL, LOW_VOL_RANGE, VOLATILE_PULLBACK, STRONG_BEAR)

_FEATURE_COLS = ("ret", "atr_ratio", "vol20", "vol_z")

try:  # hmmlearn 은 선택적 의존성
    from hmmlearn.hmm import GaussianHMM

    _HMM_AVAILABLE = True
except Exception:  # pragma: no cover - 환경 의존
    GaussianHMM = None  # type: ignore[assignment]
    _HMM_AVAILABLE = False


@dataclass
class RegimeResult:
    regime: str
    confidence: float
    source: str  # "hmm" | "rule" | "override"
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def is_bear(self) -> bool:
        return self.regime == STRONG_BEAR


@dataclass
class _FittedModel:
    model: Any
    mean: np.ndarray
    std: np.ndarray
    state_labels: dict[int, str]
    fitted_at: float


class RegimeClassifier:
    """마켓별로 HMM 을 유지하며 주기적으로 롤링 재적합한다."""

    def __init__(self, settings) -> None:
        self.s = settings
        self._models: dict[str, _FittedModel] = {}
        self._warned_no_hmm = False

    # ------------------------------------------------------------------ #
    # 공개 API
    # ------------------------------------------------------------------ #
    def classify(self, market: str, feat: pd.DataFrame) -> RegimeResult:
        """지표가 계산된 상위 타임프레임 DataFrame 을 받아 현재 국면을 판정한다."""
        rule = self._classify_rule(feat)

        result = rule
        if self.s.regime_use_hmm and _HMM_AVAILABLE:
            hmm_res = self._classify_hmm(market, feat)
            if hmm_res is not None and hmm_res.confidence >= self.s.regime_min_confidence:
                result = hmm_res
            elif hmm_res is not None:
                # 확신이 낮으면 규칙 기반을 채택하되 HMM 판정을 참고값으로 남긴다
                rule.detail["hmm_regime"] = hmm_res.regime
                rule.detail["hmm_confidence"] = round(hmm_res.confidence, 3)
        elif self.s.regime_use_hmm and not self._warned_no_hmm:
            log.warning("hmmlearn 미설치 - 규칙 기반 국면 분류기로 동작합니다. (pip install hmmlearn)")
            self._warned_no_hmm = True

        # 구조적 하락 오버라이드: 어떤 모델이 뭐라 하든 롱 진입을 차단한다
        if result.regime != STRONG_BEAR and self._structural_bear(feat):
            return RegimeResult(
                regime=STRONG_BEAR,
                confidence=max(result.confidence, 0.6),
                source="override",
                detail={**result.detail, "overridden_from": result.regime},
            )
        return result

    def alloc_weight(self, regime: str) -> float:
        """국면별 권장 자본 배분 비중 (전략서 2장 표)."""
        return float(self.s.alloc_by_regime.get(regime, 0.0))

    # ------------------------------------------------------------------ #
    # HMM 경로
    # ------------------------------------------------------------------ #
    def _classify_hmm(self, market: str, feat: pd.DataFrame) -> RegimeResult | None:
        obs = self._observations(feat)
        if obs is None:
            return None

        fitted = self._models.get(market)
        stale = fitted is None or (time.time() - fitted.fitted_at) > self.s.regime_refit_hours * 3600
        if stale:
            fitted = self._fit(market, obs)
            if fitted is None:
                return None
            self._models[market] = fitted

        z = (obs - fitted.mean) / fitted.std
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                posterior = fitted.model.predict_proba(z)[-1]
        except Exception as exc:  # pragma: no cover - 수치 불안정 방어
            log.warning("%s HMM 추론 실패(%s) - 규칙 기반으로 폴백", market, exc)
            self._models.pop(market, None)
            return None

        # 같은 라벨을 공유하는 상태들의 사후확률을 합산해 라벨 확률을 만든다
        label_prob: dict[str, float] = {}
        for state, prob in enumerate(posterior):
            label = fitted.state_labels.get(state, VOLATILE_PULLBACK)
            label_prob[label] = label_prob.get(label, 0.0) + float(prob)

        regime = max(label_prob, key=label_prob.get)
        return RegimeResult(
            regime=regime,
            confidence=label_prob[regime],
            source="hmm",
            detail={
                "probs": {k: round(v, 3) for k, v in sorted(label_prob.items(), key=lambda kv: -kv[1])},
                "n_states": len(posterior),
            },
        )

    def _observations(self, feat: pd.DataFrame) -> np.ndarray | None:
        cols = [c for c in _FEATURE_COLS if c in feat.columns]
        if len(cols) < 2:
            return None
        obs = feat[cols].tail(self.s.regime_candles).replace([np.inf, -np.inf], np.nan).dropna()
        if len(obs) < 120:
            return None
        return obs.to_numpy(dtype=float)

    def _fit(self, market: str, obs: np.ndarray) -> _FittedModel | None:
        mean = obs.mean(axis=0)
        std = obs.std(axis=0)
        std[std < 1e-12] = 1.0
        z = (obs - mean) / std

        n_states = max(2, min(self.s.regime_n_states, len(obs) // 40))
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                model = GaussianHMM(
                    n_components=n_states,
                    covariance_type="diag",
                    n_iter=200,
                    tol=1e-4,
                    random_state=42,
                )
                model.fit(z)
                states = model.predict(z)
        except Exception as exc:
            log.warning("%s HMM 적합 실패(%s) - 규칙 기반으로 폴백", market, exc)
            return None

        labels = self._label_states(z, states, n_states)
        log.info(
            "%s HMM 재적합 완료 | 상태수=%d | 라벨=%s",
            market,
            n_states,
            {k: v for k, v in sorted(labels.items())},
        )
        return _FittedModel(model=model, mean=mean, std=std, state_labels=labels, fitted_at=time.time())

    @staticmethod
    def _label_states(z: np.ndarray, states: np.ndarray, n_states: int) -> dict[int, str]:
        """
        각 은닉 상태의 평균 수익률과 평균 변동성으로 해석 가능한 국면 이름을 붙인다.
        z 의 컬럼 순서는 _FEATURE_COLS 와 같으므로 0번이 로그수익률, 1번이 ATR 비율이다.
        """
        stats: dict[int, tuple[float, float]] = {}
        for st in range(n_states):
            mask = states == st
            if not mask.any():
                stats[st] = (0.0, 0.0)
                continue
            rows = z[mask]
            mean_ret = float(rows[:, 0].mean())
            mean_vol = float(rows[:, 1].mean()) if rows.shape[1] > 1 else 0.0
            stats[st] = (mean_ret, mean_vol)

        vols = np.array([v[1] for v in stats.values()])
        vol_median = float(np.median(vols))

        # 상태들을 평균 수익률 순으로 세워 상대 비교로 라벨을 붙인다.
        # 절대 임계치를 쓰면 변동성이 낮은 구간에서 어떤 상태도 기준을 넘지 못해
        # 상승 국면이 아예 사라지고 추세 전략이 영원히 대기하는 문제가 생긴다.
        order = sorted(stats, key=lambda st: stats[st][0], reverse=True)
        best, worst = order[0], order[-1]

        labels: dict[int, str] = {}
        for st, (mean_ret, mean_vol) in stats.items():
            if st == best and mean_ret > 0:
                labels[st] = STRONG_BULL
            elif st == worst and mean_ret < 0:
                labels[st] = STRONG_BEAR
            elif mean_vol <= vol_median:
                labels[st] = LOW_VOL_RANGE
            else:
                labels[st] = VOLATILE_PULLBACK

        # 전부 같은 라벨로 뭉치면 판별력이 없으므로 최고/최저 수익률 상태를 강제 분리
        if len(set(labels.values())) == 1 and n_states >= 2:
            order = sorted(stats, key=lambda s: stats[s][0])
            labels[order[0]] = STRONG_BEAR
            labels[order[-1]] = STRONG_BULL
        return labels

    # ------------------------------------------------------------------ #
    # 규칙 기반 폴백
    # ------------------------------------------------------------------ #
    @staticmethod
    def _classify_rule(feat: pd.DataFrame) -> RegimeResult:
        if feat.empty:
            return RegimeResult(VOLATILE_PULLBACK, 0.0, "rule", {"reason": "데이터 없음"})

        row = feat.iloc[-1]

        def g(key: str, default: float = float("nan")) -> float:
            val = row.get(key, default)
            try:
                val = float(val)
            except (TypeError, ValueError):
                return default
            return default if np.isnan(val) else val

        close = g("close")
        ema50 = g("ema50", close)
        ema200 = g("ema200", close)
        adx_ = g("adx", 0.0)
        plus_di = g("plus_di", 0.0)
        minus_di = g("minus_di", 0.0)
        bb_pct = g("bb_pct", 0.5)
        ret_ma = g("ret_ma20", 0.0)
        rsi14 = g("rsi14", 50.0)

        trending = adx_ >= 20.0
        detail = {
            "adx": round(adx_, 1),
            "bb_pct": round(bb_pct, 2),
            "ema50>ema200": bool(ema50 > ema200),
            "rsi": round(rsi14, 1),
        }

        if close > ema50 > ema200 and trending and plus_di > minus_di:
            return RegimeResult(STRONG_BULL, 0.7, "rule", detail)
        if close < ema200 and ema50 < ema200 and (ret_ma < 0 or minus_di > plus_di):
            return RegimeResult(STRONG_BEAR, 0.7, "rule", detail)
        if not trending and bb_pct <= 0.35:
            return RegimeResult(LOW_VOL_RANGE, 0.65, "rule", detail)
        return RegimeResult(VOLATILE_PULLBACK, 0.55, "rule", detail)

    @staticmethod
    def _structural_bear(feat: pd.DataFrame) -> bool:
        """4시간봉 구조가 명백히 무너진 상태인지 - 롱 진입 전면 차단 조건."""
        if feat.empty:
            return False
        row = feat.iloc[-1]
        try:
            close = float(row["close"])
            ema50 = float(row["ema50"])
            ema200 = float(row["ema200"])
            ret_ma = float(row["ret_ma20"])
        except (KeyError, TypeError, ValueError):
            return False
        if any(np.isnan(v) for v in (close, ema50, ema200, ret_ma)):
            return False
        return close < ema200 and ema50 < ema200 and ret_ma < 0
