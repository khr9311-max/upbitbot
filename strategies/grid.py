"""
횡보 국면 전략 - AI 적응형 동적 ATR 그리드.

전략서 3.1절:
  * 주문 간격  dG = alpha x ATR(14),  alpha 는 볼린저 밴드 수축/팽창 상태로 변조
  * 상단 이탈  -> 분할 익절로 수익 확정
  * 하단 이탈  -> 미체결 매수주문 전량 취소 + 잔여 포지션 하드 스톱로스

정적 그리드가 하락 추세에서 하단 주문을 모두 맞고 최대 손실을 떠안는 구조적 결함을
막기 위해, 그리드는 (1) 국면 분류기가 횡보로 판정한 동안에만 유지되고
(2) 밴드 하단을 ATR 배수만큼 이탈하면 즉시 전량 정리된다.
"""
from __future__ import annotations

import time
import uuid as uuidlib

from core.logger import get_logger
from core.regime import LOW_VOL_RANGE, STRONG_BEAR
from core.state import Position
from strategies.base import (
    BUY_LIMIT,
    CANCEL,
    SELL_LIMIT,
    SELL_MARKET,
    Action,
    Context,
    MarketView,
    Strategy,
)

log = get_logger("grid")


class AtrGridStrategy(Strategy):
    name = "grid"
    regimes = (LOW_VOL_RANGE,)

    # ------------------------------------------------------------------ #
    def plan(self, view: MarketView, position: Position | None, ctx: Context) -> list[Action]:
        if not view.ready:
            return []

        grid = (position.grid if position else None) or {}
        has_orders = bool(grid.get("buys"))
        has_coins = bool(position and position.volume > 0)

        # ---- 세션이 없으면 신규 개설 여부만 판단 ----
        if not has_orders and not has_coins:
            if view.regime.regime != LOW_VOL_RANGE:
                return []
            return self._open_session(view, ctx)

        # ---- 운영 중인 세션 관리 ----
        return self._manage(view, position, ctx)

    # ------------------------------------------------------------------ #
    # 신규 그리드 개설
    # ------------------------------------------------------------------ #
    def _open_session(self, view: MarketView, ctx: Context) -> list[Action]:
        spacing_pct = self.spacing_pct(view)
        levels = self._affordable_levels(view, ctx, spacing_pct)
        if levels <= 0:
            return []

        # 그리드 전체에 배정할 원화. 손절폭은 최하단 레벨까지의 거리로 잡는다
        stop_dist = spacing_pct * levels + self.s.grid_break_atr * view.atr_macro / view.price
        total_krw, reason = ctx.size_krw(stop_dist, self.name, view.market)
        if total_krw <= 0:
            log.debug("%s 그리드 사이징 불가: %s", view.market, reason)
            return []

        per_level = total_krw / levels
        if per_level < self.s.min_order_krw:
            levels = max(1, int(total_krw // self.s.min_order_krw))
            per_level = total_krw / levels
            if per_level < self.s.min_order_krw:
                return []

        actions: list[Action] = []
        for i in range(1, levels + 1):
            level_price = view.price * (1 - i * spacing_pct)
            volume = per_level / level_price
            actions.append(
                Action(
                    kind=BUY_LIMIT,
                    market=view.market,
                    price=level_price,
                    volume=volume,
                    reason=f"그리드 {i}단 매수 예약 ({level_price:,.0f}원, 간격 {spacing_pct * 100:.2f}%)",
                    meta={
                        "strategy": self.name,
                        "level": i,
                        "spacing_pct": spacing_pct,
                        "center": view.price,
                        "session_new": i == 1,
                        "lower": self._lower_bound(view, spacing_pct, levels),
                        "upper": self._upper_bound(view),
                    },
                )
            )
        log.info(
            "%s 그리드 개설 | 총 %s원 x %d단 | 간격 %.2f%% | 하단이탈선 %s",
            view.market, f"{total_krw:,.0f}", levels, spacing_pct * 100,
            f"{self._lower_bound(view, spacing_pct, levels):,.0f}",
        )
        return actions

    # ------------------------------------------------------------------ #
    # 운영 중 관리
    # ------------------------------------------------------------------ #
    def _manage(self, view: MarketView, pos: Position, ctx: Context) -> list[Action]:
        grid = pos.grid
        spacing_pct = float(grid.get("spacing_pct") or self.spacing_pct(view))
        lower = float(grid.get("lower") or self._lower_bound(view, spacing_pct, self.s.grid_levels))
        upper = float(grid.get("upper") or self._upper_bound(view))
        actions: list[Action] = []

        # 1) 하단 이탈 - 전량 정리 (그리드 최대 리스크 차단 지점)
        if view.price <= lower:
            return self._liquidate(view, pos, f"밴드 하단 이탈 정리 (현재가 {view.price:,.0f} <= 하단선 {lower:,.0f})")

        # 2) 하락 국면 전환 - 즉시 정리 (BEAR_FORCE_EXIT=False 면 매수만 중단)
        if view.regime.regime == STRONG_BEAR and self.s.bear_force_exit:
            return self._liquidate(view, pos, "하락 국면 전환 - 그리드 전량 정리")

        # 3) 횡보 국면 이탈 - 신규 매수만 중단하고 보유분은 매도주문으로 소진
        winding_down = view.regime.regime != LOW_VOL_RANGE
        if winding_down:
            for oid in list(grid.get("buys", {})):
                actions.append(Action(CANCEL, view.market, uuid=oid, reason="국면 이탈 - 그리드 매수 예약 취소"))

        # 4) 상단 이탈 - 분할 익절. 상단 아래로 되돌아오면 다음 이탈을 위해 플래그를 푼다
        if view.price < upper:
            grid.pop("upper_taken", None)
        elif pos.volume > 0 and not grid.get("upper_taken"):
            actions.append(
                Action(
                    kind=SELL_MARKET,
                    market=view.market,
                    ratio=0.5,
                    reason=f"밴드 상단 이탈 분할 익절 (현재가 {view.price:,.0f} >= 상단선 {upper:,.0f})",
                    meta={"grid_upper_take": True},
                )
            )

        # 5) 체결된 로트마다 +1간격 지정가 매도를 걸어둔다
        for lot in grid.get("lots", []):
            if lot.get("sell_uuid"):
                continue
            target = lot["price"] * (1 + spacing_pct)
            volume = float(lot["volume"])
            if volume * target < self.s.min_order_krw:
                continue  # 최소 주문금액 미달 로트는 다음 매수와 합쳐질 때까지 대기
            actions.append(
                Action(
                    kind=SELL_LIMIT,
                    market=view.market,
                    price=target,
                    volume=volume,
                    reason=f"그리드 익절 예약 ({lot['price']:,.0f} -> {target:,.0f})",
                    meta={"lot_id": lot["id"]},
                )
            )

        if winding_down:
            return actions

        # 6) 매수 예약 유지 - 만료되었거나 현재가에서 너무 멀어진 주문은 재배치
        ttl = self.s.grid_order_ttl_min * 60
        now = time.time()
        buys = grid.get("buys", {})
        alive_levels: set[int] = set()
        for oid, info in list(buys.items()):
            too_far = info["price"] < view.price * (1 - (self.s.grid_levels + 1.5) * spacing_pct)
            expired = now - float(info.get("at", now)) > ttl
            if too_far or expired:
                actions.append(
                    Action(CANCEL, view.market, uuid=oid,
                           reason="그리드 매수 재배치 (" + ("기간 만료" if expired else "현재가 이탈") + ")")
                )
            else:
                alive_levels.add(int(info.get("level", 0)))

        levels = self._affordable_levels(view, ctx, spacing_pct)
        missing = [i for i in range(1, levels + 1) if i not in alive_levels]
        if missing:
            stop_dist = spacing_pct * levels + self.s.grid_break_atr * view.atr_macro / view.price
            total_krw, _ = ctx.size_krw(stop_dist, self.name, view.market)
            per_level = total_krw / levels if levels else 0.0
            if per_level >= self.s.min_order_krw:
                for i in missing:
                    level_price = view.price * (1 - i * spacing_pct)
                    actions.append(
                        Action(
                            kind=BUY_LIMIT,
                            market=view.market,
                            price=level_price,
                            volume=per_level / level_price,
                            reason=f"그리드 {i}단 재예약 ({level_price:,.0f}원)",
                            meta={
                                "strategy": self.name,
                                "level": i,
                                "spacing_pct": spacing_pct,
                                "center": view.price,
                                "lower": self._lower_bound(view, spacing_pct, levels),
                                "upper": self._upper_bound(view),
                            },
                        )
                    )
        return actions

    # ------------------------------------------------------------------ #
    def _liquidate(self, view: MarketView, pos: Position, reason: str) -> list[Action]:
        actions = [
            Action(CANCEL, view.market, uuid=oid, reason="그리드 정리 - 매수 예약 취소")
            for oid in list(pos.grid.get("buys", {}))
        ]
        for lot in pos.grid.get("lots", []):
            if lot.get("sell_uuid"):
                actions.append(
                    Action(CANCEL, view.market, uuid=lot["sell_uuid"], reason="그리드 정리 - 매도 예약 취소")
                )
        if pos.volume > 0:
            actions.append(Action(SELL_MARKET, view.market, ratio=1.0, reason=reason))
        return actions

    # ------------------------------------------------------------------ #
    # 파라미터 계산
    # ------------------------------------------------------------------ #
    def spacing_pct(self, view: MarketView) -> float:
        """dG = alpha x ATR(14) 를 현재가 대비 비율로 환산. alpha 는 BB 수축/팽창으로 변조."""
        bb_pct = view.sig("bb_pct", 0.5)
        if bb_pct != bb_pct:  # NaN
            bb_pct = 0.5
        alpha = self.s.grid_alpha * (0.6 + 0.8 * bb_pct)
        alpha = max(self.s.grid_alpha_min, min(self.s.grid_alpha_max, alpha))
        # 15분봉 ATR 로 간격을 잡으면 0.2% 수준이라 왕복 수수료(0.1%)를 겨우 넘는 반면
        # 하단 손절선까지의 거리는 그 10배가 되어 손익비가 붕괴한다. 포지션 시간축과
        # 같은 상위 타임프레임 ATR 을 쓴다.
        spacing = alpha * view.atr_macro / view.price
        # 왕복 수수료(0.1%) + 슬리피지를 확실히 넘어야 그리드가 수익을 낸다
        floor = max(self.s.grid_min_spacing_pct, 4 * self.s.fee_rate + self.s.slippage_pct)
        return max(spacing, floor)

    def _lower_bound(self, view: MarketView, spacing_pct: float = 0.0, levels: int = 0) -> float:
        """하단 이탈선은 항상 가장 깊은 매수 레벨보다 아래에 있어야 한다."""
        bb_lower = view.sig("bb_lower", view.price * 0.97)
        deepest = view.price * (1 - levels * spacing_pct) if levels else bb_lower
        return min(bb_lower, deepest) - self.s.grid_break_atr * view.atr_macro

    def _upper_bound(self, view: MarketView) -> float:
        bb_upper = view.sig("bb_upper", view.price * 1.03)
        return bb_upper

    def _affordable_levels(self, view: MarketView, ctx: Context, spacing_pct: float) -> int:
        """소액 계좌에서 최소 주문금액(5,000원) 제약을 넘지 못하는 단수는 만들지 않는다."""
        stop_dist = spacing_pct * self.s.grid_levels + self.s.grid_break_atr * view.atr_macro / view.price
        total_krw, _ = ctx.size_krw(stop_dist, self.name, view.market)
        if total_krw <= 0:
            return 0
        return max(0, min(self.s.grid_levels, int(total_krw // self.s.min_order_krw)))

    # ------------------------------------------------------------------ #
    # 엔진 콜백 - 그리드 장부 관리
    # ------------------------------------------------------------------ #
    def on_order_placed(self, pos: Position, action: Action, order) -> None:
        grid = pos.grid
        if action.kind == BUY_LIMIT:
            grid.setdefault("buys", {})[order.uuid] = {
                "price": action.price,
                "volume": action.volume,
                "level": action.meta.get("level", 0),
                "at": time.time(),
            }
            grid["spacing_pct"] = action.meta.get("spacing_pct", grid.get("spacing_pct"))
            grid["center"] = action.meta.get("center", grid.get("center"))
            grid["lower"] = action.meta.get("lower", grid.get("lower"))
            grid["upper"] = action.meta.get("upper", grid.get("upper"))
        elif action.kind == SELL_LIMIT:
            lot_id = action.meta.get("lot_id")
            for lot in grid.get("lots", []):
                if lot["id"] == lot_id:
                    lot["sell_uuid"] = order.uuid
                    break

    def on_order_cancelled(self, pos: Position, order_uuid: str) -> None:
        pos.grid.get("buys", {}).pop(order_uuid, None)
        for lot in pos.grid.get("lots", []):
            if lot.get("sell_uuid") == order_uuid:
                lot["sell_uuid"] = ""

    def on_buy_filled(self, pos: Position, order_uuid: str, price: float, volume: float) -> None:
        pos.grid.get("buys", {}).pop(order_uuid, None)
        pos.grid.setdefault("lots", []).append(
            {"id": uuidlib.uuid4().hex[:12], "price": price, "volume": volume, "sell_uuid": "", "at": time.time()}
        )

    def on_sell_filled(self, pos: Position, order_uuid: str) -> None:
        lots = pos.grid.get("lots", [])
        pos.grid["lots"] = [l for l in lots if l.get("sell_uuid") != order_uuid]

    def drop_lots_for_volume(self, pos: Position, volume: float) -> None:
        """시장가로 일부/전량 매도했을 때 로트 장부를 줄인다 (오래된 로트부터)."""
        remaining = volume
        lots = pos.grid.get("lots", [])
        kept = []
        for lot in lots:
            if remaining <= 1e-12:
                kept.append(lot)
                continue
            if lot["volume"] <= remaining + 1e-12:
                remaining -= lot["volume"]
            else:
                lot["volume"] -= remaining
                remaining = 0.0
                kept.append(lot)
        pos.grid["lots"] = kept
