# 배포 가이드 — 자동 갱신되는 공개 웹사이트 (전부 무료)

목표: **매일 아침 7:30(KST) 전일 종가로 자동 갱신되는 공개 URL 대시보드**

구성:
- **GitHub Actions** — 매일 07:30 KST 자동 실행 → 전일 종가 수집 → 포트폴리오 갱신 → 결과 커밋
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

## 2단계 — 자동 스케줄 켜기 (GitHub Actions)

- 저장소 → **Settings → Actions → General → Workflow permissions** 를 **Read and write** 로 변경. (결과 JSON 커밋에 필요 — 안 하면 push 단계에서 403)
- 저장소 → **Actions** 탭 → 워크플로우 활성화(처음엔 "I understand..." 버튼).
- `daily-update` 워크플로우가 매일 07:30 KST 자동 실행됩니다.
- 지금 바로 한 번 돌리려면: Actions → daily-update → **Run workflow** 클릭.
  (첫 실행은 수백 종목 수집이라 몇 분 걸립니다. 끝나면 `portfolio_state.json` / `snapshot.json`이 커밋됨.)

## 3단계 — 웹사이트 배포 (Streamlit Community Cloud)

1. https://share.streamlit.io 접속 → GitHub 계정으로 로그인.
2. **New app → Deploy a public app from GitHub** → 저장소 `quant-dashboard`, 브랜치 `main`, 파일 `dashboard.py`, Python 3.11 → Deploy.
3. 잠시 후 `https://<앱이름>.streamlit.app` 공개 URL이 생깁니다. 이게 대시보드 사이트입니다.

이후 매일 아침 Actions가 데이터를 갱신·커밋하면, Streamlit 앱이 자동으로 새 데이터로 리로드됩니다. **접속만 하면 항상 최신입니다.**

---

## 동작 요약

```
매일 07:30 KST (GitHub Actions)
  └─ python run_daily.py
       ├─ 전일 종가까지 KRX 데이터 수집 (pykrx, requirements-daily.txt)
       ├─ 시총 상위 200 → 정제 유니버스
       ├─ 오늘 진입/청산 판정, 포지션·손익 갱신
       └─ portfolio_state.json / snapshot.json 커밋
  └─ Streamlit Cloud 가 새 커밋 감지 → 사이트 자동 갱신
```

## 자주 막히는 곳

- **첫 실행이 느림**: 정상입니다(200+종목 첫 수집). 이후엔 캐시로 빨라집니다.
- **pykrx 컬럼 오류**: pykrx 버전에 따라 컬럼명이 다를 수 있습니다. `data.py`의 `COLS` 매핑만 맞춰주세요.
- **Actions 커밋 권한 오류(403)**: 저장소 Settings → Actions → General → Workflow permissions → **Read and write** 로 설정.
- **앱이 잠들어 있음**: 무료 플랜은 일정 기간 미접속 시 슬립합니다. 접속하면 "Yes, get this app back up!" 버튼으로 몇 초 만에 깨어납니다.
- **Streamlit 배포 시 pykrx 설치 실패**: 대시보드는 pykrx가 필요 없습니다. `requirements.txt` 에 pykrx가 들어가 있지 않은지 확인하세요(데이터 수집용은 `requirements-daily.txt`).
- **시작을 다시 하고 싶으면**: 저장소의 `portfolio_state.json` 삭제 후 워크플로우 재실행 → 그날부터 새로 누적.

## ⚠️ 중요

이 사이트는 **모의(페이퍼) 기록**입니다. 실제 주문은 하지 않으며, 화면의 진입/청산 신호를 보고 증권사에서 직접 체결해야 합니다. 자동매매로 확장하려면 `engine._buy/_sell`에 증권사 주문 API를 연결해야 합니다.
