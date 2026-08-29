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
        # 국면 확인(hysteresis) 상태 - 마켓별로 "지금까지 확정된 국면"과
        # "확정 대기 중인 후보 국면이 몇 봉 연속으로 나왔는지"를 추적한다.
        self._confirmed: dict[str, RegimeResult] = {}
        self._pending: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------------ #
    # 공개 API
    # ------------------------------------------------------------------ #
    def classify(self, market: str, feat: pd.DataFrame) -> RegimeResult:
        """
        지표가 계산된 상위 타임프레임 DataFrame 을 받아 현재 국면을 판정한다.

        원시 판정(raw)에 바로 반응하지 않고 REGIME_CONFIRM_BARS 봉 연속으로 같은
        라벨이 나와야 실제 전환으로 인정한다. 전략서 2.1절이 명시하는 "휩소에 의한
        손실 차단"을 실제로 보장하려면 이 확인 절차가 필수적이다 - 절차가 없으면
        HMM 판정이 인접한 봉에서 정반대 라벨(예: 강한 상승 <-> 강한 하락)로 튈 때마다
        그리드/추세 포지션이 불필요하게 청산-재개설을 반복하게 된다.

        단, 구조적 하락 오버라이드는 확인 절차를 건너뛰고 즉시 반영한다 - 실제
        급락 국면에서 확인을 기다리다 대응이 늦어지는 것이 훨씬 위험하기 때문이다.
        """
        raw = self._classify_raw(market, feat)
        confirmed = self._confirm(market, raw)

        if confirmed.regime != STRONG_BEAR and self._structural_bear(feat):
            return RegimeResult(
                regime=STRONG_BEAR,
                confidence=max(confirmed.confidence, 0.6),
                source="override",
                detail={**confirmed.detail, "overridden_from": confirmed.regime},
            )
        return confirmed

    def _classify_raw(self, market: str, feat: pd.DataFrame) -> RegimeResult:
        """확인 절차를 거치기 전의 원시 국면 판정 (HMM 우선, 저신뢰 시 규칙 기반)."""
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
        return result

    def _confirm(self, market: str, raw: RegimeResult) -> RegimeResult:
        need = max(1, self.s.regime_confirm_bars)
        confirmed = self._confirmed.get(market)

        if need <= 1 or confirmed is None:
            self._confirmed[market] = raw
            self._pending.pop(market, None)
            return raw

        if raw.regime == confirmed.regime:
            # 이미 확정된 국면과 같은 라벨 - 신뢰도만 최신값으로 갱신하고 대기열 초기화
            self._pending.pop(market, None)
            self._confirmed[market] = raw
            return raw

        pending = self._pending.get(market)
        if pending is None or pending["regime"] != raw.regime:
            pending = {"regime": raw.regime, "streak": 1, "result": raw}
        else:
            pending["streak"] += 1
            pending["result"] = raw
        self._pending[market] = pending

        if pending["streak"] >= need:
            self._confirmed[market] = raw
            self._pending.pop(market, None)
            return raw

        # 아직 확인 중 - 직전 확정 국면을 유지하되 대기 중인 후보를 참고값으로 남긴다
        return RegimeResult(
            regime=confirmed.regime,
            confidence=confirmed.confidence,
            source=confirmed.source,
            detail={
                **confirmed.detail,
                "pending_regime": raw.regime,
                "pending_streak": pending["streak"],
                "pending_need": need,
            },
        )

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

        labels = self._label_states(obs, states, n_states)
        log.info(
            "%s HMM 재적합 완료 | 상태수=%d | 라벨=%s",
            market,
            n_states,
            {k: v for k, v in sorted(labels.items())},
        )
        return _FittedModel(model=model, mean=mean, std=std, state_labels=labels, fitted_at=time.time())

    @staticmethod
    def _label_states(obs: np.ndarray, states: np.ndarray, n_states: int) -> dict[int, str]:
        """
        각 은닉 상태의 평균 수익률과 평균 변동성으로 해석 가능한 국면 이름을 붙인다.
        obs 는 원본(비표준화) 관측치이고 컬럼 순서는 _FEATURE_COLS 와 같으므로
        0번이 로그수익률, 1번이 ATR 비율이다.

        반드시 원본 스케일을 써야 한다. HMM 학습에 쓰는 z-score 는 "그 재적합
        윈도우 자체의 평균"을 0으로 맞추므로, +200% 폭등장처럼 윈도우 평균 자체가
        매우 높은 구간에서는 절대 수익률이 플러스인 '숨고르기' 상태조차 윈도우
        평균보다 낮다는 이유로 z 값이 음수가 되어 STRONG_BEAR 로 오분류된다.
        절대 로그수익률(부호가 곧 상승/하락)을 기준으로 판정해야 이 문제가 없다.
        """
        stats: dict[int, tuple[float, float]] = {}
        for st in range(n_states):
            mask = states == st
            if not mask.any():
                stats[st] = (0.0, 0.0)
                continue
            rows = obs[mask]
            mean_ret = float(rows[:, 0].mean())
            mean_vol = float(rows[:, 1].mean()) if rows.shape[1] > 1 else 0.0
            stats[st] = (mean_ret, mean_vol)

        vols = np.array([v[1] for v in stats.values()])
        vol_median = float(np.median(vols))

        # 상태들을 평균 수익률 순으로 세워 후보를 정하되, 부호(절대 수익률)가
        # 맞을 때만 상승/하락으로 확정한다. 상대 순위만 쓰면 폭등장에서도 "가장
        # 덜 오른" 상태가 매번 하락으로 찍히는 문제가 생기고, 절대 임계치만 쓰면
        # 저변동성 구간에서 어떤 상태도 기준을 넘지 못해 상승 국면이 사라진다.
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

        # 전부 같은 라벨로 뭉치면 판별력이 없으므로 최고/최저 수익률 상태를 강제 분리.
        # 단, 이때도 부호가 맞지 않으면 억지로 반대쪽 라벨을 붙이지 않는다
        # (폭등장에 하락 국면을 만들어내는 것을 방지).
        if len(set(labels.values())) == 1 and n_states >= 2:
            if stats[best][0] > 0:
                labels[best] = STRONG_BULL
            if stats[worst][0] < 0:
                labels[worst] = STRONG_BEAR
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
