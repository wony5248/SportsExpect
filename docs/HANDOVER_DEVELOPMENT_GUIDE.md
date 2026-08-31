# Dugout Lab 인수인계·개발 가이드

이 문서는 새 담당자가 프로젝트를 인수한 뒤 로컬 실행, 데이터 확인, 코드 변경, 테스트, 운영 배포와 장애 대응까지 수행할 수 있도록 정리한 실무 문서다. 모델 내부 원리는 [구조·예측 프로세스 설명서](ARCHITECTURE_PREDICTION_PROCESS.md)를 함께 참고한다.

## 1. 현재 운영 기준

2026-08-24 기준 운영 상태는 다음과 같다.

| 항목 | 현재 값 |
|---|---|
| 서비스 | KBO·MLB 경기 데이터 수집 및 통계 예측 |
| 프런트 운영 주소 | `https://sports-expect-six.vercel.app` |
| API 운영 주소 | `https://sports-expect.vercel.app` |
| 프런트 Vercel 프로젝트 | `sports-expect`, Root Directory `frontend` |
| API Vercel 프로젝트 | `sports-expect-api`, Root Directory `.` |
| 운영 DB·스케줄러 | Supabase PostgreSQL·Cron·Vault |
| 로컬 기본 DB | `data/baseball.db` SQLite |
| 예측 시뮬레이션 | 경기당 최소 20,000회 |
| 현재 요약 스키마 | `SIMULATION_SUMMARY_SCHEMA_VERSION = 33` |
| 승률 보정 | KBO PASS·활성, MLB HOLD·원승률 유지 |
| 기본 브랜치 | `main` |

운영 비밀값은 이 문서나 Git에 기록하지 않는다. 인수 시 실제 값이 아니라 각 서비스의 접근 권한을 이전받아야 한다.

## 2. 인수받아야 할 계정과 권한

최소한 다음 권한을 확보한다.

- GitHub 저장소 읽기·쓰기 권한
- Vercel 팀과 `sports-expect`, `sports-expect-api` 프로젝트 권한
- Supabase 프로젝트의 Database, SQL Editor, Authentication, Vault, Cron 확인 권한
- 선택 데이터인 The Odds API 계정 또는 키 관리 권한
- 운영 사용자 초대·삭제가 필요하면 Supabase Authentication 관리자 권한

비밀값 목록은 다음과 같으며 기존 값의 원문을 메신저로 전달하기보다 서비스에서 교체하는 것이 안전하다.

- Vercel API: `BASEBALL_DATABASE_URL`, `ADMIN_TOKEN`, `SECRET_ENCRYPTION_KEY`
- Vercel API: `SUPABASE_URL`, `SUPABASE_PUBLISHABLE_KEY`
- Vercel API 선택값: `ODDS_API_KEY`, `ODDS_API_REGIONS`, `ODDS_API_REGIONS_KBO`
- Vercel 프런트: `VITE_API_BASE_URL`, `VITE_SUPABASE_URL`, `VITE_SUPABASE_PUBLISHABLE_KEY`
- Supabase Vault: `dugout_backend_url`, `dugout_admin_token`

`ADMIN_TOKEN`과 Vault의 `dugout_admin_token`은 반드시 같아야 한다. 사용자별 Claude API 키는 공용 환경변수가 아니라 로그인 사용자가 화면에서 등록하며 DB에는 암호화되어 저장된다.

## 3. 저장소 구조

```text
sports-expect/
├── api/                         # Vercel Python 함수 진입점
├── backend/app/
│   ├── collectors/              # KBO, MLB, Odds 공식 데이터 수집기
│   ├── database/                # SQLAlchemy 세션과 DB 초기화
│   ├── models/                  # ORM 엔터티
│   ├── repositories/            # upsert, 조회, API 카드 직렬화
│   ├── services/                # 예측·시뮬레이션·운영 작업 핵심
│   ├── cli.py                   # 로컬 관리 명령
│   └── main.py                  # FastAPI 라우트
├── backend/tests/test_core.py   # 통합 성격의 핵심 회귀 테스트
├── frontend/src/                # React·TypeScript·MUI 화면
├── migrations/versions/         # Alembic DB 마이그레이션
├── supabase/cron.sql            # Vault 기반 운영 스케줄
├── scripts/                     # DB 이관·운영 보조 스크립트
├── docs/                        # 설계·운영·배포 문서
├── requirements.txt             # Python 의존성
└── vercel.json                  # API Vercel 설정과 CORS 헤더
```

처음 코드를 읽을 때는 다음 순서가 효율적이다.

1. `backend/app/services/refresh.py`: 수집 결과가 DB와 예측으로 연결되는 지점
2. `backend/app/services/prediction.py`: 한 경기 예측 조립과 저장 payload
3. `backend/app/services/simulation.py`: 20,000회 시뮬레이션과 시장 확률
4. `backend/app/services/feature_engineering.py`: 특징과 기대득점 계산
5. `backend/app/repositories/repository.py`: API 카드가 만들어지는 방식
6. `frontend/src/components/GameCard.tsx`: 사용자가 보는 결과의 의미

## 4. 로컬 개발 환경

### 요구 버전

- Python 3.11 이상 권장
- Node.js 20 이상
- PostgreSQL은 선택 사항이며 기본 로컬 개발은 SQLite로 가능

### 최초 설치

```bash
cd /path/to/sports-expect
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cd frontend
npm install
cd ..
```

### 로컬 실행

터미널 1:

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --reload --port 8000
```

터미널 2:

```bash
cd frontend
npm run dev
```

프런트 개발 환경에서 API 주소가 필요하면 `frontend/.env.local`에 다음 공개 설정만 둔다.

```text
VITE_API_BASE_URL=http://127.0.0.1:8000
VITE_SUPABASE_URL=...
VITE_SUPABASE_PUBLISHABLE_KEY=...
```

관리자 토큰, DB 비밀번호, Odds 키, Claude 키를 `VITE_*` 변수나 프런트 코드에 넣으면 안 된다.

### 로컬 데이터 생성

```bash
source .venv/bin/activate
python -m backend.app.cli refresh --date 2026-08-25 --league KBO --force
python -m backend.app.cli refresh --date 2026-08-25 --league MLB --force
```

외부 공식 사이트를 호출하므로 테스트와 달리 시간이 걸릴 수 있다. 단순 화면 개발에는 기존 `data/baseball.db`를 사용한다.

## 5. 주요 개발 명령

```bash
# 전체 테스트
.venv/bin/pytest -q

# 프런트 타입 검사와 운영 빌드
cd frontend && npm run build

# 코드 변경 공백·충돌 확인
git diff --check

# 누수 방지 백테스트
.venv/bin/python -m backend.app.cli backtest --league KBO
.venv/bin/python -m backend.app.cli backtest --league MLB

# 과거 경기 회고 재현
.venv/bin/python -m backend.app.cli historical-replay --league KBO --limit 20

# 과거 경기 선발과 당시 경기 전 누적 기록 복원
.venv/bin/python -m backend.app.cli backfill-starters --league KBO --season 2026 --limit 400
.venv/bin/python -m backend.app.cli backfill-starters --league MLB --season 2026 --limit 400

# 모델 생명주기 수동 평가
.venv/bin/python -m backend.app.cli model-lifecycle --league KBO
```

기능 변경 전후 최소 기준은 백엔드 전체 테스트, 프런트 빌드, `git diff --check` 통과다. 확률·점수 계산을 바꾼 경우에는 walk-forward Brier, Log Loss, 득점 MAE·RMSE도 비교한다.

## 6. 데이터베이스와 마이그레이션

ORM 정의는 `backend/app/models/entities.py`, 마이그레이션은 `migrations/versions`에 있다. 운영 DB에서 ORM의 자동 테이블 생성을 기대하지 않는다.

스키마 변경 절차:

1. ORM 모델 수정
2. 새 Alembic revision 작성
3. 로컬 빈 DB와 기존 DB 모두에서 `alembic upgrade head` 확인
4. 코드 배포 전에 Supabase Session pooler 또는 Direct URI로 운영 마이그레이션
5. API 배포 후 `/health`, `/ready`, 실제 조회 API 확인

```bash
export BASEBALL_DATABASE_URL='postgresql://...:5432/postgres?sslmode=require'
alembic upgrade head
```

Vercel 런타임은 Transaction pooler 6543 URI를 사용한다. 마이그레이션에는 Session pooler 5432 또는 Direct URI를 사용한다.

중요 테이블군:

- 경기·팀·결과: `games`, `teams`, `game_results`, `team_stats`
- 경기 전 입력: `pitcher_stats`, `game_starters`, `lineup_entries`, `batter_splits`, `team_bullpens`
- 예측·감사: `predictions`, `prediction_snapshots`, `prediction_evaluations`
- 시장: `market_consensus`, `market_snapshots`
- 모델: `model_versions`, `model_lifecycle_events`
- 운영: `crawl_logs`
- 사용자 설정: `user_claude_settings`

종료 결과의 `finalized_at`은 최초 확인 시각을 보존한다. 이 값을 덮어쓰면 잔차·승률 보정과 백테스트에서 미래 결과 누수가 발생할 수 있다.

## 7. 예측 스키마 변경 규칙

`prediction.py`의 `SIMULATION_SUMMARY_SCHEMA_VERSION`은 저장 payload의 의미가 바뀔 때 올린다.

올려야 하는 예:

- 승률·핸디캡·총점 계산 모집단 변경
- 대표 점수 선택 규칙 변경
- 시뮬레이션 엔진 결과 필드 추가 또는 의미 변경
- 기대득점 공식 변경으로 과거 재현을 새 모델로 다시 만들어야 하는 경우
- 과거 재현에 선발처럼 새 경기 전 입력을 추가해 기존 replay를 다시 만들어야 하는 경우

단순 문구나 CSS 변경만으로는 올리지 않는다. 버전을 올리면 과거 재현 작업이 구버전 아카이브를 현재 모델로 다시 생성할 수 있으므로 처리량을 확인한다.

## 8. 운영 배포 절차

기본 배포는 `main` 푸시 후 Vercel Git 연동 자동 배포다.

1. 변경 범위와 다른 작업자의 수정이 섞이지 않았는지 `git status` 확인
2. 전체 테스트와 프런트 빌드 통과
3. 관련 파일만 커밋
4. `main` 푸시
5. API와 프런트 두 Vercel 프로젝트가 `Ready`인지 확인
6. API `/health`, `/ready` 확인
7. 필요하면 대상 날짜·리그만 관리자 갱신
8. `/api/v1/games`에서 스키마·생성 시각·확률 정합성 확인

관리자 호출 예시는 토큰을 셸 이력에 남기지 않는 방식으로 실행한다.

```bash
read -s DUGOUT_ADMIN_TOKEN
curl -X POST -H "x-admin-token: ${DUGOUT_ADMIN_TOKEN}" \
  'https://sports-expect.vercel.app/api/v1/admin/refresh?date=2026-08-25&league=KBO&force=false'
unset DUGOUT_ADMIN_TOKEN
```

전체 리그 한 번 호출은 Vercel 5분 제한을 넘을 수 있다. KBO와 MLB를 분리 호출하고, 응답이 시간초과돼도 즉시 재호출하지 말고 `/api/v1/games`에서 경기별 저장 여부를 먼저 확인한다.

## 9. Supabase Cron 운영

실제 스케줄 정의는 `supabase/cron.sql`이 단일 기준이다.

- 전체 수집: KBO 매시 04분, MLB 매시 19분
- 임박·진행 경기: 각 리그 5분 간격
- 정확 스냅샷: 매분 검사, T-24h·T-3h·T-60m·T-15m ±2.5분
- MLB 다음 날 일정·기본 예측: 12:50 KST 일정 선취득, 13:00 KST 저장 데이터 예측
- MLB 늦은 최신화: 22:55 KST 일정 재확인, 23:00 KST 경기당 독립 외부 데이터 재계산
- 시장: KBO 12:00 KST, MLB 00:00 KST
- 타자 split: 리그별 매시간 두 번
- 과거 재현: KBO 05:00 KST, MLB 16:00 KST
- 모델 생명주기: KBO 05:30 KST, MLB 16:30 KST

점검 SQL:

```sql
select jobid, jobname, schedule, active from cron.job order by jobname;
select * from cron.job_run_details order by start_time desc limit 30;
select id, status_code, error_msg, created
from net._http_response order by created desc limit 30;
```

정상은 200, 같은 리그·날짜 작업 실행 중이면 advisory lock으로 409가 반환될 수 있다. 401은 Vault와 Vercel 토큰 불일치, 503은 API 환경변수 누락을 우선 의심한다.

## 10. 운영 점검과 장애 대응

### 기본 점검 순서

1. `GET /health`: DB 연결 여부
2. `GET /ready`: 최근 수집 성공과 실패율
3. `GET /api/v1/operations/status`: 최근 성공, 실패, 예정 경기, 예측 수
4. Vercel Functions 로그
5. Supabase `cron.job_run_details`, `net._http_response`
6. DB의 `crawl_logs`

### 경기 카드가 없을 때

- 한국 날짜와 MLB 미국 현지 `venue_date`를 혼동하지 않았는지 확인
- `/api/v1/game-dates`에 날짜가 있는지 확인
- 일정 수집은 됐지만 팀 통계 부족으로 예측만 생략됐는지 `crawl_logs` 확인
- 다음 날 발견 작업 또는 리그별 수동 refresh 실행

### 예측이 구버전일 때

- API 배포가 새 커밋을 가리키는지 확인
- 저장 prediction의 `summary_schema_version` 확인
- 해당 날짜와 리그를 `force=false`로 갱신한다. 스키마가 hash에 포함되어 예측은 다시 생성된다.

### 라인업이 있는데 이닝별 엔진일 때

- 화면에 9명 이름이 있는 것과 타석 엔진 입력이 준비된 것은 다르다.
- 양 팀 모두 `batter_splits` 기반 base-state 테이블이 있어야 타석별 엔진이 선택된다.
- `split_coverage`, split backfill Cron, 수집 오류를 확인한다.

### 과거 재현에서 선발 지표가 비어 있을 때

- `game_starters`에 대상 경기의 홈·원정 선발이 있는지 확인한다.
- `backfill-starters`는 공식 일정의 선발 ID와 경기 로그를 결합하되 대상 경기 날짜보다 앞선 등판만 누적한다.
- 선발 backfill을 마친 뒤 구버전 replay를 다시 생성해야 실제 선발 특징이 반영된다.
- `POST /api/v1/admin/backfill-starters`와 `POST /api/v1/admin/replay`는 별도 작업이므로 순서를 지킨다.

### 관리자 호출이 오래 걸릴 때

- KBO와 MLB를 분리한다.
- `force=false`로 신선한 팀 통계를 재사용한다.
- 시간초과 후 API 조회로 부분 저장부터 확인한다.
- 동일 작업을 연속 호출해 advisory lock 충돌과 외부 호출량을 늘리지 않는다.

### 운영 DB 중복 데이터가 의심될 때

관리자 무결성 API를 먼저 audit 모드로 호출하고 결과를 확인한 뒤 repair한다.

- `/api/v1/admin/data-integrity/pitchers?repair=false`
- `/api/v1/admin/data-integrity/cancelled-games?repair=false`

동일 `player_id`가 여러 경기에서 나타나는 것은 정상이다. 한 경기·팀 side 안의 중복만 오류 대상이며 경기별 수치는 수집 시점 스냅샷일 수 있다.

## 11. 보안 원칙

- 비밀값을 코드, 문서, 커밋, 프런트 번들, 로그에 남기지 않는다.
- Supabase `service_role` 키를 프런트나 일반 API 인증에 사용하지 않는다.
- `ADMIN_TOKEN`은 Cron·관리자 API 전용이다.
- 사용자 Claude 키는 `SECRET_ENCRYPTION_KEY`로 암호화하고 API 응답에 원문을 반환하지 않는다.
- Odds API 키가 포함된 URL을 로그에 기록하지 않는다.
- 운영 DB 수정 전 대상 행을 읽기 전용으로 확인한다.
- 기존 예측과 스냅샷은 감사 자료이므로 임의 업데이트·삭제보다 새 버전 append를 우선한다.

## 12. 현재 모델 운영 정책과 주의점

- KBO Platt 승률 보정은 555경기 walk-forward에서 Brier·Log Loss가 소폭 개선되어 PASS다.
- MLB Platt 보정은 1,963경기에서 두 지표가 악화되어 HOLD이며 원 시뮬레이션 승률을 유지한다.
- HOLD 리그도 보정 메타데이터와 표본 수는 저장하므로 향후 재검증할 수 있다.
- 시장 배당은 기준점 비교와 표시용이며 팀 능력 추정 입력으로 사용하지 않는다.
- 55% 미만 승률은 단일 조건부 대표 점수로 단정하지 않고 양 팀 승리 시나리오를 함께 표시한다.
- 실제 라인업 미확정, 날씨·불펜 데이터 부족은 신뢰도를 낮추지만 임의 값을 확정 정보처럼 만들지 않는다.
- Elo/SRS/Pythagorean과 상대 보정 공격·수비는 `team_strength.py`에서 목표 경기 전 결과만으로 계산한다.
- MLB Statcast와 양 리그 불펜 workload는 `advanced`/`pregame_context` 경기 전 스냅샷으로 저장하며, 과거 replay에 현재 값을 역으로 붙이지 않는다.
- 현재 시뮬레이션 요약 스키마는 25, 학습 특징 스키마는 7이다.
- 생성형 AI는 공용 기본 예측을 만들지 않는다. 로그인 사용자의 선택형 개인 분석만 별도 실행한다.
- 자동 승격 전에 `/api/v1/admin/model/dry-run?league=KBO|MLB`로 DB를 변경하지 않는 challenger 평가와 상수 특징 목록을 확인할 수 있다.

## 13. 변경 유형별 필수 확인

| 변경 | 필수 확인 |
|---|---|
| 수집기 | 실제 응답 fixture, 빈 응답 시 기존 데이터 보존, source URL·시각 |
| 기대득점 공식 | 입력 단조성, 상대 팀 값의 잘못된 교차 영향, MAE·RMSE |
| 승률 공식 | Brier, Log Loss, calibration error, 홈·원정 합 1 |
| 시뮬레이션 | 20,000회 합계, MLB 무승부 0, KBO 무승부 처리, 재현성 |
| 핸디캡 | 홈/원정이 아닌 실제 minus/plus side, 동적 1.5·2.5 기준 |
| 언더오버 | over+under+push=1, 화면 기준점과 계산 기준점 일치 |
| UI | 모바일 빌드, 구버전 payload fallback, 최종 경기 비교 |
| DB | 마이그레이션 전후 호환, nullable·index·문자열 길이 |

## 14. 최종 인수인계 체크리스트

- [ ] GitHub, Vercel 2개 프로젝트, Supabase 권한을 받았다.
- [ ] 운영 비밀값의 저장 위치를 알고 필요 시 교체할 수 있다.
- [ ] 로컬 테스트와 프런트 빌드가 통과한다.
- [ ] 로컬 API와 프런트를 실행해 경기 카드를 열었다.
- [ ] 운영 `/health`, `/ready`, `/operations/status`를 확인했다.
- [ ] Supabase Cron과 Vault 값을 확인했다.
- [ ] KBO·MLB 수동 갱신을 분리 실행할 수 있다.
- [ ] Alembic 운영 마이그레이션 절차를 이해했다.
- [ ] 예측 스키마 버전을 올려야 하는 조건을 이해했다.
- [ ] 백테스트와 모델 승격·롤백 정책을 이해했다.
- [ ] 관리자 토큰과 사용자 Claude 키의 역할 차이를 이해했다.

## 15. 관련 문서

- [구조·예측 프로세스 설명서](ARCHITECTURE_PREDICTION_PROCESS.md)
- [Vercel + Supabase 배포](VERCEL_SUPABASE.md)
- [자동 수집·운영](OPERATIONS.md)
- [상세 설계](DESIGN.md)
- [데이터 소스](DATA_SOURCES.md)
- [모델·AI 로드맵](MODEL_AI_ROADMAP.md)
