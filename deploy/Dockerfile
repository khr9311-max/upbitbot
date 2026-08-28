# syntax=docker/dockerfile:1
#
# 업비트 AI 자동매매 봇 - 운영 이미지
# ARM(t4g/Graviton) 과 x86 모두에서 동일하게 빌드된다.
#
#   docker build -f deploy/Dockerfile -t upbit-bot .
#
FROM python:3.12-slim

# 한국 시간 기준으로 일일 손익 기준선이 리셋되므로 타임존을 맞춘다
ENV TZ=Asia/Seoul \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends tzdata ca-certificates \
 && ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 의존성 레이어를 먼저 굳혀 소스 변경 시 재빌드를 빠르게 한다
COPY requirements.txt .
RUN pip install --upgrade pip && pip install -r requirements.txt

COPY config.py main.py backtest.py ./
COPY core/ ./core/
COPY strategies/ ./strategies/

# 상태/로그는 볼륨으로 마운트한다 (컨테이너 재시작 시 포지션 유지)
RUN mkdir -p /app/data /app/logs \
 && useradd -m -u 1000 bot && chown -R bot:bot /app
USER bot

VOLUME ["/app/data", "/app/logs"]

# 상태 파일이 최근에 갱신되고 있는지로 헬스체크 (루프가 멈추면 unhealthy)
HEALTHCHECK --interval=2m --timeout=10s --start-period=90s --retries=3 \
  CMD python -c "import os,time,sys; p='/app/data/state.json'; sys.exit(0 if os.path.exists(p) and time.time()-os.path.getmtime(p) < 600 else 1)"

ENTRYPOINT ["python", "main.py"]
CMD ["run"]
