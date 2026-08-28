"""
매매 엔진 - 2계층 파이프라인의 오케스트레이터.

  1. 시계열 수집 (멀티 타임프레임 캔들)
  2. HMM 국면 판별
  3. 국면별 미시 실행 전략이 액션 생성
  4. 리스크 계층 검증 후 주문 실행
  5. 체결 반영 / 상태 영속화 / 알림

전략 코드는 주문을 직접 내지 않는다. 모든 주문은 이 엔진을 통과하며,
서킷브레이커가 발동하면 어떤 진입 신호도 실행되지 않는다.
"""
from __future__ import annotations

import time
from typing import Any

from core.indicators import build_features
from core.logger import get_logger
from core.notifier import Notifier
from core.regime import STRONG_BEAR, RegimeClassifier, RegimeResult
from core.risk import RiskManager
from core.screener import UniverseScreener
from core.sizing import PositionSizer
from core.state import BotState, Position, Trade, kst_now_str
from core.upbit_client import UpbitClient
from strategies.base import (
    BUY_LIMIT,
    BUY_MARKET,
    CANCEL,
    SELL_LIMIT,
    SELL_MARKET,
    SET_STOP,
    Action,
    Context,
    MarketView,
)
from strategies.dca import SmartDcaStrategy
from strategies.grid import AtrGridStrategy
from strategies.trend import TrendBreakoutStrategy

log = get_logger("engine")

DUST_KRW = 100.0  # 이 금액 미만은 잔여 먼지로 간주


class TradingEngine:
    def __init__(self, settings) -> None:
        self.s = settings
        self.client = UpbitClient(settings)
        self.state = BotState.load(settings.state_file)
        self.sizer = PositionSizer(settings)
        self.risk = RiskManager(settings)
        self.regime = RegimeClassifier(settings)
        self.screener = UniverseScreener(settings, self.client)
        self.notifier = Notifier(settings)
        self.strategies = {
            s.name: s
            for s in (
                TrendBreakoutStrategy(settings),
                AtrGridStrategy(settings),
                SmartDcaStrategy(settings),
            )
        }
        self._running = True
        self._last_regimes: dict[str, str] = {}

    # ================================================================== #
    # 메인 루프
    # ================================================================== #
    def run(self) -> None:
        mode = "모의매매(DRY_RUN)" if self.s.dry_run else "실거래"
        log.info("=" * 78)
        log.info("업비트 AI 자동매매 봇 기동 | 모드: %s | %s", mode, kst_now_str())
        log.info(
            "유니버스=%s | 동시포지션=%d | 켈리계수=%.2f | MDD한도=%.0f%% | 일일손실한도=%.0f%%",
            self.s.universe_mode, self.s.max_concurrent_positions, self.s.kelly_fraction,
            self.s.max_drawdown_pct * 100, self.s.daily_loss_limit_pct * 100,
        )
        log.info("=" * 78)
        self.notifier.send(f"🤖 자동매매 봇 시작 ({mode})\n{kst_now_str()}")

        while self._running:
            started = time.time()
            try:
                self.run_once()
            except KeyboardInterrupt:
                raise
            except Exception as exc:  # 루프는 어떤 예외에도 죽지 않는다
                log.exception("루프 처리 중 예외: %s", exc)
                self.notifier.send(f"⚠️ 봇 예외 발생: {exc}")
            finally:
                self.state.save(self.s.state_file)

            elapsed = time.time() - started
            time.sleep(max(1.0, self.s.loop_interval_sec - elapsed))

    def stop(self) -> None:
        self._running = False

    # ================================================================== #
    # 1회 사이클
    # ================================================================== #
    def run_once(self) -> None:
        universe = self._universe()
        held = set(self.state.positions.keys())
        markets = list(dict.fromkeys(universe + sorted(held)))
        if not markets:
            log.warning("거래 가능한 마켓이 없습니다 - 다음 루프에서 재시도")
            return

        tickers = self.client.get_tickers(markets)
        price_map = {m: t["price"] for m, t in tickers.items()}
        if not price_map:
            log.warning("현재가 조회 실패 - 이번 루프를 건너뜁니다")
            return

        # 모의매매에서는 지정가 체결을 여기서 시뮬레이션한다
        for filled in self.client.poll_paper_fills(price_map):
            self._apply_fill(filled)

        equity = self.client.equity(price_map)
        verdict = self.risk.evaluate(self.state, equity)

        if verdict.force_liquidate:
            log.critical("강제 청산 발동: %s", verdict.reason)
            self.notifier.send(f"🛑 강제 청산\n{verdict.reason}")
            self._liquidate_all(price_map, verdict.reason)
            if verdict.shutdown:
                self.stop()
            return

        cash = self.client.get_balances().get("KRW")
        cash_krw = cash.balance if cash else 0.0
        active = self._active_positions()
        ctx_slots = max(1, self.s.max_concurrent_positions)

        if verdict.blocked:
            log.info("신규 진입 차단: %s", verdict.reason)

        for market in markets:
            try:
                view = self._build_view(market, price_map.get(market, 0.0))
            except Exception as exc:
                log.warning("%s 시장 데이터 준비 실패: %s", market, exc)
                continue
            if view is None:
                continue

            pos = self.state.positions.get(market)
            if pos is not None and not self.s.dry_run:
                self._reconcile(market, pos)
                pos = self.state.positions.get(market)

            self._log_regime_change(market, view.regime)

            strategy = self._strategy_for(view, pos)
            if strategy is None:
                continue

            ctx = Context(
                settings=self.s,
                client=self.client,
                sizer=self.sizer,
                state=self.state,
                equity=equity,
                cash=cash_krw,
                regime_weight=self.regime.alloc_weight(view.regime.regime),
                n_slots=ctx_slots,
            )

            try:
                actions = strategy.plan(view, pos, ctx)
            except Exception as exc:
                log.exception("%s %s 전략 계산 실패: %s", market, strategy.name, exc)
                continue

            for action in actions:
                is_entry = action.kind in (BUY_MARKET, BUY_LIMIT)
                if is_entry and verdict.blocked:
                    continue
                if is_entry and market not in active and len(active) >= self.s.max_concurrent_positions:
                    log.debug("%s 슬롯 부족 (%d/%d) - 진입 보류", market, len(active), self.s.max_concurrent_positions)
                    continue
                try:
                    spent = self._execute(action, view, strategy)
                except Exception as exc:
                    log.exception("%s 액션 실행 실패(%s): %s", market, action.kind, exc)
                    continue
                if spent:
                    cash_krw = max(0.0, cash_krw - spent)
                    active = self._active_positions()

        self._cleanup_positions(price_map)
        self._heartbeat(equity, price_map)

    # ================================================================== #
    # 시장 데이터
    # ================================================================== #
    def _build_view(self, market: str, price: float) -> MarketView | None:
        if price <= 0:
            return None
        macro_raw = self.client.get_candles(market, self.s.regime_timeframe, self.s.regime_candles)
        signal_raw = self.client.get_candles(market, self.s.signal_timeframe, self.s.signal_candles)
        if len(macro_raw) < 60 or len(signal_raw) < 60:
            log.debug("%s 캔들 부족 (상위 %d / 실행 %d)", market, len(macro_raw), len(signal_raw))
            return None

        macro = build_features(macro_raw)
        signal = build_features(signal_raw)
        regime = self.regime.classify(market, macro)

        open_orders = []
        pos = self.state.positions.get(market)
        if pos is not None:
            open_orders = self.client.list_open_orders(market)

        return MarketView(
            market=market,
            price=price,
            regime=regime,
            macro=macro,
            signal=signal,
            open_orders=open_orders,
        )

    def _strategy_for(self, view: MarketView, pos: Position | None):
        """포지션이 있으면 그 포지션을 연 전략이, 없으면 현재 국면 담당 전략이 관리한다."""
        if pos is not None and (pos.volume > 0 or pos.grid.get("buys")):
            return self.strategies.get(pos.strategy)
        for strat in self.strategies.values():
            if strat.handles(view.regime.regime):
                return strat
        return None

    def _universe(self) -> list[str]:
        if self.state.universe_updated_at <= 0 or self.screener.is_stale(self.state.universe_updated_at):
            keep = {m for m, p in self.state.positions.items() if p.volume > 0 or p.grid.get("buys")}
            try:
                self.state.universe = self.screener.select(keep)
                self.state.universe_updated_at = time.time()
            except Exception as exc:
                log.warning("유니버스 갱신 실패(%s) - 기존 목록 유지", exc)
        return list(self.state.universe)

    def _active_positions(self) -> set[str]:
        return {m for m, p in self.state.positions.items() if p.volume > 0 or p.grid.get("buys")}

    # ================================================================== #
    # 액션 실행
    # ================================================================== #
    def _execute(self, action: Action, view: MarketView, strategy) -> float:
        """실행한 주문의 원화 소진액을 반환한다 (현금 잔고 추적용)."""
        market = action.market
        pos = self.state.positions.get(market)

        if action.kind == SET_STOP:
            if pos:
                pos.stop_price = action.price
                if pos.init_stop <= 0:
                    pos.init_stop = action.price
                log.info("%s | %s", market, action.reason)
            return 0.0

        if action.kind == CANCEL:
            if self.client.cancel_order(action.uuid):
                log.info("%s | %s", market, action.reason)
                if pos and hasattr(strategy, "on_order_cancelled"):
                    strategy.on_order_cancelled(pos, action.uuid)
            return 0.0

        if action.kind == BUY_MARKET:
            return self._do_buy_market(action, view, strategy)

        if action.kind == SELL_MARKET:
            self._do_sell_market(action, view, strategy)
            return 0.0

        if action.kind == BUY_LIMIT:
            return self._do_buy_limit(action, view, strategy)

        if action.kind == SELL_LIMIT:
            self._do_sell_limit(action, view, strategy)
            return 0.0

        log.warning("알 수 없는 액션 종류: %s", action.kind)
        return 0.0

    # ---------------- 시장가 매수 ---------------- #
    def _do_buy_market(self, action: Action, view: MarketView, strategy) -> float:
        market = action.market
        krw = float(int(action.krw))
        if krw < self.s.min_order_krw:
            return 0.0

        result = self.client.buy_market(market, krw, view.price)
        if result.executed_volume <= 0:
            log.warning("%s 매수 체결 수량 0 - 다음 루프에서 재확인", market)
            return 0.0

        pos = self.state.positions.get(market)
        if pos is None:
            pos = Position(
                market=market,
                strategy=action.meta.get("strategy", strategy.name),
                regime_at_entry=view.regime.regime,
            )
            self.state.positions[market] = pos

        fill_price = result.avg_price or view.price
        pos.add_fill(fill_price, result.executed_volume, krw)
        pos.meta.update({k: v for k, v in action.meta.items() if k not in ("strategy",)})
        if action.price > 0:
            pos.stop_price = max(pos.stop_price, action.price) if pos.steps > 1 else action.price
            if pos.init_stop <= 0:
                pos.init_stop = action.price
        pos.highest = max(pos.highest, fill_price)

        msg = (
            f"🟢 매수 {market} [{pos.strategy}/{view.regime.regime}]\n"
            f"체결가 {fill_price:,.2f} / 수량 {result.executed_volume:.8f} / 금액 {krw:,.0f}원\n"
            f"평단 {pos.avg_price:,.2f} / 손절선 {pos.stop_price:,.2f}\n{action.reason}"
        )
        log.info("%s | 매수 체결 %s원 @ %s | %s", market, f"{krw:,.0f}", f"{fill_price:,.2f}", action.reason)
        self.notifier.send(msg)
        return krw

    # ---------------- 시장가 매도 ---------------- #
    def _do_sell_market(self, action: Action, view: MarketView, strategy) -> None:
        market = action.market
        pos = self.state.positions.get(market)
        if pos is None or pos.volume <= 0:
            return

        # 지정가 매도에 묶여 있는 수량은 거래소에서 locked 이라 시장가로 팔 수 없다.
        # 시장가 청산 전에 해당 마켓의 미체결 매도 예약을 먼저 회수한다.
        self._release_locked_asks(market, pos, strategy)

        volume = action.volume or pos.volume * (action.ratio or 1.0)
        volume = min(volume, pos.volume)

        # 부분 매도 후 잔여분이 최소 주문금액 미만이 되면 먼지가 되므로 전량 매도
        remainder_krw = (pos.volume - volume) * view.price
        if 0 < remainder_krw < self.s.min_order_krw:
            volume = pos.volume
        if volume * view.price < self.s.min_order_krw:
            if pos.volume * view.price < self.s.min_order_krw:
                log.warning(
                    "%s 보유 평가액 %s원이 최소 주문금액 미만 - 매도 불가(먼지 잔량)",
                    market, f"{pos.volume * view.price:,.0f}",
                )
                return
            volume = pos.volume

        result = self.client.sell_market(market, volume, view.price)
        if result.executed_volume <= 0:
            log.warning("%s 매도 체결 실패", market)
            return

        fill_price = result.avg_price or view.price
        self._book_sell(pos, fill_price, result.executed_volume, result.paid_fee, action.reason, view.regime.regime)

        if hasattr(strategy, "drop_lots_for_volume"):
            strategy.drop_lots_for_volume(pos, result.executed_volume)
        if action.meta.get("grid_upper_take"):
            pos.grid["upper_taken"] = True
        if action.meta.get("partial"):
            pos.partial_taken = True

    def _release_locked_asks(self, market: str, pos: Position, strategy) -> None:
        try:
            open_orders = self.client.list_open_orders(market)
        except Exception as exc:
            log.warning("%s 미체결 주문 조회 실패(%s) - 매도 예약 회수를 건너뜁니다", market, exc)
            return
        for order in open_orders:
            if order.side != "ask":
                continue
            if self.client.cancel_order(order.uuid):
                log.info("%s | 시장가 청산 위해 매도 예약 회수 (%s)", market, order.uuid)
                if hasattr(strategy, "on_order_cancelled"):
                    strategy.on_order_cancelled(pos, order.uuid)

    # ---------------- 지정가 ---------------- #
    def _do_buy_limit(self, action: Action, view: MarketView, strategy) -> float:
        market = action.market
        notional = action.price * action.volume
        if notional < self.s.min_order_krw:
            return 0.0

        result = self.client.buy_limit(market, action.price, action.volume)
        pos = self.state.positions.get(market)
        if pos is None:
            pos = Position(
                market=market,
                strategy=action.meta.get("strategy", strategy.name),
                regime_at_entry=view.regime.regime,
            )
            self.state.positions[market] = pos
        if hasattr(strategy, "on_order_placed"):
            strategy.on_order_placed(pos, action, result)
        log.info("%s | %s", market, action.reason)
        return notional

    def _do_sell_limit(self, action: Action, view: MarketView, strategy) -> None:
        market = action.market
        pos = self.state.positions.get(market)
        if pos is None:
            return
        if action.price * action.volume < self.s.min_order_krw:
            return
        result = self.client.sell_limit(market, action.price, action.volume)
        if hasattr(strategy, "on_order_placed"):
            strategy.on_order_placed(pos, action, result)
        log.info("%s | %s", market, action.reason)

    # ================================================================== #
    # 체결 반영
    # ================================================================== #
    def _book_sell(
        self, pos: Position, price: float, volume: float, fee: float, reason: str, regime: str
    ) -> None:
        cost = pos.avg_price * volume
        proceeds = price * volume - fee
        pnl = proceeds - cost
        pnl_pct = (price / pos.avg_price - 1.0) if pos.avg_price > 0 else 0.0

        pos.volume = max(0.0, pos.volume - volume)
        pos.realized_krw += pnl
        pos.invested_krw = max(0.0, pos.invested_krw - cost)

        self.state.record_trade(
            Trade(
                market=pos.market,
                strategy=pos.strategy,
                regime=regime,
                entry_price=pos.avg_price,
                exit_price=price,
                volume=volume,
                pnl_krw=pnl,
                pnl_pct=pnl_pct,
                opened_at=pos.opened_at,
                closed_at=time.time(),
                reason=reason,
            )
        )
        emoji = "🔴" if pnl < 0 else "🔵"
        log.info(
            "%s | 매도 체결 %.8f @ %s | 손익 %s원 (%+.2f%%) | %s",
            pos.market, volume, f"{price:,.2f}", f"{pnl:,.0f}", pnl_pct * 100, reason,
        )
        self.notifier.send(
            f"{emoji} 매도 {pos.market} [{pos.strategy}]\n"
            f"체결가 {price:,.2f} / 수량 {volume:.8f}\n"
            f"손익 {pnl:,.0f}원 ({pnl_pct:+.2f}%)\n{reason}"
        )

    def _apply_fill(self, order) -> None:
        """지정가 주문이 체결되었을 때 포지션 장부에 반영한다."""
        pos = self.state.positions.get(order.market)
        if pos is None:
            return
        strategy = self.strategies.get(pos.strategy)
        price = order.avg_price or order.price
        volume = order.executed_volume or order.volume
        if volume <= 0 or price <= 0:
            return

        if order.side == "bid":
            # 지정가 매수 원가 = 체결대금 + 수수료 (평단에 수수료를 태운다)
            pos.add_fill(price, volume, price * volume + order.paid_fee)
            if strategy and hasattr(strategy, "on_buy_filled"):
                strategy.on_buy_filled(pos, order.uuid, price, volume)
            log.info("%s | 지정가 매수 체결 @ %s (수량 %.8f)", order.market, f"{price:,.2f}", volume)
            self.notifier.send(
                f"🟢 지정가 매수 체결 {order.market}\n{price:,.2f} x {volume:.8f}\n평단 {pos.avg_price:,.2f}"
            )
        else:
            self._book_sell(pos, price, volume, order.paid_fee, "지정가 익절 체결", pos.regime_at_entry)
            if strategy and hasattr(strategy, "on_sell_filled"):
                strategy.on_sell_filled(pos, order.uuid)

    def _reconcile(self, market: str, pos: Position) -> None:
        """실거래 모드에서 기록된 지정가 주문의 체결/취소 여부를 확인한다."""
        tracked: list[str] = list(pos.grid.get("buys", {}).keys())
        tracked += [l["sell_uuid"] for l in pos.grid.get("lots", []) if l.get("sell_uuid")]
        if not tracked:
            return

        try:
            open_uuids = {o.uuid for o in self.client.list_open_orders(market)}
        except Exception as exc:
            log.warning("%s 미체결 주문 조회 실패: %s", market, exc)
            return

        strategy = self.strategies.get(pos.strategy)
        for uid in tracked:
            if uid in open_uuids:
                continue
            order = self.client.get_order(uid)
            if order is None:
                # 조회 불가 - 장부에서만 제거해 유령 주문이 남지 않도록 한다
                if strategy and hasattr(strategy, "on_order_cancelled"):
                    strategy.on_order_cancelled(pos, uid)
                continue
            if order.executed_volume > 0:
                self._apply_fill(order)
            elif strategy and hasattr(strategy, "on_order_cancelled"):
                strategy.on_order_cancelled(pos, uid)

    # ================================================================== #
    # 정리 / 청산
    # ================================================================== #
    def _cleanup_positions(self, price_map: dict[str, float]) -> None:
        for market, pos in list(self.state.positions.items()):
            price = price_map.get(market, 0.0)
            value = pos.volume * price
            has_orders = bool(pos.grid.get("buys")) or any(
                l.get("sell_uuid") for l in pos.grid.get("lots", [])
            )
            if value < DUST_KRW and not has_orders:
                if pos.realized_krw:
                    log.info(
                        "%s 포지션 종료 | 누적 실현손익 %s원", market, f"{pos.realized_krw:,.0f}"
                    )
                del self.state.positions[market]

    def _liquidate_all(self, price_map: dict[str, float], reason: str) -> None:
        for market, pos in list(self.state.positions.items()):
            try:
                self.client.cancel_all(market)
            except Exception as exc:
                log.warning("%s 주문 취소 실패: %s", market, exc)
            price = price_map.get(market, 0.0)
            if pos.volume <= 0 or price <= 0:
                continue
            if pos.volume * price < self.s.min_order_krw:
                log.warning("%s 잔량이 최소 주문금액 미만 - 청산 생략", market)
                continue
            try:
                result = self.client.sell_market(market, pos.volume, price)
                self._book_sell(
                    pos, result.avg_price or price, result.executed_volume, result.paid_fee, reason, STRONG_BEAR
                )
            except Exception as exc:
                log.error("%s 강제 청산 실패: %s", market, exc)
        self._cleanup_positions(price_map)

    def liquidate_all(self) -> None:
        """외부(종료 훅, CLI)에서 호출하는 안전 청산."""
        markets = list(self.state.positions.keys())
        if not markets:
            return
        tickers = self.client.get_tickers(markets)
        self._liquidate_all({m: t["price"] for m, t in tickers.items()}, "수동 전량 청산")
        self.state.save(self.s.state_file)

    # ================================================================== #
    # 로깅 / 알림
    # ================================================================== #
    def _log_regime_change(self, market: str, regime: RegimeResult) -> None:
        prev = self._last_regimes.get(market)
        if prev != regime.regime:
            log.info(
                "%s 국면 %s -> %s (%s, 신뢰도 %.0f%%) %s",
                market, prev or "?", regime.regime, regime.source, regime.confidence * 100,
                regime.detail or "",
            )
            if prev is not None:
                self.notifier.send(
                    f"📊 {market} 국면 전환\n{prev} → {regime.regime} "
                    f"(신뢰도 {regime.confidence * 100:.0f}%)"
                )
            self._last_regimes[market] = regime.regime

    def _heartbeat(self, equity: float, price_map: dict[str, float]) -> None:
        now = time.time()
        if now - self.state.last_heartbeat_at < self.s.heartbeat_minutes * 60:
            return
        self.state.last_heartbeat_at = now

        dd = self.risk.drawdown(self.state, equity) * 100
        day = self.risk.day_pnl_pct(self.state, equity) * 100
        total = (equity / self.state.initial_equity - 1.0) * 100 if self.state.initial_equity > 0 else 0.0
        kelly = self.sizer.kelly(self.state.trades)

        lines = [
            f"📈 상태 보고 {kst_now_str()}",
            f"평가자산 {equity:,.0f}원 (누적 {total:+.2f}%)",
            f"당일 {day:+.2f}% / 고점대비 -{dd:.2f}%",
            f"거래 {len(self.state.trades)}건 | 승률 {kelly.win_rate * 100:.0f}% | "
            f"손익비 {kelly.payoff_ratio:.2f} | f* {kelly.f_star:.3f}",
        ]
        for market, pos in self.state.open_positions().items():
            px = price_map.get(market, 0.0)
            lines.append(
                f"· {market} [{pos.strategy}] {pos.unrealized_pct(px) * 100:+.2f}% "
                f"({pos.volume * px:,.0f}원)"
            )
        if not self.state.open_positions():
            lines.append("· 보유 포지션 없음")

        text = "\n".join(lines)
        log.info(text.replace("\n", " | "))
        self.notifier.send(text)

    def summary(self) -> dict[str, Any]:
        trades = self.state.trades
        wins = [t for t in trades if t.pnl_krw > 0]
        return {
            "거래수": len(trades),
            "승률": round(len(wins) / len(trades) * 100, 1) if trades else 0.0,
            "누적손익": round(sum(t.pnl_krw for t in trades)),
            "보유포지션": list(self.state.open_positions().keys()),
            "고점자산": round(self.state.equity_hwm),
        }
