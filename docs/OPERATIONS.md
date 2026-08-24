# 자동 수집·변경 감지 운영 가이드

## 핵심 흐름

```text
Supabase Cron
  → KBO·MLB 전체 수집 API를 매시간 호출
  → 전체 수집 사이 30분에 경기 임박 수집 API 호출
  → 매분 대상 경기만 조회해 T-24h/T-3h/T-60m/T-15m 정확 시점 스냅샷 저장
  → 다음 날 일정을 KBO 13:10 / MLB 00:20 KST에 선취득
  → 시장 배당은 KBO 12:00 / MLB 00:00 KST에 하루 한 번만 조회
  → 선발·라인업·팀 기록 수집(실패 시 최대 3회 backoff 재시도)
  → 현재 상태 upsert
  → 예측 입력 fingerprint와 이전 snapshot 비교
  → 변경 입력이면 새 예측, 같은 입력이면 중복 방지
  → 의미 있는 시점 snapshot과 변경 이유 저장
  → 경기 후 결과 저장 및 경기 전 예측만 walk-forward 평가
  → 날짜순 자동 재학습 후 검증 통과 후보 승격, 운영 성능 하락 시 이전 모델 롤백
```

당일 KBO·MLB 전체 갱신은 Supabase Cron으로 1시간마다 실행됩니다. 경기 임박·진행 상태는 5분마다 별도로 확인하며, 경기 시작 3시간 전부터 시작 후 6시간까지의 `SCHEDULED`·`LIVE` 경기만 대상으로 삼습니다. 경기 시작 후에는 선발·라인업을 다시 수집하지 않고 공식 상태와 최종 결과만 갱신합니다. 별도 체크포인트 job은 매분 DB만 확인하며, 경기 시작 24시간·3시간·60분·15분 전 ±2.5분 창에 들어온 경기만 공식 데이터를 다시 수집합니다. `checkpoint_exact` trigger가 없는 넓은 시간대 snapshot은 시점별 정식 평가에 포함하지 않습니다.

라인업 미발표 시 KBO는 최근 라인업, MLB는 팀 시즌 공격력을 사용하고 신뢰도를 낮춥니다. 실제 9명 타순이 확인되면 KBO WAR 또는 MLB season OPS를 작게 축소해 반영합니다.

## 프로세스

개발 환경은 API를 실행하고 수집 CLI를 필요할 때 호출합니다. 운영 자동화는 별도 상주 프로세스 없이 Supabase Cron이 인증된 Vercel API를 호출합니다.

```bash
source .venv/bin/activate
uvicorn backend.app.main:app --port 8000
python -m backend.app.cli refresh --league ALL
```

운영 환경은 Vercel API의 `ADMIN_TOKEN`과 Supabase Vault의 `dugout_admin_token`을 같은 값으로 설정합니다. Vercel 인스턴스가 겹쳐도 PostgreSQL transaction advisory lock이 같은 리그·날짜의 동시 수집을 차단합니다.

## 상태 확인

- `GET /health`: 프로세스와 DB 연결
- `GET /ready`: 마지막 성공 수집이 기본 6시간 이내이고 최근 실패율이 과도하지 않은지 확인
- `GET /api/v1/operations/status`: 최근 성공, 24시간 시도/실패율, 예정 경기, 예측 수, 변경 알림 수
- `GET /api/v1/model/lifecycle?league=KBO|MLB`: champion, 이전 모델, 평가 표본, 정책, 최근 결정
- 화면의 `최신/갱신 필요` 칩: 카드 입력의 마지막 갱신 시각 기준

수집 스키마가 바뀌거나 HTTP 요청이 실패하면 `crawl_logs`에 최종 오류와 재시도 횟수가 남습니다. 한 소스 실패가 전체 갱신을 중단하지 않으며 마지막 정상 캐시를 유지합니다.

## 백업과 복구

Supabase 무료 플랜에는 자동 백업이 없으므로 중요한 예측 이력은 Dashboard의 CSV 내보내기 또는 `pg_dump`로 별도 보관합니다. 로컬 SQLite 모드에서만 `python -m backend.app.cli backup`이 파일 백업을 만듭니다.

## 캐시와 호출량

- KBO 팀 기록: 120분
- MLB 30개 팀 기록: 720분, 최대 6개 동시 요청
- 매시간 경기 일정·상태·선발을 재확인하고, KBO 라인업 및 경기 3시간 이내 MLB 라인업을 다시 확인
- 매시간 예측을 재계산하되 입력 hash가 같으면 중복 예측을 저장하지 않음
- MLB 라인업: 경기 3시간 전부터 또는 경기 대상 job에서 확인
- 빈 라인업 응답은 기존 라인업을 지우지 않음
- MLB 선발: FIP 추정, K-BB%, 최근 등판일, 최근 5일 투구 수를 공식 game log에서 계산
- The Odds API: KBO 12:00 KST, MLB 00:00 KST 기준 각 1회. 성공한 슬롯은 매시간 수집에서도 재호출하지 않으며 정상 사용량은 약 120크레딧/월

## 예측 스냅샷과 알림

- `lineups`, `pitcher_stats`: 현재 적용 입력
- `predictions`: 입력 hash가 달라질 때만 append
- `prediction_snapshots`: 시점, trigger, 전체 입력, 변경 사유를 불변 저장
- `prediction_history`: 예측 수치의 간단한 감사 이력

선발, 라인업, 최근 흐름, 공격력, 불펜 proxy, 휴식 조건의 변화는 `STARTER`, `LINEUP`, `STATS` 이벤트로 기록됩니다. 화면의 변경 알림 수와 경기별 타임라인은 이 이벤트를 사용하며 외부 문자/메신저 비용은 없습니다.

## 사용자별 Claude API 키

`ADMIN_TOKEN`은 Supabase Cron과 Vercel API 사이에서만 사용하며 사용자 화면에 입력하지 않습니다. 사용자는 Supabase Auth 계정으로 로그인한 뒤 `내 Claude 설정`에서 자기 API 키와 모델을 등록합니다. 키는 `user_claude_settings`에 사용자 ID별로 암호화해 저장하고 원문을 브라우저 저장소나 API 응답에 남기지 않습니다. 암호화에는 Cron 토큰과 별개인 필수 `SECRET_ENCRYPTION_KEY`를 사용합니다.

초대 이메일로 가입한 사용자는 비밀번호가 없을 수 있습니다. 이 경우 로그인 화면에서 이메일 주소만 입력하고 `이메일 로그인 링크 받기`를 사용합니다. Supabase 클라이언트가 초대·매직링크 반환 URL의 세션을 복원하고 이후 토큰도 자동 갱신합니다.

공용 Cron은 통계 예측만 생성합니다. Claude는 사용자가 경기 카드에서 `개인 분석 실행`을 눌렀을 때만 그 사용자의 키로 호출되며 결과는 응답으로만 전달됩니다. 공용 `predictions`와 다른 사용자 화면에는 저장하거나 반영하지 않습니다.

Claude 혼합 비율은 `0.15`, 요청 제한시간은 `20초`, 최대 출력은 `600토큰`으로 코드에 고정합니다. 배포 환경변수로 이 안전 한도를 변경하지 않습니다.

운영 PostgreSQL에서는 먼저 최신 Alembic 마이그레이션을 적용해야 합니다.

```bash
alembic upgrade head
```

## 평가 누수 방지

- `game_results.finalized_at`은 결과를 처음 확인한 시각으로 고정
- `prediction.created_at <= game.start_at`이고 결과 저장 전인 예측만 평가
- 같은 경기에서는 요청한 stage의 마지막 예측 또는 전체 마지막 경기 전 예측 하나만 사용
- 운영 승률 보정은 리그·시즌별로 해당 경기 시작 전에 종료 확인된 결과만 사용하는 expanding Platt 방식이다. 최소 30경기 전에는 원 확률을 유지하고, 보정된 홈승·원정승 비중으로 20,000회 표본을 결정론적으로 재가중한 뒤 승률·핸디캡·언더오버·점수 분포를 모두 다시 계산한다.
- 현재 시즌 누적값을 과거 날짜에 역으로 붙이지 않음

`python -m backend.app.cli backtest --league ALL`에서 Accuracy, Brier, Log Loss, 득점 MAE·RMSE, calibration error, 월·리그·모델별 결과와 expanding home-rate 기준 모델을 함께 확인합니다. 두 모델이 같은 경기를 예측한 경우에는 paired 차이와 bootstrap 95% 신뢰구간을 추가로 계산합니다. 200경기는 예비 판단선, 500경기는 권장 판단선이며 그 전에는 승격 결론을 내리지 않습니다.

`team_residual_walk_forward`는 팀 공격·수비 EWMA, 홈·원정 분리, 축소된 맞대결 잔차를 과거 경기마다 순서대로 다시 계산해 보정 전후 득점 MAE·RMSE와 승률 보정 지표를 비교합니다. 200경기 이상에서 MAE·RMSE·Brier·Log Loss·calibration error가 모두 악화되지 않아야 `deployment_gate=PASS`입니다. 팀 잔차 계층은 2026-08-23 경기부터 KBO와 MLB에 활성화되어 있으며, 종료 결과는 최초 `finalized_at`을 보존하므로 다음 refresh부터 잔차 이력과 리그 승률 보정 이력에 자동으로 포함됩니다.

과거 아카이브 재현은 `python -m backend.app.cli historical-replay --league KBO --limit 20`으로 백필합니다. 재현은 경기 시작 전 종료 경기만으로 팀 기록을 다시 만들며 `HISTORICAL_REPLAY`로 저장됩니다. 백테스트의 공식 실전 지표와 재현 지표는 분리되고, 재현 표본은 학습을 보조할 수 있지만 최소 40개의 독립 실전 검증 표본 없이는 자동 모델 승격에 사용되지 않습니다.

## 자동 학습·승격·롤백

- 매일 KBO 05:30, MLB 16:30 KST에 리그별 lifecycle job 실행
- 경기 전 snapshot과 실제 결과가 연결된 경기만 사용하고, 경기마다 가장 늦은 사전 snapshot 하나만 선택
- 최소 200경기에서 학습 시작, 날짜순 마지막 20%이자 최소 40경기는 검증 전용
- 후보: 표준화 L2 로지스틱 승률 모델 + ridge 홈·원정 득점 모델
- 승격: Brier 0.002 또는 득점 MAE 0.10 이상 개선, 동시에 Brier·Log Loss·득점 MAE 악화 하한 통과
- 재학습: 마지막 학습 뒤 25경기 이상 추가됐을 때만 실행
- 롤백: 승격 뒤 새 50경기 이상에서 이전 모델 대비 Brier 0.015, Log Loss 0.025, 득점 MAE 0.15 중 하나를 초과 악화하면 이전 포인터 복구
- 모든 `WAITING_FOR_DATA`, `PROMOTED`, `REJECTED`, `ROLLED_BACK` 결정과 지표를 DB에 보존

## 아직 선택적으로 남겨둔 데이터

부상자, 실시간 날씨, 실제 불펜 엔트리 가용성, 타자 좌우 split, 이동 거리, 시장 배당은 현재 공식 무료 소스만으로 리그 간 동일 품질을 보장하기 어려워 모델 입력으로 가장하지 않습니다. 현재 불펜 값은 팀 ERA·선발 평균 이닝·최근 선발 투구 부담을 이용한 명시적 proxy입니다. 이 누락을 반영해 신뢰도 상한은 94입니다.
