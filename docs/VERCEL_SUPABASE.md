# Vercel + Supabase 배포 가이드

이 구성은 비상업적 소규모 사용을 전제로 합니다. React 프런트엔드와 FastAPI API는 서로 다른 Vercel 프로젝트로 배포하고, Supabase PostgreSQL과 Supabase Cron을 공유합니다.

## 1. 준비할 값

- Supabase 프로젝트 1개
- Vercel 프로젝트 2개: `dugout-api`, `dugout-web`
- 32바이트 이상의 임의 `ADMIN_TOKEN`
- 선택 사항: `ODDS_API_KEY` (Claude 키는 배포 후 관리자 UI에서 등록)

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

새로 시작해도 된다면 5번은 생략합니다. Cron이 경기와 예측 데이터를 다시 채웁니다.

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
| `SECRET_ENCRYPTION_KEY` | UI에서 등록한 Claude 키를 암호화할 별도 장기 비밀값 |
| `ODDS_API_KEY` | 보유한 경우만 등록 |

Claude API 키·모델·활성 여부는 배포 후 웹의 `Claude 설정`에서 관리합니다. 혼합 비율, 타임아웃, 최대 출력 토큰은 코드 정책이므로 Vercel 환경변수로 등록하지 않습니다.

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

배포 후 프런트 주소가 `https://YOUR-WEB.vercel.app`이라면 API 프로젝트의 `CORS_ORIGINS`를 이 주소로 바꾸고 API를 재배포합니다. 여러 고정 주소가 필요하면 쉼표로 구분합니다. 와일드카드 대신 실제 주소만 등록하는 편이 안전합니다.

## 5. Supabase Vault와 Cron 설정

Supabase SQL Editor에서 먼저 두 비밀값을 저장합니다.

```sql
select vault.create_secret('https://YOUR-API.vercel.app', 'dugout_backend_url');
select vault.create_secret('YOUR_ADMIN_TOKEN', 'dugout_admin_token');
```

그다음 [`supabase/cron.sql`](../supabase/cron.sql)의 전체 내용을 SQL Editor에서 실행합니다. 등록되는 작업은 다음과 같습니다.

- KBO/MLB 전체 갱신: 각 1시간
- 경기 임박 갱신: 전체 갱신 사이의 30분 시점
- 다음 날 경기 발견: KBO 13:10 KST, MLB 00:20 KST

중복 호출은 PostgreSQL advisory lock으로 차단되므로 같은 리그·날짜 수집이 동시에 DB를 갱신하지 않습니다.

설정 확인 SQL:

```sql
select jobid, jobname, schedule, active from cron.job order by jobname;
select * from cron.job_run_details order by start_time desc limit 30;
select id, status_code, error_msg, created from net._http_response order by created desc limit 30;
```

정상 응답은 `200`, 이미 같은 수집이 실행 중이면 `409`입니다. `401`은 Vault 토큰과 Vercel `ADMIN_TOKEN`이 다른 경우이고, `503`은 Vercel에 `ADMIN_TOKEN`이 빠진 경우입니다.

## 6. 최종 확인

1. API `/health`가 `database: connected`를 반환합니다.
2. 수동 KBO 및 MLB 갱신이 각각 `200`을 반환합니다.
3. 프런트에서 오늘 날짜의 경기 카드가 나타납니다.
4. `cron.job`에 6개 `dugout-*` 작업이 활성화되어 있습니다.
5. 다음 정각 이후 `net._http_response`에서 응답 코드가 확인됩니다.
6. Claude를 켰다면 Vercel 로그에서 모델 ID 오류나 시간 초과가 없는지 확인합니다.

## 운영상 제한

- Vercel 함수는 한 번에 최대 5분을 전제로 구성했습니다. 특정 리그 전체 수집이 이 시간을 넘으면 날짜/경기 단위로 추가 분할해야 합니다.
- Supabase 무료 프로젝트는 자동 백업이 없으므로 중요한 예측 이력은 주기적으로 CSV 또는 SQL로 내보냅니다.
- Vercel Preview 주소까지 허용해야 한다면 `CORS_ORIGIN_REGEX`를 별도로 설정할 수 있지만, 운영 주소만 `CORS_ORIGINS`에 넣는 구성이 권장됩니다.
