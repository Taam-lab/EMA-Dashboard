"""
strategy.py — 순수 전략 로직 (데이터 소스 비의존, 테스트 가능)

모든 함수는 pandas 자료구조를 받아 신호/판정을 반환합니다.
백테스트와 동일한 규칙을 구현하므로, 이 모듈만 pytest로 회귀 검증하면 됩니다.
"""
from __future__ import annotations
import numpy as np
import pandas as pd
import config as C


# ─── 지표 ─────────────────────────────────────────────────
def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False).mean()


def disparity(kospi_close: pd.Series, span: int = C.DISPARITY_EMA) -> pd.Series:
    """코스피 이격도 = 종가 / EMA(span) * 100"""
    return kospi_close / ema(kospi_close, span) * 100.0


# ─── 유니버스 정제 ────────────────────────────────────────
def clean_universe(close_wide: pd.DataFrame,
                   move_thresh: float = C.ARTIFACT_MOVE) -> list[str]:
    """단일일 |수익률| 이 임계 초과인 종목 제외 (분할·합병 세이프넷)."""
    max_move = close_wide.pct_change().abs().max()
    return max_move[max_move <= move_thresh].index.tolist()


# ─── 진입 신호 ────────────────────────────────────────────
def entry_signals(close_wide: pd.DataFrame) -> pd.DataFrame:
    """
    각 (날짜, 종목)에 대해 진입 신호(과열/슬롯/유니버스 제외) 불리언 반환.
    조건: 전일 20일 신고가 & 당일 -5%~0% & 120일 모멘텀 상위 50%.
    """
    roll_high = close_wide.rolling(C.HIGH_LOOKBACK).max()
    high_prev = close_wide.shift(1) >= roll_high.shift(1)
    ret = close_wide.pct_change()
    pullback = (ret < 0) & (ret >= -C.PULLBACK_MAX)
    mom = close_wide / close_wide.shift(C.MOM_LOOKBACK) - 1
    mom_top = mom.rank(axis=1, pct=True) >= C.MOM_TOP_PCT
    return high_prev & pullback & mom_top


def momentum(close_wide: pd.DataFrame) -> pd.DataFrame:
    """120일 상대수익률 (진입 우선순위 정렬용)."""
    return close_wide / close_wide.shift(C.MOM_LOOKBACK) - 1


# ─── 청산 판정 (보유 포지션 1건) ──────────────────────────
def effective_stop(entry_price: float, peak_close: float) -> float:
    """손절/트레일 체결선 = max(진입가*(1-HARD), 고점*(1-TRAIL))."""
    return max(entry_price * (1 - C.HARD_STOP),
               peak_close * (1 - C.TRAIL_STOP))


def check_exit(entry_price: float, peak_close: float,
               today_open: float, today_close: float,
               death_cross: bool) -> tuple[bool, float | None, str | None]:
    """
    보유 포지션의 당일 청산 여부 판정.
    반환: (청산?, 체결가, 사유)
    - 손절/트레일: 종가가 체결선 이하 → 체결가 = min(체결선, 시가)  [갭하락 시 시가]
    - 데드크로스: 종가 청산
    - 우선순위: 스탑(장중) 먼저, 그다음 데드크로스(종가)
    """
    stop = effective_stop(entry_price, peak_close)
    if today_close <= stop:
        fill = min(stop, today_open) if not np.isnan(today_open) else stop
        reason = "hard_stop" if stop == entry_price * (1 - C.HARD_STOP) else "trailing"
        return True, float(fill), reason
    if death_cross:
        return True, float(today_close), "death_cross"
    return False, None, None


def stop_distance_pct(current_price: float, entry_price: float,
                      peak_close: float) -> float:
    """현재가가 체결선 위로 얼마나 남았는지(%). 위험도 표시용. 음수면 이미 이탈."""
    stop = effective_stop(entry_price, peak_close)
    return (current_price - stop) / current_price * 100.0


# ─── 국면/헷지 (히스테리시스 상태 갱신) ───────────────────
def update_overheat(prev_state: bool, disp_today: float) -> bool:
    """과열(신규매수 제한) 상태 갱신. ON>=120, OFF<=110."""
    if np.isnan(disp_today):
        return prev_state
    if prev_state and disp_today <= C.OVERHEAT_OFF:
        return False
    if (not prev_state) and disp_today >= C.OVERHEAT_ON:
        return True
    return prev_state


def update_hedge(prev_active: bool, disp_today: float) -> tuple[bool, float]:
    """
    헷지 활성 상태 + 목표 비중 갱신.
    켜짐 <=99, 전량청산 >=101. 켜진 동안 이격도 깊이별 10/20/30%.
    반환: (active, target_weight)
    """
    if np.isnan(disp_today):
        return prev_active, 0.0
    active = prev_active
    if active and disp_today >= C.HEDGE_DEACTIVATE:
        active = False
    elif (not active) and disp_today <= C.HEDGE_ACTIVATE:
        active = True
    if not active:
        return False, 0.0
    for upper, w in C.HEDGE_TIERS:
        if disp_today < upper:
            return True, w
    return True, 0.0


def max_slots_for_hedge(hedge_weight: float) -> int:
    """헷지 비중만큼 롱 슬롯 축소: 0%→10, 10%→9, 20%→8, 30%→7."""
    return int(round((1 - hedge_weight) / C.POSITION_PCT - 1e-9))
