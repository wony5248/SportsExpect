# 데이터 소스 채택 기준과 조사 결과

자동 수집에는 정확성뿐 아니라 API 안정성, 호출 한도, 재배포 허용 범위가 필요합니다. 화면에서 조회된다는 이유만으로 HTML을 무단 스크래핑하지 않습니다.

## 현재 자동 반영

| 데이터 | 리그 | 소스 | 반영 방식 |
|---|---|---|---|
| 일정·결과·팀 기록·선발·라인업 | KBO | KBO 공식 웹 데이터 | 정기 갱신 |
| 선발의 상대팀별 ERA·WHIP | KBO | KBO 공식 선수 상세 | 이닝 수로 표본 축소 |
| 확정 타자의 상대 선발 시즌 기록 | KBO | [KBO 투수 VS 타자](https://web1.koreabaseball.com/Record/Etc/HitVsPit.aspx) | PA·AVG·OBP·SLG·OPS와 타격 이벤트 저장, 12시간 캐시, 최대 45% 가중치 |
| 일정·결과·팀/투수/라인업/BvP | MLB | MLB Stats API | 확정 라인업 쌍만 조회, PA로 표본 축소 |
| 시장 승률·기준 총점 | KBO/MLB | The Odds API(선택) | 모델 입력에서 제외하고 비교 기준으로만 저장 |

## 조사했지만 기본 수집에서 제외

| 후보 | 제공 가능 정보 | 현재 판단 |
|---|---|---|
| [Sports2i](https://www.sports2i.com/) | KBO 상세·고급 기록 | KBO 공식 기록 사업자이지만 공개 무료 API가 확인되지 않아 제휴/계약 전에는 미사용 |
| [STATS Perform Developer Portal](https://developer.stats.com/io-docs) | BvP를 포함한 상용 스포츠 피드 | 엔터프라이즈 계약 후보. 4명용 무료 운영에는 부적합 |
| [BALLDONTLIE MLB API](https://mlb.balldontlie.io/) | MLB 부상, 선수 split, player-vs-player, 투구 유형, 배당·라인업 | API 키 기반 보완 후보. MLB 공식 API와 중복되는 BvP보다 부상·구종 데이터가 필요할 때 검토 |
| [Stathead Versus Finder](https://www.sports-reference.com/stathead/baseball/versus-finder.fcgi) | MLB 타자-투수 상대 기록 | 대화형 구독 서비스이며 자동 재배포용 공개 API가 아니므로 미수집 |
| [KBO insight](https://kbo-analytics-home.vercel.app/) | KBO 투구 단위 리포트 | 분석 참고용. 안정된 공개 API와 재배포 조건이 확인되지 않아 자동 수집하지 않음 |
| [VISUAL BASEBALL](https://visualbaseball.com/batting) | KBO 구종·구속별 타격 split | 분석 참고용. 공개 API/라이선스 확인 전에는 자동 수집하지 않음 |
| Statiz·KBO.GG·라이브스코어·베트맨 | 고급 기록 또는 배당 화면 | 동적 HTML·비공식 추정·이용 조건 문제 때문에 무단 스크래핑하지 않음 |

## 추가 데이터 우선순위

1. 무료 공식 데이터로 KBO/MLB 확정 라인업 BvP 표본을 먼저 축적합니다.
2. 실제 정확도 개선 여부를 walk-forward로 확인합니다. BvP 표본이 작으면 모델 영향은 작게 유지합니다.
3. 다음 유료 후보는 MLB 부상·구종 데이터입니다. 도입 전 API 비용과 재배포 권한을 확인합니다.
4. KBO 구종·타구·수비 고급 데이터는 Sports2i 제휴 또는 명시적인 API 사용 허가가 있을 때만 연결합니다.

외부 시장 값은 예측 모형의 입력으로 넣지 않습니다. 같은 경기의 모델 성능을 독립적인 시장 기준과 비교하기 위해 경기 전 불변 스냅샷으로만 보존합니다.
