"""과거 체결로 실수 기록을 소급 계산한다 (처음 한 번).

체결 기록만으로 판단할 수 있는 규칙만 재생한다:
  강제 청산, 복구 매매, 과매매, 평소보다 큰 진입, 승리 후 과신, 지표 발표 직전 진입
손절 주문·레버리지·청산가 같은 "그때의 포지션 상태"가 필요한 규칙은 과거 기록이 없어 제외.
FOMO 는 진입마다 과거 시세를 조회해야 해서 제외 (실시간부터 감지).

기록은 source='retro' 로 저장되고, 실시간 감지와 같은 중복 키를 써서 두 번 세지 않는다.
"""
from __future__ import annotations

import time
from typing import Any, Optional

from monitor.models import Fill
from monitor.rules import RuleEngine
from monitor.store import Store

RETRO_KEY = "retro_done_v1"
BUCKET_MS = 10 * 60_000   # 10분 단위로 묶어 재생 (복구 매매 쿨다운보다 짧게)


def run_retro(cfg: dict[str, Any], store: Store, events: Optional[list[tuple[int, str]]] = None,
              force: bool = False) -> Optional[int]:
    """소급 계산 실행. 이미 했으면 None, 했으면 새로 기록된 건수."""
    if store.get_kv(RETRO_KEY) and not force:
        return None
    fills = store.fills_between(0)
    clock = {"t": 0}
    eng = RuleEngine(cfg, store, now_fn=lambda: clock["t"], events=events,
                     state_key=None, source="retro")
    count = 0
    batch: list[Fill] = []
    for f in fills:
        if batch and f.ts // BUCKET_MS != batch[0].ts // BUCKET_MS:
            clock["t"] = batch[-1].ts
            count += len(eng.process_fills(batch))
            batch = []
        batch.append(f)
    if batch:
        clock["t"] = batch[-1].ts
        count += len(eng.process_fills(batch))
    store.set_kv(RETRO_KEY, {"at": int(time.time() * 1000), "fills": len(fills), "found": count})
    return count
