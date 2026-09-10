"""
test_strategy.py — 순수 전략 로직 회귀 테스트
실행:  pytest -q
"""
import numpy as np
import pandas as pd
import config as C
import strategy as S


def test_effective_stop_hard_binds_early():
    # 진입 직후 고점≈진입가 → -7% 하드손절이 더 높은(먼저 닿는) 체결선
    assert S.effective_stop(100, 100) == 100 * (1 - C.HARD_STOP)


def test_effective_stop_trailing_binds_after_run():
    # 큰 수익 후엔 고점 -10% 트레일이 진입가 -7%보다 위 → 트레일이 바인딩
    assert S.effective_stop(100, 150) == 150 * (1 - C.TRAIL_STOP)


def test_check_exit_gap_down_fills_at_open():
    # 종가가 체결선 아래 + 시가가 체결선보다 더 아래로 갭 → 시가 체결
    entry, peak = 100, 100
    stop = S.effective_stop(entry, peak)          # 93
    exit_now, fill, reason = S.check_exit(entry, peak, today_open=90, today_close=91, death_cross=False)
    assert exit_now and fill == 90 and reason == "hard_stop"


def test_check_exit_touch_fills_at_stop():
    # 시가는 체결선 위, 종가만 아래 → 체결선 체결
    entry, peak = 100, 100
    stop = S.effective_stop(entry, peak)
    exit_now, fill, reason = S.check_exit(entry, peak, today_open=95, today_close=92, death_cross=False)
    assert exit_now and fill == stop


def test_check_exit_death_cross_at_close():
    exit_now, fill, reason = S.check_exit(100, 105, today_open=104, today_close=103, death_cross=True)
    assert exit_now and fill == 103 and reason == "death_cross"


def test_check_exit_hold():
    exit_now, fill, reason = S.check_exit(100, 110, today_open=108, today_close=107, death_cross=False)
    assert not exit_now and fill is None


def test_hedge_tiers():
    assert S.update_hedge(False, 92)[1] == 0.30   # 깊은 약세
    assert S.update_hedge(False, 95)[1] == 0.20
    assert S.update_hedge(False, 98)[1] == 0.10
    assert S.update_hedge(False, 100)[1] == 0.0   # 켜졌으나 100 이상 구간 → 0
    # 히스테리시스: 켜짐 <=99, 끔 >=101
    assert S.update_hedge(False, 100)[0] is False  # 아직 안 켜짐(>99)
    assert S.update_hedge(True, 100)[0] is True    # 켜진 상태 유지(<101)
    assert S.update_hedge(True, 101)[0] is False   # 청산


def test_overheat_hysteresis():
    assert S.update_overheat(False, 119) is False
    assert S.update_overheat(False, 120) is True
    assert S.update_overheat(True, 111) is True
    assert S.update_overheat(True, 110) is False


def test_max_slots_for_hedge():
    assert S.max_slots_for_hedge(0.0) == 10
    assert S.max_slots_for_hedge(0.10) == 9
    assert S.max_slots_for_hedge(0.20) == 8
    assert S.max_slots_for_hedge(0.30) == 7


def test_entry_signals_shape():
    # 합성 데이터로 20일 신고가 눌림목이 최소 한 번 잡히는지
    idx = pd.date_range("2021-01-01", periods=200, freq="B")
    up = pd.Series(np.linspace(100, 200, 200), index=idx)      # 상승 추세
    up.iloc[-1] = up.iloc[-2] * 0.97                            # 마지막날 -3% 눌림
    df = pd.DataFrame({"A|테스트": up, "B|기타": up * 0.5})
    sig = S.entry_signals(df)
    assert sig.iloc[-1]["A|테스트"]  # 전일 신고가 + 당일 -3% + 모멘텀 상위 → 진입
