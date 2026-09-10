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
    }


def main():
    os.makedirs(C.DATA_DIR, exist_ok=True)
    asof = D.latest_trading_day()
    print(f"[run_daily] 최신 거래일: {asof.date()}")

    state = E.load_state()
    held = list(state["positions"].keys())

    print("[run_daily] 데이터 수집 중... (첫 실행은 수백 종목이라 느립니다)")
    bundle = D.build_bundle(asof, extra_tickers=held)
    print(f"[run_daily] 유니버스 {len(bundle['universe'])}종목, 가격패널 {bundle['close'].shape}")

    E.run_to_latest(state, bundle)
    snap = build_snapshot(state, bundle, asof)

    E.save_state(state)
    with open(SNAPSHOT_FILE, "w", encoding="utf-8") as f:
        json.dump(snap, f, ensure_ascii=False, indent=2)
    print(f"[run_daily] 완료. 포지션 {snap['n_positions']}/{snap['max_slots']}  "
          f"자산 {snap['equity']:,.0f}  진입후보 {len(snap['watch_entries'])}")


if __name__ == "__main__":
    main()
