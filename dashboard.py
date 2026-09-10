"""
dashboard.py — Streamlit 2탭 대시보드

실행:  streamlit run dashboard.py
데이터: run_daily.py 가 만든 snapshot.json + portfolio_state.json 을 읽음.

탭1(메인): 포트폴리오 수익률 / 현재 포지션 / 금일 진입 예정 / 금일 청산(위험)
탭2(매매이력): 전체 체결 로그 + 요약 통계

주의: 이 대시보드는 신호/현황을 '보여줄' 뿐 실제 주문은 하지 않습니다.
      snapshot 의 진입/청산 예정을 보고 증권사에서 직접 체결하세요.
"""
from __future__ import annotations
import json
import os
import pandas as pd
import streamlit as st

st.set_page_config(page_title="20일 신고가 눌림목 대시보드", layout="wide")

# 표 강조색 — 밝은 배경(.streamlit/config.toml) 기준으로 대비를 맞춘 값.
# 어두운 테마로 되돌린다면 UP="#3ddc84", DOWN="#ff6b6b", RISK_ROW 배경="#4a1113" 로.
UP = "#14804a"                              # 수익 (흰 배경 대비 4.9:1)
DOWN = "#c5221f"                            # 손실 (흰 배경 대비 5.9:1)
RISK_ROW = "background-color:#fdecea"       # 손절 임박 행


def load(path, default):
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    return default


snap = load("snapshot.json", None)
state = load("portfolio_state.json", None)

if snap is None or state is None:
    st.warning("데이터가 없습니다. 먼저 `python run_daily.py` 를 실행하세요.")
    st.stop()

st.title("📈 20일 신고가 눌림목 전략")
st.caption(
    f"기준일 {snap['asof']}  ·  시작일 {snap.get('inception_date','-')}  ·  "
    f"운용자본 {snap['initial_capital']/1e8:.0f}억  ·  코스피 이격도 {snap['disparity']}  ·  "
    f"{'🔴 과열(신규매수 제한)' if snap['overheat'] else '🟢 정상'}  ·  "
    f"헷지 {int(snap['hedge_weight']*100)}%")

# 가격 기준을 숨기지 않는다 — 스냅샷이면 아직 주문할 수 있고, 확정 종가면 이미 늦었다.
_basis = snap.get("price_basis")
_gen = snap.get("generated_at", "")
if _basis == "snapshot":
    st.success(f"🟢 **주문 가능 구간** — 아래 가격은 마감 직전 스냅샷(종가 대용)입니다. "
               f"마감 동시호가에 주문하세요.  ·  갱신 {_gen}")
elif _basis == "final":
    st.info(f"기준가는 **확정 종가**입니다. 이미 마감된 거래일이라 이 가격에는 주문할 수 없습니다.  "
            f"·  갱신 {_gen}")

tab1, tab2 = st.tabs(["메인", "매매이력"])

# ══════════════════════════════ 탭 1 ══════════════════════════════
with tab1:
    eq = snap["equity"]; init = snap["initial_capital"]
    total_ret = (eq / init - 1) * 100
    ec = pd.DataFrame(state["equity_curve"])
    mdd = 0.0
    if len(ec):
        ec["date"] = pd.to_datetime(ec["date"])
        eq_ser = ec.set_index("date")["equity"]
        mdd = (eq_ser / eq_ser.cummax() - 1).min() * 100

    ur = snap.get("unrealized_pnl", 0)
    rz = snap.get("realized_pnl", 0)
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("평가자산", f"{eq/1e8:,.2f}억", f"{total_ret:+.1f}%")
    c2.metric("평가손익(미실현)", f"{ur/1e8:+,.2f}억")
    c3.metric("누적 매매손익(실현)", f"{rz/1e8:+,.2f}억")
    c4.metric("현금", f"{snap['cash']/1e8:,.2f}억", f"{snap['cash']/eq*100:.0f}%")
    c5.metric("포지션", f"{snap['n_positions']} / {snap['max_slots']}")
    c6.metric("MDD", f"{mdd:.1f}%")

    st.subheader("포트폴리오 수익률")
    if len(ec):
        st.line_chart(ec.set_index("date")["equity"], height=260)

    # ── 현재 포지션 ──
    st.subheader("현재 포지션")
    pos = snap["positions"]
    if pos:
        dfp = pd.DataFrame(pos)
        dfp = dfp.rename(columns={
            "name": "종목", "entry_date": "진입일", "entry_price": "진입가",
            "current_price": "현재가", "pnl_pct": "수익률%", "peak_dd_pct": "고점대비%",
            "stop_price": "손절선", "stop_dist_pct": "손절까지%", "shares": "수량"})
        show = dfp[["종목", "진입일", "진입가", "현재가", "수익률%",
                    "고점대비%", "손절선", "손절까지%", "수량", "risk", "in_universe"]]

        def hl(row):
            styles = [""] * len(row)
            if row["risk"]:
                styles = [RISK_ROW] * len(row)   # 위험(손절 임박)
            return styles

        sty = show.style.apply(hl, axis=1).map(
            lambda v: f"color:{UP}" if isinstance(v, (int, float)) and v > 0
            else (f"color:{DOWN}" if isinstance(v, (int, float)) and v < 0 else ""),
            subset=["수익률%"]).format(
            # Styler 기본값은 소수점 6자리라 원화가 50100.000000 으로 나온다.
            {"진입가": "{:,.0f}", "현재가": "{:,.0f}", "손절선": "{:,.0f}",
             "수량": "{:,.0f}", "수익률%": "{:+.2f}", "고점대비%": "{:.2f}",
             "손절까지%": "{:.2f}"}, na_rep="-")
        st.dataframe(sty, width="stretch", hide_index=True)
        st.caption("🟥 행 = 손절선 3% 이내(위험)  ·  in_universe=False = 시총 200위 밖(보유는 유지, 신규진입만 제한)")
    else:
        st.info("보유 포지션 없음 (전량 현금)")

    col_a, col_b = st.columns(2)

    # ── 금일 진입 예정 ──
    with col_a:
        st.subheader("🟩 금일 진입 예정 종목")
        entered = {e["ticker"] for e in snap["today_entries"]}
        rows = []
        for e in snap["today_entries"]:
            rows.append({"종목": e["name"], "체결가": e["price"], "상태": "✅ 진입"})
        for w in snap["watch_entries"]:
            if w["ticker"] not in entered:
                rows.append({"종목": w["name"], "체결가": w["price"],
                             "상태": f"관심(모멘텀 {w['mom_120d_pct']}%)"})
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("진입 신호 없음" if not snap["overheat"] else "과열 구간 — 신규매수 제한")

    # ── 금일 청산(위험) ──
    with col_b:
        st.subheader("🟥 금일 청산 예정 / 위험 종목")
        rows = []
        reason_kr = {"hard_stop": "-7% 손절", "trailing": "트레일 -10%",
                     "death_cross": "데드크로스", "derisk": "헷지 슬롯축소"}
        for x in snap["today_exits"]:
            rows.append({"종목": x["name"], "사유": reason_kr.get(x["reason"], x["reason"]),
                         "수익률%": x["pnl_pct"], "상태": "🔴 청산"})
        for p in snap["positions"]:
            if p["risk"]:
                rows.append({"종목": p["name"], "사유": f"손절선 {p['stop_dist_pct']}% 이내",
                             "수익률%": p["pnl_pct"], "상태": "⚠️ 위험"})
        if rows:
            st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
        else:
            st.info("청산/위험 종목 없음")

# ══════════════════════════════ 탭 2 ══════════════════════════════
with tab2:
    st.subheader("매매 이력")
    hist = state["history"]
    if not hist:
        st.info("체결 이력 없음")
    else:
        dfh = pd.DataFrame(hist)
        sells = dfh[dfh["action"] == "SELL"]
        if len(sells):
            wins = sells["pnl_pct"] > 0
            wr = wins.mean() * 100
            avg = sells["pnl_pct"].mean()
            aw = sells.loc[wins, "pnl_pct"].mean() if wins.any() else 0
            al = sells.loc[~wins, "pnl_pct"].mean() if (~wins).any() else 0
            payoff = (aw / abs(al)) if al else float("nan")
            m1, m2, m3, m4 = st.columns(4)
            m1.metric("청산 거래", f"{len(sells)}")
            m2.metric("승률", f"{wr:.1f}%")
            m3.metric("손익비", f"{payoff:.2f}")
            m4.metric("거래당 평균", f"{avg:+.2f}%")

        dfh = dfh.rename(columns={
            "date": "날짜", "name": "종목", "action": "구분", "price": "가격",
            "shares": "수량", "reason": "사유", "pnl_pct": "수익률%", "hold_days": "보유일"})
        reason_kr = {"entry": "진입", "hard_stop": "-7% 손절", "trailing": "트레일 -10%",
                     "death_cross": "데드크로스", "derisk": "헷지 슬롯축소"}
        dfh["사유"] = dfh["사유"].map(lambda r: reason_kr.get(r, r))
        dfh["구분"] = dfh["구분"].map({"BUY": "🟢 매수", "SELL": "🔴 매도"})
        cols = [c for c in ["날짜", "종목", "구분", "가격", "수량", "사유", "수익률%", "보유일"]
                if c in dfh.columns]
        st.dataframe(dfh[cols].iloc[::-1], width="stretch", hide_index=True)
