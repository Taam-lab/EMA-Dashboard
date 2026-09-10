"""
engine.py — 페이퍼 포트폴리오 상태 머신

매 거래일 1스텝씩 전략 규칙을 적용해 상태(state)를 갱신한다.
상태는 JSON에 영속화되어 매일 누적된다. (정수 주식수 + 현금 원장)

핵심 규칙(config 확정값):
  진입: 20일 신고가 눌림목 + 모멘텀 상위50%, 과열 시 금지, 모멘텀 우선순위
  청산: 데드크로스 / -7% 하드손절 / 고점 -10% 트레일 (갭하락 시 시가체결)
  사이징: 10슬롯 x 10%, 헷지 비중만큼 슬롯 축소
  헷지: 코스피 이격도(EMA50) 단계별 KODEX 인버스
보유 종목이 시총 200위 밖으로 밀려도 청산은 규칙대로(신규 진입만 유니버스 제한).
"""
from __future__ import annotations
import os
import json
import numpy as np
import pandas as pd
import config as C
import strategy as S


def new_state() -> dict:
    return {
        "inception_date": None,   # 최초 실행일 = 포트폴리오 시작일 (오늘부터 누적)
        "last_date": None,
        "cash": float(C.INITIAL_CAPITAL),
        "positions": {},          # ticker -> {name, entry_date, entry_price, shares, peak}
        "hedge": {"active": False, "weight": 0.0, "shares": 0, "entry_price": 0.0},
        "overheat": False,
        "history": [],            # 체결 로그
        "equity_curve": [],       # {date, equity}
        "today_entries": [],      # 최근 처리일 신규 진입
        "today_exits": [],        # 최근 처리일 청산
    }


def load_state(path: str = None) -> dict:
    path = path or C.STATE_FILE
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return new_state()


def save_state(state: dict, path: str = None):
    path = path or C.STATE_FILE
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


# ─── 헬퍼 ─────────────────────────────────────────────────
def _equity(state: dict, price_of) -> float:
    """현금 + 롱 포지션 평가 + 헷지 평가."""
    eq = state["cash"]
    for t, p in state["positions"].items():
        px = price_of(t)
        eq += p["shares"] * (px if not np.isnan(px) else p["entry_price"])
    if state["hedge"]["shares"] > 0:
        ip = price_of(C.HEDGE_TICKER)
        eq += state["hedge"]["shares"] * (ip if not np.isnan(ip) else state["hedge"]["entry_price"])
    return eq


def _sell(state, ticker, fill, reason, date, name):
    p = state["positions"].pop(ticker)
    proceeds = p["shares"] * fill * (1 - C.COMMISSION - C.SELL_TAX)
    cost_basis = p["shares"] * p["entry_price"] * (1 + C.COMMISSION)
    state["cash"] += proceeds
    pnl = (fill * (1 - C.COMMISSION - C.SELL_TAX)) / (p["entry_price"] * (1 + C.COMMISSION)) - 1
    rec = {"date": date, "ticker": ticker, "name": name, "action": "SELL",
           "price": round(fill, 2), "shares": p["shares"], "reason": reason,
           "pnl_pct": round(pnl * 100, 2),
           "pnl_krw": round(proceeds - cost_basis, 0),   # 실현손익 (원)
           "hold_days": (pd.Timestamp(date) - pd.Timestamp(p["entry_date"])).days}
    state["history"].append(rec)
    return rec


def _buy(state, ticker, name, price, date, equity):
    target = C.POSITION_PCT * equity
    shares = int(target // (price * (1 + C.COMMISSION)))
    if shares <= 0:
        return None
    cost = shares * price * (1 + C.COMMISSION)
    if cost > state["cash"]:
        shares = int(state["cash"] // (price * (1 + C.COMMISSION)))
        cost = shares * price * (1 + C.COMMISSION)
    if shares <= 0:
        return None
    state["cash"] -= cost
    state["positions"][ticker] = {"name": name, "entry_date": date,
                                  "entry_price": float(price), "shares": shares,
                                  "peak": float(price)}
    rec = {"date": date, "ticker": ticker, "name": name, "action": "BUY",
           "price": round(price, 2), "shares": shares, "reason": "entry", "pnl_pct": None}
    state["history"].append(rec)
    return rec


def _rebalance_hedge(state, target_w, inv_price, equity, date):
    """헷지 목표 비중으로 KODEX 인버스 정수 조정 (ETF: 매도세 면제)."""
    h = state["hedge"]
    target_val = target_w * equity
    target_shares = int(target_val // (inv_price * (1 + C.COMMISSION))) if inv_price > 0 else 0
    cur = h["shares"]
    if target_shares == cur:
        return
    if target_shares > cur:            # 매수
        add = target_shares - cur
        cost = add * inv_price * (1 + C.COMMISSION)
        if cost > state["cash"]:
            add = int(state["cash"] // (inv_price * (1 + C.COMMISSION)))
            cost = add * inv_price * (1 + C.COMMISSION)
        state["cash"] -= cost
        h["shares"] = cur + add
        h["entry_price"] = inv_price
    else:                              # 매도 (ETF 세금 없음)
        cut = cur - target_shares
        state["cash"] += cut * inv_price * (1 - C.COMMISSION)
        h["shares"] = target_shares
    h["weight"] = target_w


# ─── 하루 처리 ────────────────────────────────────────────
def step(state: dict, bundle: dict, date: pd.Timestamp) -> dict:
    """bundle(가격 이력)로 date 하루를 처리하고 상태를 갱신."""
    date_str = pd.Timestamp(date).strftime("%Y-%m-%d")
    close_wide = bundle["close"]
    ohlcv = bundle["ohlcv"]
    uni = bundle["universe"]

    # 코드->컬럼명(코드|이름) 매핑
    col_of = {c.split("|")[0]: c for c in close_wide.columns}

    def price_of(ticker):
        if ticker == C.HEDGE_TICKER:
            inv = bundle["inverse"]
            return float(inv.loc[date, "close"]) if date in inv.index else np.nan
        col = col_of.get(ticker)
        if col and date in close_wide.index:
            v = close_wide.at[date, col]
            return float(v) if pd.notna(v) else np.nan
        return np.nan

    state["today_entries"], state["today_exits"] = [], []

    # 데드크로스 판정 (전 종목)
    ema_f = close_wide.ewm(span=C.EMA_FAST, adjust=False).mean()
    ema_s = close_wide.ewm(span=C.EMA_SLOW, adjust=False).mean()
    death = (ema_f < ema_s) & (ema_f.shift(1) >= ema_s.shift(1))

    # 1) 보유 포지션 청산 판정
    for ticker in list(state["positions"].keys()):
        col = col_of.get(ticker)
        if col is None or ticker not in ohlcv:
            continue
        df = ohlcv[ticker]
        if date not in df.index:
            continue
        o, c = float(df.at[date, "open"]), float(df.at[date, "close"])
        p = state["positions"][ticker]
        dc = bool(death.at[date, col]) if (date in death.index and col in death.columns) else False
        exit_now, fill, reason = S.check_exit(p["entry_price"], p["peak"], o, c, dc)
        if exit_now:
            rec = _sell(state, ticker, fill, reason, date_str, p["name"])
            state["today_exits"].append(rec)
        else:
            if c > p["peak"]:
                p["peak"] = c

    # 2) 국면/헷지 상태 갱신
    disp_series = S.disparity(bundle["kospi"])
    disp_today = float(disp_series.loc[date]) if date in disp_series.index else np.nan
    state["overheat"] = S.update_overheat(state["overheat"], disp_today)
    hedge_active, target_w = S.update_hedge(state["hedge"]["active"], disp_today)
    state["hedge"]["active"] = hedge_active

    equity_now = _equity(state, price_of)

    # 3) 헷지 리밸런싱
    inv_price = price_of(C.HEDGE_TICKER)
    if not np.isnan(inv_price) and inv_price > 0:
        _rebalance_hedge(state, target_w, inv_price, equity_now, date_str)

    # 4) 슬롯 캡: 헷지 비중만큼 축소 → 초과 시 약한 종목 정리
    max_slots = S.max_slots_for_hedge(state["hedge"]["weight"])
    if len(state["positions"]) > max_slots:
        rank = sorted(state["positions"].keys(),
                      key=lambda t: (price_of(t) / state["positions"][t]["entry_price"] - 1))
        for t in rank[:len(state["positions"]) - max_slots]:
            fill = price_of(t)
            if np.isnan(fill):
                fill = state["positions"][t]["entry_price"]
            rec = _sell(state, t, fill, "derisk", date_str, state["positions"][t]["name"])
            state["today_exits"].append(rec)

    # 5) 신규 진입 (과열이면 금지)
    if not state["overheat"]:
        sig = S.entry_signals(close_wide)
        mom = S.momentum(close_wide)
        if date in sig.index:
            cand_cols = [c for c in close_wide.columns if sig.at[date, c]]
            cands = []
            for col in cand_cols:
                tk = col.split("|")[0]
                if tk in state["positions"]:
                    continue
                if tk not in uni.index:          # 유니버스(시총 상위 N) 제한 — 신규만
                    continue
                m = mom.at[date, col]
                if pd.notna(m):
                    cands.append((tk, col, float(m)))
            cands.sort(key=lambda x: x[2], reverse=True)
            equity_now = _equity(state, price_of)
            for tk, col, _ in cands:
                if len(state["positions"]) >= max_slots:
                    break
                px = price_of(tk)
                if np.isnan(px):
                    continue
                rec = _buy(state, tk, col.split("|")[1], px, date_str, equity_now)
                if rec:
                    state["today_entries"].append(rec)

    # 6) equity 기록
    eq = _equity(state, price_of)
    state["equity_curve"].append({"date": date_str, "equity": round(eq, 0)})
    state["last_date"] = date_str
    return state


def run_to_latest(state: dict, bundle: dict) -> dict:
    """
    번들에 담긴 거래일 중 미처리분을 순차 처리.
    포트폴리오는 '최초 실행일(inception)'부터 시작 — 과거 데이터는 지표 계산에만 쓰고
    매매/누적은 inception 이후로만. 첫 실행이면 번들의 마지막 거래일을 inception으로 설정.
    """
    dates = bundle["close"].index
    if state.get("inception_date") is None:
        # 오늘(=최신 거래일)부터 시작
        state["inception_date"] = pd.Timestamp(dates[-1]).strftime("%Y-%m-%d")
    inception = pd.Timestamp(state["inception_date"])
    last = pd.Timestamp(state["last_date"]) if state["last_date"] else None

    for d in dates:
        if d < inception:
            continue                      # inception 이전은 매매 안 함(지표용 과거일 뿐)
        if last is not None and d <= last:
            continue                      # 이미 처리한 날
        step(state, bundle, d)
    return state
