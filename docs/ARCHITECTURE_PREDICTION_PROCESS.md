# Dugout Lab 구조·예측 프로세스 설명서

이 문서는 시스템 구조와 한 경기의 데이터가 최종 승률·예상 점수·핸디캡·언더오버로 변환되는 원리를 설명한다. 설치·배포·운영 절차는 [인수인계·개발 가이드](HANDOVER_DEVELOPMENT_GUIDE.md)를 참고한다.

## 1. 시스템이 해결하는 문제

Dugout Lab은 KBO와 MLB의 공식 경기·팀·선수 데이터를 수집해 다음 결과를 제공한다.

- 홈팀·원정팀 승률
- 팀별 평균 예상 득점과 총점
- 실제 시장 기준 핸디캡 minus/plus 확률
- 실제 시장 또는 모델 기준 언더·오버 확률
- 20,000회 전체 분포의 대표 결말
- 예상 승리팀이 이긴다는 조건의 대표 점수
- 55% 미만 접전의 양 팀 승리 시나리오
- 팀별 득점 구간, 총점 구간, 접전·대승·연장 확률

핵심 원칙은 모든 출력이 가능한 한 동일한 시뮬레이션 모집단에서 나오게 하는 것이다. 승률은 A 계산, 핸디캡은 B 계산, 대표 점수는 C 계산으로 서로 모순되게 만들지 않는다.

## 2. 전체 아키텍처

```text
KBO 공식 데이터 ─┐
MLB Stats API ───┼─> collectors ─> refresh service ─> Supabase PostgreSQL
The Odds API ────┘                         │                    │
                                           │                    ├─ games/results/stats
                                           │                    ├─ starters/lineups/splits
                                           │                    ├─ predictions/snapshots
                                           │                    └─ evaluations/models
                                           ▼
                                  feature engineering
                                           │
                         ┌─────────────────┴─────────────────┐
                         ▼                                   ▼
                  승률 분류 신호                      홈·원정 기대득점
                         └─────────────────┬─────────────────┘
                                           ▼
                                  팀 잔차·분산 보정
                                           ▼
                              20,000회 경기 시뮬레이션
                                           ▼
                              리그별 승률 보정·분기 재가중
                                           ▼
                       승패·핸디캡·총점·점수 분포 일괄 재계산
                                           ▼
                                  FastAPI 카드 payload
                                           ▼
                                     React 사용자 화면
```

운영 자동화는 상주 서버가 아니라 Supabase Cron이 Vercel FastAPI 관리자 엔드포인트를 호출하는 구조다. PostgreSQL advisory lock이 같은 리그·날짜의 중복 실행을 막는다.

## 3. 계층별 책임

### 수집 계층

- `collectors/kbo`: KBO 일정, 결과, 팀 기록, 선발, 라인업, 타자·투수 맞대결
- `collectors/mlb`: MLB 일정, 결과, 팀 기록, 선발 game log, 라인업, 선수 split
- `collectors/odds.py`: 구조화된 승패·총점 시장 중앙값

수집기는 값과 함께 source URL, 수집 시각을 반환한다. 일부 선택 소스가 실패해도 전체 경기를 중단하지 않고 기존 정상 데이터를 유지하는 것이 원칙이다.

### 저장·조회 계층

- `models/entities.py`: DB 엔터티
- `repositories/repository.py`: upsert, 중복 방지, API 카드 직렬화
- `prediction_snapshots`: 시점별 불변 감사 자료
- `prediction_evaluations`: 실제 결과가 시뮬레이션에서 몇 번 나왔는지 평가
- `game_starters`: 과거 경기 선발과 대상 경기 전까지의 누적 투구 기록

### 예측 계층

- `feature_engineering.py`: 특징, 기본 승률 신호, 기대득점
- `team_residuals.py`: 팀별 공격·수비 오차와 분산 보정
- `probability_calibration.py`: 리그별 승률 보정
- `bullpen.py`: 선발 이후 불펜 leverage tier 계획
- `plate_engine.py`: 타석 단위 base-out 시뮬레이션
- `simulation.py`: 이닝 엔진, 연장, 표본 요약, 시장 확률
- `prediction.py`: 위 요소를 한 경기 payload로 조립

### 학습·검증 계층

- `historical_replay.py`: 당시 경기 전 정보만 복원하는 회고 재현
- `archived_starters.py`: 공식 일정·등판 로그로 과거 경기 선발 입력 복원
- `backtest.py`: 날짜순 walk-forward 평가
- `model_lifecycle.py`: challenger 학습, champion 승격, 자동 롤백

## 4. 수집에서 예측까지의 실행 순서

한 날짜를 갱신하면 다음 순서로 진행된다.

1. 일정과 경기 상태 upsert
2. 종료 경기 결과와 이닝 결과 저장
3. 팀 시즌·최근 기록 갱신
4. 예정 경기 선발과 투수 세부 기록 수집
5. 발표된 라인업 저장
6. 타자 split 캐시에서 타석 엔진 테이블 구성
7. 날씨·불펜 소모·일정 피로 context 구성
8. 최신 시장 기준점 조회
9. 팀 잔차 이력과 리그 승률 보정 이력 구성
10. `predict_game`으로 경기별 예측
11. 입력 hash가 달라진 경우 새 prediction append
12. 체크포인트 창이면 불변 snapshot 저장
13. 종료 경기의 경기 전 prediction 평가

경기 시작 후 새로 수집한 라인업이나 결과로 경기 전 예측을 덮어쓰지 않는다.

과거 재현은 별도 선발 복원 단계를 갖는다. 공식 일정에서 당시 선발 identity를 가져오고, 그 투수의 대상 경기 날짜 이전 등판만 합산해 ERA·WHIP·FIP·평균 선발 이닝·K-BB·Quality Start를 재구성한다. 대상 경기 자신의 투구 결과는 제외한다.

## 5. 특징 공학

### 팀 기본 전력

주요 팀 특징은 다음과 같다.

- 시즌 승률, 홈 승률, 원정 승률
- 경기당 득점·실점
- AVG, OBP, SLG, OPS
- 최근 5·10경기 승률과 득실점
- 최근 팀 간 맞대결 승률과 득실점
- 수비율, 주루, 도루 저지 관련 가용 지표

시즌 초·소표본 값은 리그 평균 쪽으로 축소한다. 두 팀 평균을 리그 평균처럼 사용하지 않는다. 타율·출루율·장타율은 코드에 버전 관리된 리그 기준점과 비교해 한 팀의 타격 상승이 상대 팀 예상 득점을 잘못 낮추는 교차 영향을 방지한다.

### 선발과 투수진

- ERA, WHIP, WAR, FIP
- K-BB%, Quality Start 비율
- 평균 선발 이닝
- 휴식일, 최근 투구 수, 최근 등판 폼
- 상대 팀 상대 ERA·WHIP
- 확정 여부

선발 지표는 기대득점 수준에 반영된다. 시뮬레이션의 staff plan은 같은 선발을 다시 득점 수준에 중복 가산하지 않고, 선발이 언제 내려가고 어떤 불펜 tier가 어느 이닝에 나오는지 분포 모양을 바꾼다.

실전 prediction은 `pitcher_stats`의 경기 전 수집값을 사용한다. 과거 재현은 `game_starters`의 공식 선발과 strictly-prior 누적 기록을 `PitcherStat`과 같은 형태로 변환한다. 과거 경기 box score에서 선발 identity를 확인하는 것은 허용하지만, 해당 경기 성적을 선발 능력치에 포함하는 것은 금지한다.

### 라인업과 타자 split

- 실제 타순의 KBO 생산력 또는 MLB OPS
- 타자-선발 맞대결 AVG·OBP·SLG·OPS
- 좌·우 투수 상대 platoon OPS
- 표본 수 기반 축소 가중치

라인업이 없으면 최근 라인업 또는 팀 시즌 공격력을 사용하고 신뢰도를 낮춘다. 이름 9명이 저장됐다고 타석별 엔진이 자동 활성화되는 것은 아니다.

### 구장·날씨·피로

- KBO·MLB 구장별 run factor
- 경기 시각 온도·바람·상태에서 계산된 날씨 배수
- 최근 불펜 투구량과 고부하 투수
- 최근 3일 경기 수, 연속 경기일, 이동 거리 proxy
- 더블헤더 여부

공식 세부 데이터가 없으면 중립값 또는 명시된 proxy를 사용하고 `confidence_missing`에 부족 항목을 남긴다.

## 6. 기본 승률 신호

베이스라인은 특징의 방향과 계수를 코드로 명시한 logistic 함수다.

```text
p_classifier = sigmoid(
  홈 기본 절편
  시즌·최근 승률 차이
  득점·실점·OPS 차이
  선발 ERA·WHIP·FIP·K-BB 차이
  불펜·휴식·일정 차이
  맞대결·라인업·platoon 차이
  수비·주루 차이
)
```

최근 기록, 맞대결, 선발, 라인업은 표본과 확정 여부에 따라 reliability가 달라진다. 예를 들어 실제 라인업 미확정 시 라인업 차이의 영향은 축소된다.

운영 champion 모델이 있으면 버전 관리된 L2 logistic 모델을 사용한다. champion의 run model은 이미 분류 승률을 기대득점 차이에 결합하므로 `predict_game`에서 같은 classifier를 두 번 혼합하지 않는다. champion이 없을 때만 베이스라인 기대득점에 classifier 신호를 한 번 결합한다.

## 7. 홈·원정 기대득점

기대득점은 단순히 두 팀 평균 득점을 더하거나 빼는 방식이 아니다. 개념적으로 다음 구조다.

```text
리그 득점 환경
× 우리 팀 공격 강도^0.90
× 상대 팀 실점 강도^0.78
× AVG·OBP·SLG 타격 배수
× 상대 선발·불펜 배수
× 구장·날씨 배수
× 라인업·platoon·맞대결의 축소 배수
× 홈·원정 조정
```

리그 득점 환경은 당일 전체 팀 통계로 계산하며, 격리 테스트처럼 전체 값이 없을 때만 고정 리그 평균과 두 팀 관측치를 혼합한다.

홈 어드밴티지는 득점 배수와 야구 규칙에 나뉘어 있다. 홈팀은 앞서면 9회말을 치지 않고 동점·열세에서 끝내기가 가능하므로 시뮬레이션 자체에도 홈 이점이 생긴다. 이 효과를 큰 득점 배수로 다시 중복 계산하지 않도록 작은 관측 기반 배수만 사용한다.

## 8. 팀 예측 잔차 보정

잔차는 과거 경기의 실제 득점에서 당시 경기 전 기대득점을 뺀 값이다.

```text
공격 잔차 = 실제 팀 득점 - 경기 전 팀 기대득점
수비 잔차 = 상대 실제 득점 - 상대 경기 전 기대득점
```

`TeamResidualHistory`는 한 경기당 하나의 유효한 경기 전 prediction을 선택한다. 최신 실전 예측을 우선하고, 실전 예측이 없으면 누수 감사를 통과한 과거 재현을 사용할 수 있다.

보정 요소:

- 팀별 공격·수비 EWMA
- 홈·원정 상황별 잔차
- 같은 상대와의 맞대결 잔차
- 엔진·확정 상태·득점 환경·시즌 단계가 유사한 구조 잔차
- 팀별 잔차 변동성

최근 폼은 기본 모델에도 들어가므로 같은 방향으로 잔차를 그대로 더하면 이중 계산된다. 현재 검증 정책은 축소된 잔차 신호에 평균회귀 계수를 적용한다. 맞대결과 구조 잔차는 표본 사전값으로 강하게 축소해 작은 비중만 반영한다.

잔차 평균은 홈·원정 기대득점을 조정하고, 잔차 분산은 각 팀 gamma 충격의 variance multiplier로 전달된다. 타석별 엔진은 자체 타석 변동성이 있으므로 추가 팀 분산을 더 보수적으로 사용한다.

## 9. 두 시뮬레이션 엔진

### 이닝별 엔진 `INNING_RATE`

양 팀 타자 base-state 테이블이 모두 준비되지 않으면 사용하는 기본 엔진이다.

1. 경기 공통 gamma 환경을 20,000개 생성
2. 홈·원정 팀별 독립 gamma 충격 생성
3. 기대득점을 9이닝 가중치로 분배
4. 이닝별 점수와 리드에 따라 상대 불펜 tier 선택
5. Poisson으로 각 이닝 득점 생성
6. 9회말 미실시와 끝내기 득점 상한 적용
7. 동점이면 리그별 연장 규칙 적용

공통 gamma 환경은 날씨·구장·심판처럼 양 팀 득점을 함께 올리거나 내리는 상관을 만든다. 팀별 gamma 충격은 타선 연결, 수비 실행, 불펜 난조처럼 한 팀에 집중된 과산포를 만든다.

### 타석별 엔진 `PLATE_APPEARANCE`

양 팀 모두 9명의 타순과 수집된 base-state split 테이블이 있을 때만 사용한다.

- 타자별 아웃·볼넷·단타·2루타·3루타·홈런 확률
- 상대 투수와 handedness split
- 주자·아웃 base-state 전이
- 선발 예상 이닝과 불펜 leverage tier
- 리그별 연장 규칙

실제 라인업 `confirmed` 여부는 신뢰도와 특징 가중치에 영향을 주지만, 엔진 선택의 핵심 조건은 양 팀 타자 테이블의 존재다. 표본이 없는 타자를 임의로 정교한 타석 확률로 꾸미지 않고 이닝별 엔진으로 안전하게 후퇴한다.

## 10. 선발·불펜 운영 모델

불펜은 네 tier로 나눈다.

- `high_leverage`: 필승조
- `middle`: 일반 중간계투
- `chase`: 추격조
- `mop_up`: 큰 점수차 처리조

6회 이후 동점, 3점 이내 리드, 1점 열세는 high leverage 상황으로 본다. 2~5점 열세는 chase, 6점 이상 차이는 mop-up 성격으로 전환한다. 각 팀의 선발 평균 소화 이닝은 고정 교체 시점이 아니라 분포로 사용된다.

엔진은 먼저 중립 calibration pass를 실행해 실제 tier 사용 비율과 9회말 미실시로 줄어드는 득점을 측정한다. 이후 rate normalizer를 적용해 staff plan이 득점의 시점과 분산은 바꾸되 목표 기대득점 수준을 과도하게 이동시키지 않게 한다.

## 11. 연장 규칙

- MLB: 10회부터 자동 주자 효과를 반영하고 승부가 날 때까지 시뮬레이션한다. 최종 무승부 확률은 0이다.
- KBO: 정규시즌 규칙에 따라 최대 11회까지 진행하고 남은 동점은 무승부로 유지한다.

KBO 승률 화면은 무승부를 홈·원정에 임의 분할하지 않고, 무승부를 제외한 two-way 승률을 사용한다.

## 12. 리그별 승률 보정과 표본 재가중

원 시뮬레이션 승률이 장기적으로 과신 또는 과소신이면 리그별 Platt 보정기를 사용할 수 있다.

### 이력 조건

- 같은 리그·같은 시즌
- 대상 경기 시작 전에 종료가 확인된 경기만 사용
- 한 경기당 하나의 경기 전 prediction
- 과거 재현은 `training_eligible`이며 leakage audit가 PASS인 경우만 사용
- KBO 무승부는 two-way 보정 학습에서 제외
- 최소 30경기, 최대 최근 1,000경기

Platt 식은 다음과 같다.

```text
p_calibrated = sigmoid(a × logit(p_raw) + b)
```

`a`, `b`는 L2 정규화된 IRLS/Newton 방식으로 수렴시킨다. 표본이 작을 때는 `a=1`, `b=0` 근처에 머물게 한다.

### 리그별 운영 게이트

보정기가 존재한다고 무조건 적용하지 않는다. chronological replay에서 Brier와 Log Loss가 모두 악화되지 않아야 운영 PASS다.

| 리그 | 검증 표본 | 상태 | 운영 동작 |
|---|---:|---|---|
| KBO | 555경기 | PASS | 보정 승률로 분기 재가중 |
| MLB | 1,963경기 | HOLD | 원 승률 유지, HOLD 사유 저장 |

### 승리 분기 재가중

보정 승률을 컬럼 값만 바꾸는 것은 금지한다. 원 20,000개 경기 중 홈승·원정승 경로를 결정론적 systematic sampling으로 늘리거나 줄인다.

- 홈승 표본 내부의 점수·이닝 경로 모양은 유지
- 원정승 표본 내부의 점수·이닝 경로 모양은 유지
- KBO 무승부 표본 수는 그대로 유지
- 전체 표본 수는 정확히 20,000회 유지
- 같은 입력은 같은 결과를 재현

그 후 평균 득점, 승률, 핸디캡, 총점, 구간, 대표 점수를 모두 재가중 모집단에서 다시 계산한다.

## 13. 승패·핸디캡·언더오버 계산

### 승패

```text
홈승 = count(home > away) / 전체 표본
원정승 = count(away > home) / 전체 표본
KBO two-way 홈승 = 홈승 표본 / (홈승 + 원정승 표본)
```

### 핸디캡

핸디캡 side는 홈·원정으로 고정하지 않는다. 시장의 `home_spread` 부호로 minus 팀과 plus 팀을 정한다.

예를 들어 원정팀 -1.5, 홈팀 +1.5라면:

- 원정 -1.5 적중: 원정팀이 2점 이상 승리
- 홈 +1.5 적중: 홈팀 승리 또는 홈팀 1점차 패배

원정 -2.5라면 적중 최소 점수차는 3점이다. 대표 점수도 실제 run line의 최소 margin을 사용하며 항상 2점차로 고정하지 않는다.

### 언더오버

모든 지원 기준점에 대해 다음을 계산한다.

```text
over = count(total > line) / 20,000
under = count(total < line) / 20,000
push = count(total = line) / 20,000
```

화면에 실제 시장 total line이 있으면 그 기준을 우선한다. 시장이 없으면 시뮬레이션에서 over와 under가 가장 균형적인 half-run line을 계산해 표시할 수 있다.

## 14. 예상 점수의 세 가지 의미

야구의 정확한 스코어 하나는 보통 전체 확률의 몇 퍼센트밖에 차지하지 않는다. 그래서 하나의 숫자에 서로 다른 의미를 섞지 않는다.

### 1) 평균 예상 점수

20,000회 전체에서 팀별 득점 평균이다. 팀당 제곱오차를 줄이는 데 적합하고 경기별 공격 환경 차이를 잘 드러낸다. 소수점 한 자리로 표시한다.

### 2) 전체 분포 대표

승패 조건 없이 전체 20,000회에서 가장 자주 나온 정확한 최종 스코어다. `top_scores[0]`, `full_distribution_score`로 저장한다. 이 점수의 승리팀이 전체 승률 1위와 다를 수 있으며 이는 오류가 아니다. 정확한 한 점수의 최빈값과 모든 승리 점수의 합은 다른 통계이기 때문이다.

### 3) 예상 승리팀 조건부 대표

예상 승리팀이 실제로 이긴 표본 안에서 run line과 headline total 방향에 모순되지 않는 Bayes-median 시나리오를 선택한다. `winner_conditional_score`, `primary_score`로 저장한다.

예상 승률이 55% 미만이면 단일 조건부 점수를 강한 예측처럼 표시하지 않는다. `HOME_WIN`, `AWAY_WIN` 각 분기에서 대표 점수를 하나씩 보여준다.

## 15. 입력 hash와 재현성

`prediction.py`는 경기 입력 전체를 JSON으로 직렬화해 SHA-256 `input_hash`를 만든다.

hash에 포함되는 주요 요소:

- 모델 이름·checksum·요약 스키마
- 팀·선발·라인업·split coverage
- staff plan
- 팀 잔차 context
- 승률 보정 계수와 이력 cutoff
- 경기 전 날씨·불펜·일정 context
- headline 시장 기준점

같은 input hash는 중복 prediction을 저장하지 않는다.

시장 line과 Platt 계수가 바뀌면 저장 요약은 다시 만들어야 하지만 야구 원본 난수까지 바뀌면 두 변화의 효과를 비교하기 어렵다. 따라서 simulation seed는 야구 입력에 묶고 시장 기준점과 승률 보정 context는 제외한다. 원 점수 모집단은 유지한 채 시장 선택과 승리 분기 재가중의 효과만 분리할 수 있다.

## 16. 신뢰도

신뢰도는 승률과 다르다. 60% 승률이더라도 입력이 불완전하면 LOW 또는 MEDIUM일 수 있다.

가산 요소:

- 양 팀 선발 확정과 기록 충분
- 양 팀 실제 라인업 확정
- 타자-투수 맞대결 coverage
- 불펜 일일 소모 데이터
- 날씨 데이터

감점·누락 표시:

- 선발 미확정 또는 표본 부족
- 실제 라인업 미발표
- platoon 표본 부족
- 공식 수비·주루 데이터 부재
- 날씨·불펜 가용성 부족
- 분류 승률과 시뮬레이션 승률의 큰 불일치

## 17. 과거 재현과 누수 방지

`HISTORICAL_REPLAY`는 현재 코드로 과거 경기를 다시 계산한 값이며 당시 저장된 실전 예측처럼 표시하지 않는다.

누수 방지 조건:

- `data_cutoff <= game.start_at`
- 대상 경기보다 먼저 시작하고, 대상 경기 시작 전에 종료 확인된 결과만 누적
- 대상 경기 실제 결과는 입력에 사용하지 않음
- 과거 날짜에 현재 시즌 최종 누적값을 역으로 붙이지 않음
- 실전 예측과 회고 재현 성능을 보고서에서 분리

과거 재현은 잔차와 보정의 이력을 보조할 수 있지만 자동 champion 승격에는 독립 실전 검증 표본이 필요하다.

현재 요약 스키마 25는 과거 재현의 실제 경기 전 선발 입력에 더해 상대 강도 보정과 고급 경기 전 입력을 포함한다. 이전 버전 replay는 스키마 버전이 낮아 재생성 대상이 된다.

## 18. 자동 학습·승격·롤백

평가 가능 표본이 200경기에 도달하면 다음 후보를 학습한다.

- 표준화 L2 logistic 승률 모델
- ridge 홈 득점 모델
- ridge 원정 득점 모델

날짜순 마지막 20%, 최소 40경기는 검증 전용이다. 동일 경기 현 운영 모델과 비교해 Brier 또는 득점 MAE 개선 조건과 악화 방지 하한을 모두 지나야 champion으로 승격한다.

승격 후 새 50경기에서 Brier, Log Loss, 득점 MAE가 정책 한도를 넘게 악화되면 이전 champion으로 롤백한다. 모든 결정은 `model_lifecycle_events`에 남는다.

관리자 dry run은 같은 학습·검증을 수행하되 champion을 승격하거나 모델 artifact를 남기지 않는다. 보고서의 `constant_training_features`는 과거 입력이 비어 특정 특징이 모든 경기에서 같은 값이 되는 문제를 찾는 용도다. 선발 archive backfill은 이 진단에서 발견된 상수 선발 특징을 실제 경기 전 값으로 복원하기 위해 추가됐다.

## 19. API와 화면 표현

주요 API:

- `/api/v1/games`: 날짜·리그별 경기 카드
- `/api/v1/games/{external_id}`: 한 경기 상세
- `/api/v1/game-dates`: 시즌 아카이브 날짜
- `/api/v1/model/backtest`: 실전·회고 walk-forward 성능
- `/api/v1/model/lifecycle`: champion과 최근 결정
- `/api/v1/operations/status`: 수집 운영 상태

`GameCard.tsx`는 구버전 저장 payload도 열 수 있도록 fallback을 둔다. 최신 카드에서는 다음 순서를 사용한다.

1. 양 팀 승률
2. 승리팀 → 핸디캡 → 총점 예측 흐름
3. 전체 평균 점수
4. 전체 분포 최빈 결말
5. 비접전이면 예상 승리팀 조건부 대표
6. 접전이면 양 팀 승리 시나리오
7. 상세 분포·구간·이닝 경로·입력 지표

MLB 화면 날짜는 한국 시간 카드 날짜와 미국 현지 `venue_date`를 함께 표시한다.

## 20. 반드시 유지해야 할 정합성 조건

코드를 변경할 때 다음 불변식을 깨면 안 된다.

```text
home_win + away_win + tie = 1
home_two_way + away_two_way = 1
home -1.5 + away +1.5 = 1
away -1.5 + home +1.5 = 1
over + under + push = 1
frequency table의 모든 outcome count 합 = 20,000
MLB tie = 0
KBO tie는 two-way 승률 계산에서 제외
full_distribution_score = top_scores[0]
winner_conditional_score = primary_score
```

API는 소수점 네 자리 반올림을 하므로 합계 검사는 작은 반올림 허용오차를 둔다. 원본 빈도표에서는 정확히 일치해야 한다.

## 21. 모델을 고도화할 때의 원칙

새 지표를 넣기 전 다음 질문에 답해야 한다.

1. 경기 시작 전에 안정적으로 구할 수 있는가?
2. 과거 경기에도 같은 정의로 복원 가능한가?
3. 기존 특징과 같은 정보를 이중 계산하지 않는가?
4. 표본이 작을 때 리그 평균으로 축소되는가?
5. walk-forward에서 Brier·Log Loss 또는 MAE·RMSE가 개선되는가?
6. 리그별로 결과가 다르면 독립 게이트를 둘 수 있는가?
7. UI의 승률·핸디캡·총점·대표 점수 정합성이 유지되는가?

시장 배당, 생성형 AI 의견, 경기 후 확정 정보는 모델을 그럴듯하게 보이게 만들 수 있지만 미래 예측 검증을 오염시킬 수 있다. 데이터 시점과 역할을 명확히 분리하는 것이 지표 수를 늘리는 것보다 우선이다.

## 22. 상대 강도·Statcast·불펜·구장 고도화

- 시즌 최종값이 아니라 경기 시작 전 확정 결과만으로 Elo, SRS, Pythagorean 기대승률, 상대 보정 공격·수비와 일정 강도를 매번 재구성한다.
- MLB 선발은 Baseball Savant xERA·xwOBA와 최근 14일 대 직전 15~46일 구속·회전·무브먼트·구종 비율 변화를 저장한다.
- 확정 라인업은 타자 xwOBA, 선발 주 구종별 성과, 실제 선발 야수 FRV·OAA, 포수 프레이밍과 선발-포수 최근 배터리 값을 사용한다.
- MLB 현역 로스터와 양 리그 공식 박스스코어를 결합해 등판 가능 후보별 최근 1~3일 투구수·연투를 계산한다. 감독 발표가 없는 `UNAVAILABLE`은 확정 결장이 아니라 workload 판정으로 구분한다.
- MLB 구장 계수는 좌·우 타자별 3년 값을 표본 축소하고 2B·3B·HR 확률을 타석 엔진 내부에서 직접 조정한다. 전체 기대득점은 같은 구장 계수로 다시 보정해 이중 반영하지 않는다.
- KBO는 공식 날씨, 박스스코어 투구수, 최근 선발 로그, 좌·우 투수 스플릿, 팀 수비·포수 도루저지를 수집한다. 공식 OAA·프레이밍·구종 이동 데이터가 없으면 추정하지 않는다.

## 23. 관련 코드와 문서

- `backend/app/services/refresh.py`
- `backend/app/services/prediction.py`
- `backend/app/services/feature_engineering.py`
- `backend/app/services/team_residuals.py`
- `backend/app/services/team_strength.py`
- `backend/app/services/probability_calibration.py`
- `backend/app/services/simulation.py`
- `backend/app/services/plate_engine.py`
- `backend/app/services/backtest.py`
- `backend/app/services/archived_starters.py`
- `backend/app/services/model_lifecycle.py`
- [인수인계·개발 가이드](HANDOVER_DEVELOPMENT_GUIDE.md)
- [자동 수집·운영](OPERATIONS.md)
- [상세 설계](DESIGN.md)
- [데이터 소스](DATA_SOURCES.md)
