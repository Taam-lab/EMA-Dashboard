"""
data.py — 거래소(KRX) 데이터 수집 + 로컬 parquet 캐시 (pykrx 기반)

주 경로는 KRX 일괄 시세다. get_etf_ohlcv_by_ticker(ETF 유니버스) 또는 get_market_ohlcv_by_ticker
(주식 유니버스)가 '하루치 전종목'을 한 번에 주므로,
캐시에 쌓아 둔 수정주가 이력에 최근 5거래일만 덮어쓰면 된다. 수집 전체가 수 초다.

네이버 수정주가(get_market_ohlcv, adjusted=True)는 필요한 종목에만 부른다.
  - 캐시가 없는 종목(신규 편입·첫 실행)
  - 장중인데 KRX 일괄에 당일 시세가 아직 없을 때

네이버를 주 경로로 쓰지 않는 이유(9/14 실측):
  - 200종목 순차 조회 136초, 연달아 부르면 레이트 리밋에 걸린다
  - 당일 봉을 늦게 확정한다. 마감 1시간 뒤에도 157종목의 당일 종가가 KRX 공식 종가와
    달랐다. 지난 거래일 값은 KRX 와 1005건 전부 일치.

권리 변동 감지:
  KRX 등락률은 권리락이 반영된 '기준가' 대비 값이다. 종가 ÷ (1+등락률) 로 역산한 기준가가
  실제 전일 종가와 어긋나면 그날 권리 변동이 있었다는 뜻이다. 두 달치 57,491건에서 정상 종목의
  역산 오차는 99.9퍼센타일 0.006% 였고, 실제 권리 변동 42건은 전부 감지됐다.
  감지하면 그 기준가로 과거 가격을 직접 조정한다(_from_bulk 참고).
  (가격제한폭 ±30% 를 넘는 변동만 보는 방식은 5%·20% 짜리 조정을 놓친다.)

캐시: data/ohlcv/{ticker}.parquet, data/index/{code}.parquet
"""
from __future__ import annotations
import os
import socket
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import numpy as np
import pandas as pd
import config as C

# pykrx 는 요청 타임아웃을 걸지 않아, 응답이 없으면 무한정 매달린다.
# 9/14 15:05 실행이 이 상태로 멈춰 주문 구간을 통째로 놓쳤다.
#
# socket.setdefaulttimeout 으로는 못 막는다 — requests 가 timeout=None 을 명시적으로
# 넘겨서 전역 소켓 기본값을 덮어쓰기 때문이다. 실제로 걸어보고 확인했다.
# 그래서 requests 레벨에서 기본 타임아웃을 주입한다.
#
# '멈춤'을 '실패'로 바꾸는 것이 목적이다. 실패는 재시도·대체 경로로 복구되고 로그에
# 남지만, 멈춤은 아무 일도 일어나지 않은 채 주문 시각만 지나간다.
REQUEST_TIMEOUT = (10, 30)          # (연결, 읽기) 초


def _install_request_timeout():
    try:
        import requests
    except ImportError:
        return
    if getattr(requests.Session.request, "_timeout_patched", False):
        return
    _orig = requests.Session.request

    def _request(self, *args, **kwargs):
        if kwargs.get("timeout") is None:
            kwargs["timeout"] = REQUEST_TIMEOUT
        return _orig(self, *args, **kwargs)

    _request._timeout_patched = True
    requests.Session.request = _request


_install_request_timeout()
socket.setdefaulttimeout(60)        # 보조 안전망 (requests 이외 경로)

OHLCV_DIR = os.path.join(C.DATA_DIR, "ohlcv")
INDEX_DIR = os.path.join(C.DATA_DIR, "index")
# pykrx 버전에 따라 돌려주는 컬럼이 다르다. 1.2.x 의 get_market_ohlcv 는
# 종목 조회 시 거래대금을 주지 않으므로, 있는 것만 취하고 없으면 넘어간다.
COLS = {"시가": "open", "고가": "high", "저가": "low",
        "종가": "close", "거래량": "volume", "거래대금": "value"}
REQUIRED = ["open", "high", "low", "close"]   # 전략·엔진이 실제로 쓰는 컬럼
REFRESH_OVERLAP_DAYS = 7    # 네이버 조회: 캐시가 있어도 최근 N일은 다시 받아 덮어씀
BULK_WINDOW = 5             # KRX 일괄: 최근 N거래일을 공식 시세로 덮어씀
CORP_ACTION_TOL = 0.002     # 등락률 역산 기준가와 전일 종가가 이만큼 어긋나면 권리 변동
HEAL_TOLERANCE = 0.005      # 네이버 재조회 시 캐시와 이만큼 어긋나면 이력 전체 재조회
NAVER_BREAKER = 3           # 네이버 조회가 연속 N회 실패하면 이번 실행에서는 더 부르지 않음
KST = ZoneInfo("Asia/Seoul")

_STOCK = None


LOGIN_RETRIES = 4           # KRX 로그인 실패 시 재시도 (15·30·45초 간격 → 최대 약 1분 30초)


def _pykrx():
    """pykrx 모듈. 매 호출마다 import 를 확인하면 한 실행에 1000번 넘게 불려 2.5초가 새서 한 번만 연다.

    pykrx 는 import 하는 순간 KRX 에 로그인한다. 로그인 응답이 JSON 이 아니면(일시 차단·점검)
    import 자체가 예외로 끝나 이 파일의 재시도 로직이 손도 못 쓰고 배치가 죽는다
    (10/23 이력 생성 직후 실제로 발생). 여기서 잡아 재시도한다. 실패한 import 는 반쯤 초기화된
    pykrx 모듈을 sys.modules 에 남겨 다음 import 를 망가뜨리므로 지우고 다시 연다.
    """
    global _STOCK
    if _STOCK is not None:
        return _STOCK
    import sys
    last = None
    for attempt in range(LOGIN_RETRIES):
        try:
            from pykrx import stock
            _STOCK = stock
            return _STOCK
        except ImportError as e:
            raise ImportError("pykrx 필요: pip install pykrx") from e
        except Exception as e:
            last = e
            for m in [m for m in sys.modules if m == "pykrx" or m.startswith("pykrx.")]:
                del sys.modules[m]
            if attempt == LOGIN_RETRIES - 1:
                break
            wait = 15 * (attempt + 1)
            print(f"[data] KRX 로그인 실패({type(e).__name__}) — {wait}초 후 재시도 "
                  f"({attempt + 1}/{LOGIN_RETRIES - 1})")
            time.sleep(wait)
    raise RuntimeError(f"KRX 로그인에 {LOGIN_RETRIES}회 연속 실패했습니다. KRX 일시 차단·점검일 수 "
                       "있습니다. 작업 스케줄러가 5분 뒤 다시 실행합니다.") from last


def _ymd(d) -> str:
    return pd.Timestamp(d).strftime("%Y%m%d")


def _retry(fn, label: str, tries: int = 3):
    """짧은 백오프 재시도. 마지막 시도의 예외는 그대로 올린다."""
    for attempt in range(tries):
        try:
            return fn()
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 2 * (attempt + 1)
            print(f"[data] {label} 실패({type(e).__name__}) — {wait}초 후 재시도")
            time.sleep(wait)


# ─── 거래일 달력 ───────────────────────────────────────────
def trading_days(start, end) -> list[pd.Timestamp]:
    """거래일 달력. 코스피 지수 일봉의 날짜를 그대로 쓴다.

    get_previous_business_days 는 한 번에 6.6초인데, 같은 기간 지수 일봉(name_display=False)은
    0.08초이고 날짜 목록이 정확히 같다(9/14 검증). 지수 조회가 실패할 때만 원래 함수로 대체한다.
    개장 전에는 당일 지수가 없으므로 자연히 전 거래일까지만 나온다.
    """
    stock = _pykrx()
    try:
        df = _retry(lambda: stock.get_index_ohlcv_by_date(
            _ymd(start), _ymd(end), C.KOSPI_INDEX, name_display=False), "거래일(지수) 조회")
        if len(df):
            return [pd.Timestamp(d) for d in df.index]
    except Exception as e:
        print(f"[data] 지수로 거래일 조회 실패({type(e).__name__}) — 영업일 조회로 대체")
    days = _retry(lambda: stock.get_previous_business_days(
        fromdate=_ymd(start), todate=_ymd(end)), "거래일 조회")
    return [pd.Timestamp(d) for d in days]


def latest_trading_day(ref=None) -> pd.Timestamp:
    ref = pd.Timestamp(ref or datetime.now())
    return trading_days(ref - timedelta(days=10), ref)[-1]


def recent_trading_days(asof, n: int) -> list[pd.Timestamp]:
    asof = pd.Timestamp(asof)
    days = trading_days(asof - timedelta(days=n * 3 + 10), asof)
    return [d for d in days if d <= asof][-n:]


def _session_started(day) -> bool:
    """그 거래일의 정규장이 이미 열렸는가 — 즉 시세가 있어야 정상인가."""
    now = datetime.now(KST)
    d = pd.Timestamp(day).date()
    if d != now.date():
        return d < now.date()
    return now.hour * 60 + now.minute >= 9 * 60


# ─── KRX 일괄 시세 (하루치 전종목) ─────────────────────────
class KrxBulk:
    """날짜별 전종목 시세를 실행 중 한 번씩만 받아 두는 메모리 캐시."""

    def __init__(self, market: str = C.MARKET):
        self.market = market
        self._stock: dict = {}
        self._etf: dict = {}

    def _get(self, store, d, fetch, label):
        d = pd.Timestamp(d)
        if d not in store:
            try:
                store[d] = _retry(fetch, f"{label} {d:%m-%d}")
            except Exception as e:
                print(f"[data] {label} {d:%m-%d} 조회 실패({type(e).__name__})")
                store[d] = pd.DataFrame()
        return store[d]

    def stock_day(self, d) -> pd.DataFrame:
        s = _pykrx()
        return self._get(self._stock, d, lambda: s.get_market_ohlcv_by_ticker(
            _ymd(d), market=self.market), "KRX 일괄")

    def etf_day(self, d) -> pd.DataFrame:
        s = _pykrx()
        return self._get(self._etf, d, lambda: s.get_etf_ohlcv_by_ticker(_ymd(d)),
                         "ETF 일괄")

    def primary_day(self, d) -> pd.DataFrame:
        """유니버스가 속한 시장의 하루치 시세. ETF 유니버스면 ETF 일괄이 주 시세다."""
        return self.etf_day(d) if self.market == "ETF" else self.stock_day(d)

    def usable_days(self, days: list) -> list:
        """시세가 실제로 들어 있는 날만 고른다.

        당일 값이 전일과 거의 전부 같으면 아직 갱신되지 않은 것으로 보고 뺀다 —
        그대로 쓰면 어제 가격으로 오늘 신호를 판정하게 된다.
        """
        out, prev = [], None
        for d in days:
            f = self.primary_day(d)
            if not len(f) or "종가" not in f.columns or not (f["종가"] > 0).any():
                continue
            if prev is not None:
                common = f.index.intersection(prev.index)
                same = (f.loc[common, "종가"] == prev.loc[common, "종가"]).mean() if len(common) else 0.0
                if same > 0.9:
                    print(f"[data] KRX 일괄 {pd.Timestamp(d):%m-%d} 가 전일과 {same:.0%} 동일 — 갱신 전으로 보고 제외")
                    continue
            out.append(pd.Timestamp(d))
            prev = f
        return out

    def rows(self, ticker: str, days: list) -> pd.DataFrame | None:
        """ticker 의 days 구간 OHLCV + 등락률(rate). 하루라도 없으면 None."""
        recs = {}
        for d in days:
            frame = self.primary_day(d)
            if ticker not in frame.index:
                frame = self.stock_day(d) if self.market == "ETF" else self.etf_day(d)
                if ticker not in frame.index:
                    return None
            r = frame.loc[ticker]
            close = float(r["종가"])
            if close <= 0:
                return None
            # 거래정지일은 시가·고가·저가가 0 으로 온다. 그대로 두면 갭하락 체결가가
            # min(체결선, 시가=0) 이 되어 0원 매도로 기록되므로 종가로 채운다.
            o, h, lo = (float(r[k]) or close for k in ("시가", "고가", "저가"))
            recs[pd.Timestamp(d)] = {
                "open": o, "high": h, "low": lo, "close": close,
                "volume": float(r["거래량"]),
                "value": float(r["거래대금"]) if "거래대금" in r.index else float("nan"),
                "rate": float(r["등락률"]) if "등락률" in r.index else float("nan"),
            }
        return pd.DataFrame.from_dict(recs, orient="index")


# ─── 유니버스: ETF ────────────────────────────────────────
def _index_key(name: str) -> str:
    """기초지수명을 '같은 노출'끼리 묶이도록 정규화한다.

    코스피 200 / 코스피 200 TR, S&P 500 / S&P 500 (원화환산) 처럼 수익 계산 방식이나 표기만 다른
    지수는 같은 것으로 본다. 환헤지(H) 여부는 기초지수명이 달라서 다른 노출로 남는다.
    """
    import re
    s = str(name).lower().replace(" ", "")
    for tok in ("(총수익지수)", "총수익지수", "(총수익)", "총수익", "(tr)", "(pr)", "totalreturn",
                "pricereturn", "(원화환산)", "원화환산", "지수", "index"):
        s = s.replace(tok, "")
    return re.sub(r"(tr|pr)$", "", s)


def fetch_etf_info() -> pd.DataFrame:
    """ETF 전종목 기본정보 — 종목명·기초지수·추적배수·복제방법·자산군·상장좌수 등. 1회 0.4초.

    pykrx 의 get_etf_ticker_name 은 이 표를 ETN·ELW 까지 합쳐 받느라 1건에 6초가 걸린다
    (1,183개면 2시간). ETF 표만 직접 받으면 전체가 0.4초다.
    """
    from pykrx.website.krx.etx.core import ETF_전종목기본종목
    _pykrx()                                    # 로그인 세션 준비
    df = _retry(lambda: ETF_전종목기본종목().fetch(), "ETF 기본정보 조회")
    df = df.set_index("ISU_SRT_CD")
    df["LIST_SHRS"] = pd.to_numeric(df["LIST_SHRS"].astype(str).str.replace(",", ""), errors="coerce")
    return df


def fetch_etf_universe(asof: pd.Timestamp, price_frame: pd.DataFrame | None = None,
                       size: int = C.UNIVERSE_SIZE) -> pd.DataFrame:
    """IRP 에서 살 수 있는 주식형 ETF 중 시가총액 상위 N. 반환: index=ticker, cols=[name, mktcap, index_key].

    제외 순서(전부 KRX 공식 분류 기준):
      1) 추적배수가 '일반' 이 아님 — 레버리지·인버스 (IRP 매수 금지)
      2) 복제방법 '합성' — 파생형 (IRP 매수 금지)
      3) 이름·기초지수에 '선물' — 실물로 분류돼도 선물 100% 인 파생형
      4) 자산군·지역이 설정 밖 (기본: 주식형, 국내+해외)
      5) config.ETF_EXCLUDE_TICKERS (증권사에서 막힌 것을 수동으로)
      6) 같은 기초지수는 시총 1위 하나만
    """
    info = fetch_etf_info()
    if price_frame is None or not len(price_frame):
        price_frame = _retry(lambda: _pykrx().get_etf_ohlcv_by_ticker(_ymd(asof)), "ETF 일괄 조회")
    df = info.join(price_frame[["종가"]], how="inner")
    df["mktcap"] = df["LIST_SHRS"] * df["종가"]

    futures = (df["ISU_ABBRV"].astype(str).str.contains("선물")
               | df["ETF_OBJ_IDX_NM"].astype(str).str.contains("선물|futures", case=False))
    keep = ((df["IDX_CALC_INST_NM2"] == "일반")
            & ~df["ETF_REPLICA_METHD_TP_CD"].astype(str).str.startswith("합성")
            & ~futures
            & df["IDX_ASST_CLSS_NM"].isin(C.ETF_ASSET_CLASSES)
            & df["IDX_MKT_CLSS_NM"].isin(C.ETF_REGIONS)
            & ~df.index.isin(C.ETF_EXCLUDE_TICKERS)
            & (df["mktcap"] > 0))
    df = df[keep].sort_values("mktcap", ascending=False)
    df["index_key"] = df["ETF_OBJ_IDX_NM"].map(_index_key)
    n_before = len(df)
    if C.ETF_DEDUP_BY_INDEX:
        df = df.drop_duplicates("index_key", keep="first")
    top = df.head(size)
    print(f"[data] ETF 유니버스: 매수 가능 {n_before}개 → 지수 중복 제거 {len(df)}개 → 시총 상위 {len(top)}개")
    out = pd.DataFrame({"name": top["ISU_ABBRV"].astype(object), "mktcap": top["mktcap"],
                        "index_key": top["index_key"], "index_name": top["ETF_OBJ_IDX_NM"],
                        "region": top["IDX_MKT_CLSS_NM"]})
    out.attrs["eligible"] = list(keep[keep].index)    # 매수 가능 전체(중복 지수·순위 밖 포함)
    return out


# ─── 유니버스: 주식 (시총 상위 N) ─────────────────────────
def fetch_universe(asof: pd.Timestamp, size: int = C.UNIVERSE_SIZE,
                   market: str = C.MARKET, cap_frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """asof 일자의 시가총액 상위 종목. 반환: index=ticker, cols=[name, mktcap].

    cap_frame(KRX 일괄 시세, 시가총액 포함)이 있으면 그걸 쓰고 요청을 아낀다.

    종목명은 반드시 대량 조회로 가져온다. get_market_ticker_name 은 1종목당 약 4.7초라
    200종목이면 16분이 걸려서 주문 구간 안에 끝나지 못한다(실제로 9/14 15:05 실행이
    여기서 멈췄다). get_market_price_change_by_ticker 는 943종목 전체가 0.5초다.
    """
    stock = _pykrx()
    if cap_frame is not None and len(cap_frame) and "시가총액" in cap_frame.columns:
        mktcap = cap_frame["시가총액"]
    else:
        mktcap = _retry(lambda: stock.get_market_cap(_ymd(asof), market=market),
                        "시가총액 조회")["시가총액"]
    top = mktcap[mktcap > 0].sort_values(ascending=False).head(size)
    if not len(top):
        print(f"[data] {pd.Timestamp(asof).date()} 시가총액이 비어 유니버스가 없습니다")

    ymd = _ymd(asof)
    names = {}
    try:
        bulk = _retry(lambda: stock.get_market_price_change_by_ticker(ymd, ymd, market=market),
                      "종목명 대량 조회")
        names = bulk["종목명"].to_dict()
    except Exception as e:                      # 대량 조회 실패 시에도 멈추지는 않는다
        print(f"[data] 종목명 대량 조회 실패({type(e).__name__}) — 개별 조회로 대체합니다")

    missing = [t for t in top.index if t not in names]
    if missing:
        print(f"[data] 종목명 개별 조회 {len(missing)}건 (건당 약 5초)")
        for t in missing:
            names[t] = stock.get_market_ticker_name(t)

    return pd.DataFrame({"name": pd.Series({t: names[t] for t in top.index}, dtype=object),
                         "mktcap": top})


# ─── 종목별 수정주가 (네이버) ──────────────────────────────
def _cache_path(ticker: str) -> str:
    return os.path.join(OHLCV_DIR, f"{ticker}.parquet")


def _normalize(ticker: str, raw) -> pd.DataFrame | None:
    if raw is None or not len(raw):
        return None
    raw = raw.rename(columns=COLS)
    missing = [c for c in REQUIRED if c not in raw.columns]
    if missing:
        raise KeyError(
            f"{ticker}: pykrx 응답에 {missing} 컬럼이 없습니다. "
            f"받은 컬럼={list(raw.columns)} — data.py 의 COLS 매핑을 확인하세요.")
    raw = raw[[c for c in COLS.values() if c in raw.columns]]
    raw.index = pd.to_datetime(raw.index)
    return raw


def _combine(old: pd.DataFrame | None, new: pd.DataFrame) -> pd.DataFrame:
    merged = new if old is None else pd.concat([old, new])
    return merged[~merged.index.duplicated(keep="last")].sort_index()


def fetch_ohlcv(ticker: str, start, end, adjusted: bool = True, full: bool = False) -> pd.DataFrame:
    """네이버 수정주가. full=True 면 캐시를 무시하고 워밍업 시작일부터 다시 받는다."""
    os.makedirs(OHLCV_DIR, exist_ok=True)
    path = _cache_path(ticker)
    cached = None if full or not os.path.exists(path) else pd.read_parquet(path)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    stock = _pykrx()

    def get(frm):
        if C.MARKET == "ETF":
            # ETF 는 KRX 원주가만 쓴다. 네이버는 분배금을 가격에서 조정해서(위클리 커버드콜 최대 7.4%,
            # 10/23 실측) KRX 일괄 시세와 이어붙이면 경계에 가짜 상승이 생긴다. 느리다(건당 7~12초).
            raw = _retry(lambda: stock.get_etf_ohlcv_by_date(_ymd(frm), _ymd(end), ticker),
                         f"{ticker} 조회")
        else:
            raw = _retry(lambda: stock.get_market_ohlcv(
                _ymd(frm), _ymd(end), ticker, adjusted=adjusted), f"{ticker} 조회")
        return _normalize(ticker, raw)

    need_start = start
    if cached is not None and len(cached):
        last = cached.index.max()
        need_start = min(max(start, last - timedelta(days=REFRESH_OVERLAP_DAYS)), end)

    raw = get(need_start)

    if raw is not None and cached is not None and len(cached):
        # 권리 변동이 생기면 네이버가 과거 수정주가 전체를 다시 계산한다. 캐시와 겹치는 구간이
        # 어긋나면 이력을 처음부터 다시 받는다. 마지막 캐시일은 장중 스냅샷일 수 있어 비교에서 뺀다.
        common = raw.index.intersection(cached.index)
        common = common[common < cached.index.max()]
        if len(common):
            diff = (raw.loc[common, "close"] / cached.loc[common, "close"] - 1).abs()
            if (diff > HEAL_TOLERANCE).any():
                print(f"[data] {ticker} 수정주가 변경 감지(최대 {diff.max():.1%}) — 이력 전체 재조회")
                raw, cached = get(start), None

    if raw is not None and cached is None and C.MARKET == "ETF":
        raw, _ = _split_adjust(raw)          # 원주가 전체 이력은 분할만 조정

    if raw is not None:
        cached = _combine(cached, raw)
        cached.to_parquet(path)
    if cached is None:
        return pd.DataFrame(columns=list(COLS.values()))
    return cached.loc[start:end]


# ─── KRX 일괄 시세로 캐시 갱신 ─────────────────────────────
PRICE_COLS = ["open", "high", "low", "close"]


def _split_adjust(df: pd.DataFrame) -> tuple[pd.DataFrame, list]:
    """원주가 이력에서 분할·병합을 찾아 이전 가격 전체를 조정한다.

    ETF 는 KRX 등락률이 일괄 시세에 없어서, 하루 변동이 가격제한폭(±30%)을 넘으면 분할·병합으로
    본다. 1배수 주식형 ETF 가 하루에 30% 넘게 움직이는 일은 사실상 없다. 분배금은 조정하지 않는다
    (주식 버전도 현금배당은 조정하지 않았다 — 같은 처리).
    """
    df = df.sort_index().copy()
    df[PRICE_COLS] = df[PRICE_COLS].astype(float)
    raw = df["close"].to_numpy(dtype=float)
    mult, events = np.ones(len(df)), []
    for i in range(1, len(df)):
        if raw[i - 1] > 0 and raw[i] > 0 and abs(raw[i] / raw[i - 1] - 1) > C.ARTIFACT_MOVE:
            mult[:i] *= raw[i] / raw[i - 1]
            events.append(df.index[i])
    if events:
        df[PRICE_COLS] = df[PRICE_COLS].mul(mult, axis=0)
    return df, events


def warm_etf_cache(start, end, tickers: list[str]) -> dict:
    """KRX ETF 일괄 시세를 하루씩 받아 종목별 이력 캐시를 처음부터 만든다(원주가 + 분할 조정).

    종목별 이력 조회(get_etf_ohlcv_by_date)는 건당 7~12초라 200개면 30분이 넘는다. 하루치 전종목은
    1초 안팎이라 300거래일이 약 5분이고, 매수 가능한 ETF 전부가 한 번에 채워진다.
    """
    t0 = time.time()
    days = trading_days(start, end)
    stock = _pykrx()
    frames = {}
    for i, d in enumerate(days):
        f = _retry(lambda: stock.get_etf_ohlcv_by_ticker(_ymd(d)), f"ETF 일괄 {d:%m-%d}")
        if f is not None and len(f):
            frames[d] = f
        if (i + 1) % 50 == 0:
            print(f"[data] ETF 이력 생성 {i + 1}/{len(days)}일 ({time.time() - t0:.0f}초)")
    big = pd.concat(frames, names=["date", "ticker"])
    want = set(tickers)
    os.makedirs(OHLCV_DIR, exist_ok=True)
    made, split_events = 0, {}
    for tk, g in big.groupby(level="ticker"):
        if tk not in want:
            continue
        df = _normalize(tk, g.droplevel("ticker"))
        if df is None or not len(df):
            continue
        df = df[df["close"] > 0]
        df, ev = _split_adjust(df)
        if ev:
            split_events[tk] = ev
        df.to_parquet(_cache_path(tk))
        made += 1
    print(f"[data] ETF 이력 생성 완료: {made}종목 · {len(frames)}거래일 · {time.time() - t0:.0f}초"
          f"{' · 분할 조정 ' + str(len(split_events)) + '종목' if split_events else ''}")
    return {"tickers": made, "days": len(frames), "splits": split_events}


def _from_bulk(ticker: str, start, end, days: list, bulk: KrxBulk, anchor=None):
    """캐시된 수정주가 이력에 KRX 공식 시세로 최근 구간을 덮어쓰고, 권리 변동은 직접 조정한다.

    반환 (df, actions)
      df       갱신된 이력(저장 완료). 이어붙일 수 없으면 None(캐시 없음·구간 공백·시세 없음).
      actions  이번에 새로 조정한 권리 변동일 목록.

    anchor 는 days[0] 바로 전 거래일. 캐시가 거기까지만 있어도 이력은 끊김 없이 이어진다.

    권리 변동 조정은 수정주가의 표준 계산을 그대로 쓴다:
      조정비율 = 기준가 ÷ 전일 종가,  기준가 = 종가 ÷ (1 + 등락률)
    이 비율을 권리 변동일 이전 가격 전체에 곱한다. 거래정지 중이라 거래량이 0 이면 등락률이
    0 으로 오므로, 가격 변화 전체를 권리 변동으로 본다(거래가 없었으니 시장 변동이 없다).

    네이버 수정주가에 맡기지 않는 이유: 006740(5:1 분할, 6/26)은 두 달 반이 지나도 네이버
    수정주가에 반영되지 않았다. 반영된 5건은 이 계산과 조정 전일가가 0.2% 이내로 일치했다.
    """
    path = _cache_path(ticker)
    if not days or not os.path.exists(path):
        return None, []
    cached = pd.read_parquet(path)
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    # 캐시 시작일은 따지지 않는다. 캐시는 늘 워밍업 시작일부터 받아 만들어지므로, 시작이 늦으면
    # 최근 상장 종목이라 원래 이력이 거기까지뿐이다.
    if not len(cached) or cached.index.max() < (anchor if anchor is not None else days[0]):
        return None, []

    rows = bulk.rows(ticker, days)
    if rows is None:
        return None, []
    rate = rows.pop("rate")

    # 권리 변동은 반드시 원주가끼리 비교해 찾는다. 창 첫날의 전일값을 캐시에서 가져오면, 지난 실행이
    # 창 안의 권리 변동으로 캐시를 조정해 뒀을 때 원주가와 조정가를 비교하게 되어 '거꾸로 된 권리
    # 변동'으로 오인하고 조정을 되돌린다(두 달치 실데이터 재실행 시험에서 실제로 3건 발생).
    # 그래서 창 바로 전 거래일(anchor)의 원주가도 KRX 일괄에서 가져온다.
    raw_prev = {}
    anchor_row = bulk.rows(ticker, [anchor]) if anchor is not None else None
    raw_prev[days[0]] = ((anchor, float(anchor_row["close"].iloc[0]))
                         if anchor_row is not None else None)   # 모르면 창 첫날은 판단하지 않음
    for a, b in zip(days[:-1], days[1:]):
        raw_prev[b] = (a, float(rows.at[a, "close"]))

    work, seg_from, actions = cached, days[0], []
    for d in days:
        if raw_prev[d] is None:
            continue
        prev_day, prev_close = raw_prev[d]
        close, r, vol = float(rows.at[d, "close"]), rate.loc[d], rows.at[d, "volume"]
        if prev_close <= 0:
            continue
        if pd.notna(r) and vol > 0:
            factor = close / (1 + r / 100) / prev_close
            flagged = abs(factor - 1) > CORP_ACTION_TOL
        else:
            # 거래정지일(등락률 0)이나 등락률이 없는 ETF: 가격제한폭을 넘는 변화만 권리 변동으로 본다
            factor = close / prev_close
            flagged = abs(factor - 1) > C.ARTIFACT_MOVE
        if not flagged:
            continue

        # 캐시가 이미 이 권리 변동 기준으로 조정돼 있으면(지난 실행에서 조정) 다시 곱하지 않는다.
        # 그때 권리 변동일 이전의 원주가 행으로 캐시를 덮으면 조정이 되돌려지므로 건너뛴다.
        adj_prev = cached["close"].get(prev_day)
        if adj_prev is not None and abs(float(adj_prev) / (prev_close * factor) - 1) <= CORP_ACTION_TOL:
            seg_from = d
            continue
        work = _combine(work, rows[(rows.index >= seg_from) & (rows.index < d)]).copy()
        work[PRICE_COLS] = work[PRICE_COLS].astype(float)     # 네이버 캐시는 정수형
        work.loc[work.index < d, PRICE_COLS] *= factor
        seg_from = d
        actions.append(d)

    merged = _combine(work, rows[rows.index >= seg_from])
    os.makedirs(OHLCV_DIR, exist_ok=True)
    merged.to_parquet(path)
    return merged.loc[start:end], actions


# ─── 지수 ──────────────────────────────────────────────────
def fetch_index(code: str, start, end) -> pd.Series:
    """지수 종가. 종목 가격처럼 캐시하고 최근 구간만 다시 받는다."""
    os.makedirs(INDEX_DIR, exist_ok=True)
    path = os.path.join(INDEX_DIR, f"{code}.parquet")
    start, end = pd.Timestamp(start), pd.Timestamp(end)
    cached = pd.read_parquet(path)["close"] if os.path.exists(path) else None

    need_start = start
    if cached is not None and len(cached) and cached.index.min() <= start + timedelta(days=10):
        need_start = min(max(start, cached.index.max() - timedelta(days=REFRESH_OVERLAP_DAYS)), end)
    else:
        cached = None                   # 캐시가 워밍업 구간을 못 덮으면 처음부터 받는다

    stock = _pykrx()
    # name_display=False 필수. 기본값(True)은 지수 이름을 붙이려고 get_index_ticker_name 을
    # 부르는데 그게 한 번에 12~16초다. 데이터는 똑같고 0.07초로 끝난다(9/14 실측).
    df = _retry(lambda: stock.get_index_ohlcv_by_date(
        _ymd(need_start), _ymd(end), code, name_display=False), "지수 조회")
    fresh = df["종가"].astype(float)
    fresh.index = pd.to_datetime(fresh.index)
    merged = fresh if cached is None else pd.concat([cached, fresh])
    merged = merged[~merged.index.duplicated(keep="last")].sort_index().rename("close")
    merged.to_frame().to_parquet(path)
    return merged.loc[start:end]


# ─── 번들 조립 (신호 계산용) ──────────────────────────────
def build_bundle(asof: pd.Timestamp, extra_tickers: list[str] | None = None,
                 warmup: int = C.HISTORY_WARMUP_DAYS, maintain_cache: bool = False) -> dict:
    """
    asof 기준 유니버스 + 보유(extra) 종목의 가격 이력 번들.

    maintain_cache=True 면 유니버스 밖의 매수 가능 ETF 캐시도 최근 구간을 갱신한다. 그래야 내일
    순위가 바뀌어 새로 편입되는 ETF 도 이력이 이어져 있다. 시간이 급한 15:05 에는 끈다.

    반환 dict: universe(df), close(wide df), ohlcv(dict[ticker]->df), kospi(series),
               inverse(df), asof, corp_actions(list[(ticker, date)]).
    """
    t0 = time.time()
    asof = pd.Timestamp(asof)
    start = asof - timedelta(days=int(warmup * 1.9) + 40)  # 거래일 warmup 확보용 여유

    bulk = KrxBulk()
    # 창 바로 전 거래일(anchor)까지 캐시가 있으면 이어붙일 수 있다. 이게 없으면 PC 가 딱 일주일
    # 꺼져 있던 경우 전 종목이 '구간 공백'으로 판정돼 네이버 200회 조회로 넘어간다.
    window = recent_trading_days(asof, BULK_WINDOW + 1)
    anchor, window = (window[0], window[1:]) if len(window) > BULK_WINDOW else (None, window)
    usable = bulk.usable_days(window)
    bulk_days, bulk_primary = usable, True
    if usable != window[:len(usable)] or len(usable) < len(window) - 1:
        # 중간 날짜가 비면 캐시와 이어붙일 때 구멍이 생긴다
        print("[data] KRX 일괄 시세에 빠진 날이 있어 일괄 경로를 쓰지 않습니다")
        bulk_days, bulk_primary = [], False
    elif window and window[-1] not in usable and _session_started(window[-1]):
        # 장이 열렸는데 당일 일괄 값이 없으면 일괄로는 오늘을 채울 수 없다
        print("[data] KRX 일괄에 당일 시세가 아직 없음 — 종목별 조회로 전환")
        bulk_primary = False

    uni_day = bulk_days[-1] if bulk_days else asof
    if C.MARKET == "ETF":
        uni = fetch_etf_universe(uni_day, bulk.primary_day(uni_day) if bulk_days else None)
    else:
        uni = fetch_universe(uni_day, cap_frame=bulk.stock_day(uni_day) if bulk_days else None)
    tickers = list(dict.fromkeys(list(uni.index) + list(extra_tickers or [])))

    if C.MARKET == "ETF":
        eligible = list(dict.fromkeys(uni.attrs.get("eligible", []) + tickers))
        missing = [t for t in tickers if not os.path.exists(_cache_path(t))]
        if len(missing) > 20:
            # 첫 실행(또는 캐시를 잃은 GitHub Actions 러너): 종목별 조회로 채우면 30분이 넘는다
            print(f"[data] 이력 캐시 없는 ETF {len(missing)}개 — 일괄 시세로 이력을 처음부터 만듭니다(약 5분)")
            warm_etf_cache(start, asof, eligible)

    # 보유 종목은 반드시 있어야 한다. 패널에서 빠지면 청산 판정 자체가 돌지 않아
    # 손절선을 넘겨도 그냥 들고 있게 된다 — 조용히 넘어가면 안 되는 실패다.
    required = set(extra_tickers or [])

    counts = {"일괄": 0, "종목별": 0, "대체": 0}
    naver = {"streak": 0, "off": False}
    corp_actions = []

    def load(t: str) -> pd.DataFrame | None:
        df, acts = _from_bulk(t, start, asof, bulk_days, bulk, anchor)
        for a in acts:
            corp_actions.append((t, a))
            print(f"[data] {t} {a:%m-%d} 권리 변동 감지 — KRX 기준가로 과거 가격 조정")
        if df is not None and bulk_primary:
            counts["일괄"] += 1
            return df
        fallback = df

        # 캐시가 없거나(신규 편입) 공백이 있거나, 장중인데 KRX 일괄에 당일 값이 없을 때만 네이버
        if not naver["off"]:
            try:
                fetch_ohlcv(t, start, asof)
                naver["streak"] = 0
                counts["종목별"] += 1
                # 네이버는 당일 봉 확정이 늦고 권리 변동을 반영하지 않기도 한다.
                # 최근 구간을 KRX 공식 시세로 다시 맞추면서 권리 변동도 같이 조정한다.
                df2, acts2 = _from_bulk(t, start, asof, bulk_days, bulk, anchor)
                for a in acts2:
                    corp_actions.append((t, a))
                if df2 is not None:
                    return df2
                return pd.read_parquet(_cache_path(t)).loc[start:asof]
            except Exception as e:
                naver["streak"] += 1
                print(f"[data] {t} 네이버 조회 실패({type(e).__name__})")
                if naver["streak"] >= NAVER_BREAKER:
                    # 네이버가 막혔는데 종목마다 타임아웃을 기다리면 200종목에 수십 분이 걸려
                    # 실행 제한 시간에 걸린다. 이번 실행은 KRX 일괄로만 진행한다.
                    naver["off"] = True
                    print(f"[data] 네이버 조회 {NAVER_BREAKER}회 연속 실패 — 이번 실행은 KRX 일괄로만 진행")
        if fallback is not None:
            counts["대체"] += 1
        return fallback

    ohlcv, closes, skipped = {}, {}, []
    for t in tickers:
        df = load(t)
        if df is None:
            if t in required:
                raise RuntimeError(
                    f"보유 종목 {t} 의 가격을 가져오지 못했습니다. "
                    "청산 판정이 불가능하므로 중단합니다.")
            skipped.append(t)           # 신규 진입 후보일 뿐이라 빠져도 무방
            continue
        if len(df):
            ohlcv[t] = df
            closes[f"{t}|{uni.loc[t, 'name'] if t in uni.index else t}"] = df["close"]
    if skipped:
        print(f"[data] 조회 실패로 제외된 진입 후보 {len(skipped)}종목: {skipped[:5]}"
              f"{' ...' if len(skipped) > 5 else ''}")
    close_wide = pd.DataFrame(closes).sort_index()

    kospi = fetch_index(C.KOSPI_INDEX, start, asof)

    # 인버스 한 종목은 KRX 경로가 오히려 느리다(ETF 일괄 5일치 3초). 네이버 1회(0.05초)로 받고
    # 당일 봉만 ETF 일괄 1회로 공식 시세에 맞춘다. 네이버가 막혔으면 ETF 일괄 5일치로 대체한다.
    inverse = None
    if not C.HEDGE_ENABLED:
        # IRP: 인버스를 사지 않으므로 시세도 필요 없다
        inverse = pd.DataFrame(columns=list(COLS.values()))
    elif not naver["off"]:
        try:
            inverse = fetch_ohlcv(C.HEDGE_TICKER, start, asof)
            today = bulk.rows(C.HEDGE_TICKER, [asof]) if asof in bulk_days else None
            if today is not None:
                pinned = _combine(pd.read_parquet(_cache_path(C.HEDGE_TICKER)),
                                  today.drop(columns="rate"))
                pinned.to_parquet(_cache_path(C.HEDGE_TICKER))
                inverse = pinned.loc[start:asof]
            counts["종목별"] += 1
        except Exception as e:
            print(f"[data] 인버스 네이버 조회 실패({type(e).__name__}) — ETF 일괄로 대체")
            inverse = None
    if C.HEDGE_ENABLED and (inverse is None or not len(inverse)):
        inverse = load(C.HEDGE_TICKER)
    if inverse is None:
        raise RuntimeError("인버스 ETF 시세를 가져오지 못했습니다. 헷지 평가가 불가능하므로 중단합니다.")

    if maintain_cache and C.MARKET == "ETF" and bulk_days:
        t1 = time.time()
        rest = [t for t in eligible if t not in set(tickers)]
        kept = sum(_from_bulk(t, start, asof, bulk_days, bulk, anchor)[0] is not None for t in rest)
        print(f"[data] 유니버스 밖 ETF 캐시 갱신 {kept}/{len(rest)}개 ({time.time() - t1:.1f}초)")

    print(f"[data] 수집 {time.time() - t0:.1f}초 · KRX 일괄 {counts['일괄']} / "
          f"종목별 {counts['종목별']} / 대체 {counts['대체']}"
          f"{' · 권리 변동 ' + str(len(corp_actions)) + '건' if corp_actions else ''}")
    index_key = uni["index_key"].to_dict() if "index_key" in uni.columns else {}
    return {"universe": uni, "close": close_wide, "ohlcv": ohlcv, "kospi": kospi,
            "inverse": inverse, "asof": asof, "corp_actions": corp_actions,
            "index_key": index_key}
