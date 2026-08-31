# AWS EC2 배포 가이드

소액 계좌 24시간 무중단 운용을 위한 EC2 + Docker + systemd 배포 절차입니다.
전략서 5장의 "제로 비용 오픈소스 인프라" 원칙에 따라 상용 봇 구독료 없이
월 고정비를 인스턴스 비용만으로 억제하는 것이 목표입니다.

---

## 0. 비용 개념부터

시드가 30만원일 때 **월 서버비는 그 자체가 확정 손실**입니다.

| 인스턴스 | 월 비용(서울 리전, 온디맨드) | 30만원 대비 월 부담 |
|---|---|---|
| t4g.nano (2 vCPU / 0.5GB) | 약 $3.4 | 1.5% |
| **t4g.small (2 vCPU / 2GB)** | 약 $13.5 | 6.0% |
| t4g.micro (2 vCPU / 1GB) | 약 $6.8 | 3.0% |

> **권장**: 첫 1년은 **프리 티어 t4g.small (2024년 이후 신규 계정은 t2.micro/t3.micro)** 를
> 쓰거나, 프리 티어가 없다면 **t4g.micro + 스왑 1GB** 조합을 쓰세요.
> HMM 적합은 순간적으로 200~300MB를 쓰므로 0.5GB(nano)는 스왑 없이는 부족합니다.
> 시드가 100만원을 넘기 전까지는 **1년 예약 인스턴스(약 40% 할인)** 도 검토할 만합니다.

---

## 1. EC2 인스턴스 생성

1. **리전**: `ap-northeast-2` (서울) — 업비트 API 와 물리적으로 가까워 지연이 가장 낮습니다.
2. **AMI**: Ubuntu Server 24.04 LTS (**ARM64**)
3. **인스턴스 타입**: `t4g.micro` 또는 `t4g.small`
4. **스토리지**: gp3 20GB
5. **키페어**: 새로 생성해 `.pem` 파일 안전 보관
6. **보안 그룹**:
   - 인바운드: SSH(22) 를 **본인 IP 에서만** 허용. 그 외 전부 차단.
   - 아웃바운드: 전체 허용 (업비트 API·텔레그램 호출용)

### 고정 IP(Elastic IP) 할당 — 필수

업비트 API 키의 IP 화이트리스팅에 등록할 주소가 재부팅마다 바뀌면 안 됩니다.

```
EC2 콘솔 > 탄력적 IP > 탄력적 IP 주소 할당 > 인스턴스에 연결
```

> 탄력적 IP 는 **인스턴스에 연결된 상태에서는 무료**, 연결 해제 상태에서 과금됩니다.
> (2024년 2월부터는 연결 상태여도 IPv4 공인 주소 요금이 시간당 $0.005 부과됩니다. 월 약 $3.6)

---

## 2. 서버 초기 설정

```bash
ssh -i ~/upbit-ec2-key.pem ubuntu@43.202.132.125

# 시스템 업데이트 + 타임존
sudo apt update && sudo apt upgrade -y
sudo timedatectl set-timezone Asia/Seoul

# Docker 설치
sudo apt install -y ca-certificates curl git
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker ubuntu
newgrp docker   # 또는 재로그인

# 스왑 1GB (t4g.micro/nano 에서 HMM 적합 시 OOM 방지)
sudo fallocate -l 1G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 3. 봇 배포

```bash
cd ~
git clone <저장소 주소> upbit-bot     # 또는 scp 로 업로드
cd upbit-bot

cp .env.example .env
nano .env      # API 키 입력, DRY_RUN=True 로 두고 시작
chmod 600 .env # 키 파일 권한 잠그기

# data/ logs/ 는 .gitignore 대상이라 클론 직후에는 존재하지 않는다. 이 상태로
# docker compose 를 올리면 Docker 가 바인드마운트 디렉터리를 root 소유로
# 자동 생성해버려서, 컨테이너 안의 bot 유저(uid 1000)가 로그를 못 쓰고
# PermissionError 로 죽는다. 반드시 먼저 만들고 소유권을 맞춰둘 것.
mkdir -p data logs
sudo chown -R 1000:1000 data logs

# 이미지 빌드 + 기동
docker compose -f deploy/docker-compose.yml up -d --build

# 로그 확인
docker compose -f deploy/docker-compose.yml logs -f
```

### 배포 전 점검

```bash
docker compose -f deploy/docker-compose.yml run --rm upbit-bot check
docker compose -f deploy/docker-compose.yml run --rm upbit-bot universe
docker compose -f deploy/docker-compose.yml run --rm upbit-bot regime
```

---

## 4. systemd 로 자동 기동 등록

```bash
sudo cp deploy/upbit-bot.service /etc/systemd/system/
sudo nano /etc/systemd/system/upbit-bot.service   # WorkingDirectory 경로 확인
sudo systemctl daemon-reload
sudo systemctl enable --now upbit-bot

systemctl status upbit-bot
journalctl -u upbit-bot -f
```

이제 EC2 를 재부팅해도 봇이 자동으로 살아납니다.

---

## 5. 업비트 API 키 보안 설정 (전략서 5장)

업비트 [Open API 관리](https://upbit.com/mypage/open_api_management) 에서:

1. **자산 조회** ✅ 체크
2. **주문 조회** ✅ 체크
3. **주문하기** ✅ 체크
4. **출금하기** ❌ **절대 체크하지 말 것** — 키가 유출돼도 자금이 빠져나갈 수 없게 하는 최후의 방어선
5. **허용 IP 주소**: 위에서 할당한 탄력적 IP 만 등록
6. 키는 **60~90일 주기로 재발급**. 캘린더에 알림을 걸어두세요.

> `.env` 는 `.gitignore` 에 이미 포함되어 있습니다. 절대 커밋하지 마세요.

---

## 6. 운영 명령어

```bash
cd ~/upbit-bot
C=deploy/docker-compose.yml

docker compose -f $C logs -f --tail 200      # 실시간 로그
docker compose -f $C run --rm upbit-bot status    # 계좌/포지션/성과
docker compose -f $C restart                 # 재시작
docker compose -f $C down                    # 정지

# 긴급 정지 - 킬 스위치 (다음 루프에서 전량 청산 후 종료)
touch data/STOP
# 해제
rm data/STOP && docker compose -f $C restart

# 수동 전량 청산
docker compose -f $C run --rm upbit-bot liquidate --yes

# 서킷브레이커(MDD 정지) 해제
docker compose -f $C run --rm upbit-bot reset-halt
```

---

## 7. 실거래 전환 체크리스트

전략서 6장은 실자본 투입 전 **워크포워드 백테스트 + 2~4주 실시간 DRY_RUN** 을 요구합니다.

- [ ] `python backtest.py --markets ... --days 365` 결과에서 MDD 가 설정 한도 이내
- [ ] 백테스트 거래 표본 30건 이상, 손익팩터 > 1.2
- [ ] EC2 에서 `DRY_RUN=True` 로 최소 2주 무중단 가동 (재시작·네트워크 단절 복구 확인)
- [ ] `status` 성과 지표가 백테스트와 크게 다르지 않음
- [ ] 텔레그램 알림 정상 수신
- [ ] API 키 출금 권한 비활성화 + IP 화이트리스팅 확인
- [ ] `data/` 볼륨 마운트 확인 (컨테이너 재생성 후 포지션 유지되는지 테스트)

모두 통과하면 `.env` 의 `DRY_RUN=False` 로 바꾸고 재시작합니다.
**처음에는 반드시 잃어도 되는 금액으로 시작하세요.**

```bash
nano .env    # DRY_RUN=False
docker compose -f deploy/docker-compose.yml up -d --force-recreate
```

---

## 8. 백업 (선택)

포지션 상태 파일은 매일 S3 로 백업해두면 인스턴스 장애 시 복구가 쉽습니다.

```bash
# crontab -e
0 4 * * * cd /home/ubuntu/upbit-bot && aws s3 cp data/state.json s3://<버킷>/upbit-bot/state-$(date +\%F).json
```

---

## 9. 자주 겪는 문제

| 증상 | 원인 / 해결 |
|---|---|
| `401 Unauthorized` | API 키 오타, 또는 서버 IP 가 화이트리스트에 없음. `curl ifconfig.me` 로 실제 송신 IP 확인 |
| `429 Too Many Requests` 로그 반복 | 정상 (자동 백오프 재시도). 계속되면 `LOOP_INTERVAL_SEC` 를 60 으로 늘릴 것 |
| 컨테이너가 OOM 으로 재시작 | 스왑 설정 누락. 3번 항목의 스왑 1GB 를 적용하거나 `REGIME_USE_HMM=False` 로 전환 |
| 주문이 전혀 안 나감 | `MAX_CONCURRENT_POSITIONS`/시드 대비 산출 금액이 5,000원 미만일 수 있음. `logs` 에서 "사이징 불가" 메시지 확인 |
| 재시작 후 포지션이 사라짐 | `data/` 볼륨 마운트 누락. compose 파일의 volumes 확인 |
| 시간이 UTC 로 찍힘 | `TZ=Asia/Seoul` 환경변수 확인. 일일 손실 한도 리셋 시각에 영향을 줌 |
