"""트레이딩 일지의 실수 유형 7가지와 규칙의 대응표, 경고 팝업 문구.

규칙(rule)은 감지 방법, 실수 유형(kind)은 사람이 보는 분류다.
여러 규칙이 같은 실수 유형으로 묶인다 (예: 레버리지·리스크·사이즈 급증 → 사이즈/레버리지 초과).
"""
from __future__ import annotations

# 화면 표시 순서 = 일지 실수분석 순서. liquidation 은 별도 (GAME OVER 화면)
KINDS: dict[str, dict] = {
    "no_stop": dict(
        label="손절 미설정", accent="#FF55FF", t1="STOP", t2="LOSS!", ko="손절, 걸 수 있지?",
        say="지금 걸면 HEALTH 는 그대로다.", image="no_stop.png", image_name="독거미",
        choices=["손절 걸러 감", "알았으니 닫기"]),
    "revenge": dict(
        label="복구 매매", accent="#FF3B3B", t1="TILT", t2="DETECTED", ko="방금 잃은 거, 지금 찾으려고?",
        say="손실 직후 진입은 판단이 아니라 감정이다. 잠깐 떨어져 있어라.",
        image="revenge.png", image_name="악마", choices=["쉬고 온다", "포지션 줄인다"]),
    "fomo": dict(
        label="FOMO", accent="#C6FF33", t1="FOMO", t2="ALERT!", ko="계획에 없던 진입이다",
        say="쫓아 들어간 자리는 남이 던지는 자리다. 계획부터 적어라.",
        image="fomo.png", image_name="뱀", choices=["계획부터 쓴다", "이번엔 보낸다"]),
    "oversize": dict(
        label="사이즈/레버리지 초과", accent="#FF9E3B", t1="TOO", t2="BIG!", ko="그 사이즈, 감당 돼?",
        say="한 번에 크게 잃으면 회복이 몇 배로 어렵다. 사이즈부터 줄여라.",
        image="oversize.png", image_name="상어", choices=["사이즈 줄이러 감", "레버리지 낮춘다"]),
    "impulse": dict(
        label="뇌동매매", accent="#55FFFF", t1="CALM", t2="DOWN", ko="손이 먼저 나가고 있다",
        say="이벤트 직전이나 연속 진입 중엔 차트가 의미 없다. 손 떼고 보자.",
        image="impulse.png", image_name="불붙은 폭탄", choices=["오늘은 여기까지", "발표 끝나고 본다"]),
    "overconfident": dict(
        label="승리 후 과신", accent="#FFD23F", t1="HOT", t2="HAND?", ko="연승 다음이 제일 위험하다",
        say="이긴 날 늘린 사이즈가 일주일 수익을 가져간다. 원래 사이즈로.",
        image="overconfident.png", image_name="라이터 불꽃", choices=["원래 사이즈로", "오늘은 접는다"]),
    "hope_hold": dict(
        label="근거 없는 홀딩", accent="#6B8CFF", t1="LET", t2="IT GO", ko="'오겠지'는 전략이 아니다",
        say="계획이 틀렸다고 말하고 있다. 지금 정리하면 다음 기회가 남는다.",
        image="hope_hold.png", image_name="절벽 위 닌자", choices=["지금 정리한다", "원래 손절가로"]),
    "liquidation": dict(
        label="강제 청산", accent="#55FFFF", t1="GAME", t2="OVER", ko="청산당했구나…",
        say="손절은 습관이니라…", image="", image_name="사신",
        choices=["CONTINUE?", "오늘은 쉰다"]),
}

RULE_KIND: dict[str, str] = {
    "no_stop": "no_stop",
    "revenge": "revenge",
    "daily_loss_limit": "revenge",
    "rest_day_entry": "revenge",
    "fomo": "fomo",
    "leverage": "oversize",
    "account_leverage": "oversize",
    "risk_per_trade": "oversize",
    "size_spike": "oversize",
    "liq_distance": "oversize",
    "overtrading": "impulse",
    "event_entry": "impulse",
    "overconfident": "overconfident",
    "stop_moved": "hope_hold",
    "stop_removed": "hope_hold",
    "stop_breach": "hope_hold",
    "averaging_down": "hope_hold",
    "liquidation": "liquidation",
}

RULE_LABEL: dict[str, str] = {
    "no_stop": "손절 주문 없음",
    "revenge": "손실 직후 재진입",
    "daily_loss_limit": "일 손실 한도 후 진입",
    "rest_day_entry": "쉬기로 한 날 진입",
    "fomo": "급등락 추격 진입",
    "leverage": "포지션 레버리지 초과",
    "account_leverage": "계좌 레버리지 초과",
    "risk_per_trade": "1회 리스크 초과",
    "size_spike": "평소보다 큰 진입",
    "liq_distance": "청산가 근접",
    "overtrading": "과매매",
    "event_entry": "지표 발표 직전 진입",
    "overconfident": "연승 후 사이즈 확대",
    "stop_moved": "손절가 밀기",
    "stop_removed": "손절 주문 취소",
    "stop_breach": "계획 손절가 이탈 후 보유",
    "averaging_down": "계획 없는 물타기",
    "liquidation": "강제 청산",
}


def kind_of(rule: str) -> str:
    return RULE_KIND.get(rule, "impulse")


REST_DAY_KEY = "rest_day"   # GAME OVER 에서 "오늘은 쉰다"를 고르면 그날(UTC 날짜)을 저장
