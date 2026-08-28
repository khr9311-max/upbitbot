"""텔레그램 알림 (선택 사항). 토큰이 없으면 조용히 무시한다."""
from __future__ import annotations

import threading
import time
from queue import Empty, Queue

import requests

from core.logger import get_logger

log = get_logger("notify")

_API = "https://api.telegram.org/bot{token}/sendMessage"


class Notifier:
    """
    알림 전송이 매매 루프를 막지 않도록 백그라운드 워커에서 처리한다.
    텔레그램 장애나 타임아웃이 봇 정지로 이어지지 않는 것이 핵심.
    """

    def __init__(self, settings) -> None:
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self._queue: Queue[str] = Queue(maxsize=200)
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        if self.enabled:
            self._worker = threading.Thread(target=self._run, name="notifier", daemon=True)
            self._worker.start()
        else:
            log.info("텔레그램 알림 비활성화 (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 미설정)")

    def send(self, text: str) -> None:
        if not self.enabled:
            return
        try:
            self._queue.put_nowait(text)
        except Exception:
            pass  # 큐가 가득 차면 알림을 버린다 - 매매가 우선

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                text = self._queue.get(timeout=1.0)
            except Empty:
                continue
            for attempt in range(3):
                try:
                    resp = requests.post(
                        _API.format(token=self.token),
                        json={"chat_id": self.chat_id, "text": text, "parse_mode": "HTML"},
                        timeout=10,
                    )
                    if resp.ok:
                        break
                    log.debug("텔레그램 응답 %s: %s", resp.status_code, resp.text[:200])
                except Exception as exc:
                    log.debug("텔레그램 전송 실패(%s)", exc)
                time.sleep(1.5 * (attempt + 1))

    def close(self) -> None:
        self._stop.set()
        if self._worker:
            self._worker.join(timeout=3)
