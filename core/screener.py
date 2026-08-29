"""
매매 유니버스 스크리너.

전략서 5장: "슬리피지와 유동성 부족에 따른 손실을 차단하기 위해 거래대금이
풍부한 상위 페어 3~5종목으로 압축 집중한다."

auto 모드는 KRW 마켓 전 종목에서 아래 필터를 통과한 종목만 거래대금 순으로 뽑는다.
  - 업비트 유의종목(market_warning) / 주의종목(market_event.caution) 제외
  - 24시간 누적 거래대금 하한 (기본 300억원) - 잡코인 슬리피지 차단
  - 최소 상장 경과일 (기본 14일) - 상장 직후 거래대금 폭증/급락 코인 차단
  - 호가 스프레드 상한 (기본 0.2%)
  - 사용자 지정 제외 목록
보유 포지션이 있는 종목은 어떤 경우에도 유니버스에서 빠지지 않는다.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from core.logger import get_logger

log = get_logger("screener")

# 스테이블코인 - 변동성이 없어 그리드/추세 어느 전략으로도 수수료를 넘지 못한다
STABLECOINS = ("USDT", "USDC", "DAI", "TUSD", "BUSD", "PYUSD", "FDUSD", "USDS")


@dataclass
class Candidate:
    market: str
    korean_name: str
    trade_price_24h: float
    spread_pct: float


class UniverseScreener:
    def __init__(self, settings, client) -> None:
        self.s = settings
        self.client = client
        self._pairs_cache: tuple[float, dict] | None = None

    def _trading_pairs(self) -> dict:
        if self._pairs_cache and time.time() - self._pairs_cache[0] < 3600:
            return self._pairs_cache[1]
        pairs = self.client.get_trading_pairs()
        self._pairs_cache = (time.time(), pairs)
        return pairs

    # ------------------------------------------------------------------ #
    def select(self, keep: set[str] | None = None, size: int | None = None) -> list[str]:
        keep = keep or set()
        size = size if size is not None else self.s.universe_size
        if self.s.universe_mode == "fixed":
            fixed = [m for m in self.s.universe_fixed if m not in self.s.universe_exclude]
            return list(dict.fromkeys(list(keep) + fixed))

        pairs = self._trading_pairs()
        tickers = self.client.get_krw_tickers()

        candidates: list[Candidate] = []
        skipped_warn = 0
        for market, tk in tickers.items():
            if not market.startswith("KRW-"):
                continue
            if market in self.s.universe_exclude:
                continue
            if self.s.universe_exclude_stablecoins and market.split("-")[1] in STABLECOINS:
                continue
            info = pairs.get(market)
            if info is None:
                continue
            if info["warning"] or (self.s.universe_exclude_caution and info["caution"]):
                skipped_warn += 1
                continue
            if tk["acc_trade_price_24h"] < self.s.universe_min_trade_price_24h:
                continue
            candidates.append(
                Candidate(market, info["korean_name"], tk["acc_trade_price_24h"], 0.0)
            )

        candidates.sort(key=lambda c: -c.trade_price_24h)
        # 스프레드/상장일 검증은 상위 후보에만 적용해 호출 수를 아낀다
        pool = candidates[: max(size * 3, size + 4)]

        skipped_new = 0
        if self.s.universe_min_listing_days > 0 and pool:
            aged = []
            for c in pool:
                if self._is_seasoned(c.market):
                    aged.append(c)
                else:
                    skipped_new += 1
            pool = aged

        if pool:
            books = self.client.get_orderbooks([c.market for c in pool])
            for c in pool:
                c.spread_pct = books.get(c.market, {}).get("spread_pct", 1.0)

        passed = [c for c in pool if c.spread_pct <= self.s.universe_max_spread_pct]
        selected = [c.market for c in passed[: size]]

        # 포지션 보유 종목은 강제 유지
        final = list(dict.fromkeys(list(keep) + selected))

        log.info(
            "유니버스 갱신 | 후보 %d개(유의/주의 %d개, 신규상장 %d개 제외) -> 선정 %s",
            len(candidates), skipped_warn, skipped_new,
            ", ".join(f"{c.market}({c.trade_price_24h / 1e8:,.0f}억)" for c in passed[: size])
            or "없음",
        )
        if not selected:
            log.warning(
                "필터를 통과한 종목이 없습니다. UNIVERSE_MIN_TRADE_PRICE_24H(%s원) 를 낮추거나 "
                "UNIVERSE_MODE=fixed 로 전환을 검토하세요.",
                f"{self.s.universe_min_trade_price_24h:,.0f}",
            )
        return final

    def is_stale(self, updated_at: float) -> bool:
        return time.time() - updated_at > self.s.universe_refresh_hours * 3600

    # ------------------------------------------------------------------ #
    # 단타 레이어 전용 워치리스트
    # ------------------------------------------------------------------ #
    def select_scalp_watchlist(
        self, keep: set[str] | None = None, exclude: set[str] | None = None
    ) -> list[str]:
        """
        단타 레이어용 종목 선정. select() 와 달리 "거래대금 순위"가 아니라
        "스프레드가 좁은 순"으로 뽑는다.

        실측(30분봉 백테스트)에서 TRUMP 처럼 스프레드는 좁지만 이벤트성 급등
        몇 건이 평균을 왜곡하는 가짜 신호와, ENA 처럼 변동성은 크지만 스프레드가
        넓어 지정가 체결 가정 자체가 무너지는 함정을 확인했다. 유동성 하한
        (SCALP_MIN_TRADE_PRICE_24H) 으로 이벤트성 잡코인을 먼저 거르고, 남은
        후보를 스프레드로 줄 세워 "변동성 대비 비용이 가장 유리한" 종목만 남긴다.

        exclude 는 이미 추세 엔진 유니버스에 속한 종목이다. 같은 종목을 두
        전략이 동시에 매매하면 포지션 장부(평단가·수량)가 충돌하므로 겹치지
        않게 만든다.
        """
        keep = keep or set()
        exclude = exclude or set()
        pairs = self._trading_pairs()
        tickers = self.client.get_krw_tickers()

        candidates: list[Candidate] = []
        for market, tk in tickers.items():
            if not market.startswith("KRW-") or market in exclude:
                continue
            if market in self.s.universe_exclude:
                continue
            if self.s.universe_exclude_stablecoins and market.split("-")[1] in STABLECOINS:
                continue
            info = pairs.get(market)
            if info is None or info["warning"] or (self.s.universe_exclude_caution and info["caution"]):
                continue
            if tk["acc_trade_price_24h"] < self.s.scalp_min_trade_price_24h:
                continue
            candidates.append(Candidate(market, info["korean_name"], tk["acc_trade_price_24h"], 0.0))

        if self.s.universe_min_listing_days > 0:
            candidates = [c for c in candidates if self._is_seasoned(c.market)]

        # 유동성 상위 후보에서만 스프레드를 조회해 호출 수를 아낀다
        candidates.sort(key=lambda c: -c.trade_price_24h)
        pool = candidates[: max(self.s.scalp_watchlist_size * 5, 20)]
        if pool:
            books = self.client.get_orderbooks([c.market for c in pool])
            for c in pool:
                c.spread_pct = books.get(c.market, {}).get("spread_pct", 1.0)

        passed = [c for c in pool if c.spread_pct <= self.s.scalp_max_spread_pct]
        passed.sort(key=lambda c: c.spread_pct)
        selected = [c.market for c in passed[: self.s.scalp_watchlist_size]]
        final = list(dict.fromkeys(list(keep - exclude) + selected))

        log.info(
            "단타 워치리스트 갱신 | 후보 %d개(추세유니버스 제외 후) -> 선정 %s",
            len(candidates),
            ", ".join(f"{c.market}(스프레드 {c.spread_pct * 100:.3f}%)" for c in passed[: self.s.scalp_watchlist_size])
            or "없음",
        )
        return final

    def _is_seasoned(self, market: str) -> bool:
        """상장 후 최소 UNIVERSE_MIN_LISTING_DAYS 일이 지났는지 확인한다."""
        try:
            df = self.client.get_candles(market, None, self.s.universe_min_listing_days, use_cache=True)
        except Exception as exc:
            log.debug("%s 상장일 확인 실패(%s) - 통과로 간주", market, exc)
            return True
        return len(df) >= self.s.universe_min_listing_days
