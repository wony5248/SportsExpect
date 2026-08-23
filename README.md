# Dugout Lab — KBO / MLB 데이터 기반 승부예측

> 기본 권장 배포는 **Vercel(React + FastAPI) + Supabase(PostgreSQL + Cron)** 입니다. 실제 배포 순서는 [Vercel + Supabase 배포 가이드](docs/VERCEL_SUPABASE.md)를 따르세요. Oracle VM 문서는 기존 설치 유지보수용으로만 남겨 두었습니다.

공식 KBO·MLB 공개 데이터, 버전 관리된 통계 모델, 20,000회 Monte Carlo 시뮬레이션을 결합한 소규모 예측 서비스입니다. 기본 모드는 외부 AI 없이 동작하며, 명시적으로 활성화하면 Claude의 구조화된 보조 분석을 제한된 가중치로 앙상블할 수 있습니다.

> 예측은 정보 제공용 통계 추정치이며 베팅 수익이나 경기 결과를 보장하지 않습니다.

## 베이스라인 모델이란

베이스라인은 ChatGPT 같은 생성형 AI가 아닙니다. 시즌 승률, 최근 득실점, OPS, 홈/원정 성적, 선발 ERA·WHIP, 라인업을 고정된 보수적 계수로 결합한 **비교 기준 예측 공식**입니다. 득점은 Poisson 모형으로 추정하고 20,000회 시뮬레이션 결과와 승패 확률을 앙상블합니다.

현재 후보 버전은 `KBO_MATCHUP_V10`, `MLB_MATCHUP_V9`입니다. 당일 전체 팀 기록에서 리그 득점 환경을 동적으로 계산하고 공격·상대 수비 강도, AVG·OBP·SLG·OPS, 선발 ERA·FIP·WHIP와 예상 소화 이닝, 최근 5/10경기, 팀 간 맞대결 득실점, 불펜 부담 proxy를 결합합니다. 시즌 초 팀 기록, 미확정 선발·라인업, 최근 경기와 맞대결의 소표본은 리그 평균 쪽으로 축소합니다. KBO는 공식 투수 상대팀별 기록과 `투수 VS 타자`의 해당 시즌 맞대결 기록, MLB는 공식 투수 game log와 확정 라인업의 타자-선발 `vsPlayerTotal`을 표본 수에 따라 축소 반영합니다. 득점 분포는 공통 경기 환경과 팀별 득점 충격을 분리한 9이닝 과산포 gamma-Poisson Monte Carlo로 계산합니다. 화면에는 소수점 한 자리의 팀별 평균 예상 스코어와 그 합계, 팀·총점 80% 예상 범위, 5점차 이상 확률을 함께 표시합니다. 가장 자주 나온 정수 스코어는 상세 시나리오로 분리합니다.

화면과 API의 경기 날짜는 항상 한국 시간(KST) 기준입니다. MLB 카드는 MLB 공식 `officialDate`를 미국 현지 날짜로 함께 표시하므로, 미국 현지 8월 21일 경기는 한국 시간으로 8월 22일 카드에 나타납니다. 시즌 일정 동기화 시 MLB 정규시즌 전체 일정과 KBO가 올해 공개한 월별 전체 일정(완료·예정)을 저장하며, 화면의 `시즌 경기 아카이브`에서 경기 있는 날짜를 선택할 수 있습니다.

## 빠른 시작

Python 3.11+와 Node.js 20+가 필요합니다.

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
python -m backend.app.cli refresh --league ALL
uvicorn backend.app.main:app --reload --port 8000
```

개발 프런트엔드는 `cd frontend && npm install && npm run dev`로 실행합니다. 프로덕션 빌드가 있으면 FastAPI가 `frontend/dist`를 직접 제공합니다.

## 로컬 Docker 확인

```bash
export ADMIN_TOKEN='충분히-긴-관리자-토큰'
docker compose up -d --build
```

`http://127.0.0.1:8000`으로 접속합니다. Docker 구성은 로컬 확인용 API와 SQLite만 실행하며 자동 수집은 하지 않습니다. 수동 갱신은 API 컨테이너에서 실행합니다.

```bash
docker compose exec api python -m backend.app.cli refresh --date 2026-08-22 --league ALL --force
```

## 자동 업데이트와 스냅샷

- KBO·MLB 전체 갱신: Supabase Cron으로 1시간마다
- 다음 날 일정 선취득: MLB 00:20, KBO 13:10 KST
- 경기 근처 3시간: 전체 갱신 사이 30분 시점에 해당 경기 집중 갱신
- 정확 시점 수집: 매분 대상 경기만 확인해 시작 24시간·3시간·60분·15분 전 ±2.5분 안에 불변 스냅샷 저장
- 모델 생명주기: KBO 05:30, MLB 16:30 KST에 날짜순 재학습·승격·롤백 평가
- 중복 요청: PostgreSQL advisory lock으로 차단

선발 ID, 전체 타순, 선수 생산력, 팀 기록이 달라지면 새 예측을 만들고 변경 이유를 저장합니다. 같은 입력은 예측을 중복 저장하지 않지만 `T-24h`, `T-3h`, `T-60m`, `T-15m` 시점 스냅샷은 남깁니다. 경기 시작 후 생성된 값은 평가에서 제외합니다.

리그별 평가 가능 표본이 200경기에 도달하면 NumPy 기반 L2 로지스틱 승률 모델과 ridge 홈·원정 득점 모델을 자동 학습합니다. 마지막 20%(최소 40경기)는 날짜순 검증용으로 분리하며, 후보가 동일 경기의 현 운영 모델보다 개선되고 성능 하한을 모두 지날 때만 champion으로 승격합니다. 승격 뒤 새 50경기에서 이전 모델 대비 성능 하락 한도를 넘으면 이전 champion으로 자동 롤백합니다. 표본이 부족한 동안은 버전 관리된 기존 베이스라인이 계속 운영됩니다.

## 선택형 실제 시장 기준점

베트맨·라이브스코어의 동적 화면을 스크래핑하지 않습니다. 선택적으로 The Odds API의 구조화된 `h2h`, `totals` 응답을 받아 북메이커 중앙 기준 총점과 마진 제거 승률을 모델 값 옆에 **비교용**으로 표시합니다. 시장 값은 모델 입력으로 쓰지 않아 순환 참조를 막습니다.

```bash
# .env에 추가한 뒤 재기동
ODDS_API_KEY=발급받은키
ODDS_API_REGIONS=us
docker compose up -d --build
```

키가 없으면 이 수집만 자동으로 건너뛰므로 무료 기본 운영은 그대로 유지됩니다. KBO는 매일 12:00 KST, MLB는 매일 00:00 KST에 한 지역·두 마켓을 한 번씩 조회합니다. 리그당 2크레딧, 두 리그 합계 하루 4크레딧으로 30일 기준 약 120크레딧입니다. 다음 날 경기까지 같은 응답에서 함께 저장하며 성공한 일일 슬롯은 다시 호출하지 않습니다.

### The Odds API 무료 키 발급·등록

1. [The Odds API Get Access](https://the-odds-api.com/#get-access)에서 `Starter FREE · 500 credits/month`의 `START`를 선택합니다.
2. 이메일 주소로 가입하고 받은 API 키를 복사합니다. 원문 키는 Git, 프런트 코드, 메신저에 올리지 않습니다.
3. Oracle VM은 `/home/ubuntu/sports-expect/.env`에 `ODDS_API_KEY=발급키`와 `ODDS_API_REGIONS=us`를 넣고 `docker compose -f compose.yaml -f compose.oracle.yaml up -d --build`를 실행합니다.
4. Vercel은 백엔드 프로젝트의 `Settings → Environment Variables`에 같은 두 값을 `Production` 대상으로 등록한 뒤 재배포합니다. Supabase와 프런트 Vercel 프로젝트에는 배당 키를 등록하지 않습니다.
5. 배포 후 관리자 API의 `scope=market`을 호출하거나 다음 정기 시각을 기다립니다. 응답이 `collected`면 수집, `already_collected`면 해당 일일 슬롯에서 이미 수집된 상태입니다.

```bash
curl -X POST -H 'x-admin-token: YOUR_ADMIN_TOKEN' \
  'https://YOUR-API.vercel.app/api/v1/admin/cron/refresh?league=KBO&scope=market'
```

## 선택형 Claude 보조 예측

Claude는 기존 통계 모델을 대체하지 않습니다. 팀명, 리그·구장, 파생 통계 특징, 기존 승률·기대득점만 구조화 출력 API에 보내고, 응답을 최대 25% 이내의 보조 가중치로 결합합니다. 실제 기본 가중치는 15%에 Claude가 반환한 자신도를 곱해 더 낮아질 수 있습니다. 선수명, 수집 원문, API 키는 프롬프트에 넣지 않습니다. API 오류·시간 초과·한도 초과 시 해당 경기는 통계 모델만으로 즉시 계산됩니다.

배포 후 화면 상단 `Claude 설정`에서 관리자 토큰과 API 키를 입력하고 `키 인증 · 모델 불러오기`를 누릅니다. 이 키의 Anthropic 계정에서 실제 사용 가능한 모델만 표시되며, 모델과 사용 여부를 선택해 저장합니다. 이미 키가 저장되어 있으면 API 키를 다시 입력하지 않고 모델이나 활성 상태만 변경할 수 있습니다.

보조 혼합 비율 `0.15`, 요청 제한시간 `20초`, 최대 출력 `600토큰`은 안전 정책으로 코드에 고정되어 배포 환경변수로 조절하지 않습니다. Claude 웹/앱 구독과 API Console 사용량 결제는 동일하지 않을 수 있으므로 Anthropic Console에서 API 키와 사용 한도를 확인해야 합니다. API 형식은 [Anthropic Messages API](https://platform.claude.com/docs/en/api/messages), [Models API](https://platform.claude.com/docs/en/api/models/list), [구조화 출력](https://platform.claude.com/docs/en/build-with-claude/structured-outputs)을 따릅니다.

Oracle Cloud VM 배포는 `docs/ORACLE_VM.md`의 순서대로 진행합니다.
데이터 소스별 채택·제외 근거는 `docs/DATA_SOURCES.md`에 정리했습니다.
유료화 단계·가격 가설·데이터 라이선스 체크리스트는 `docs/MONETIZATION.md`, 예측 모델과 AI 고도화 순서는 `docs/MODEL_AI_ROADMAP.md`에 정리했습니다.

## 명령과 API

```bash
python -m backend.app.cli refresh --league KBO
python -m backend.app.cli refresh --league MLB
python -m backend.app.cli backtest --league ALL
python -m backend.app.cli backtest --league MLB --stage T_MINUS_15M
python -m backend.app.cli model-lifecycle --league KBO
python -m backend.app.cli historical-replay --league KBO --limit 20
python -m backend.app.cli backup
pytest backend/tests
cd frontend && npm run build
```

- `/health`: 프로세스와 DB 연결 확인
- `/ready`: 최근 수집 성공까지 포함한 준비 상태
- `/api/v1/operations/status`: 오류율, 최근 성공, 변경 알림, 예측 수
- `/api/v1/model/lifecycle?league=KBO`: 운영 모델, 학습 준비도, 최근 승격·롤백 결정
- `/api/v1/model/backtest`: 누수 방지 walk-forward 평가
- `/api/v1/admin/replay`: 경기 전 데이터만 복원하는 과거 재현 백필(관리자 전용)
- 화면의 `내 Claude 설정`: 로그인 사용자별 Claude API 키 확인·암호화 저장·교체
- `/api/v1/games/{id}/claude-analysis`: 로그인 사용자 키로만 계산하는 비공개 Claude 보조 분석
- `/api/v1/games`: KBO/MLB 카드·신선도·변화 타임라인
- `/api/v1/game-dates`: 한국 날짜 기준 연도별 저장 경기일과 리그별 경기 수
- `/api/v1/admin/refresh`, `/api/v1/admin/backup`: `ADMIN_TOKEN` 설정 시 `X-Admin-Token` 필요

## 비용

약 4명이 개인 PC·NAS·기존 서버에서 이용하면 애플리케이션 필수 비용은 0원입니다.

| 항목 | 기본 구성 | 비용 발생 조건 |
|---|---|---|
| KBO/MLB 데이터 | 공식 공개 데이터 | 유료 부상·배당·고급 데이터로 교체할 때 |
| 모델 | 로컬 CPU 통계 계산 | 외부 ML API나 GPU를 선택할 때 |
| DB/스케줄 | Supabase 무료 PostgreSQL/Cron | 무료 한도를 초과할 때 |
| 알림 | 화면의 변경 알림 | 문자·카카오·유료 메일 서비스를 붙일 때 |
| 서버 | Vercel Hobby | 상업화하거나 무료 한도를 초과할 때 |
| 주소/HTTPS | Vercel 기본 도메인 | 개인 도메인을 구매할 때 |

전기료와 인터넷 회선은 기존 환경 비용으로 보며, 외부 공개 시에는 서버·도메인·HTTPS 운영비가 선택적으로 생길 수 있습니다.

## 데이터베이스와 마이그레이션

로컬 기본 DB는 `data/baseball.db`이고 배포 환경은 Supabase PostgreSQL을 사용합니다. `BASEBALL_DATABASE_URL`에 Transaction pooler URI를 지정하고 신규 환경에서 `alembic upgrade head`를 실행합니다. 기존 SQLite 이력은 `scripts/migrate_sqlite_to_postgres.py`로 옮길 수 있습니다.

주요 환경변수:

```text
ADMIN_TOKEN=network-use-secret
SUPABASE_URL=https://PROJECT_REF.supabase.co
SUPABASE_PUBLISHABLE_KEY=sb_publishable_REPLACE_ME
CACHE_TTL_MINUTES=120
MLB_STATS_TTL_MINUTES=720
LIVE_UPDATE_WINDOW_MINUTES=180
MONTE_CARLO_SIMS=20000
COLLECTOR_RETRY_ATTEMPTS=3
BACKUP_RETENTION_DAYS=14
STALE_AFTER_MINUTES=360
ODDS_API_KEY=
ODDS_API_REGIONS=us
SECRET_ENCRYPTION_KEY=long-random-secret
```

상세 설계는 [docs/DESIGN.md](docs/DESIGN.md), 장애·백업·복구 흐름은 [docs/OPERATIONS.md](docs/OPERATIONS.md), [유료화 전략](docs/MONETIZATION.md), [모델·AI 로드맵](docs/MODEL_AI_ROADMAP.md)을 참고하세요.
