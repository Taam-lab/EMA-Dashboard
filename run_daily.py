"""
run_daily.py — 매일 장 마감 후 실행하는 배치

1) 최신 거래일까지 거래소 데이터 캐시 갱신
2) 페이퍼 포트폴리오 엔진을 최신일까지 진행
3) 대시보드용 snapshot.json + portfolio_state.json 저장

크론 예시 (평일 16:00 KST):
  0 16 * * 1-5  cd /path/to/quant_dashboard && python run_daily.py
"""
from __future__ import annotations
import os
import json
import time
from datetime import datetime
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import config as C
import data as D
import engine as E
import strategy as S

SNAPSHOT_FILE = "snapshot.json"


def build_snapshot(state, bundle, asof) -> dict:
    close_wide = bundle["close"]
    col_of = {c.split("|")[0]: c for c in close_wide.columns}
    date = pd.Timestamp(asof)

    def cur_price(tk):
        if tk == C.HEDGE_TICKER:
            inv = bundle["inverse"]
            return float(inv.loc[date, "close"]) if date in inv.index else np.nan
        col = col_of.get(tk)
        if col and date in close_wide.index and pd.notna(close_wide.at[date, col]):
            return float(close_wide.at[date, col])
        return np.nan

    disp = S.disparity(bundle["kospi"])
    disp_today = float(disp.loc[date]) if date in disp.index else float("nan")

    # 현재 포지션 + 수익률 + 위험도
    positions = []
    for tk, p in state["positions"].items():
        px = cur_price(tk)
        pnl = (px / p["entry_price"] - 1) * 100 if not np.isnan(px) else None
        peak_dd = (px / p["peak"] - 1) * 100 if not np.isnan(px) else None
        stop = S.effective_stop(p["entry_price"], p["peak"])
        dist = S.stop_distance_pct(px, p["entry_price"], p["peak"]) if not np.isnan(px) else None
        in_uni = tk in bundle["universe"].index
        positions.append({
            "ticker": tk, "name": p["name"], "entry_date": p["entry_date"],
            "entry_price": p["entry_price"], "shares": p["shares"],
            "current_price": None if np.isnan(px) else round(px, 2),
            "pnl_pct": None if pnl is None else round(pnl, 2),
            "peak_dd_pct": None if peak_dd is None else round(peak_dd, 2),
            "stop_price": round(stop, 2),
            "stop_dist_pct": None if dist is None else round(dist, 2),
            "risk": (dist is not None and dist < 3.0),
            "in_universe": in_uni,
        })
    positions.sort(key=lambda x: (x["pnl_pct"] is None, x["pnl_pct"] or 0))

    # 금일 진입 후보(신호 발생 & 유니버스 & 미보유) — 슬롯 초과분은 관심종목
    watch = []
    sig = S.entry_signals(close_wide)
    mom = S.momentum(close_wide)
    if date in sig.index and not state["overheat"]:
        held = set(state["positions"].keys())
        for col in close_wide.columns:
            if not sig.at[date, col]:
                continue
            tk = col.split("|")[0]
            if tk in held or tk not in bundle["universe"].index:
                continue
            m = mom.at[date, col]
            px = cur_price(tk)
            watch.append({"ticker": tk, "name": col.split("|")[1],
                          "price": None if np.isnan(px) else round(px, 2),
                          "mom_120d_pct": None if pd.isna(m) else round(float(m) * 100, 1)})
        watch.sort(key=lambda x: (x["mom_120d_pct"] is None, -(x["mom_120d_pct"] or -999)))

    eq = state["equity_curve"][-1]["equity"] if state["equity_curve"] else C.INITIAL_CAPITAL
    max_slots = S.max_slots_for_hedge(state["hedge"]["weight"])

    # 평가손익 (미실현) = 보유 포지션 (현재가-진입가)*수량, 수수료 반영
    unrealized = 0.0
    for p in positions:
        if p["current_price"] is not None:
            pos = state["positions"][p["ticker"]]
            unrealized += (p["current_price"] * (1 - C.COMMISSION - C.SELL_TAX)
                           - pos["entry_price"] * (1 + C.COMMISSION)) * pos["shares"]
    # 누적 매매손익 (실현) = 청산된 거래들의 pnl_krw 합
    realized = sum(h.get("pnl_krw", 0) or 0 for h in state["history"] if h["action"] == "SELL")

    return {
        "asof": pd.Timestamp(asof).strftime("%Y-%m-%d"),
        "inception_date": state.get("inception_date"),
        "disparity": round(disp_today, 1),
        "overheat": state["overheat"],
        "hedge_active": state["hedge"]["active"],
        "hedge_weight": state["hedge"]["weight"],
        "equity": eq,
        "cash": round(state["cash"], 0),
        "initial_capital": C.INITIAL_CAPITAL,
        "unrealized_pnl": round(unrealized, 0),   # 평가손익
        "realized_pnl": round(realized, 0),       # 누적 매매손익
        "n_positions": len(state["positions"]),
        "max_slots": max_slots,
        "positions": positions,
        "today_entries": state["today_entries"],
        "today_exits": state["today_exits"],
        "watch_entries": watch,
        "corp_actions": _relevant_corp_actions(state, bundle),
    }


def _relevant_corp_actions(state, bundle) -> list[dict]:
    """권리 변동이 감지된 종목 중 오늘 매매·보유와 관련된 것만.

    엔진은 권리 변동 때 진입가를 조정하지 않는다. 5:1 분할이면 가격만 1/5 이 되어
    가짜 -80% 손절이 나온다. 데이터를 정확히 받아도 생기는 엔진 쪽 한계라, 최소한
    그런 날의 매매 신호에는 경고를 붙인다.
    """
    names = dict(bundle["universe"]["name"])
    names.update({t: p["name"] for t, p in state["positions"].items()})
    for x in state["today_entries"] + state["today_exits"]:
        names.setdefault(x["ticker"], x["name"])
    relevant = (set(state["positions"])
                | {x["ticker"] for x in state["today_entries"] + state["today_exits"]})
    return [{"ticker": t, "name": names.get(t, t),
             "date": pd.Timestamp(d).strftime("%Y-%m-%d"), "held": t in state["positions"]}
            for t, d in bundle.get("corp_actions", []) if t in relevant]


def _require_krx_credentials():
    """KRX 가 로그인 필수로 바뀌어 pykrx 조회에 계정이 필요하다.

    없으면 조회가 빈 응답을 돌려주고 한참 뒤 엉뚱한 IndexError 로 죽으므로
    (실제로 GitHub Actions 첫 실행이 이렇게 실패했다) 여기서 먼저 끊는다.
    """
    missing = [k for k in ("KRX_ID", "KRX_PW") if not os.environ.get(k)]
    if missing:
        raise SystemExit(
            "[run_daily] 환경변수 {} 가 없습니다.\n"
            "  KRX 는 로그인해야 시세를 조회할 수 있습니다.\n"
            "  로컬(PowerShell):  $env:KRX_ID=\"...\"; $env:KRX_PW=\"...\"\n"
            "  GitHub Actions:    Settings -> Secrets and variables -> Actions 에 등록"
            .format(", ".join(missing)))


REASON_KR = {"entry": "진입", "hard_stop": "-7% 손절", "trailing": "트레일 -10%",
             "death_cross": "데드크로스", "derisk": "헷지 슬롯축소"}


def _print_orders(snap: dict) -> None:
    """오늘 마감 동시호가에 낼 주문을 사람이 읽는 형태로 출력."""
    buys, sells = snap["today_entries"], snap["today_exits"]
    print(f"\n{'─' * 56}\n오늘 주문 ({snap['asof']} 마감 동시호가)\n{'─' * 56}")
    if not buys and not sells:
        print("  주문 없음")
    for x in sells:
        print(f"  매도  {x['name']:<16} {x['shares']:>7,}주  @{x['price']:>10,.0f}  "
              f"{REASON_KR.get(x['reason'], x['reason'])}  ({x['pnl_pct']:+.2f}%)")
    for x in buys:
        print(f"  매수  {x['name']:<16} {x['shares']:>7,}주  @{x['price']:>10,.0f}")
    risky = [p for p in snap["positions"] if p["risk"]]
    if risky:
        print("\n  손절선 3% 이내 — 내일 주의:")
        for p in risky:
            print(f"    {p['name']:<16} 손절선 {p['stop_price']:>10,.0f}  "
                  f"({p['stop_dist_pct']:.2f}% 남음)")
    for c in snap.get("corp_actions", []):
        print(f"\n  ⚠ {c['name']}: {c['date']} 권리 변동(분할·증자 등) 감지 — 엔진은 진입가를 조정하지"
              f"\n    않으므로 이 종목의 손익·손절·진입 신호는 틀렸을 수 있습니다. 주문 전 직접 확인하세요.")
    print(f"{'─' * 56}\n")


MARKET_OPEN = 9 * 60 + 0             # 09:00 KST — 정규장 개장
ORDER_WINDOW_OPEN = 15 * 60 + 0      # 15:00 KST — 이 시각부터 당일 판정을 허용
MARKET_CLOSE = 15 * 60 + 30          # 15:30 KST — 정규장 마감


def _price_basis(asof) -> str:
    """당일 가격을 무엇으로 보고 있는지 판정하고, 애매한 시각이면 실행을 막는다.

    이 전략은 '당일 등락률 -5~0%' 를 보므로 신호가 종가에 의존한다. 그런데 종가를
    안 뒤에는 그 가격에 살 수 없다. 그래서 마감 직전(15:05) 스냅샷을 종가 대용으로
    쓰고 그 가격에 체결한 것으로 기록한다 — 판정 시점과 주문 시점이 같아 룩어헤드가 없다.

    하루 두 번 도는 것을 전제로 한다:
      08:00  개장 전. 오늘은 아직 데이터가 없으므로 전 거래일 확정 종가로 평가만 갱신.
             (전 거래일은 이미 처리됐으니 매매는 일어나지 않는다 — run_to_latest 가 건너뜀)
      15:05  주문 구간. 오늘을 스냅샷 가격으로 판정하고 체결까지 기록.

    반환: 'final'(확정 종가) | 'snapshot'(마감 직전 스냅샷)
    """
    if os.environ.get("ALLOW_INTRADAY"):          # 테스트용 우회
        return "snapshot"
    now = datetime.now(ZoneInfo("Asia/Seoul"))
    if pd.Timestamp(asof).date() != now.date():
        return "final"                            # 과거 거래일 = 이미 확정된 데이터
    mins = now.hour * 60 + now.minute
    if mins < MARKET_OPEN:
        # 개장 전이라 오늘 값이 아직 없다. 가격 패널에도 없을 테니 main() 이
        # 전 거래일로 되돌린다. 평가 갱신용 실행이므로 통과시킨다.
        return "final"
    if mins < ORDER_WINDOW_OPEN:
        raise SystemExit(
            "[run_daily] {} 는 아직 장중이고 주문 구간(15:00~15:30)도 아닙니다 (현재 {} KST).\n"
            "  지금 가격은 종가 대용으로 쓰기엔 이릅니다 — 마감까지 크게 움직일 수 있습니다.\n"
            "  개장 전(~09:00) 평가 갱신이나 15:00 이후 주문 구간에 실행하세요.\n"
            "  (테스트 목적이면 ALLOW_INTRADAY=1)"
            .format(pd.Timestamp(asof).date(), now.strftime("%H:%M")))
    return "final" if mins >= MARKET_CLOSE else "snapshot"


def main(preview: bool = False):
    t0 = time.time()
    _require_krx_credentials()
    os.makedirs(C.DATA_DIR, exist_ok=True)
    asof = D.latest_trading_day()
    basis = _price_basis(asof)
    label = {"final": "확정 종가", "snapshot": "마감 직전 스냅샷(종가 대용)"}[basis]
    print(f"[run_daily] 최신 거래일: {asof.date()}  ·  가격 기준: {label}"
          f"{'  ·  PREVIEW (기록하지 않음)' if preview else ''}")

    state = E.load_state()
    held = list(state["positions"].keys())

    print("[run_daily] 데이터 수집 중...")
    bundle = D.build_bundle(asof, extra_tickers=held)
    print(f"[run_daily] 유니버스 {len(bundle['universe'])}종목, 가격패널 {bundle['close'].shape}")

    # 개장 전 실행이면 오늘 행이 아직 없다. 그대로 두면 모든 가격이 NaN 인
    # 스냅샷이 나오므로(이격도까지 NaN) 실제 데이터가 있는 마지막 날로 되돌린다.
    idx = bundle["close"].index
    if len(idx) and pd.Timestamp(asof) not in idx:
        asof = pd.Timestamp(idx[-1])
        # 과거 거래일로 되돌렸으면 그건 확정 종가다. 'snapshot' 을 그대로 두면 대시보드가
        # 어제 데이터를 두고 '주문 가능 구간'이라고 말하게 된다.
        basis = _price_basis(asof)
        print(f"[run_daily] 해당 일자 데이터 없음 → 마지막 거래일 {asof.date()} 기준으로 전환")

    E.run_to_latest(state, bundle)
    snap = build_snapshot(state, bundle, asof)
    snap["price_basis"] = basis
    snap["generated_at"] = datetime.now(ZoneInfo("Asia/Seoul")).strftime("%Y-%m-%d %H:%M KST")

    if preview:
        # 원장을 건드리지 않고 '오늘 마감에 낼 주문'만 보여준다.
        _print_orders(snap)
        print(f"[run_daily] PREVIEW 모드 ({time.time() - t0:.0f}초) — portfolio_state.json / snapshot.json 을 쓰지 않았습니다.")
        return

    E.save_state(state)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    _print_orders(snap)
    print(f"[run_daily] 완료 {time.time() - t0:.0f}초. 포지션 {snap['n_positions']}/{snap['max_slots']}  "
          f"자산 {snap['equity']:,.0f}  진입후보 {len(snap['watch_entries'])}")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="데일리 배치 — 데이터 갱신 후 포트폴리오 진행")
    ap.add_argument("--preview", action="store_true",
                    help="원장에 기록하지 않고 오늘 낼 주문만 출력")
    main(preview=ap.parse_args().preview)
