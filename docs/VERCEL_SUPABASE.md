# Vercel + Supabase 배포 가이드

이 구성은 비상업적 소규모 사용을 전제로 합니다. React 프런트엔드와 FastAPI API는 서로 다른 Vercel 프로젝트로 배포하고, Supabase PostgreSQL과 Supabase Cron을 공유합니다.

## 1. 준비할 값

- Supabase 프로젝트 1개
- Vercel 프로젝트 2개: `dugout-api`, `dugout-web`
- 32바이트 이상의 임의 `ADMIN_TOKEN`
- 선택 사항: `ODDS_API_KEY` (Claude 키는 각 사용자가 로그인 후 자기 화면에서 등록)

토큰은 아래처럼 생성할 수 있습니다.

```bash
openssl rand -hex 32
```

## 2. Supabase 프로젝트와 테이블 생성

1. Supabase에서 새 프로젝트를 만들고 데이터베이스 비밀번호를 안전하게 보관합니다.
2. **Connect**에서 URI를 두 개 복사합니다.
   - **Transaction pooler (`6543`)**: Vercel의 `BASEBALL_DATABASE_URL`로 사용
   - **Session pooler (`5432`)**: 최초 Alembic 마이그레이션과 SQLite 이관에 사용. Direct connection이 가능한 IPv6 환경이면 Direct URI를 대신 사용해도 됩니다.
3. 프로젝트 폴더에서 Python 환경을 준비합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

4. Session pooler 또는 Direct URI를 사용해 Alembic 스키마를 생성합니다. 비밀번호에 특수문자가 있다면 URL 인코딩된 URI를 사용합니다.

```bash
export BASEBALL_DATABASE_URL='postgresql://postgres.PROJECT_REF:PASSWORD@POOLER_HOST:5432/postgres?sslmode=require'
alembic upgrade head
```

5. 기존 SQLite 이력을 유지하려면, 테이블이 비어 있는 상태에서 한 번만 실행합니다.

```bash
python scripts/migrate_sqlite_to_postgres.py --sqlite /absolute/path/to/baseball.db
```

새로 시작해도 된다면 5번은 생략합니다. 배포된 화면의 수동 최신화가 경기와 예측 데이터를 채웁니다.

## 3. FastAPI를 Vercel에 배포

Git 저장소를 Vercel에서 가져오고 첫 번째 프로젝트를 다음처럼 만듭니다.

- Project name: 원하는 API 이름
- Root Directory: 저장소 루트 (`.`)
- Framework Preset: Other 또는 자동 감지
- Build/Output 설정: 변경하지 않음

환경변수는 Production에 다음 값을 등록합니다.

| 이름 | 값 |
|---|---|
| `BASEBALL_DATABASE_URL` | Supabase Transaction pooler URI, 포트 `6543` |
| `ADMIN_TOKEN` | 위에서 생성한 토큰 |
| `AUTO_CREATE_SCHEMA` | `false` |
| `CORS_ORIGINS` | 프런트 URL 확정 전에는 로컬 URL, 확정 후 Vercel 프런트 URL |
| `SECRET_ENCRYPTION_KEY` | 사용자별 Claude 키를 암호화할 별도 장기 비밀값(필수) |
| `SUPABASE_URL` | Supabase Project URL |
| `SUPABASE_PUBLISHABLE_KEY` | Supabase publishable key (`sb_publishable_...`) |
| `ODDS_API_KEY` | 보유한 경우만 등록 |
| `ODDS_API_REGIONS` | `us` (한 지역만 조회해 크레딧 제한) |
| `ODDS_API_REGIONS_KBO` | `eu` (KBO 런라인은 eu 북메이커 위주, 지역 추가 시 크레딧 배수 증가) |
| `ODDS_API_DAILY_CREDIT_BUDGET` | `24` (요청 수가 아닌 일일 크레딧 상한) |

`ADMIN_TOKEN`은 관리자 API 전용 서버 비밀값이며 프런트에 등록하지 않습니다. 화면의 수동 최신화 비밀번호는 기본값 `0930`이며, 필요하면 API 프로젝트에 `MANUAL_REFRESH_PASSWORD` 환경변수로 바꿀 수 있습니다. Claude API 키·모델·활성 여부는 각 사용자가 로그인 후 `내 Claude 설정`에서 관리합니다.

배포 후 API 주소를 기록하고 확인합니다.

```bash
curl https://YOUR-API.vercel.app/health
curl -X POST \
  -H 'x-admin-token: YOUR_ADMIN_TOKEN' \
  'https://YOUR-API.vercel.app/api/v1/admin/cron/refresh?league=KBO&scope=full'
```

두 번째 호출은 실제 공식 사이트 수집과 예측을 실행하므로 수십 초 걸릴 수 있습니다.

## 4. React 프런트엔드를 Vercel에 배포

같은 Git 저장소를 다시 가져와 두 번째 프로젝트를 만듭니다.

- Project name: 원하는 웹 이름
- Root Directory: `frontend`
- Framework Preset: Vite
- 환경변수 `VITE_API_BASE_URL`: `https://YOUR-API.vercel.app`
- 환경변수 `VITE_SUPABASE_URL`: Supabase Project URL
- 환경변수 `VITE_SUPABASE_PUBLISHABLE_KEY`: Supabase publishable key

배포 후 프런트 주소가 `https://YOUR-WEB.vercel.app`이라면 API 프로젝트의 `CORS_ORIGINS`를 이 주소로 바꾸고 API를 재배포합니다. 여러 고정 주소가 필요하면 쉼표로 구분합니다. 와일드카드 대신 실제 주소만 등록하는 편이 안전합니다.

## 5. 사용자 로그인 준비

Supabase Dashboard에서 다음을 설정합니다.

1. **Authentication → URL Configuration**의 Site URL과 Redirect URLs에 프런트 운영 주소를 등록합니다.
2. **Authentication → Users → Add user**에서 이용할 4명의 이메일 계정을 초대합니다. 초대 사용자는 비밀번호 없이도 화면의 `이메일 로그인 링크 받기`로 다시 로그인할 수 있습니다.
3. **Project Settings → API Keys**에서 Project URL과 publishable key를 복사해 위의 API·프런트 환경변수에 넣습니다. `service_role` 키는 사용하지 않습니다.
4. 아래 마이그레이션을 최신까지 실행해 사용자별 암호화 키 테이블을 만듭니다.

```bash
export BASEBALL_DATABASE_URL='SESSION_POOLER_OR_DIRECT_URI'
alembic upgrade head
```

API와 프런트를 모두 재배포한 뒤 각 계정으로 로그인해 자기 Claude 키를 등록합니다.

## 6. Supabase Vault와 Cron 설정

Supabase SQL Editor에서 먼저 두 비밀값을 저장합니다.

```sql
select vault.create_secret('https://YOUR-API.vercel.app', 'dugout_backend_url');
select vault.create_secret('YOUR_ADMIN_TOKEN', 'dugout_admin_token');
```

그다음 [`supabase/cron.sql`](../supabase/cron.sql)의 전체 내용을 SQL Editor에서 실행합니다. 무료 티어에 맞춰 다음의 사전 경기 갱신만 자동 등록됩니다.

- KBO 전체 사전 갱신: 매일 13:00 KST
- MLB 배당 수집: 매일 22:00 KST
- MLB 전체 사전 갱신: 매일 23:00 KST
- 양 리그 라인업 갱신: 경기 시작 정확히 40분 전 1회

40분 전 확인은 Supabase에서 매분 실행하지만, 해당 시간이 된 시작 전 경기가 있을 때만 Vercel API를 호출합니다. 그 외에는 Vercel 사용량이 발생하지 않습니다. 화면 우측 상단 `새로고침 · 최신화`에 비밀번호 `0930`을 입력하면 위 일정과 별개로 즉시 수동 최신화할 수 있습니다.

설정 확인 SQL:

```sql
select jobid, jobname, schedule, active from cron.job where jobname like 'dugout-%' order by jobname;
```

위 조회에서 `dugout-kbo-daily-pregame`, `dugout-mlb-market`, `dugout-mlb-daily-pregame`, 그리고 두 `lineup-40m-dispatch` 작업만 활성화되어야 합니다.

## 7. 최종 확인

1. API `/health`가 `database: connected`를 반환합니다.
2. 수동 KBO 및 MLB 갱신이 각각 `200`을 반환합니다.
3. 프런트에서 오늘 날짜의 경기 카드가 나타납니다.
4. `cron.job`에 일일 사전 갱신 3개와 40분 전 dispatcher 2개만 활성화되어 있습니다.
5. 화면의 `새로고침 · 최신화`에 비밀번호 `0930`을 입력하면 즉시 갱신이 실행됩니다.
6. 서로 다른 두 사용자로 로그인했을 때 Claude 키 상태와 개인 분석이 분리되는지 확인합니다.

## 운영상 제한

- Vercel 함수는 한 번에 최대 5분을 전제로 구성했습니다. 특정 리그 전체 수집이 이 시간을 넘으면 날짜/경기 단위로 추가 분할해야 합니다.
- Supabase 무료 프로젝트는 자동 백업이 없으므로 중요한 예측 이력은 주기적으로 CSV 또는 SQL로 내보냅니다.
- Vercel Preview 주소까지 허용해야 한다면 `CORS_ORIGIN_REGEX`를 별도로 설정할 수 있지만, 운영 주소만 `CORS_ORIGINS`에 넣는 구성이 권장됩니다.
