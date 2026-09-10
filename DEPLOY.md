# 배포 가이드 — 자동 갱신되는 공개 웹사이트 (전부 무료)

목표: **매일 아침 7:30(KST) 전일 종가로 자동 갱신되는 공개 URL 대시보드**

구성:
- **GitHub Actions** — 매일 07:30 KST 자동 실행 → 전일 종가 수집 → 포트폴리오 갱신 → 결과 커밋
- **Streamlit Community Cloud** — 그 저장소를 읽어 공개 URL 웹사이트 제공

포트폴리오는 **최초 실행일(오늘)부터 시작**, 자본 **10억**, 매매내역·평가손익·누적 매매손익이 매일 누적됩니다.

---

## 1단계 — GitHub에 올리기

GitHub 계정으로 새 저장소(예: `quant-dashboard`)를 만들고, 이 폴더 전체를 push:

```bash
cd quant_dashboard
git init
git add .
git commit -m "init"
git branch -M main
git remote add origin https://github.com/<본인아이디>/quant-dashboard.git
git push -u origin main
```

> Claude Code에서 "이 폴더를 내 GitHub에 새 저장소로 올려줘"라고 하면 대신 해줍니다.

## 2단계 — 자동 스케줄 켜기 (GitHub Actions)

- 저장소 → **Actions** 탭 → 워크플로우 활성화(처음엔 "I understand..." 버튼).
- `daily-update` 워크플로우가 매일 07:30 KST 자동 실행됩니다.
- 지금 바로 한 번 돌리려면: Actions → daily-update → **Run workflow** 클릭.
  (첫 실행은 수백 종목 수집이라 몇 분 걸립니다. 끝나면 `portfolio_state.json` / `snapshot.json`이 커밋됨.)

## 3단계 — 웹사이트 배포 (Streamlit Community Cloud)

1. https://share.streamlit.io 접속 → GitHub 계정으로 로그인.
2. **New app** → 저장소 `quant-dashboard`, 브랜치 `main`, 파일 `dashboard.py` 지정 → Deploy.
3. 잠시 후 `https://<앱이름>.streamlit.app` 공개 URL이 생깁니다. 이게 대시보드 사이트입니다.

이후 매일 아침 Actions가 데이터를 갱신·커밋하면, Streamlit 앱이 자동으로 새 데이터로 리로드됩니다. **접속만 하면 항상 최신입니다.**

---

## 동작 요약

```
매일 07:30 KST (GitHub Actions)
  └─ python run_daily.py
       ├─ 전일 종가까지 KRX 데이터 수집 (pykrx)
       ├─ 시총 상위 200 → 정제 유니버스
       ├─ 오늘 진입/청산 판정, 포지션·손익 갱신
       └─ portfolio_state.json / snapshot.json 커밋
  └─ Streamlit Cloud 가 새 커밋 감지 → 사이트 자동 갱신
```

## 자주 막히는 곳

- **첫 실행이 느림**: 정상입니다(200+종목 첫 수집). 이후엔 캐시로 빨라집니다.
- **pykrx 컬럼 오류**: pykrx 버전에 따라 컬럼명이 다를 수 있습니다. `data.py`의 `COLS` 매핑만 맞춰주세요.
- **Actions 커밋 권한 오류**: 저장소 Settings → Actions → General → Workflow permissions → **Read and write** 로 설정.
- **시작을 다시 하고 싶으면**: 저장소의 `portfolio_state.json` 삭제 후 워크플로우 재실행 → 그날부터 새로 누적.

## ⚠️ 중요

이 사이트는 **모의(페이퍼) 기록**입니다. 실제 주문은 하지 않으며, 화면의 진입/청산 신호를 보고 증권사에서 직접 체결해야 합니다. 자동매매로 확장하려면 `engine._buy/_sell`에 증권사 주문 API를 연결해야 합니다.
