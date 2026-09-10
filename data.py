"""
data.py — 거래소(KRX) 데이터 수집 + 로컬 parquet 캐시 (pykrx 기반)

- 시총 상위 유니버스: get_market_cap
- 종목별 수정주가 OHLCV: get_market_ohlcv(adjusted=True) — 분할·합병 자동 반영
- 코스피 지수 / KODEX 인버스

캐시는 종목별 parquet(data/ohlcv/{ticker}.parquet)에 증분 저장.
첫 실행은 느리고(수백 종목), 이후엔 신규 일자만 받아 빠릅니다.
"""
from __future__ import annotations
import os
from datetime import datetime, timedelta
import pandas as pd
import config as C

OHLCV_DIR = os.path.join(C.DATA_DIR, "ohlcv")
# pykrx 버전에 따라 돌려주는 컬럼이 다르다. 1.2.x 의 get_market_ohlcv 는
# 종목 조회 시 거래대금을 주지 않으므로, 있는 것만 취하고 없으면 넘어간다.
COLS = {"시가": "open", "고가": "high", "저가": "low",
        "종가": "close", "거래량": "volume", "거래대금": "value"}
REQUIRED = ["open", "high", "low", "close"]   # 전략·엔진이 실제로 쓰는 컬럼


def _pykrx():
    try:
        from pykrx import stock
        return stock
    except ImportError as e:
        raise ImportError("pykrx 필요: pip install pykrx") from e


def _ymd(d) -> str:
    return pd.Timestamp(d).strftime("%Y%m%d")


def trading_days(start, end) -> list[pd.Timestamp]:
    stock = _pykrx()
    days = stock.get_previous_business_days(fromdate=_ymd(start), todate=_ymd(end))
    return [pd.Timestamp(d) for d in days]


def latest_trading_day(ref=None) -> pd.Timestamp:
    stock = _pykrx()
    ref = pd.Timestamp(ref or datetime.now())
    days = stock.get_previous_business_days(
        fromdate=_ymd(ref - timedelta(days=10)), todate=_ymd(ref))
    return pd.Timestamp(days[-1])


# ─── 유니버스 (전일 종가 기준 시총 상위 N) ────────────────
def fetch_universe(asof: pd.Timestamp, size: int = C.UNIVERSE_SIZE,
                   market: str = C.MARKET) -> pd.DataFrame:
    """asof 일자의 시가총액 상위 종목. 반환: index=ticker, cols=[name, mktcap]."""
    stock = _pykrx()
    cap = stock.get_market_cap(_ymd(asof), market=market)
    cap = cap.sort_values("시가총액", ascending=False).head(size)
    names = {t: stock.get_market_ticker_name(t) for t in cap.index}
    out = pd.DataFrame({"name": pd.Series(names), "mktcap": cap["시가총액"]})
    return out


# ─── 종목별 OHLCV (수정주가, 증분 캐시) ───────────────────
def fetch_ohlcv(ticker: str, start, end, adjusted: bool = True) -> pd.DataFrame:
    os.makedirs(OHLCV_DIR, exist_ok=True)
    path = os.path.join(OHLCV_DIR, f"{ticker}.parquet")
    cached = pd.read_parquet(path) if os.path.exists(path) else None

    need_start = start
    if cached is not None and len(cached):
        last = cached.index.max()
        if last >= pd.Timestamp(end):
            return cached.loc[pd.Timestamp(start):pd.Timestamp(end)]
        need_start = last + timedelta(days=1)

    stock = _pykrx()
    raw = stock.get_market_ohlcv(_ymd(need_start), _ymd(end), ticker, adjusted=adjusted)
    if raw is not None and len(raw):
        raw = raw.rename(columns=COLS)
        missing = [c for c in REQUIRED if c not in raw.columns]
        if missing:
            raise KeyError(
                f"{ticker}: pykrx 응답에 {missing} 컬럼이 없습니다. "
                f"받은 컬럼={list(raw.columns)} — data.py 의 COLS 매핑을 확인하세요.")
        raw = raw[[c for c in COLS.values() if c in raw.columns]]
        raw.index = pd.to_datetime(raw.index)
        cached = raw if cached is None else pd.concat([cached, raw])
        cached = cached[~cached.index.duplicated(keep="last")].sort_index()
        cached.to_parquet(path)
    if cached is None:
        return pd.DataFrame(columns=list(COLS.values()))
    return cached.loc[pd.Timestamp(start):pd.Timestamp(end)]


def fetch_index(code: str, start, end) -> pd.Series:
    stock = _pykrx()
    df = stock.get_index_ohlcv(_ymd(start), _ymd(end), code)
    s = df["종가"]; s.index = pd.to_datetime(s.index)
    return s.rename("close")


def fetch_inverse(start, end) -> pd.DataFrame:
    return fetch_ohlcv(C.HEDGE_TICKER, start, end, adjusted=True)


# ─── 번들 조립 (신호 계산용) ──────────────────────────────
def build_bundle(asof: pd.Timestamp, extra_tickers: list[str] | None = None,
                 warmup: int = C.HISTORY_WARMUP_DAYS) -> dict:
    """
    asof 기준 유니버스 + 보유(extra) 종목의 가격 이력 번들.
    반환 dict: universe(df), close(wide df), ohlcv(dict[ticker]->df),
               kospi(series), inverse(df).
    """
    start = asof - timedelta(days=int(warmup * 1.9) + 40)  # 거래일 warmup 확보용 여유
    uni = fetch_universe(asof)
    tickers = list(dict.fromkeys(list(uni.index) + list(extra_tickers or [])))

    ohlcv = {}
    closes = {}
    for t in tickers:
        df = fetch_ohlcv(t, start, asof)
        if len(df):
            ohlcv[t] = df
            closes[f"{t}|{uni.loc[t, 'name'] if t in uni.index else t}"] = df["close"]
    close_wide = pd.DataFrame(closes).sort_index()

    kospi = fetch_index(C.KOSPI_INDEX, start, asof)
    inverse = fetch_inverse(start, asof)
    return {"universe": uni, "close": close_wide, "ohlcv": ohlcv,
            "kospi": kospi, "inverse": inverse, "asof": asof}
