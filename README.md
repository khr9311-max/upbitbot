# 업비트 AI 자동매매 봇

`AI 코인 자동매매봇 운용 전략.pdf` 의 **2계층 동적 국면 전환 아키텍처**를 업비트 공식
Python SDK(`upbit-sdk`) 위에 구현한 자동매매 봇입니다. 소액 자본의 마찰비용을 최소화하고
시장 국면에 따라 실행 알고리즘을 전환해 장기 기하학적 성장률을 극대화하는 것이 목표입니다.

```
① 멀티 타임프레임 캔들 수집
        ↓
② HMM 국면 판별  (STRONG_BULL / LOW_VOL_RANGE / VOLATILE_PULLBACK / STRONG_BEAR)
        ↓
③ 국면별 미시 실행 전략   추세→변동성돌파  횡보→ATR그리드  조정→스마트DCA  하락→현금
        ↓
④ 국면별 청산            샹들리에 출구 / 그리드 익절 / 타임스톱
        ↓
⑤ 부분 켈리 사이징 + 서킷브레이커 검증 → 주문 실행
```

---

## 1. 국면별 전략 매핑

| 시장 국면 | 판별 근거 | 실행 전략 | 자본 배분 | 위험 통제 |
|---|---|---|---|---|
| **STRONG_BULL** | 양의 평균 로그수익률, 이평선 정배열, ADX≥20 | 변동성 돌파 추세추종 | 70% | 샹들리에 출구 추적 청산 |
| **LOW_VOL_RANGE** | 0 수렴 수익률, 볼린저 스퀴즈 | ATR 적응형 동적 그리드 | 80% | 밴드 하단 이탈 시 전량 정리 |
| **VOLATILE_PULLBACK** | 일시적 과매도, 변동성 스파이크 | 한도 제어형 스마트 DCA | 45% | 최대 3회 진입 + 48시간 타임스톱 |
| **STRONG_BEAR** | 음의 평균 수익률, 하방 변동성 급증 | 현금 보유 | 0% | 신규 진입 전면 차단 + 보유분 청산 |

> 업비트 현물은 공매도가 없으므로 전략서의 "숏 헤징"은 **현금 보유**로 대체했습니다.

### 국면 판별 3중 구조

1. **HMM (`hmmlearn`)** — 로그수익률 / ATR비율 / 실현변동성 / 거래량 z-score 4개 관측치로
   4개 은닉 상태를 추정하고, 각 상태의 평균 수익률·변동성으로 국면 라벨을 자동 부여합니다.
   24시간마다 롤링 재적합합니다.
2. **규칙 기반 폴백** — `hmmlearn` 미설치, 적합 실패, 또는 사후확률이 `REGIME_MIN_CONFIDENCE`
   미만이면 EMA 배열 / ADX / 볼린저 밴드폭 백분위 기반 분류로 자동 전환합니다.
3. **구조적 하락 오버라이드** — 어떤 모델이 뭐라 하든 4시간봉이 `종가 < EMA200`,
   `EMA50 < EMA200`, `20봉 평균수익률 < 0` 을 모두 만족하면 **강제로 STRONG_BEAR** 로
   덮어씁니다. 모델 오분류로 하락장에 롱을 잡는 사고를 막는 최후 방어선입니다.

---

## 2. 자금 관리 — 부분 켈리

전략서 4장의 켈리 공식을 그대로 씁니다.

```
f* = W - (1 - W) / R        W = 승률,  R = 평균이익 / 평균손실
```

여기서 **f\* 는 "베팅 규모"가 아니라 "이번 거래에서 잃을 각오를 한 자본 비율"** 로
해석합니다. 손절폭이 서로 다른 세 전략 사이에서 리스크를 일관되게 만들기 위해서입니다.

```
리스크비율 = clamp(f* × KELLY_FRACTION,  0.25×RISK_PER_TRADE,  3×RISK_PER_TRADE)
주문금액  = min( 자산 × 리스크비율 / 손절거리,
                자산 × 종목상한 − 기보유,
                자산 × 국면배분 / 동시포지션수,
                가용현금 − 예비현금 )
```

거래 표본이 `KELLY_MIN_TRADES`(기본 20건) 미만이면 켈리 대신 고정비율 리스크
(`RISK_PER_TRADE_PCT`)를 씁니다. 팻테일 구간에서 풀 켈리가 곧 파산이므로
`KELLY_FRACTION` 은 0.25~0.33(쿼터 켈리)을 벗어나지 않는 것을 권장합니다.

### 서킷브레이커 (수익보다 우선하는 방어선)

| 조건 | 동작 | 해제 |
|---|---|---|
| `data/STOP` 파일 존재 | 전량 청산 후 프로세스 종료 | 파일 삭제 후 재시작 |
| 고점 대비 낙폭 ≥ `MAX_DRAWDOWN_PCT` | 전량 청산 + 신규 진입 영구 중단 | `python main.py reset-halt` |
| 당일 손실 ≥ `DAILY_LOSS_LIMIT_PCT` | 당일 신규 진입 중단 | KST 자정 자동 해제 |
| 연속 손실 ≥ `CONSECUTIVE_LOSS_LIMIT` | `COOLDOWN_MINUTES` 동안 진입 중단 | 시간 경과 시 자동 |

---

## 3. 빠른 시작

```bash
git clone <저장소> upbit-bot && cd upbit-bot
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env      # API 키 입력 (처음에는 DRY_RUN=True 유지)

python tests/test_core.py     # 단위 테스트 28건
python main.py check          # API 연결 / 권한 점검
python main.py universe       # 어떤 종목이 선정되는지 확인
python main.py regime         # 현재 시장 국면 판별 결과
python main.py run            # 모의매매 시작
```

### CLI

| 명령 | 설명 |
|---|---|
| `python main.py run` | 매매 루프 실행 (기본값) |
| `python main.py check` | API 연결·설정·주문권한 점검 |
| `python main.py status` | 계좌 / 포지션 / 켈리 지표 요약 |
| `python main.py universe` | 유니버스 스크리닝 결과 |
| `python main.py regime` | 종목별 국면 판별 결과 |
| `python main.py liquidate` | 보유 포지션 전량 시장가 청산 |
| `python main.py reset-halt` | 서킷브레이커 해제 |
| `python backtest.py --markets KRW-BTC --days 365` | 워크포워드 백테스트 |

---

## 4. 매매 종목 자동 선정

`UNIVERSE_MODE=auto` 는 KRW 마켓 전 종목에서 다음 필터를 모두 통과한 종목을
24시간 거래대금 순으로 `UNIVERSE_SIZE` 개 선정합니다 (6시간마다 갱신).

- 업비트 **유의 종목(market_warning)** 제외 — 항상 적용
- 업비트 **주의 종목**(급등락·입금량 급증 등) 제외 — `UNIVERSE_EXCLUDE_CAUTION`
- **스테이블코인**(USDT/USDC/DAI…) 제외 — 변동성이 없어 수수료만 나감
- **24시간 거래대금** ≥ `UNIVERSE_MIN_TRADE_PRICE_24H` (기본 300억원)
- **호가 스프레드** ≤ `UNIVERSE_MAX_SPREAD_PCT` (기본 0.2%)
- **포지션 보유 종목은 유니버스에서 절대 빠지지 않음** (관리 공백 방지)

거래대금 하한을 낮출수록 잡코인이 들어오고 슬리피지 손실이 커집니다.
안정성을 우선한다면 `UNIVERSE_MODE=fixed` + `UNIVERSE_FIXED=KRW-BTC,KRW-ETH,KRW-SOL` 을 쓰세요.

---

## 5. 백테스트 (실자본 투입 전 필수)

실거래와 **동일한 국면 분류기·전략·사이징 코드**를 과거 캔들 위에서 돌립니다.
상위 타임프레임은 해당 시점까지 완성된 봉만 노출해 룩어헤드를 차단하고,
HMM 은 `--refit-hours` 주기로 롤링 재적합합니다.

```bash
python backtest.py --markets KRW-BTC,KRW-ETH,KRW-XRP --days 365 --seed 300000
python backtest.py --markets KRW-BTC --days 730 --refit-hours 168
python backtest.py --markets KRW-SOL --days 180 --no-hmm     # 규칙 기반만 (빠름)
```

리포트는 총수익률 / CAGR / MDD / 샤프 / 승률 / 손익비 / 손익팩터 / 켈리 f\* 와
전략별·국면별 분해를 출력하고, 자산 곡선을 `data/backtest_equity.csv` 로 저장합니다.
캔들은 `data/candles/` 에 캐시되므로 두 번째 실행부터는 API 호출 없이 즉시 돌아갑니다.

---

## 6. AWS 배포

→ **[deploy/AWS_DEPLOY.md](deploy/AWS_DEPLOY.md)** 에 EC2 인스턴스 선택부터
Docker·systemd 등록, 탄력적 IP, API 키 보안 설정, 운영 명령어, 실거래 전환
체크리스트까지 정리해 두었습니다.

```bash
docker compose -f deploy/docker-compose.yml up -d --build
sudo cp deploy/upbit-bot.service /etc/systemd/system/ && sudo systemctl enable --now upbit-bot
```

---

## 7. 프로젝트 구조

```
config.py                 모든 운용 파라미터 (.env 로더)
main.py                   CLI 엔트리포인트
backtest.py               워크포워드 백테스터
core/
  upbit_client.py         SDK 래퍼 - 레이트리밋·재시도·호가단위·모의계좌
  indicators.py           RSI/ATR/ADX/볼린저/샹들리에 (외부 TA 라이브러리 무의존)
  regime.py               1계층 HMM 국면 분류기 + 규칙 폴백 + 하락 오버라이드
  sizing.py               부분 켈리 포지션 사이징
  risk.py                 서킷브레이커 (MDD/일일손실/연속손실/킬스위치)
  screener.py             거래대금 상위 유니버스 스크리닝
  state.py                포지션·거래이력 영속화 (원자적 저장)
  engine.py               2계층 파이프라인 오케스트레이터
  notifier.py             텔레그램 알림 (논블로킹)
strategies/
  base.py                 Action / MarketView / Context 프로토콜
  trend.py                변동성 돌파 + 샹들리에 출구
  grid.py                 ATR 적응형 동적 그리드
  dca.py                  한도 제어형 스마트 DCA
deploy/                   Dockerfile / compose / systemd / AWS 가이드
tests/test_core.py        단위 테스트 28건
```

전략은 주문을 직접 내지 않습니다. `Action` 목록만 반환하고, 엔진이 리스크 검증 후
실제 주문으로 옮깁니다. 덕분에 백테스트와 실거래가 같은 전략 코드를 공유합니다.

---

## 8. 소액 계좌에서 반드시 알아야 할 제약

업비트 KRW 마켓의 **최소 주문금액은 5,000원**, 수수료는 **왕복 0.1%** 입니다.
시드가 작을수록 이 마찰비용이 수익률을 지배합니다.

| 시드 | 권장 동시 포지션 | 그리드 단수 | DCA 단계 |
|---|---|---|---|
| 30만원 미만 | 2 | 3 | 3 |
| 30~100만원 | 2 | 3 | 3 |
| 100~300만원 | 3 | 4 | 4 |
| 300만원 이상 | 4~5 | 5 | 5 |

- 산출된 주문금액이 5,000원 미만이면 봇은 **주문을 내지 않고 건너뜁니다**
  (로그에 "사이징 불가"로 남습니다). 슬롯 수를 줄이면 슬롯당 금액이 커집니다.
- 부분 매도 후 잔량이 5,000원 미만이 되면 그 잔량은 **팔 수 없는 먼지**가 되므로,
  봇은 그런 경우 자동으로 전량 매도로 전환합니다.
- 손절선은 **4시간봉 ATR** 기준입니다. 15분봉 ATR(가격의 0.1~0.3%)로 손절선을 잡으면
  왕복 수수료와 호가 노이즈 안에 손절선이 들어가 사실상 100% 털립니다.

---

## 9. 실측 백테스트 결과 (반드시 읽어주세요)

아래는 **이 코드로 실제 돌린 결과**입니다. 좋게 보이려고 파라미터를 만지지 않았습니다.

```
python backtest.py --markets KRW-BTC,KRW-ETH,KRW-XRP --days 365 --seed 300000 --refit-hours 168
```

| 구성 | 총수익률 | MDD | 거래수 | 승률 | 손익팩터 |
|---|---|---|---|---|---|
| 전체 전략 (기본 설정) | **−4.48%** | −8.15% | 316건 | 52.5% | 0.73 |
| 그리드 단독 (`ALLOC_STRONG_BULL=0`, `ALLOC_VOLATILE_PULLBACK=0`) | **−0.19%** | −3.34% | 156건 | 66.0% | 0.97 |

전략별 분해 (전체 구성 기준): 그리드 −3,393원 / 추세 −8,825원 / DCA −1,218원

### 이 숫자를 어떻게 읽어야 하나

- **검증 구간의 49.7%가 STRONG_BEAR 였습니다.** 업비트 현물은 공매도가 없어 하락장에서
  할 수 있는 최선이 현금 보유입니다. 전략서도 하락 국면 자본 배분을 0%로 지정합니다.
  즉 이 1년의 절반은 봇이 구조적으로 수익을 낼 수 없는 구간이었습니다.
- **MDD는 설정 한도(30%) 대비 8%로 잘 통제되었습니다.** 리스크 계층은 의도대로 작동합니다.
- **DCA 승률 8~10%는 전략서 설계 그대로의 결과입니다.** 48시간 타임스톱이 하락 추세에서
  반등을 기다리지 않고 손실을 확정하기 때문입니다. 자본 보호에는 유리하지만
  하락장이 길면 잔손실이 누적됩니다.
- **한 구간의 백테스트는 증거가 아니라 반증 도구입니다.** 상승장 구간(예: 2023~2024)에서
  다시 돌려보고, 구간마다 결과가 어떻게 달라지는지 직접 확인하세요.

### 실전 투입 전 권장 순서

1. 여러 기간으로 재검증: `--days 730`, 그리고 상승장 구간을 잘라서 각각 확인
2. 성과가 안 나오는 구성은 **국면 배분 비중을 0으로 꺼서** 검증
   (`ALLOC_VOLATILE_PULLBACK=0` 으로 DCA 끄기 등)
3. 손익팩터가 1.2를 넘고 거래 표본이 30건 이상인 구성만 채택
4. 그 구성으로 **DRY_RUN 2~4주** 실시간 검증
5. 그래도 통과하면 잃어도 되는 금액으로 실거래 시작

> 백테스트가 플러스로 나올 때까지 파라미터를 돌리는 것은 과적합입니다.
> 전략서 2장이 지적하듯 그렇게 맞춘 값은 실전에서 그대로 무너집니다.

---

## 10. 안전 수칙

1. API 키에 **출금 권한을 절대 부여하지 마세요.** 조회 + 주문만으로 충분합니다.
2. 서버 **고정 IP 화이트리스팅**을 적용하고, 키는 60~90일마다 재발급하세요.
3. `.env` 는 절대 커밋하지 마세요 (`.gitignore` 에 포함되어 있습니다).
4. 실자본 투입 전 **백테스트 + 최소 2~4주 DRY_RUN** 을 거치세요.
5. 처음에는 **잃어도 되는 금액**으로만 시작하세요.

> 이 봇은 수익을 보장하지 않습니다. 암호화폐 시장은 팻테일과 극단적 가격 왜곡이
> 빈번하며, 어떤 백테스트 성과도 미래 수익을 담보하지 않습니다.
> 모든 투자 판단과 손익의 책임은 운용자 본인에게 있습니다.
