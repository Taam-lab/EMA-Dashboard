# 배포 가이드 — 자동 갱신되는 공개 웹사이트 (전부 무료)

목표: **평일 15:05(KST) 마감 직전에 자동 갱신되는 공개 URL 대시보드** — 보고 나서 동시호가에 주문할 수 있도록

구성:
- **GitHub Actions** — 평일 15:05 KST 자동 실행 → 마감 직전 시세 수집 → 포트폴리오 갱신 → 결과 커밋
- **Streamlit Community Cloud** — 그 저장소를 읽어 공개 URL 웹사이트 제공 (무료, `dashboard.py` 수정 없이 그대로)

> Vercel/Netlify 같은 정적 호스팅에는 Streamlit 을 올릴 수 없습니다. Streamlit 은 파이썬 프로세스가
> 계속 떠 있어야 하는 서버 앱이라, 상주 프로세스를 지원하는 Streamlit Cloud(또는 Render/Fly.io)를 씁니다.

포트폴리오는 **최초 실행일(오늘)부터 시작**, 자본 **10억**, 매매내역·평가손익·누적 매매손익이 매일 누적됩니다.

---

## 1단계 — GitHub에 올리기

GitHub 계정으로 새 저장소(예: `quant-dashboard`)를 만들고, 이 폴더 전체를 push:

로컬 저장소와 첫 커밋은 이미 만들어져 있습니다. 원격만 연결해서 push 하면 됩니다:

```bash
git remote add origin https://github.com/<본인아이디>/quant-dashboard.git
git push -u origin main
```

> 저장소는 **Public** 으로 만드세요. Streamlit Community Cloud 무료 플랜은 공개 저장소만 배포합니다.
> (`data/` 캐시와 `files.zip` 은 `.gitignore` 로 빠집니다.)

## 2단계 — KRX 계정을 시크릿으로 등록 (필수)

KRX 가 로그인 필수로 정책을 바꿔서, pykrx 로 시세를 받으려면 KRX 계정이 있어야 합니다.
등록하지 않으면 배치가 "환경변수 KRX_ID, KRX_PW 가 없습니다" 로 즉시 중단됩니다.

저장소 → **Settings → Secrets and variables → Actions → New repository secret** 에서 두 개 등록:

| 이름 | 값 |
|---|---|
| `KRX_ID` | KRX 로그인 ID |
| `KRX_PW` | KRX 로그인 비밀번호 |

gh CLI 로 등록해도 됩니다 (값은 프롬프트에 직접 입력):

```bash
gh secret set KRX_ID --repo <본인아이디>/EMA-Dashboard
```

```bash
gh secret set KRX_PW --repo <본인아이디>/EMA-Dashboard
```

> 시크릿은 저장소가 Public 이어도 외부에 노출되지 않고, 로그에도 마스킹됩니다.
> 다만 **KRX 세션은 1시간 만료**이고 접속 IP 가 해외(Actions 러너)라는 점은 감안하세요.

## 3단계 — 자동 스케줄 켜기 (GitHub Actions)

- 저장소 → **Settings → Actions → General → Workflow permissions** 를 **Read and write** 로 변경. (결과 JSON 커밋에 필요 — 안 하면 push 단계에서 403)
- 저장소 → **Actions** 탭 → 워크플로우 활성화(처음엔 "I understand..." 버튼).
- `daily-update` 워크플로우가 평일 15:05 KST 자동 실행됩니다. (Actions 스케줄은 수십 분 밀릴 수 있어 마감 25분 전으로 잡았습니다.)
- 지금 바로 한 번 돌리려면: Actions → daily-update → **Run workflow** 클릭.
  (첫 실행은 수백 종목 수집이라 몇 분 걸립니다. 끝나면 `portfolio_state.json` / `snapshot.json`이 커밋됨.)

## 4단계 — 웹사이트 배포 (Streamlit Community Cloud)

1. https://share.streamlit.io 접속 → GitHub 계정으로 로그인.
2. **New app → Deploy a public app from GitHub** → 저장소 `quant-dashboard`, 브랜치 `main`, 파일 `dashboard.py`, Python 3.11 → Deploy.
3. 잠시 후 `https://<앱이름>.streamlit.app` 공개 URL이 생깁니다. 이게 대시보드 사이트입니다.

이후 평일 15:05 에 Actions가 데이터를 갱신·커밋하면 Streamlit 앱이 자동 리로드됩니다. 대시보드 상단에 **주문 가능 구간**인지 **이미 마감된 종가**인지가 표시됩니다.

---

## 동작 요약

```
평일 15:05 KST (GitHub Actions) — 마감 25분 전
  └─ python run_daily.py
       ├─ 그 시점까지 KRX 데이터 수집 (pykrx, requirements-daily.txt)
       ├─ 시총 상위 200 → 정제 유니버스
       ├─ 오늘 진입/청산 판정, 포지션·손익 갱신
       └─ portfolio_state.json / snapshot.json 커밋
  └─ Streamlit Cloud 가 새 커밋 감지 → 사이트 자동 갱신
```

## 자주 막히는 곳

- **첫 실행이 느림**: 정상입니다(200+종목 첫 수집). 이후엔 캐시로 빨라집니다.
- **`IndexError: list index out of range` / `KRX 로그인 실패`**: KRX_ID/KRX_PW 시크릿 미등록입니다(2단계).
- **pykrx 컬럼 오류**: pykrx 버전에 따라 컬럼명이 다를 수 있습니다. `data.py`의 `COLS` 매핑만 맞춰주세요.
- **Actions 커밋 권한 오류(403)**: 저장소 Settings → Actions → General → Workflow permissions → **Read and write** 로 설정.
- **앱이 잠들어 있음**: 무료 플랜은 일정 기간 미접속 시 슬립합니다. 접속하면 "Yes, get this app back up!" 버튼으로 몇 초 만에 깨어납니다.
- **Streamlit 배포 시 pykrx 설치 실패**: 대시보드는 pykrx가 필요 없습니다. `requirements.txt` 에 pykrx가 들어가 있지 않은지 확인하세요(데이터 수집용은 `requirements-daily.txt`).
- **시작을 다시 하고 싶으면**: 저장소의 `portfolio_state.json` 삭제 후 워크플로우 재실행 → 그날부터 새로 누적.

## ⚠️ 중요

이 사이트는 **모의(페이퍼) 기록**입니다. 실제 주문은 하지 않으며, 화면의 진입/청산 신호를 보고 증권사에서 직접 체결해야 합니다. 자동매매로 확장하려면 `engine._buy/_sell`에 증권사 주문 API를 연결해야 합니다.
