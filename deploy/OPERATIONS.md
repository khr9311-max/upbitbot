# 운용 가이드 (실거래 배포 후)

서버: EC2 (Ubuntu 24.04, x86_64, 2 vCPU / 933MB RAM + 스왑 1GB), 서울 리전
경로: `~/upbitbot` · 서비스: `systemd upbit-bot.service` (컨테이너 죽으면 자동 재시작)

---

## 1. 접속 방법

```bash
# 로컬(Windows, 프로젝트 폴더)에서
ssh -i upbit-ec2-key.pem ubuntu@43.202.132.125
```

- 키 파일 `upbit-ec2-key.pem` 은 프로젝트 루트에 있음. **Google Drive 동기화 폴더 안이라 계속 신경 쓰이는 지점** — 여유 될 때 `~/.ssh/` 같은 동기화 안 되는 로컬 경로로 옮기는 걸 권함 (`.gitignore` 에는 이미 `*.pem` 등록돼 있어 커밋은 안 됨).
- 서버 안에서 자주 쓰는 위치:
  ```bash
  cd ~/upbitbot
  C=deploy/docker-compose.yml
  ```

---

## 2. 지금 바로 확인할 것 — 설정 정합성

배포 시 권장했던 소액 조정이 `.env`에 아직 반영 안 됐습니다. 서버에서 확인:

```bash
cd ~/upbitbot
grep -E "^(TREND_UNIVERSE_SIZE|TREND_ALLOC_PCT|SCALP_ENABLED|SCALP_ALLOC_PCT)=" .env
```

아무것도 안 뜨면 전부 기본값(`TREND_UNIVERSE_SIZE=3`, `TREND_ALLOC_PCT=0.80`, `SCALP_ENABLED=True`, `SCALP_ALLOC_PCT=0.15`)으로 돌아가는 중입니다. 계좌가 5~10만원대라면 **단타 슬롯당 배정액이 최소주문 5,000원에 못 미쳐 단타는 사실상 작동하지 않습니다** (위험하진 않고 그냥 매매를 안 합니다). 의도한 게 아니라면:

```bash
nano .env
# 추가:
# TREND_UNIVERSE_SIZE=2
# TREND_ALLOC_PCT=0.70
# SCALP_ENABLED=False

docker compose -f deploy/docker-compose.yml up -d --build --force-recreate
```

---

## 3. 매일 확인할 것 (5분)

```bash
# 계좌/포지션/성과 요약
docker compose -f deploy/docker-compose.yml run --rm upbit-bot status

# 최근 로그 200줄
docker compose -f deploy/docker-compose.yml logs --tail 200

# 컨테이너가 살아있는지
docker compose -f deploy/docker-compose.yml ps
sudo systemctl status upbit-bot --no-pager
```

체크리스트:
- [ ] `평가자산`이 원래 넣은 금액과 크게 다르지 않은지 (급격한 변화가 있으면 원인부터 확인)
- [ ] `보유 포지션`이 이상한 종목/수량으로 잡혀있지 않은지
- [ ] `고점 대비 -X%`가 서서히 커지고 있진 않은지 (30% 근처면 서킷브레이커 발동 임박)
- [ ] 컨테이너가 재시작을 반복하고 있지 않은지 (`docker compose ps`의 STATUS, 또는 아래 4번)

텔레그램이 연동돼 있어서 매수/매도/국면전환/정지 알림이 옵니다. 별도로 안 열어봐도 큰 이벤트는 옵니다.

---

## 4. 컨테이너가 계속 재시작되는지 확인

```bash
docker compose -f deploy/docker-compose.yml ps
# STATUS 가 "Restarting" 이면 문제 있는 것
journalctl -u upbit-bot -n 50 --no-pager
```

이번에 겪었던 두 가지 원인(재발 방지용으로 기록):
- `WorkingDirectory` 오타로 systemd가 디렉터리를 못 찾음 → `status=200/CHDIR`
- `data/`·`logs/` 가 root 소유로 자동 생성돼 컨테이너 안 `bot` 유저가 못 씀 → `PermissionError: /app/logs/bot.log`

둘 다 지금은 고쳐져 있지만, **컨테이너를 삭제하고 다시 만들 때(`down` 후 `up`, 또는 볼륨 재생성)마다 재발할 수 있는 종류의 문제**라 여기 적어둡니다. 같은 에러가 다시 뜨면:

```bash
sudo chown -R 1000:1000 ~/upbitbot/data ~/upbitbot/logs
```

---

## 5. 정상 로그 vs 진짜 문제

| 로그 | 정상 여부 |
|---|---|
| `레이트리밋(429) - 0.5s 후 재시도` | 정상. 업비트 API 제한 자동 백오프. 계속 반복되면 `LOOP_INTERVAL_SEC` 를 60으로 늘릴 것 |
| `HMM 재적합 완료` | 정상. 24시간마다 국면 분류기 갱신 |
| `일봉 추세 진입` / `매수 체결` | 정상. 실제 주문 체결 (DRY_RUN=false 이므로 **진짜 돈**) |
| `단타 진입 미체결 취소 (추격 매수 금지)` | 정상. 지정가가 안 맞아서 취소한 것, 손실 없음 |
| `사이징 불가` (debug 로그) | 정상. 산출 금액이 5,000원 미만이라 주문 안 냄 |
| `Model is not converging` | 정상. hmmlearn 경고, 무시해도 됨 |
| `구조적 하락 오버라이드` / `그리드/DCA 정리` 등 청산 로그 | **주목**. 방어 로직이 실제로 발동한 것. 이유가 타당한지 `status` 로 국면 확인 |
| `MDD 서킷브레이커 발동` | **긴급**. 전량 청산 후 신규진입 영구중단. 8번 참고 |
| `일일 손실 한도 도달` | **주의**. 당일 신규진입만 중단, KST 자정 자동 해제. 원인 파악 |
| `연속 손실 쿨다운` | **주의**. 4연패 시 2시간 진입 중단 |
| `401 Unauthorized` / `no_authorization_ip` | **긴급**. API 키·IP 화이트리스트 문제. 즉시 확인 (6번) |
| `PermissionError` / `status=200/CHDIR` | **긴급**. 4번 참고 |

---

## 6. API 키 관련 이상 발생 시

```bash
docker compose -f deploy/docker-compose.yml run --rm upbit-bot check
```

`[4/5] 계좌 조회`, `[5/5] 주문 권한`이 실패하면:
- 업비트 [Open API 관리](https://upbit.com/mypage/open_api_management)에서 허용 IP가 `43.202.132.125` 그대로인지 확인 (탄력적 IP가 아니면 재부팅 시 바뀔 수 있음 — 지금 이 인스턴스가 탄력적 IP인지 AWS 콘솔에서 확인 권장)
- 키 만료/재발급 여부 확인 (60~90일 주기 권장 — 아래 9번)
- 출금 권한이 여전히 꺼져 있는지 재확인

---

## 7. 코드 업데이트 (로직 수정 후 재배포)

```bash
# 로컬에서 커밋·푸시 후

# 서버에서
cd ~/upbitbot
git pull
docker compose -f deploy/docker-compose.yml up -d --build --force-recreate
journalctl -u upbit-bot -f    # 정상 기동 확인
```

`.env`는 git에 안 올라가므로 서버 쪽 `.env`는 직접 관리해야 합니다. `config.py`에 새 설정값이 추가된 업데이트라면, 서버 `.env`에 없어도 기본값으로 동작하니 급하게 손댈 필요는 없습니다 — 다만 기본값이 의도와 맞는지는 확인하세요 (2번과 같은 상황이 재발할 수 있음).

---

## 8. 긴급 정지 / 재개

```bash
# 정지 — 다음 루프에서 보유 포지션 전량 청산 후 프로세스 종료
touch ~/upbitbot/data/STOP

# 상태 확인
docker compose -f deploy/docker-compose.yml logs --tail 50

# 재개 — STOP 파일 제거 후 재시작
rm ~/upbitbot/data/STOP
sudo systemctl restart upbit-bot
```

**MDD 서킷브레이커(고점 대비 -30%)가 발동하면 킬 스위치와 달리 자동으로 안 풀립니다:**

```bash
docker compose -f deploy/docker-compose.yml run --rm upbit-bot reset-halt
```

이건 "왜 30% 빠졌는지" 파악하고 나서 실행하세요 — 원인 파악 없이 바로 해제하면 같은 이유로 다시 빠질 수 있습니다.

---

## 9. 정기적으로 (주 1회 정도)

- [ ] `status` 로 누적 손익·승률·거래수 추이 확인
- [ ] 서버 디스크 여유 확인: `df -h /` (로그가 계속 쌓이므로)
- [ ] 서버 메모리/스왑 확인: `free -h` (933MB RAM 인스턴스라 여유가 별로 없음)
- [ ] 실제 업비트 앱에서 잔고·체결내역이 봇 로그와 일치하는지 대조

## 10. 60~90일마다

- [ ] 업비트 API 키 재발급 (기존 키 폐기 → 새 키로 `.env` 교체 → 컨테이너 재기동)
- [ ] 이 문서에 새로 겪은 문제가 있으면 5번 표에 추가

---

## 11. 자주 쓰는 명령 모음

```bash
cd ~/upbitbot
C=deploy/docker-compose.yml

docker compose -f $C logs -f --tail 200          # 실시간 로그
docker compose -f $C run --rm upbit-bot status   # 계좌/포지션/성과
docker compose -f $C run --rm upbit-bot universe # 추세 유니버스 확인
docker compose -f $C run --rm upbit-bot check    # API 연결 점검
docker compose -f $C run --rm upbit-bot liquidate --yes   # 수동 전량 청산
docker compose -f $C run --rm upbit-bot reset-halt        # 서킷브레이커 해제

sudo systemctl restart upbit-bot                 # 재시작
sudo systemctl status upbit-bot --no-pager        # systemd 상태
journalctl -u upbit-bot -f                        # systemd 로그(=컨테이너 로그와 동일)
```
