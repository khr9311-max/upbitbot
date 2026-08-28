"""
업비트 AI 자동매매 봇 - 엔트리포인트.

사용법:
    python main.py run          매매 루프 실행 (기본값)
    python main.py check        API 연결 / 설정 / 주문권한 점검
    python main.py status       현재 계좌 · 포지션 · 성과 요약
    python main.py universe     유니버스 스크리닝 결과 출력
    python main.py regime       종목별 현재 시장 국면 판별 결과
    python main.py liquidate    보유 포지션 전량 시장가 청산 (확인 필요)
    python main.py reset-halt   서킷브레이커 정지 상태 해제
"""
from __future__ import annotations

import argparse
import signal
import sys

from config import settings
from core.logger import force_utf8, get_logger, setup_logging

force_utf8()
log = get_logger("main")


def _banner() -> None:
    mode = "모의매매(DRY_RUN)" if settings.dry_run else "🔴 실거래"
    print(f"\n업비트 AI 자동매매 봇 | 모드: {mode} | 환경: {settings.environment}\n")


def _check_config() -> bool:
    errors = settings.validate()
    if errors:
        for e in errors:
            log.error("설정 오류: %s", e)
        return False
    return True


# --------------------------------------------------------------------------- #
# 명령어
# --------------------------------------------------------------------------- #
def cmd_run(_args) -> int:
    from core.engine import TradingEngine

    engine = TradingEngine(settings)

    def _handle(signum, _frame):
        log.info("종료 시그널(%s) 수신 - 안전하게 정리합니다", signum)
        engine.stop()

    signal.signal(signal.SIGINT, _handle)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, _handle)

    try:
        engine.run()
    except KeyboardInterrupt:
        log.info("사용자 중단")
    finally:
        engine.state.save(settings.state_file)
        engine.notifier.send("🛑 자동매매 봇 종료")
        engine.notifier.close()
        log.info("종료 완료 | %s", engine.summary())
    return 0


def cmd_check(_args) -> int:
    from core.upbit_client import UpbitClient

    ok = True
    print("[1/5] 설정 검증 ...", end=" ")
    if _check_config():
        print("통과")
    else:
        print("실패")
        ok = False

    client = UpbitClient(settings)

    print("[2/5] 시세 API 연결 ...", end=" ")
    try:
        tickers = client.get_tickers(["KRW-BTC"])
        print(f"통과 (KRW-BTC {tickers['KRW-BTC']['price']:,.0f}원)")
    except Exception as exc:
        print(f"실패: {exc}")
        ok = False

    print("[3/5] 캔들 조회 ...", end=" ")
    try:
        df = client.get_candles("KRW-BTC", settings.signal_timeframe, 200)
        print(f"통과 ({len(df)}개, 최신 {df.index[-1]})")
    except Exception as exc:
        print(f"실패: {exc}")
        ok = False

    print("[4/5] 계좌 조회 ...", end=" ")
    try:
        balances = client.get_balances()
        krw = balances.get("KRW")
        print(f"통과 (원화 {krw.balance:,.0f}원, 보유자산 {len(balances)}종)")
    except Exception as exc:
        print(f"실패: {exc}")
        print("     -> API 키 / IP 화이트리스팅 설정을 확인하세요.")
        ok = False

    print("[5/5] 주문 권한 ...", end=" ")
    if settings.dry_run:
        print("건너뜀 (DRY_RUN 모드)")
    else:
        try:
            chance = client._call("exchange", client._client.orders.retrieve_chance, market="KRW-BTC")
            print(f"통과 (매수 수수료 {float(chance.bid_fee) * 100:.3f}%, 최소주문 {chance.market.bid.min_total}원)")
        except Exception as exc:
            print(f"실패: {exc}")
            print("     -> API 키에 '주문' 권한이 있는지 확인하세요. 출금 권한은 반드시 비활성화하세요.")
            ok = False

    print("\n결과:", "✅ 모든 점검 통과" if ok else "❌ 문제가 있습니다. 위 항목을 수정하세요.")
    return 0 if ok else 1


def cmd_status(_args) -> int:
    from core.sizing import PositionSizer
    from core.state import BotState, kst_now_str
    from core.upbit_client import UpbitClient

    client = UpbitClient(settings)
    state = BotState.load(settings.state_file)
    markets = list(state.positions.keys()) or ["KRW-BTC"]
    tickers = client.get_tickers(markets)
    price_map = {m: t["price"] for m, t in tickers.items()}
    equity = client.equity(price_map)

    total = (equity / state.initial_equity - 1) * 100 if state.initial_equity > 0 else 0.0
    dd = (1 - equity / state.equity_hwm) * 100 if state.equity_hwm > 0 else 0.0
    kelly = PositionSizer(settings).kelly(state.trades)

    print(f"\n=== 계좌 현황 ({kst_now_str()}) ===")
    print(f"평가자산      {equity:,.0f}원  (시작 대비 {total:+.2f}%)")
    print(f"자산 고점     {state.equity_hwm:,.0f}원  (고점 대비 -{dd:.2f}%)")
    print(f"당일 기준선   {state.day_start_equity:,.0f}원 ({state.day_key})")
    if state.halted:
        print(f"⚠️ 정지 상태  {state.halt_reason}")

    print("\n=== 보유 포지션 ===")
    positions = state.open_positions()
    if not positions:
        print("없음")
    for market, pos in positions.items():
        px = price_map.get(market, 0.0)
        print(
            f"{market:10s} [{pos.strategy:5s}] 평단 {pos.avg_price:>12,.2f} "
            f"현재 {px:>12,.2f} 손익 {pos.unrealized_pct(px) * 100:>+7.2f}% "
            f"평가액 {pos.volume * px:>10,.0f}원 손절선 {pos.stop_price:,.2f}"
        )

    print("\n=== 성과 ===")
    trades = state.trades
    if trades:
        wins = [t for t in trades if t.pnl_krw > 0]
        print(f"총 거래 {len(trades)}건 | 승률 {len(wins) / len(trades) * 100:.1f}% | "
              f"누적 실현손익 {sum(t.pnl_krw for t in trades):,.0f}원")
        print(f"켈리 f* {kelly.f_star:.4f} (표본 {kelly.sample}건, 손익비 {kelly.payoff_ratio:.2f}) "
              f"-> 적용 베팅비율 {max(0, kelly.f_star) * settings.kelly_fraction * 100:.2f}%")
        print("\n최근 5건:")
        for t in trades[-5:]:
            print(f"  {t.market:10s} {t.strategy:5s} {t.pnl_pct * 100:>+7.2f}% "
                  f"{t.pnl_krw:>+10,.0f}원  {t.reason[:48]}")
    else:
        print("거래 이력 없음")
    print()
    return 0


def cmd_universe(_args) -> int:
    from core.screener import UniverseScreener
    from core.upbit_client import UpbitClient

    client = UpbitClient(settings)
    screener = UniverseScreener(settings, client)
    selected = screener.select()
    print(f"\n선정 유니버스 ({settings.universe_mode} 모드, 상위 {settings.universe_size}종목):")
    tickers = client.get_tickers(selected) if selected else {}
    for m in selected:
        t = tickers.get(m, {})
        print(f"  {m:12s} {t.get('price', 0):>14,.2f}원  "
              f"24h거래대금 {t.get('acc_trade_price_24h', 0) / 1e8:>10,.0f}억  "
              f"등락 {t.get('change_rate', 0) * 100:>+6.2f}%")
    print()
    return 0


def cmd_regime(_args) -> int:
    from core.indicators import build_features
    from core.regime import RegimeClassifier
    from core.screener import UniverseScreener
    from core.upbit_client import UpbitClient

    client = UpbitClient(settings)
    markets = UniverseScreener(settings, client).select()
    clf = RegimeClassifier(settings)
    print(f"\n=== 시장 국면 판별 (상위 {settings.regime_timeframe}분봉 {settings.regime_candles}개) ===")
    for m in markets:
        df = client.get_candles(m, settings.regime_timeframe, settings.regime_candles)
        if len(df) < 60:
            print(f"  {m:12s} 캔들 부족")
            continue
        res = clf.classify(m, build_features(df))
        weight = clf.alloc_weight(res.regime)
        print(f"  {m:12s} {res.regime:18s} 신뢰도 {res.confidence * 100:5.1f}% "
              f"({res.source})  배분 {weight * 100:.0f}%  {res.detail}")
    print()
    return 0


def cmd_liquidate(args) -> int:
    from core.engine import TradingEngine

    engine = TradingEngine(settings)
    positions = engine.state.open_positions()
    if not positions:
        print("청산할 포지션이 없습니다.")
        return 0
    print("다음 포지션을 시장가로 전량 청산합니다:")
    for m, p in positions.items():
        print(f"  {m} 수량 {p.volume:.8f} 평단 {p.avg_price:,.2f}")
    if not args.yes:
        if input("진행하시겠습니까? (yes 입력): ").strip().lower() != "yes":
            print("취소했습니다.")
            return 1
    engine.liquidate_all()
    print("청산 완료.")
    return 0


def cmd_reset_halt(_args) -> int:
    from core.state import BotState

    state = BotState.load(settings.state_file)
    if not state.halted:
        print("정지 상태가 아닙니다.")
        return 0
    print(f"해제 대상: {state.halt_reason}")
    state.halted = False
    state.halt_reason = ""
    state.equity_hwm = 0.0  # 새 고점 기준으로 MDD 재계산
    state.consecutive_losses = 0
    state.cooldown_until = 0.0
    state.save(settings.state_file)
    print("서킷브레이커를 해제했습니다. 자산 고점 기준선도 초기화했습니다.")
    return 0


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="upbit-bot", description="업비트 AI 자동매매 봇 (2계층 국면 적응형)"
    )
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("run", help="매매 루프 실행")
    sub.add_parser("check", help="API 연결 및 설정 점검")
    sub.add_parser("status", help="계좌/포지션/성과 요약")
    sub.add_parser("universe", help="유니버스 스크리닝 결과")
    sub.add_parser("regime", help="시장 국면 판별 결과")
    liq = sub.add_parser("liquidate", help="전량 시장가 청산")
    liq.add_argument("--yes", action="store_true", help="확인 없이 즉시 실행")
    sub.add_parser("reset-halt", help="서킷브레이커 해제")
    return parser


COMMANDS = {
    "run": cmd_run,
    "check": cmd_check,
    "status": cmd_status,
    "universe": cmd_universe,
    "regime": cmd_regime,
    "liquidate": cmd_liquidate,
    "reset-halt": cmd_reset_halt,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(settings.log_level, settings.log_dir)
    command = args.command or "run"
    _banner()
    if command == "run" and not _check_config():
        return 1
    return COMMANDS[command](args)


if __name__ == "__main__":
    sys.exit(main())
