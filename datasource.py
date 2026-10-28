# -*- coding: utf-8 -*-
"""
数据获取层
-----------
统一封装 A股 / 港股 / 美股 的行情与基本面数据获取，具备：
  1. 多源降级：主源失败自动切备源
  2. 本地缓存：磁盘 parquet/csv 缓存 + 内存 TTL 缓存，降低对上游依赖
  3. 统一输出：所有函数返回统一 schema 的 pandas DataFrame

数据源说明（实测结论）：
  - A股历史行情   : akshare.stock_zh_a_daily（新浪源，稳定）
  - 港股历史行情   : akshare.stock_hk_daily（新浪源，稳定）
  - 指数历史行情   : akshare.stock_zh_index_daily（新浪源，稳定）
  - 实时快照      : akshare.stock_zh_a_spot（新浪源，全市场快照）
  - A股财务指标   : akshare.stock_financial_analysis_indicator（新浪源）
  - 美股         : yfinance（有限流，带重试与缓存）
"""
from __future__ import annotations

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable, Any

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), "data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)

# ----------------------------------------------------------------------------
# 通用工具：重试 + 内存缓存
# ----------------------------------------------------------------------------
_mem_cache: dict[str, tuple[float, Any]] = {}
_mem_lock = threading.Lock()


def _mem_get(key: str, ttl: int):
    with _mem_lock:
        item = _mem_cache.get(key)
    if not item:
        return None
    ts, val = item
    if time.time() - ts > ttl:
        return None
    return val


def _mem_set(key: str, val: Any):
    with _mem_lock:
        _mem_cache[key] = (time.time(), val)


def retry_call(fn: Callable, attempts: int = 3, delay: float = 1.0, backoff: float = 2.0):
    """简单重试包装，用于应对上游偶发连接中断。"""
    last_err = None
    for i in range(attempts):
        try:
            return fn()
        except Exception as e:  # noqa: BLE001
            last_err = e
            logger.warning("调用失败(第%d次): %s", i + 1, str(e)[:150])
            if i < attempts - 1:
                time.sleep(delay * (backoff ** i))
    raise last_err  # type: ignore[misc]


def _disk_cache_path(key: str) -> str:
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in key)
    return os.path.join(CACHE_DIR, f"{safe}.csv")


def cached_frame(key: str, loader: Callable[[], pd.DataFrame],
                 ttl: int = 3600, disk_ttl: int = 12 * 3600) -> pd.DataFrame:
    """
    带内存 + 磁盘双级缓存的 DataFrame 加载。
    磁盘缓存保证在数据源临时不可用时仍能返回最近一次成功结果。
    """
    mem = _mem_get(key, ttl)
    if mem is not None:
        return mem

    path = _disk_cache_path(key)
    if os.path.exists(path):
        age = time.time() - os.path.getmtime(path)
        if age < disk_ttl:
            try:
                df = pd.read_csv(path)
                _mem_set(key, df)
                return df
            except Exception:  # noqa: BLE001
                pass

    # 缓存过期/不存在，尝试重新拉取
    try:
        df = retry_call(loader, attempts=3, delay=1.0)
        if df is None or len(df) == 0:
            raise ValueError("数据源返回空数据")
        _mem_set(key, df)
        try:
            df.to_csv(path, index=False)
        except Exception:  # noqa: BLE001
            pass
        return df
    except Exception as e:  # noqa: BLE001
        # 降级：返回过期磁盘缓存（如果有）
        if os.path.exists(path):
            logger.warning("数据源失败，降级使用旧缓存 %s: %s", key, str(e)[:120])
            try:
                df = pd.read_csv(path)
                _mem_set(key, df)
                return df
            except Exception:  # noqa: BLE001
                pass
        raise


# ----------------------------------------------------------------------------
# 代码规范化
# ----------------------------------------------------------------------------
def normalize_symbol(symbol: str, market: Optional[str] = None) -> dict:
    """
    把用户输入的各种代码格式统一成内部结构。

    支持：
      600519 / sh600519 / 600519.SH   -> A股沪市
      000001 / sz000001 / 000001.SZ   -> A股深市
      300750 / 430047 / 830799        -> A股创业板/北交所
      00700 / hk00700 / 0700.HK       -> 港股
      AAPL / usAAPL / BRK.B           -> 美股

    返回: {market, code, ak_symbol, display, secid}
    """
    raw = (symbol or "").strip().upper()
    if not raw:
        raise ValueError("股票代码不能为空")

    mkt = (market or "").strip().lower()

    # 显式前缀判定
    if raw.startswith("SH") or raw.endswith(".SH"):
        code = raw.replace("SH", "").replace(".", "")
        return {"market": "A", "code": code, "ak_symbol": f"sh{code}", "display": f"{code}.SH"}
    if raw.startswith("SZ") or raw.endswith(".SZ"):
        code = raw.replace("SZ", "").replace(".", "")
        return {"market": "A", "code": code, "ak_symbol": f"sz{code}", "display": f"{code}.SZ"}
    if raw.startswith("BJ"):
        code = raw.replace("BJ", "")
        return {"market": "A", "code": code, "ak_symbol": f"bj{code}", "display": f"{code}.BJ"}
    if raw.startswith("HK") or raw.endswith(".HK"):
        code = raw.replace("HK", "").replace(".", "")
        code = code.zfill(5)
        return {"market": "HK", "code": code, "ak_symbol": code, "display": f"{code}.HK"}
    if raw.startswith("US"):
        code = raw[2:]
        return {"market": "US", "code": code, "ak_symbol": code, "display": code}

    if mkt in ("hk", "港股"):
        return {"market": "HK", "code": raw.zfill(5), "ak_symbol": raw.zfill(5),
                "display": f"{raw.zfill(5)}.HK"}
    if mkt in ("us", "美股"):
        return {"market": "US", "code": raw, "ak_symbol": raw, "display": raw}

    # 纯数字 -> 判断 A股 / 港股
    if raw.replace(".", "").isdigit():
        pure = raw.replace(".", "")
        if len(pure) == 6:
            # A股
            if pure[0] in ("6", "9"):
                return {"market": "A", "code": pure, "ak_symbol": f"sh{pure}", "display": f"{pure}.SH"}
            if pure[0] == "8" or pure[:2] in ("43", "83", "87", "88"):
                return {"market": "A", "code": pure, "ak_symbol": f"bj{pure}", "display": f"{pure}.BJ"}
            if pure[0] in ("0", "2", "3"):
                return {"market": "A", "code": pure, "ak_symbol": f"sz{pure}", "display": f"{pure}.SZ"}
            # 6开头兜底
            return {"market": "A", "code": pure, "ak_symbol": f"sh{pure}", "display": f"{pure}.SH"}
        if len(pure) <= 5:
            code = pure.zfill(5)
            return {"market": "HK", "code": code, "ak_symbol": code, "display": f"{code}.HK"}

    # 字母 -> 美股
    return {"market": "US", "code": raw, "ak_symbol": raw, "display": raw}


# ----------------------------------------------------------------------------
# 历史行情
# ----------------------------------------------------------------------------
_STD_COLS = ["date", "open", "high", "low", "close", "volume", "amount"]


def _finalize_ohlcv(df: pd.DataFrame) -> pd.DataFrame:
    """把不同来源的行情表统一成标准列，并按日期升序。"""
    if df is None or len(df) == 0:
        return pd.DataFrame(columns=_STD_COLS)

    df = df.copy()
    # 统一日期列
    if "date" not in df.columns and "日期" in df.columns:
        df = df.rename(columns={"日期": "date"})
    if "date" in df.columns:
        df["date"] = pd.to_datetime(df["date"], errors="coerce")
    elif isinstance(df.index, pd.DatetimeIndex):
        df = df.reset_index().rename(columns={"index": "date", "Date": "date"})

    rename_map = {
        "日期": "date", "开盘": "open", "最高": "high", "最低": "low",
        "收盘": "close", "成交量": "volume", "成交额": "amount",
        "Open": "open", "High": "high", "Low": "low", "Close": "close",
        "Volume": "volume",
    }
    df = df.rename(columns=rename_map)

    for col in _STD_COLS:
        if col not in df.columns:
            df[col] = pd.NA

    df = df[_STD_COLS].copy()
    for col in ["open", "high", "low", "close", "volume", "amount"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    # date 统一成 YYYY-MM-DD 字符串，便于 JSON 序列化与前端消费
    df["date"] = df["date"].dt.strftime("%Y-%m-%d")
    return df


def get_history(symbol: str, market: Optional[str] = None,
                days: int = 400, adjust: str = "qfq") -> tuple[pd.DataFrame, dict]:
    """
    获取历史日线。返回 (DataFrame, info)。
    days 为希望保留的最近交易日数量（多取一些以计算长周期指标）。
    """
    import akshare as ak

    info = normalize_symbol(symbol, market)
    mkt = info["market"]

    if mkt == "A":
        def loader():
            return ak.stock_zh_a_daily(symbol=info["ak_symbol"], adjust=adjust)

        key = f"hist_A_{info['code']}_{adjust}"
        df = cached_frame(key, loader, ttl=1800)
        info["name"] = _lookup_a_name(info)

    elif mkt == "HK":
        def loader():
            return ak.stock_hk_daily(symbol=info["ak_symbol"], adjust=adjust)

        key = f"hist_HK_{info['code']}_{adjust}"
        df = cached_frame(key, loader, ttl=1800)
        info["name"] = _lookup_hk_name(info)

    else:  # US
        def loader():
            # 主源：Nasdaq 官方 API（免费、稳定）；备源：yfinance
            try:
                return _us_history_nasdaq(info["code"])
            except Exception as e1:  # noqa: BLE001
                logger.warning("Nasdaq 源失败，切换 yfinance: %s", str(e1)[:100])
                import yfinance as yf
                t = yf.Ticker(info["code"])
                h = t.history(period="5y", auto_adjust=(adjust != ""))
                if h is None or len(h) == 0:
                    raise ValueError("yfinance 返回空数据")
                return h.reset_index()

        key = f"hist_US_{info['code']}"
        df = cached_frame(key, loader, ttl=1800)
        info["name"] = _lookup_us_name(info)

    df = _finalize_ohlcv(df)
    if len(df) > days:
        df = df.iloc[-days:].reset_index(drop=True)
    if len(df) == 0:
        raise ValueError(f"未获取到 {info['display']} 的行情数据，请检查代码是否正确")

    info["last_date"] = df["date"].iloc[-1]
    info["bars"] = len(df)
    return df, info


# ----------------------------------------------------------------------------
# 实时快照（新浪全市场快照，一次拉取，本地匹配）
# ----------------------------------------------------------------------------
def _get_a_spot_all() -> pd.DataFrame:
    import akshare as ak

    def loader():
        return ak.stock_zh_a_spot()

    return cached_frame("spot_A_all", loader, ttl=180, disk_ttl=3600)


def _lookup_a_name(info: dict) -> str:
    try:
        spot = _get_a_spot_all()
        hit = spot[spot["代码"] == info["ak_symbol"]]
        if len(hit):
            return str(hit.iloc[0]["名称"])
    except Exception:  # noqa: BLE001
        pass
    return info["code"]


def _hk_spot_lookup(code: str) -> dict | None:
    """
    港股快照查询。注意不同 akshare 版本列名不一致：
      新版：日期时间/代码/中文名称/英文名称/最新价/涨跌额/涨跌幅/昨收/今开/最高/最低/成交量/成交额
      旧版：symbol/name/lasttrade/change/changepercent/open/high/low/prevclose/volume/amount
    这里统一归一化后再取值。
    """
    try:
        import akshare as ak
        spot = cached_frame("spot_HK_all", lambda: ak.stock_hk_spot(), ttl=300)
        if spot is None or len(spot) == 0:
            return None

        cols = set(spot.columns)
        code_col = "代码" if "代码" in cols else ("symbol" if "symbol" in cols else None)
        if code_col is None:
            return None
        row = spot[spot[code_col].astype(str).str.zfill(5) == code.zfill(5)]
        if len(row) == 0:
            return None
        r = row.iloc[0]

        def pick(*names):
            for n in names:
                if n in cols:
                    v = _f(r[n])
                    if v is not None:
                        return round(v, 3)
            return None

        def pick_name():
            for n in ("中文名称", "名称", "name", "英文名称"):
                if n in cols:
                    v = str(r[n]).strip()
                    if v and v.lower() != "nan":
                        return v
            return None

        return {
            "name": pick_name(),
            "price": pick("最新价", "lasttrade"),
            "change": pick("涨跌额", "change"),
            "pct": pick("涨跌幅", "changepercent"),
            "open": pick("今开", "open"),
            "high": pick("最高", "high"),
            "low": pick("最低", "low"),
            "prev_close": pick("昨收", "prevclose"),
            "volume": pick("成交量", "volume"),
            "amount": pick("成交额", "amount"),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("港股快照查询失败 %s: %s", code, str(e)[:120])
        return None


def _lookup_hk_name(info: dict) -> str:
    snap = _hk_spot_lookup(info["code"])
    if snap and snap.get("name"):
        return snap["name"]
    return info["code"]


def _lookup_us_name(info: dict) -> str:
    # 优先 Nasdaq API（附带公司名），失败再试 yfinance
    try:
        d = _us_nasdaq_quote(info["code"])
        if d and d.get("name"):
            return d["name"]
    except Exception:  # noqa: BLE001
        pass
    try:
        import yfinance as yf
        t = yf.Ticker(info["code"])
        d = t.info
        return d.get("shortName") or d.get("longName") or info["code"]
    except Exception:  # noqa: BLE001
        return info["code"]


_NASDAQ_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
}


def _us_nasdaq_quote(code: str) -> dict | None:
    """Nasdaq 官方 API 实时报价。字段嵌套在 primaryData/secondaryData 中。"""
    import requests
    url = f"https://api.nasdaq.com/api/quote/{code}/info?assetclass=stocks"
    r = requests.get(url, headers=_NASDAQ_HEADERS, timeout=15)
    r.raise_for_status()
    j = r.json()
    d = (j.get("data") or {})
    if not d:
        return None

    def num(x):
        try:
            return float(str(x).replace("$", "").replace(",", "").replace("%", ""))
        except Exception:  # noqa: BLE001
            return None

    # 优先 primaryData（实时），其次 secondaryData（收盘）
    prim = d.get("primaryData") or {}
    sec = d.get("secondaryData") or {}
    src = prim if prim.get("lastSalePrice") else sec
    if not src:
        return None
    price = num(src.get("lastSalePrice"))
    change = num(src.get("netChange"))
    pct = num(src.get("percentageChange"))
    prev_close = num(sec.get("lastSalePrice")) if src is prim else None
    if prev_close is None and price is not None and change is not None:
        prev_close = round(price - change, 2)
    return {
        "name": (d.get("companyName") or d.get("company")),
        "price": price,
        "change": change,
        "pct": pct,
        "prev_close": prev_close,
        "source": "nasdaq",
    }


def _us_history_nasdaq(code: str, years: int = 6) -> pd.DataFrame:
    """Nasdaq 官方 API 历史 OHLCV。"""
    import requests
    from datetime import datetime, timedelta
    start = (datetime.now() - timedelta(days=365 * years)).strftime("%Y-%m-%d")
    end = datetime.now().strftime("%Y-%m-%d")
    url = (f"https://api.nasdaq.com/api/quote/{code}/chart"
           f"?assetclass=stocks&fromdate={start}&todate={end}")
    r = requests.get(url, headers=_NASDAQ_HEADERS, timeout=25)
    r.raise_for_status()
    j = r.json()
    rows = ((j.get("data") or {}).get("chart")) or []
    if not rows:
        raise ValueError("Nasdaq 返回空数据")
    recs = []
    for row in rows:
        z = row.get("z") or row
        try:
            recs.append({
                "date": str(z.get("dateTime")),
                "open": float(str(z.get("open")).replace(",", "")),
                "high": float(str(z.get("high")).replace(",", "")),
                "low": float(str(z.get("low")).replace(",", "")),
                "close": float(str(z.get("close")).replace(",", "")),
                "volume": float(str(z.get("volume", "0")).replace(",", "") or 0),
            })
        except Exception:  # noqa: BLE001
            continue
    return pd.DataFrame(recs)


def get_quote(symbol: str, market: Optional[str] = None) -> dict:
    """
    获取实时/最新报价快照，字段统一：
    price, change, pct, open, high, low, prev_close, volume, amount, name
    """
    info = normalize_symbol(symbol, market)
    mkt = info["market"]
    out = {"market": mkt, "code": info["code"], "display": info["display"]}

    if mkt == "A":
        try:
            spot = _get_a_spot_all()
            hit = spot[spot["代码"] == info["ak_symbol"]]
            if len(hit):
                r = hit.iloc[0]
                out.update({
                    "name": str(r["名称"]),
                    "price": _f(r.get("最新价")),
                    "change": _f(r.get("涨跌额")),
                    "pct": _f(r.get("涨跌幅")),
                    "open": _f(r.get("今开")),
                    "high": _f(r.get("最高")),
                    "low": _f(r.get("最低")),
                    "prev_close": _f(r.get("昨收")),
                    "volume": _f(r.get("成交量")),
                    "amount": _f(r.get("成交额")),
                    "source": "sina_spot",
                })
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning("A股快照失败: %s", str(e)[:120])

    if mkt == "HK":
        snap = _hk_spot_lookup(info["code"])
        if snap and snap.get("price") is not None:
            out.update(snap)
            out["name"] = snap.get("name") or info["code"]
            out["source"] = "sina_hk_spot"
            return out

    if mkt == "US":
        # 主源：Nasdaq 官方 API；备源：yfinance；再兜底日线
        try:
            nd = _us_nasdaq_quote(info["code"])
            if nd and nd.get("price") is not None:
                out.update(nd)
                out["name"] = nd.get("name") or info["code"]
                out["open"] = out["high"] = out["low"] = None
                out["source"] = "nasdaq"
                return out
        except Exception as e:  # noqa: BLE001
            logger.warning("Nasdaq 报价失败 %s: %s", info["code"], str(e)[:100])

        if out.get("price") is None:
            try:
                import yfinance as yf
                t = yf.Ticker(info["code"])
                h = t.history(period="5d")
                if h is not None and len(h):
                    last = h.iloc[-1]
                    prev = h.iloc[-2] if len(h) > 1 else last
                    price = float(last["Close"])
                    prev_close = float(prev["Close"])
                    out.update({
                        "name": info.get("name") or info["code"],
                        "price": round(price, 2),
                        "change": round(price - prev_close, 2),
                        "pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
                        "open": round(float(last["Open"]), 2),
                        "high": round(float(last["High"]), 2),
                        "low": round(float(last["Low"]), 2),
                        "prev_close": round(prev_close, 2),
                        "volume": float(last["Volume"]),
                        "amount": None,
                        "source": "yfinance",
                    })
            except Exception as e:  # noqa: BLE001
                logger.warning("美股 yfinance 报价失败: %s", str(e)[:100])

    # 兜底：用日线最后一根构造快照
    try:
        df, inf = get_history(symbol, market, days=5)
        last = df.iloc[-1]
        prev = df.iloc[-2] if len(df) > 1 else last
        price = float(last["close"])
        prev_close = float(prev["close"])
        out.update({
            "name": inf.get("name", info["code"]),
            "price": round(price, 3),
            "change": round(price - prev_close, 3),
            "pct": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0.0,
            "open": float(last["open"]), "high": float(last["high"]),
            "low": float(last["low"]), "prev_close": round(prev_close, 3),
            "volume": float(last["volume"]) if pd.notna(last["volume"]) else None,
            "amount": None, "source": "daily_fallback",
        })
    except Exception:  # noqa: BLE001
        out.update({"name": info["code"], "price": None, "source": "none"})
    return out


def _f(v):
    try:
        if v is None or pd.isna(v):
            return None
        return float(v)
    except Exception:  # noqa: BLE001
        return None


# ----------------------------------------------------------------------------
# 指数行情
# ----------------------------------------------------------------------------
INDEX_MAP = {
    "sh000001": {"name": "上证指数", "market": "A", "ak": "sh000001"},
    "sz399001": {"name": "深证成指", "market": "A", "ak": "sz399001"},
    "sz399006": {"name": "创业板指", "market": "A", "ak": "sz399006"},
    "sh000300": {"name": "沪深300", "market": "A", "ak": "sh000300"},
    "sh000905": {"name": "中证500", "market": "A", "ak": "sh000905"},
}


def get_index_history(code: str = "sh000001", days: int = 400) -> pd.DataFrame:
    import akshare as ak

    code = code.lower()
    entry = INDEX_MAP.get(code, {"name": code, "ak": code})
    df = cached_frame(f"index_{code}", lambda: ak.stock_zh_index_daily(symbol=entry["ak"]),
                      ttl=1800)
    df = _finalize_ohlcv(df)
    if len(df) > days:
        df = df.iloc[-days:].reset_index(drop=True)
    return df


def get_market_overview() -> list[dict]:
    """主要指数概览，用于首页大盘面板。"""
    out = []
    for code, meta in INDEX_MAP.items():
        try:
            df = get_index_history(code, days=5)
            if len(df) >= 2:
                cur = float(df["close"].iloc[-1])
                prev = float(df["close"].iloc[-2])
                out.append({
                    "code": code, "name": meta["name"], "price": round(cur, 2),
                    "change": round(cur - prev, 2),
                    "pct": round((cur - prev) / prev * 100, 2) if prev else 0.0,
                })
        except Exception as e:  # noqa: BLE001
            logger.warning("指数 %s 获取失败: %s", code, str(e)[:80])
    return out


# ----------------------------------------------------------------------------
# 财务数据
# ----------------------------------------------------------------------------
def get_financials(symbol: str, market: Optional[str] = None) -> dict:
    """
    获取基本面/财务指标。A股用新浪财务指标接口；港股/美股尽力而为。
    返回结构化 dict，字段不存在则为 None。
    """
    info = normalize_symbol(symbol, market)
    result: dict = {"market": info["market"], "code": info["code"], "metrics": [],
                    "periods": [], "raw": {}}

    if info["market"] == "A":
        try:
            import akshare as ak
            df = cached_frame(
                f"fin_A_{info['code']}",
                lambda: ak.stock_financial_analysis_indicator(
                    symbol=info["code"], start_year="2021"),
                ttl=6 * 3600, disk_ttl=7 * 24 * 3600,
            )
            if df is not None and len(df):
                result["periods"] = [str(x) for x in df["日期"].tolist()[:8]]
                result["metrics"] = _extract_a_metrics(df)
                result["raw"] = {"columns": list(df.columns)[:40]}
        except Exception as e:  # noqa: BLE001
            logger.warning("A股财务获取失败 %s: %s", info["code"], str(e)[:120])
            result["error"] = str(e)[:200]

    elif info["market"] == "US":
        try:
            import yfinance as yf
            t = yf.Ticker(info["code"])
            fi = t.info or {}
            result["metrics"] = [
                {"name": "市值", "value": fi.get("marketCap"), "unit": "USD", "fmt": "money"},
                {"name": "市盈率(TTM)", "value": fi.get("trailingPE"), "fmt": "num"},
                {"name": "市净率", "value": fi.get("priceToBook"), "fmt": "num"},
                {"name": "ROE", "value": (fi.get("returnOnEquity") or 0) * 100, "unit": "%", "fmt": "num"},
                {"name": "毛利率", "value": (fi.get("grossMargins") or 0) * 100, "unit": "%", "fmt": "num"},
                {"name": "净利率", "value": (fi.get("profitMargins") or 0) * 100, "unit": "%", "fmt": "num"},
                {"name": "营收增长", "value": (fi.get("revenueGrowth") or 0) * 100, "unit": "%", "fmt": "num"},
                {"name": "每股收益", "value": fi.get("trailingEps"), "fmt": "num"},
            ]
            result["metrics"] = [m for m in result["metrics"] if m["value"] is not None]
        except Exception as e:  # noqa: BLE001
            logger.warning("美股财务获取失败 %s: %s", info["code"], str(e)[:120])
            result["error"] = str(e)[:200]

    return result


def _extract_a_metrics(df: pd.DataFrame) -> list[dict]:
    """从新浪财务指标宽表中抽取关键指标（取最新一期）。"""
    latest = df.iloc[0]

    def g(*names):
        for n in names:
            if n in df.columns:
                v = latest[n]
                v = _f(v)
                if v is not None:
                    return v
        return None

    spec = [
        ("摊薄每股收益", g("摊薄每股收益(元)", "加权每股收益(元)"), "元", "num"),
        ("每股净资产", g("每股净资产_调整前(元)", "每股净资产_调整后(元)"), "元", "num"),
        ("净资产收益率", g("净资产收益率(%)", "加权净资产收益率(%)"), "%", "num"),
        ("销售毛利率", g("销售毛利率(%)"), "%", "num"),
        ("销售净利率", g("销售净利率(%)"), "%", "num"),
        ("主营业务收入增长率", g("主营业务收入增长率(%)"), "%", "num"),
        ("净利润增长率", g("净利润增长率(%)"), "%", "num"),
        ("资产负债率", g("资产负债率(%)"), "%", "num"),
        ("流动比率", g("流动比率"), "", "num"),
        ("速动比率", g("速动比率"), "", "num"),
        ("总资产周转率", g("总资产周转率(次)"), "次", "num"),
        ("存货周转率", g("存货周转率(次)"), "次", "num"),
    ]
    out = []
    for name, val, unit, fmt in spec:
        out.append({"name": name, "value": val, "unit": unit, "fmt": fmt})
    return out
