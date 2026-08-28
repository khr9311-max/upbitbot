# Upbit Official API & Strategy Toolkit 정리 문서

업비트 공식 저장소([github.com/upbit-official](https://github.com/upbit-official))의 핵심 내용 및 자동매매 봇 개발 시 참고 가이드입니다.

---

## 1. 공식 저장소 개요

| 저장소명 | 설명 | 핵심 용도 |
|---|---|---|
| **upbit-sdk-python** | 업비트 공식 Python SDK (`pip install upbit-sdk`) | 시세 조회, 잔고 확인, 주문 실행, WebSocket 수신 |
| **upbit-strategy-toolkit** | 공식 전략 설계 & 백테스팅 프레임워크 | 과거 캔들 데이터 수집, 지표 계산, 백테스트 리포트 |
| **upbit-agent-skills** | AI 에이전트 연동 스킬 | 자연어 기반 업비트 API 제어 |
| **upbit-cli** | 공식 CLI 툴 | 터미널 명령어로 계좌/주문/시세 즉시 제어 |

---

## 2. 공식 SDK (`upbit-sdk`) 사용법

### 2.1 클라이언트 초기화
```python
import os
from upbit import Upbit, AsyncUpbit
from dotenv import load_dotenv

load_dotenv()

client = Upbit(
    access_key=os.getenv("UPBIT_ACCESS_KEY"),
    secret_key=os.getenv("UPBIT_SECRET_KEY"),
    environment="kr"  # 기본값: kr (한국 원화 마켓)
)
```

### 2.2 핵심 API 호출 예시

```python
# 1. 자산/계좌 잔고 조회
accounts = client.accounts.list()

# 2. 현재가(Ticker) 조회
tickers = client.tickers.list(markets="KRW-BTC")

# 3. 캔들 조회 (분/일/주/월)
# 15분봉 200개 조회
candles = client.candles.minutes.list(unit=15, market="KRW-BTC", count=200)
# 일봉 조회
day_candles = client.candles.days.list(market="KRW-BTC", count=30)

# 4. 시장가 매수 (price 방식: 1회당 매수 금액(KRW))
order_buy = client.orders.create(
    market="KRW-BTC",
    side="bid",
    price="5000",        # 원화(KRW) 금액
    ord_type="price"
)

# 5. 시장가 매도 (market 방식: 매도 수량(코인 단위))
order_sell = client.orders.create(
    market="KRW-BTC",
    side="ask",
    volume="0.0001",     # 코인 수량
    ord_type="market"
)

# 6. 지정가 매수/매도 (limit 방식)
order_limit = client.orders.create(
    market="KRW-BTC",
    side="bid",
    price="120000000",   # 희망 가격
    volume="0.0001",     # 희망 수량
    ord_type="limit"
)
```

---

## 3. 공식 예제 전략 구조

### 3.1 DCA (Dollar-Cost Averaging / 적립식 분할매수)
- **원리**: 설정된 간격(주기)마다 정해진 원화 금액으로 시장가 분할 매수.
- **주요 파라미터**:
  - `BUY_AMOUNT`: 1회 매수 금액 (최소 5,000 KRW 이상)
  - `TOTAL_ROUNDS`: 총 매수 횟수
  - `INTERVAL_SEC`: 매수 간격(초)

### 3.2 TP/SL (Take-Profit / Stop-Loss / 익절 및 손절)
- **원리**: 업비트는 Stop-Limit 예약 주문을 제공하지 않으므로, 클라이언트에서 실시간 시세를 감시하다 목표 수익률 도달 시 시장가 매도.
- **주요 파라미터**:
  - `TP_PERCENT`: 익절 목표 수익률 (예: +3.0%)
  - `SL_PERCENT`: 손절 제한 손실률 (예: -2.0%)
  - `POLL_INTERVAL`: 현재가 확인 주기 (REST API: 1~3초, WebSocket: 실시간)

### 3.3 거래대금 상위 스크리닝 & 보조지표 (RSI, MA)
- **거래대금 상위 종목**: `client.tickers.list_by_quote_currencies(quote_currencies="KRW")` 후 `acc_trade_price_24h` 기준 정렬.
- **RSI 지표**: 14일/14캔들 기준 U(상승폭)/D(하락폭)의 지수가중평균(EMA) 또는 단순평균(SMA)으로 계산.

---

## 4. 업비트 거래 규칙 및 주의사항

1. **최소 주문 금액**: 원화(KRW) 마켓 기준 **최소 5,000 KRW**
2. **거래 수수료**: 원화 마켓 기본 **0.05%**
3. **요청 제한 (Rate Limit)**:
   - 시세/캔들 조회(Quotation): 초당 약 10회
   - 주문/계좌 조회(Exchange): 초당 약 8회
   - *초과 시 HTTP 429 Too Many Requests 에러 발생*
4. **보안 규칙**:
   - `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`는 절대 GitHub에 올리지 않고 `.env`로 관리
   - 출금 권한은 API 키 발급 시 반드시 비활성화(조회 및 거래 권한만 사용)
   - 실거래 전 반드시 `DRY_RUN=true` (모의 테스트) 검증
