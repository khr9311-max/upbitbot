"""로깅 설정 - 콘솔 + 일자별 로테이션 파일."""
from __future__ import annotations

import logging
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

_CONFIGURED = False

_FMT = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"


def force_utf8() -> None:
    """
    윈도우 콘솔의 기본 인코딩(cp949)에서는 한글 로그와 이모지가 깨지거나
    UnicodeEncodeError 로 프로세스가 죽는다. 표준 출력만 UTF-8 로 강제한다.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, OSError):
            pass


def setup_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    """루트 로거를 1회만 구성한다."""
    global _CONFIGURED
    if _CONFIGURED:
        return
    force_utf8()

    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    for h in list(root.handlers):
        root.removeHandler(h)

    formatter = logging.Formatter(_FMT, _DATEFMT)

    stream = logging.StreamHandler(sys.stdout)
    stream.setFormatter(formatter)
    root.addHandler(stream)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fileh = TimedRotatingFileHandler(
            log_dir / "bot.log", when="midnight", backupCount=30, encoding="utf-8"
        )
        fileh.setFormatter(formatter)
        root.addHandler(fileh)

    # 서드파티 라이브러리 소음 억제
    for noisy in ("httpx", "httpcore", "urllib3", "upbit"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    # hmmlearn 은 200회 반복 내 완전 수렴하지 않으면 매 재적합마다 경고를 낸다.
    # 부분 수렴한 모델도 국면 판별에는 충분히 쓸 수 있으므로 소음만 줄인다.
    logging.getLogger("hmmlearn").setLevel(logging.ERROR)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    return logging.getLogger(name)
