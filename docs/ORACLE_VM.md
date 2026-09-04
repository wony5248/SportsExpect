# Oracle Cloud VM 실운영 배포 가이드

작성 기준일: 2026-08-22

이 문서는 Ubuntu 24.04 기반 OCI Compute VM에 SportsExpect를 24시간 실행하고, Caddy HTTPS와 공유 로그인으로 4명이 외부에서 사용하는 절차입니다.

## 먼저 알아둘 점

- Mac은 배포·업데이트·백업 다운로드에만 사용합니다. 배포가 끝나면 Mac이 꺼져도 Oracle VM의 API와 1시간 자동 수집기는 계속 동작합니다.
- Docker와 컨테이너는 VM 재부팅 후 자동 시작합니다.
- 화면과 데이터 API는 Caddy Basic Authentication으로 보호합니다. `/health`, `/ready`만 모니터링을 위해 공개합니다.
- SQLite DB와 일일 백업은 VM의 `/home/ubuntu/sports-expect/data`에 저장합니다.
- 외부에는 80/443만 공개하고 API의 8000번 포트는 VM 내부 `127.0.0.1`에만 둡니다.
- 무료 VM은 비용이 0원일 수 있지만 무료 자원의 재고 부족과 저사용 인스턴스 회수 가능성이 있으므로 유료 SLA 수준의 무중단을 보장하지 않습니다.
- KBO·MLB 데이터의 상업적 이용은 별도 라이선스 확인이 필요합니다. 허가 전에는 4명 비공개 검증판으로 운영합니다.

## 전체 순서

1. Oracle 계정과 Home Region을 확인합니다.
2. Mac에서 SSH 키를 만듭니다.
3. VCN과 Ubuntu ARM VM을 만듭니다.
4. 고정 Public IP와 22/80/443 방화벽 규칙을 설정합니다.
5. 도메인 A 레코드를 고정 IP에 연결합니다.
6. VM에 Docker를 설치합니다.
7. Mac에서 프로젝트를 전송합니다.
8. VM에서 관리자 토큰·사이트 로그인·도메인을 설정합니다.
9. 제공된 배포 스크립트로 실행합니다.
10. 내부 상태, 외부 HTTPS, 자동 수집, 백업을 확인합니다.

## 1. Oracle 계정과 과금 범위 확인

가입 화면에서 가능하면 **South Korea Central (Seoul, `ap-seoul-1`)**을 Home Region으로 선택합니다. Oracle에는 서울과 춘천 두 한국 리전이 있지만, Always Free Ampere A1은 South Korea North(Chuncheon)에서 제외된다고 공식 문서에 명시되어 있습니다. 무료 ARM VM이 목적이면 춘천을 선택하지 않습니다.

Home Region은 계정 생성 후 변경할 수 없습니다. 서울이 가입 목록에 없다면 계정 생성을 확정하지 말고 다음 순서로 판단합니다.

1. 지역 이름이 `South Korea Central (Seoul)` 또는 `Seoul`로 표시되는지 다시 확인합니다.
2. 계속 없다면 Oracle 가입 화면의 지원 채팅에 서울 리전 가입 가능 여부를 문의합니다.
3. 바로 진행해야 한다면 인접 리전인 `Japan East (Tokyo)`를 대안으로 선택할 수 있습니다. 이 경우 서버와 데이터는 일본에 저장되고, Always Free A1 생성 가능 여부와 재고는 가입 후 별도로 확인해야 합니다.

OCI 콘솔에서 우측 상단 리전이 계정의 **Home Region**인지 확인합니다. Always Free 자원은 Home Region에서 만들어야 합니다.

Oracle 공식 문서상 Ampere A1 Always Free 한도는 계정 전체 기준 월 1,500 OCPU-hours와 9,000 GB-hours이며, 현재 2 OCPU/12 GB에 해당합니다. 콘솔의 생성 화면에서 반드시 `Always Free-eligible` 표시와 예상 비용을 확인합니다.

소규모 운영 권장값:

| 항목 | 값 |
|---|---|
| Image | Canonical Ubuntu 24.04 |
| Shape | `VM.Standard.A1.Flex` ARM |
| OCPU / RAM | 1 OCPU / 6 GB |
| Boot volume | 50 GB |
| 네트워크 | Public subnet + Reserved Public IPv4 |

1 OCPU/6 GB면 4명용 API, 수집기, Caddy, 20,000회 CPU 시뮬레이션에 충분합니다. 빌드나 수집이 느리면 Always Free 총 한도 안에서 2 OCPU/12 GB로 조정할 수 있습니다.

> Oracle은 7일 동안 CPU·네트워크·메모리 사용률 조건이 모두 낮은 Always Free VM을 idle로 판단해 회수할 수 있다고 안내합니다. 중요한 서비스라면 별도 유료 인스턴스와 외부 백업을 검토해야 합니다.

공식 참고:

- [Oracle Always Free 자원](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm)
- [OCI 인스턴스 생성](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/launchinginstance.htm)

## 2. Mac에서 전용 SSH 키 생성

Mac 터미널에서 한 번만 실행합니다.

```bash
ssh-keygen -t ed25519 -f "$HOME/.ssh/sports-expect-oracle" -C "sports-expect-oracle"
chmod 600 "$HOME/.ssh/sports-expect-oracle"
```

공개키를 확인합니다.

```bash
cat "$HOME/.ssh/sports-expect-oracle.pub"
```

출력된 `ssh-ed25519 ...` 한 줄은 다음 단계의 VM 생성 화면에 붙여 넣습니다. 확장자가 없는 개인키는 누구에게도 보내지 않습니다.

## 3. VCN과 VM 생성

### VCN

1. OCI 콘솔에서 `Networking > Virtual cloud networks`로 이동합니다.
2. `Start VCN Wizard`를 누릅니다.
3. `Create VCN with Internet Connectivity`를 선택합니다.
4. Public Subnet, Internet Gateway, 기본 Route Rule이 생성되었는지 확인합니다.

### VM

1. `Compute > Instances > Create instance`로 이동합니다.
2. 이름을 `sports-expect`로 지정합니다.
3. Ubuntu 24.04와 `VM.Standard.A1.Flex`를 선택합니다.
4. 1 OCPU / 6 GB RAM, 50 GB 부트 볼륨을 선택합니다.
5. 앞에서 만든 Public Subnet을 선택합니다.
6. Public IPv4 할당을 켭니다.
7. `Paste public keys`에 Mac의 `.pub` 내용을 붙여 넣습니다.
8. 생성 직전 `Always Free-eligible`과 예상 비용을 다시 확인합니다.

무료 A1 재고가 없으면 `Out of host capacity`가 나올 수 있습니다. 다른 Availability Domain을 선택하거나 나중에 다시 시도합니다. South Korea North(춘천)는 A1 생성 제약이 있을 수 있으므로 콘솔과 공식 문서의 현재 제공 범위를 확인합니다.

## 4. 고정 IP와 방화벽

### Reserved Public IP

VM의 임시 Public IP가 재시작·재할당으로 바뀌지 않도록 Reserved Public IP를 만들어 Primary Private IP에 연결합니다.

1. VM 상세 화면에서 `Attached VNICs`를 엽니다.
2. Primary VNIC와 Primary Private IP를 선택합니다.
3. Public IP 편집에서 Reserved Public IP를 생성하거나 기존 고정 IP를 할당합니다.
4. 이후 이 문서의 `VM_PUBLIC_IP`는 이 주소를 뜻합니다.

### NSG 또는 Security List Ingress

| Source CIDR | Protocol | Destination port | 용도 |
|---|---|---:|---|
| 내 현재 공인 IP `/32` | TCP | 22 | SSH 관리 |
| `0.0.0.0/0` | TCP | 80 | 인증서 발급·HTTP→HTTPS |
| `0.0.0.0/0` | TCP | 443 | HTTPS |
| `0.0.0.0/0` | UDP | 443 | HTTP/3, 선택 |

Source Port는 `All`, 규칙은 Stateful로 둡니다. TCP 8000은 추가하지 않습니다. 집 인터넷의 공인 IP가 바뀌어 SSH가 안 되면 22번 Source CIDR만 새 IP `/32`로 수정합니다.

## 5. 도메인 연결

실사용에는 HTTPS가 필요하므로 `sports.example.com` 같은 도메인/서브도메인을 권장합니다.

DNS 제공자에서 다음 레코드를 만듭니다.

| Type | Name | Value | Proxy |
|---|---|---|---|
| A | `sports` 또는 원하는 이름 | `VM_PUBLIC_IP` | 첫 배포 때는 DNS only |

DNS 확인:

```bash
dig +short sports.example.com
```

출력이 Reserved Public IP와 같아진 다음 배포합니다. Caddy는 도메인이 VM을 가리키고 80/443이 열려 있으면 인증서를 자동 발급·갱신합니다. 도메인이 없으면 `http://VM_PUBLIC_IP`로 상태만 시험할 수 있지만, Basic Auth 비밀번호가 암호화되지 않으므로 실제 4명 사용에는 쓰지 않습니다.

## 6. VM 접속과 Docker 설치

Mac에서 접속합니다.

```bash
ssh -i "$HOME/.ssh/sports-expect-oracle" ubuntu@VM_PUBLIC_IP
```

VM에서 Docker 공식 저장소를 설정합니다.

```bash
sudo apt update
sudo apt install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo \"${UBUNTU_CODENAME:-$VERSION_CODENAME}\") stable" | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
```

로그아웃 후 다시 접속합니다.

```bash
exit
ssh -i "$HOME/.ssh/sports-expect-oracle" ubuntu@VM_PUBLIC_IP
docker version
docker compose version
```

Docker 공식 참고: [Ubuntu Docker Engine 설치](https://docs.docker.com/engine/install/ubuntu/)

## 7. Mac에서 프로젝트 전송

Mac의 새 터미널에서 실행합니다.

```bash
rsync -av --delete \
  --exclude .git \
  --exclude .env \
  --exclude .venv \
  --exclude data \
  --exclude oracle-backups \
  --exclude frontend/node_modules \
  -e "ssh -i $HOME/.ssh/sports-expect-oracle" \
  /Users/jangbeomjin/projects/sports-expect/ \
  ubuntu@VM_PUBLIC_IP:/home/ubuntu/sports-expect/
```

`--delete`는 서버 프로젝트 코드 중 Mac에서 삭제된 파일을 정리하지만 제외한 `.env`와 `data`는 지우지 않습니다. 명령의 출발지·도착지를 정확히 확인합니다.

### 로컬 예측 DB를 이어서 쓸 때만

기존 스냅샷을 보존하려면 실행 중인 DB 파일을 직접 복사하지 말고 먼저 Mac에서 일관된 백업을 만듭니다.

```bash
cd /Users/jangbeomjin/projects/sports-expect
source .venv/bin/activate
python -m backend.app.cli backup
```

출력된 최신 `data/backups/baseball-날짜-시간.db` 파일을 서버의 `data/baseball.db`로 전송합니다. **서버 첫 배포 전에만** 실행합니다.

```bash
ssh -i "$HOME/.ssh/sports-expect-oracle" ubuntu@VM_PUBLIC_IP "mkdir -p /home/ubuntu/sports-expect/data/backups /home/ubuntu/sports-expect/data/locks"
scp -i "$HOME/.ssh/sports-expect-oracle" \
  /Users/jangbeomjin/projects/sports-expect/data/backups/baseball-실제파일명.db \
  ubuntu@VM_PUBLIC_IP:/home/ubuntu/sports-expect/data/baseball.db
```

서버가 이미 수집을 시작한 뒤에는 이 방법으로 DB를 덮어쓰지 않습니다.

## 8. VM 환경변수와 로그인 생성

VM에서 실행합니다.

```bash
cd /home/ubuntu/sports-expect
cp .env.oracle.example .env
chmod 600 .env
openssl rand -hex 32
```

마지막 명령의 64자리 값을 복사합니다. 이것은 관리자 갱신 API용 `ADMIN_TOKEN`입니다.

사이트 공유 비밀번호의 bcrypt hash를 만듭니다. 아래 명령은 비밀번호를 대화식으로 물으며 원문을 파일에 저장하지 않습니다.

```bash
docker run --rm -it caddy:2-alpine caddy hash-password
```

이제 편집합니다.

```bash
nano .env
```

도메인이 `sports.example.com`인 예시:

```dotenv
ADMIN_TOKEN=앞에서-생성한-64자리-값
CORS_ORIGINS=https://sports.example.com
DATA_VOLUME=./data
SPORTS_EXPECT_SITE_ADDRESS=sports.example.com
ACCESS_USER=viewer
ACCESS_PASSWORD_HASH='$2a$14$생성된나머지해시'
ODDS_API_KEY=
ODDS_API_REGIONS=us
ODDS_API_REGIONS_KBO=eu
ODDS_API_DAILY_CREDIT_BUDGET=24
MONTE_CARLO_SIMS=20000
BACKUP_RETENTION_DAYS=14
STALE_AFTER_MINUTES=360
```

중요:

- bcrypt hash 전체를 작은따옴표 안에 넣습니다.
- `ADMIN_TOKEN`과 실제 사이트 비밀번호는 서로 다르게 만듭니다.
- `ODDS_API_KEY`가 없으면 빈 값으로 둡니다.
- `.env`를 메신저나 Git에 올리지 않습니다.

## 9. 첫 배포

VM에서 실행합니다.

```bash
cd /home/ubuntu/sports-expect
chmod +x scripts/oracle-*.sh
./scripts/oracle-deploy.sh
```

스크립트가 수행하는 작업:

1. `.env`와 Compose 설정 검증
2. DB·백업 디렉터리 권한 준비
3. 최신 베이스 이미지로 멀티 아키텍처 빌드
4. API·스케줄러·Caddy 실행
5. 60초 동안 내부 `/health` 확인
6. 실패 시 최근 로그 출력

첫 빌드는 Node/Python 의존성을 받아 몇 분 걸릴 수 있습니다. 스크립트가 성공하면 다음을 실행합니다.

```bash
./scripts/oracle-check.sh
```

초기 수집이 아직 진행 중이면 `/health`는 정상이고 `/ready`만 잠시 degraded일 수 있습니다. 다음 로그에서 KBO와 MLB startup refresh 성공을 확인합니다.

```bash
docker compose -f compose.yaml -f compose.oracle.yaml logs -f --tail=200 scheduler
```

로그 보기를 끝낼 때 `Ctrl+C`를 눌러도 컨테이너는 종료되지 않습니다.

## 10. 외부 HTTPS 확인

Mac 또는 휴대전화 모바일 데이터에서 확인합니다.

```bash
curl -sS -o /dev/null -w '%{http_code}\n' https://sports.example.com/health
curl -sS -o /dev/null -w '%{http_code}\n' https://sports.example.com/
```

정상 기대값:

- `/health`: `200`
- `/`: 인증 전 `401`, 브라우저에서 로그인 후 화면 표시
- 브라우저 주소창: 유효한 HTTPS 자물쇠

Caddy 인증서 발급 문제가 있으면 확인합니다.

```bash
docker compose -f compose.yaml -f compose.oracle.yaml logs --tail=200 caddy
```

주요 원인은 DNS가 다른 IP를 가리킴, OCI의 80/443 미개방, 도메인 프록시 설정입니다. [Caddy Automatic HTTPS](https://caddyserver.com/docs/automatic-https)도 참고합니다.

## 11. 자동 갱신과 재예측 확인

스케줄러는 컨테이너 시작 직후 당일 누락 리그를 수집하고 다음 작업을 계속 실행합니다.

- KBO·MLB 전체: 1시간마다
- 다음 날 MLB: 00:20 KST
- 다음 날 KBO: 13:10 KST
- 경기 24시간·3시간·60분·15분 전: 집중 갱신
- 경기 근처: 30분 간격
- 03:30 KST: SQLite 백업

입력이 변경되면 새 input hash와 새 예측을 저장합니다. 같은 입력은 중복 저장하지 않습니다.

수동 전체 갱신:

```bash
docker compose -f compose.yaml -f compose.oracle.yaml exec scheduler \
  python -m backend.app.cli refresh --league ALL --force
```

상태 확인:

```bash
./scripts/oracle-check.sh
```

## 12. 백업과 VM 밖 보관

서버 내부 일일 백업은 `data/backups`에 14일 보존됩니다. 수동으로 즉시 생성하고 체크섬을 확인할 수 있습니다.

```bash
./scripts/oracle-backup.sh
```

VM 자체가 삭제되면 같은 부트 볼륨의 백업도 함께 잃을 수 있으므로 월 1회 이상 Mac으로 다운로드합니다. Mac에서 실행합니다.

```bash
mkdir -p /Users/jangbeomjin/projects/sports-expect/oracle-backups
rsync -av \
  -e "ssh -i $HOME/.ssh/sports-expect-oracle" \
  ubuntu@VM_PUBLIC_IP:/home/ubuntu/sports-expect/data/backups/ \
  /Users/jangbeomjin/projects/sports-expect/oracle-backups/
```

운영 중요도가 올라가면 OCI Object Storage 또는 별도 저장소로 자동 복제를 추가합니다. 같은 VM 안의 사본만으로는 재해 복구 백업이 아닙니다.

## 13. 코드 업데이트

업데이트 전에 서버 백업을 생성합니다.

```bash
ssh -i "$HOME/.ssh/sports-expect-oracle" ubuntu@VM_PUBLIC_IP \
  "cd /home/ubuntu/sports-expect && ./scripts/oracle-backup.sh"
```

그 다음 7장의 `rsync --delete` 명령을 다시 실행하고 VM에서 재배포합니다.

```bash
cd /home/ubuntu/sports-expect
./scripts/oracle-deploy.sh
./scripts/oracle-check.sh
```

빌드 실패 시 기존 컨테이너는 계속 실행됩니다. `up` 이후 상태가 나쁘면 로그를 확인하고 이전 코드 사본으로 되돌려야 하므로, 정식 운영부터는 Git 원격 저장소와 릴리스 태그를 추가하는 것이 좋습니다.

## 14. 자주 쓰는 운영 명령

```bash
cd /home/ubuntu/sports-expect

# 전체 상태
docker compose -f compose.yaml -f compose.oracle.yaml ps

# 스케줄러 로그
docker compose -f compose.yaml -f compose.oracle.yaml logs -f --tail=200 scheduler

# API/Caddy 로그
docker compose -f compose.yaml -f compose.oracle.yaml logs --tail=200 api caddy

# 컨테이너 재시작
docker compose -f compose.yaml -f compose.oracle.yaml restart api scheduler caddy

# 이미지와 코드를 다시 빌드해 배포
./scripts/oracle-deploy.sh
```

`docker compose down -v`, `docker volume rm`, `rm -rf data`는 DB나 인증서 데이터를 제거할 수 있으므로 실행하지 않습니다.

## 15. 운영 완료 체크리스트

- [ ] Home Region의 Always Free-eligible VM과 예상 비용 확인
- [ ] Reserved Public IP 할당
- [ ] SSH 22는 내 IP `/32`만 허용
- [ ] 80/443만 외부 공개, 8000 미공개
- [ ] 도메인 A 레코드와 Reserved IP 일치
- [ ] `.env` 권한 600
- [ ] 사이트 비밀번호와 관리자 토큰 분리
- [ ] `/health` 200, `/` 인증 전 401
- [ ] Caddy HTTPS 인증서 정상
- [ ] API·scheduler·caddy 모두 running/healthy
- [ ] KBO·MLB 최근 수집 성공
- [ ] 1시간 후 새 수집 로그 확인
- [ ] 수동 백업 생성과 Mac 다운로드 완료
- [ ] 휴대전화 LTE/5G에서 로그인·화면 확인
- [ ] 유료 공개 전 데이터 라이선스 검토

## 제가 이어서 확인할 때 필요한 정보

다음 값이 준비되면 비밀번호·토큰 원문을 제외하고 전달합니다.

- Reserved Public IP
- 사용할 도메인
- VM 이미지가 Ubuntu인지 Oracle Linux인지
- SSH 접속 성공 여부
- `./scripts/oracle-deploy.sh`의 성공/실패 출력
- 실패했다면 `docker compose -f compose.yaml -f compose.oracle.yaml logs --tail=200 api scheduler caddy` 출력

개인키, `.env`, `ADMIN_TOKEN`, 사이트 비밀번호, bcrypt hash는 보내지 않습니다.
