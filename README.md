# Safety Net — 포지션 자가점검 시스템

실시간으로 내 포지션을 점검해 과도한 레버리지, 손절 미설정·회피, 물타기, 복수매매 같은 위험 패턴을 경고하고 규율 점수로 기록한다. 읽기 전용이며 주문 기능은 없음.

## 진행 단계
| 단계 | 내용 | 상태 |
|---|---|---|
| 1 | 설정 파일 + Hyperliquid 수집기 + 점검 스크립트 | 완료 |
| 2 | 규칙 엔진(위반 감지·점수) + 매매 계획 입력 + SQLite 기록 | 완료 |
| 3 | Bybit 수집기 (읽기 전용 API) | 완료 |
| 4 | 앱 창(대시보드·매매 계획·실수 기록) + 경고 팝업 7종 + 트레이·알림 + 신규 규칙(FOMO·지표 발표 직전·승리 후 과신) | 1차 완료 |
| 5 | 이모 PC 배포 (install.bat) + GitHub 자동 업데이트 + 첫 실행 거래소 키 설정 + 자동 실행 | 진행 중 |

## 설정 프리셋
| 파일 | 용도 | 주요 값 |
|---|---|---|
| `config.yaml` | 본인용 | Hyperliquid + Bybit, 레버리지 BTC 20배·기타 5배, 1회 리스크 2% |
| `presets/other/config.yaml` | 상대용 | Bybit 전용, 레버리지 BTC 10배·기타 5배, 1회 리스크 5% |

## 폴더 구조
```
SafetyNet/
├─ config.yaml                 경고 기준값 (본인용)
├─ presets/other/config.yaml   경고 기준값 (상대용)
├─ .env.example                → .env 로 복사 후 지갑 주소 입력
├─ requirements.txt
├─ SafetyNet.bat               앱 실행 (더블클릭)
├─ app.py                      앱 (창 + 트레이 + 경고 팝업)
├─ service.py                  앱 뒤의 감시 스레드 + 화면 API
├─ events.yaml                 경제지표 발표 일정 (CPI·FOMC, UTC)
├─ ui/                         앱 화면 (index.html, 글꼴, 사신 그림)
├─ alerts/                     경고 팝업 그림 (직접 넣는 폴더, 업데이트로 안 지움)
├─ run.py                      창 없이 실행 (터미널 출력)
├─ plan.py                     매매 계획 입력
├─ check.py                    연결 점검 (거래소별 계좌 1회 조회)
├─ check_hyperliquid.py        (이전 버전 점검 스크립트, check.py 로 대체)
├─ conftest.py
├─ monitor/
│  ├─ __init__.py
│  ├─ config.py                설정 읽기 (기본값 + 사용자 설정 병합)
│  ├─ defaults.py              모든 설정의 기본값
│  ├─ models.py                공통 데이터 형식
│  ├─ rules.py                 위반 감지 규칙 + 점수
│  ├─ mistakes.py              실수 7유형 ↔ 규칙 대응표, 팝업 문구
│  ├─ retro.py                 과거 체결로 실수 소급 계산 (처음 1회)
│  ├─ history.py               실수 기록 탭 집계 (기간 비교·월별·일별)
│  ├─ store.py                 기록 DB (data/safetynet.db)
│  └─ exchanges/
│     ├─ __init__.py
│     ├─ hyperliquid.py
│     └─ bybit.py
└─ tests/                      테스트 88개
   ├─ __init__.py
   ├─ test_bybit.py
   ├─ test_hyperliquid.py
   ├─ test_rules.py
   └─ test_run_and_plan.py
```

## 실수 유형 ↔ 감지 규칙
| 실수 유형 (일지) | 감지 규칙 | 팝업 그림 |
|---|---|---|
| 손절 미설정 | 진입 후 3분 넘게 손절 주문 없음 | alerts/no_stop.png (독거미) |
| 복구 매매 | 손실 청산 후 쿨다운 안에 재진입 · 손실 중 반대 방향 전환 · 일 손실 한도 후 진입 | alerts/revenge.png (악마) |
| FOMO | 매매 계획 없이, 직전 15분 5% 이상 움직인 방향으로 신규 진입 | alerts/fomo.png (뱀) |
| 사이즈/레버리지 초과 | 포지션·계좌 레버리지 · 1회 리스크 · 평소 2배 넘는 진입 · 청산가 근접 | alerts/oversize.png (상어) |
| 뇌동매매 | 과매매 · CPI·FOMC 발표 30분 전~5분 후 진입 | alerts/impulse.png (불붙은 폭탄) |
| 승리 후 과신 | 최근 청산 3건 연속 수익 직후 평소 1.5배 넘는 진입 | alerts/overconfident.png (라이터) |
| 근거 없는 홀딩 | 손절가 밀기 · 손절 취소 · 계획 손절가 이탈 후 보유 · 계획 없는 물타기 | alerts/hope_hold.png (절벽 위 닌자) |
| 강제 청산 | 청산 체결 | GAME OVER (사신) |

과거 기록: 처음 실행 때 체결 기록으로 판단 가능한 유형(복구 매매·과매매·사이즈·승리 후 과신·강제 청산)을 소급 계산해 "소급" 표시로 저장한다.

## 4단계 실행 (앱) — PowerShell
```
cd $env:USERPROFILE\Documents\SafetyNet
& "$env:LOCALAPPDATA\Python\pythoncore-3.14-64\python.exe" -m pip install -r requirements.txt
```
이후 `SafetyNet.bat` 더블클릭. 창을 닫으면 트레이로 숨고 감시는 계속됨. 완전 종료는 트레이 아이콘 → 종료.
문제가 생기면 `data\app.log` 확인.

## 이모 PC 설치 (5단계)
1. https://github.com/ICHIMARUJ/safetynet-monitor/raw/main/releases/SafetyNet.zip 다운로드 → `문서\SafetyNet` 폴더에 압축 풀기
2. `install.bat` 더블클릭 → Python 이 없으면 자동 설치, 패키지 설치, 이모용 설정(`presets/other`) 복사, 바탕화면 아이콘 생성 후 실행
3. 처음 화면에서 Bybit **읽기 전용** API 키 입력 → 연결 테스트 → 저장 (키는 그 PC 에만 Windows 암호화로 저장, 읽기 전용이 아니면 저장 거부)
4. ⚙ 설정 → PC 켜면 자동 실행 ON

## 자동 업데이트
- 앱이 시작 1분 뒤, 이후 6시간마다 `update.json` 의 주소에서 `version.json` 을 확인
- 새 버전이면 zip 다운로드 → sha256 확인 → 바뀔 파일 백업(`data/backup/<이전 버전>`) → 코드만 교체 → 패키지 목록이 바뀌었으면 설치 → 자동 재시작
- **절대 안 바뀌는 것**: `config.yaml`(각자의 경고 기준), `.env`·`data/`(키·기록·일지 설정), `alerts/` 그림
- 새 버전 만들기: `python tools/release.py --repo OWNER/REPO --version 1.0.1 --notes "변경 내용"` → 바뀐 파일(releases/ zip, version.json 포함)을 main 에 커밋·푸시

## 업데이트 원칙
- 코드(monitor/, run.py 등)는 본인용·상대용이 같다. 기능 수정은 양쪽에 똑같이 반영된다.
- 경고 기준값은 각자의 config.yaml 에만 있고, 업데이트로 덮어쓰지 않는다.
- 새 기능에 새 설정 항목이 생기면 defaults.py 의 기본값으로 동작하므로, 상대 config.yaml 을 고치지 않아도 된다.

## 1단계 실행 (Windows)
```
cd %USERPROFILE%\Documents\SafetyNet
pip install -r requirements.txt
copy .env.example .env        (메모장으로 열어 HL_WALLET 에 지갑 주소 입력)
python -m pytest tests -q     (테스트 통과 확인)
python check_hyperliquid.py   (실제 계좌 조회)
```

## 2단계 실행
```
python -m pytest tests -q      (테스트 68개 통과 확인)
python run.py --once --log result.txt   (한 번 점검, 결과를 result.txt 에도 저장)
python run.py                  (계속 감시, Ctrl+C 로 종료)
```
PowerShell 의 `> 파일` 저장은 한글이 깨지므로 `--log 파일` 옵션을 쓸 것.

매매 계획 입력 예시:
```
python plan.py add hyperliquid BTC long --entries 95000,93000 --stop 91000 --targets 100000,105000 --note "4h 지지 반등"
python plan.py list
python plan.py close 1
```
- entries 근처(±0.5%) 추가 진입은 물타기로 보지 않음
- stop 은 SL 주문이 없어도 감시함. 이탈 후 60초 넘게 보유하면 위반
- 포지션이 청산되면 연결된 계획은 자동 종료

## Hyperliquid 계정 모드
- 통합 계정(unifiedAccount): 담보가 현물 잔고에 있으므로 자본 = 현물 스테이블 잔고(USDC 등, 미실현 손익 포함)로 계산함
- 포트폴리오 마진: 스테이블이 아닌 담보는 아직 자본에 포함하지 않으며, 그 경우 실행 화면에 안내가 뜸
- 강제 청산 체결은 별도로 인식해 "liquidation" 위반(-30점)으로 기록함
- 처음 실행 시 받을 수 있는 과거 체결을 모두(최대 10,000건) 불러와 기준 데이터로 씀

## Bybit 연결 (3단계)

### 1. 읽기 전용 API 키 발급 (Bybit 웹사이트에서만 가능, 앱 불가)
1. 오른쪽 위 계정 아이콘 → API → API Management → Create New Key
2. System-generated API Keys 선택
3. API Transaction 선택, 이름 입력 (예: SafetyNet)
4. API Key Permissions: **Read-Only** 선택. 거래·출금·이체 권한은 모두 끄기
5. IP 제한: 
   - 지정 안 함 → 3개월 후 만료되므로 그때 재발급 필요
   - 집 IP 지정 → 만료 없음. 단 집 IP 가 바뀌면 접속이 막힘 (오류 10010)
6. 발급 직후 나오는 Secret 은 다시 볼 수 없으므로 바로 복사

### 2. .env 에 입력 (키는 채팅·메신저에 붙여넣지 말 것)
`notepad .env` 로 열어 아래 두 줄을 추가하고 저장:
```
BYBIT_API_KEY=발급받은키
BYBIT_API_SECRET=발급받은시크릿
```

### 3. 확인
```
python check.py bybit --log check.txt
```
- 자본·포지션·손절 주문이 Bybit 화면과 같은지 확인
- 처음 run.py 실행 시 최근 90일 체결을 불러와 기준 데이터로 씀

### Bybit 처리 방식
- USDT·USDC 무기한(linear)만 감시. 인버스·옵션·현물은 제외
- 포지션에 직접 건 손절가(TP/SL 설정)와 조건부 손절 주문 모두 손절로 인정
- 체결 기록에 직전 수량이 없어서, 현재 포지션에서 거꾸로 계산함 → 단방향 모드 기준 (헤지 모드면 안내 표시)
- 손익은 Bybit 청산손익(수수료 차감 후)을 사용. 청산손익 기록이 늦게 생기면 최대 2분 기다렸다가 처리
- PC 시계가 어긋나 오류가 나면 Bybit 서버 시간에 맞춰 자동 재시도
