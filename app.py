# -*- coding: utf-8 -*-

"""
StockLens · 股票分析体系（单文件版）
====================================
为方便 GitHub 网页上传而生：整个后端 + 前端模板全部内嵌在这一个文件里。

启动：
    gunicorn -c gunicorn.conf.py wsgi:application   # 生产（Render 用这个）
    python3 app.py                                  # 本地调试
"""

from __future__ import annotations



# ============================================================================
# [原 app/core/datasource.py]
# ============================================================================

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

import os
import time
import logging
import threading
from datetime import datetime, timedelta
from typing import Optional, Callable, Any

import pandas as pd

logger = logging.getLogger(__name__)

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache")
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



def _a_direct_quote(ak_symbol: str):
    """新浪单股直连报价：毫秒级返回，避免触发全市场快照（服务器上全市场快照需约6分钟）。"""
    try:
        import requests as _rq
        r = _rq.get("https://hq.sinajs.cn/list=" + ak_symbol,
                    headers={"Referer": "https://finance.sina.com.cn"}, timeout=6)
        r.encoding = "gbk"
        seg = r.text.split('"')[1]
        f = seg.split(",")
        if len(f) < 10:
            return None
        price = float(f[3]); prev = float(f[2])
        return {"name": f[0], "price": round(price, 3),
                "change": round(price - prev, 3),
                "pct": round((price - prev) / prev * 100, 3) if prev else 0.0,
                "open": float(f[1]), "high": float(f[4]), "low": float(f[5]),
                "prev_close": prev, "volume": float(f[8]), "amount": float(f[9])}
    except Exception:  # noqa: BLE001
        return None


def _hk_direct_quote(code: str):
    """腾讯港股单股直连报价：秒回，避免全市场港股快照。"""
    try:
        import requests as _rq
        r = _rq.get("https://qt.gtimg.cn/q=hk" + code, timeout=6)
        r.encoding = "gbk"
        p = r.text.split("~")
        if len(p) < 6 or not p[3]:
            return None
        price = float(p[3]); prev = float(p[4]) if p[4] else 0.0
        return {"name": p[1], "price": round(price, 3),
                "change": round(price - prev, 3),
                "pct": round((price - prev) / prev * 100, 2) if prev else 0.0,
                "open": float(p[5]) if p[5] else None,
                "high": None, "low": None, "prev_close": prev or None,
                "volume": float(p[6]) if p[6] else None, "amount": None}
    except Exception:  # noqa: BLE001
        return None


def get_quote(symbol: str, market: Optional[str] = None) -> dict:
    """
    获取实时/最新报价快照，字段统一：
    price, change, pct, open, high, low, prev_close, volume, amount, name
    """
    info = normalize_symbol(symbol, market)
    mkt = info["market"]
    out = {"market": mkt, "code": info["code"], "display": info["display"]}

    if mkt == "A":
        # 快路径：单股直连（服务器上全市场快照要抓 70 页，太慢）
        dq = _a_direct_quote(info["ak_symbol"])
        if dq and dq.get("price") is not None:
            out.update(dq)
            out["source"] = "sina_direct"
            return out
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
        # 快路径：腾讯港股直连
        hq = _hk_direct_quote(info["code"])
        if hq and hq.get("price") is not None:
            out.update(hq)
            out["source"] = "tencent_hk_direct"
            return out
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



# ============================================================================
# [原 app/core/indicators.py]
# ============================================================================

# -*- coding: utf-8 -*-
"""
技术指标计算
-------------
纯 pandas/numpy 实现，不依赖 TA-Lib，保证零外部二进制依赖。
包含：MA、EMA、MACD、KDJ、RSI、BOLL、ATR、OBV、量比、乖离率。
"""

import numpy as np
import pandas as pd


def ma(series: pd.Series, n: int) -> pd.Series:
    return series.rolling(window=n, min_periods=1).mean()


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9):
    """返回 (DIF, DEA, MACD柱)"""
    dif = ema(close, fast) - ema(close, slow)
    dea = ema(dif, signal)
    hist = (dif - dea) * 2
    return dif, dea, hist


def kdj(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 9,
        m1: int = 3, m2: int = 3):
    """返回 (K, D, J)"""
    low_n = low.rolling(window=n, min_periods=1).min()
    high_n = high.rolling(window=n, min_periods=1).max()
    rsv = (close - low_n) / (high_n - low_n).replace(0, np.nan) * 100
    rsv = rsv.fillna(50)
    k = rsv.ewm(com=m1 - 1, adjust=False).mean()
    d = k.ewm(com=m2 - 1, adjust=False).mean()
    j = 3 * k - 2 * d
    return k, d, j


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    diff = close.diff()
    up = diff.clip(lower=0)
    down = -diff.clip(upper=0)
    # Wilder 平滑
    roll_up = up.ewm(alpha=1 / n, adjust=False).mean()
    roll_down = down.ewm(alpha=1 / n, adjust=False).mean()
    rs = roll_up / roll_down.replace(0, np.nan)
    out = 100 - 100 / (1 + rs)
    return out.fillna(50)


def boll(close: pd.Series, n: int = 20, k: float = 2.0):
    """返回 (中轨, 上轨, 下轨)"""
    mid = close.rolling(window=n, min_periods=1).mean()
    std = close.rolling(window=n, min_periods=1).std(ddof=0)
    return mid, mid + k * std, mid - k * std


def atr(high: pd.Series, low: pd.Series, close: pd.Series, n: int = 14) -> pd.Series:
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def obv(close: pd.Series, volume: pd.Series) -> pd.Series:
    sign = np.sign(close.diff().fillna(0))
    return (sign * volume.fillna(0)).cumsum()


def bias(close: pd.Series, n: int = 6) -> pd.Series:
    """乖离率"""
    m = close.rolling(window=n, min_periods=1).mean()
    return (close - m) / m * 100


def compute_all(df: pd.DataFrame) -> pd.DataFrame:
    """
    对标准 OHLCV 表计算全部指标，返回追加了指标列的新表。
    """
    out = df.copy()
    close, high, low, vol = out["close"], out["high"], out["low"], out["volume"]

    for n in (5, 10, 20, 60, 120, 250):
        out[f"ma{n}"] = ma(close, n)

    out["dif"], out["dea"], out["macd"] = macd(close)
    out["k"], out["d"], out["j"] = kdj(high, low, close)
    out["rsi6"] = rsi(close, 6)
    out["rsi14"] = rsi(close, 14)
    out["boll_mid"], out["boll_up"], out["boll_low"] = boll(close)
    out["atr"] = atr(high, low, close)
    out["obv"] = obv(close, vol)
    out["bias6"] = bias(close, 6)

    # 量比：当日成交量 / 过去5日均量
    vol_ma5 = vol.rolling(window=5, min_periods=1).mean()
    out["vol_ma5"] = vol_ma5
    out["vol_ratio"] = vol / vol_ma5.replace(0, np.nan)

    # 日涨跌幅
    out["pct"] = close.pct_change() * 100

    # 换手率近似（需要流通股，缺失时跳过）
    if "turnover" in out.columns:
        out["turnover_pct"] = pd.to_numeric(out["turnover"], errors="coerce") * 100

    return out


def find_support_resistance(df: pd.DataFrame, lookback: int = 120) -> dict:
    """
    基于近端高低点 + 均线 + 布林带，识别支撑位与压力位。
    """
    d = df.tail(lookback)
    cur = float(df["close"].iloc[-1])

    highs = d["high"].astype(float).values
    lows = d["low"].astype(float).values

    # 局部极值点（窗口=5）
    piv_high, piv_low = [], []
    w = 5
    for i in range(w, len(d) - w):
        seg_h = highs[i - w:i + w + 1]
        seg_l = lows[i - w:i + w + 1]
        if highs[i] == seg_h.max():
            piv_high.append(highs[i])
        if lows[i] == seg_l.min():
            piv_low.append(lows[i])

    def cluster(levels):
        """把接近的价位聚成一类，返回代表性价位。"""
        levels = sorted(levels)
        groups, curg = [], []
        for v in levels:
            if not curg or abs(v - np.mean(curg)) / max(np.mean(curg), 1e-9) < 0.02:
                curg.append(v)
            else:
                groups.append(curg)
                curg = [v]
        if curg:
            groups.append(curg)
        return [float(np.mean(g)) for g in groups]

    resistance = [p for p in cluster(piv_high) if p > cur * 1.005]
    support = [p for p in cluster(piv_low) if p < cur * 0.995]

    # 均线也作为候选
    for n in (20, 60, 120):
        col = f"ma{n}"
        if col in df.columns:
            v = float(df[col].iloc[-1])
            if pd.notna(v):
                (resistance if v > cur else support).append(v)

    resistance = sorted(set(round(x, 2) for x in resistance))[:4]
    support = sorted(set(round(x, 2) for x in support), reverse=True)[:4]

    return {
        "current": round(cur, 2),
        "resistance": resistance,
        "support": support,
        "high_120": round(float(highs.max()), 2),
        "low_120": round(float(lows.min()), 2),
    }



# ============================================================================
# [原 app/core/scoring.py]
# ============================================================================

# -*- coding: utf-8 -*-
"""
智能评分与信号引擎
-------------------
把技术指标、量能、位置、趋势等多维度信息综合成一个 0-100 的评分，
并给出明确的交易信号与理由。所有规则可解释、可追溯。

评分维度（权重合计 100）：
  趋势(30) + 动量(25) + 均线(20) + 量能(15) + 位置/波动(10)
"""

import numpy as np
import pandas as pd



def _s(v, default=0.0):
    try:
        f = float(v)
        return default if np.isnan(f) else f
    except Exception:  # noqa: BLE001
        return default


def analyze(df: pd.DataFrame) -> dict:
    """
    输入：compute_all 处理后的日线表
    输出：评分、信号、各维度明细、要点列表
    """
    if df is None or len(df) < 30:
        raise ValueError("数据不足，至少需要 30 个交易日")

    d = ind.compute_all(df)
    last = d.iloc[-1]
    prev = d.iloc[-2]

    close = _s(last["close"])
    details: list[dict] = []
    bull: list[str] = []
    bear: list[str] = []

    # ---------------------------------------------------------------- 1 趋势 (30)
    trend_score = 0.0
    ma20, ma60, ma120 = _s(last.get("ma20")), _s(last.get("ma60")), _s(last.get("ma120"))
    ma20_prev = _s(prev.get("ma20"))

    if ma20 and close > ma20:
        trend_score += 12
        bull.append(f"股价站上20日均线（{ma20:.2f}），短期趋势偏强")
    else:
        bear.append(f"股价位于20日均线（{ma20:.2f}）下方，短期承压")
    if ma60 and close > ma60:
        trend_score += 8
        bull.append(f"站上60日均线（{ma60:.2f}），中期趋势向好")
    else:
        bear.append(f"跌破60日均线（{ma60:.2f}），中期趋势偏弱")
    if ma120 and close > ma120:
        trend_score += 5
        bull.append("站上半年线，长期趋势健康")
    else:
        bear.append("位于半年线下方，长期趋势待修复")
    if ma20 and ma20_prev and ma20 > ma20_prev:
        trend_score += 5
        bull.append("20日均线向上拐头，趋势动能增强")
    else:
        bear.append("20日均线走平或下行")

    # 多头排列
    if ma20 and ma60 and ma120 and ma20 > ma60 > ma120:
        trend_score = min(trend_score + 5, 30)
        bull.append("均线多头排列，趋势结构良好")

    trend_score = max(0.0, min(30.0, trend_score))
    details.append({"name": "趋势", "score": trend_score, "max": 30,
                    "desc": "均线系统与趋势方向"})

    # ---------------------------------------------------------------- 2 动量 (25)
    mom_score = 0.0
    dif, dea, macd_hist = _s(last.get("dif")), _s(last.get("dea")), _s(last.get("macd"))
    dif_prev, dea_prev = _s(prev.get("dif")), _s(prev.get("dea"))

    if dif > dea:
        mom_score += 9
        bull.append("MACD 金叉状态，多头动能占优")
    else:
        bear.append("MACD 死叉状态，空头动能占优")
    if macd_hist > 0 and _s(prev.get("macd")) <= macd_hist:
        mom_score += 4
        bull.append("MACD 红柱放大，动能持续增强")
    elif macd_hist < 0 and _s(prev.get("macd")) >= macd_hist:
        bear.append("MACD 绿柱放大，下跌动能未衰减")
    if dif > 0:
        mom_score += 3
    if dif > dif_prev and dea > dea_prev:
        mom_score += 2

    rsi14 = _s(last.get("rsi14"), 50)
    if 50 <= rsi14 < 70:
        mom_score += 5
        bull.append(f"RSI14 = {rsi14:.1f}，处于强势但未超买区间")
    elif rsi14 >= 70:
        mom_score += 1
        bear.append(f"RSI14 = {rsi14:.1f}，进入超买区，短线有回调风险")
    elif 30 < rsi14 < 50:
        mom_score += 2
    else:
        mom_score += 1
        bear.append(f"RSI14 = {rsi14:.1f}，处于超卖区，或有反弹机会")

    k, dd, jv = _s(last.get("k"), 50), _s(last.get("d"), 50), _s(last.get("j"), 50)
    if k > dd:
        mom_score += 2
    if jv > 100:
        bear.append("KDJ 的 J 值大于 100，短线过热")
    elif jv < 0:
        bull.append("KDJ 的 J 值小于 0，短线超卖")

    mom_score = max(0.0, min(25.0, mom_score))
    details.append({"name": "动量", "score": mom_score, "max": 25,
                    "desc": "MACD / RSI / KDJ 动能"})

    # ---------------------------------------------------------------- 3 均线 (20)
    ma_score = 0.0
    above = sum(1 for n in (5, 10, 20, 60) if _s(last.get(f"ma{n}")) and close > _s(last.get(f"ma{n}")))
    ma_score += above * 4
    if above >= 3:
        bull.append(f"股价位于 {above}/4 条主要均线上方，短期强势")
    elif above <= 1:
        bear.append(f"仅站上 {above}/4 条主要均线，短线偏弱")

    # 乖离率：过大提示回归风险
    bias6 = _s(last.get("bias6"))
    if abs(bias6) < 5:
        ma_score += 4
    elif bias6 > 8:
        bear.append(f"6日乖离率 +{bias6:.1f}%，偏离均线过远，有回踩需求")
    elif bias6 < -8:
        bull.append(f"6日乖离率 {bias6:.1f}%，超跌明显，存在修复动能")
    else:
        ma_score += 2

    ma_score = max(0.0, min(20.0, ma_score))
    details.append({"name": "均线", "score": ma_score, "max": 20,
                    "desc": "均线位置与乖离程度"})

    # ---------------------------------------------------------------- 4 量能 (15)
    vol_score = 0.0
    vol_ratio = _s(last.get("vol_ratio"), 1.0)
    pct = _s(last.get("pct"))

    if vol_ratio >= 1.5 and pct > 0:
        vol_score += 10
        bull.append(f"放量上涨（量比 {vol_ratio:.2f}），资金进场积极")
    elif vol_ratio >= 1.5 and pct < 0:
        vol_score += 3
        bear.append(f"放量下跌（量比 {vol_ratio:.2f}），抛压较重")
    elif vol_ratio < 0.8 and pct > 0:
        vol_score += 6
        bull.append("缩量上涨，抛压较轻")
    elif vol_ratio < 0.8 and pct < 0:
        vol_score += 5
        bull.append("缩量下跌，下跌动能有限")
    else:
        vol_score += 6

    # OBV 趋势
    obv_now = _s(last.get("obv"))
    obv_ma = d["obv"].tail(20).mean()
    if obv_now > obv_ma:
        vol_score += 5
        bull.append("OBV 能量潮向上，量能配合良好")
    else:
        bear.append("OBV 能量潮走弱，量价背离")

    vol_score = max(0.0, min(15.0, vol_score))
    details.append({"name": "量能", "score": vol_score, "max": 15,
                    "desc": "成交量配合与资金意愿"})

    # ---------------------------------------------------------------- 5 位置/波动 (10)
    pos_score = 0.0
    hi = d["high"].tail(120).max()
    lo = d["low"].tail(120).min()
    if hi > lo:
        pos = (close - lo) / (hi - lo)
    else:
        pos = 0.5
    # 中低位更安全，高位追涨风险大
    if 0.3 <= pos <= 0.7:
        pos_score += 6
        bull.append(f"处于120日区间 {pos*100:.0f}% 的中位区域，进可攻退可守")
    elif pos < 0.3:
        pos_score += 5
        bull.append(f"处于120日区间低位（{pos*100:.0f}%），估值安全边际较高")
    else:
        pos_score += 3
        bear.append(f"处于120日区间高位（{pos*100:.0f}%），追高风险偏大")

    boll_up, boll_low, boll_mid = _s(last.get("boll_up")), _s(last.get("boll_low")), _s(last.get("boll_mid"))
    if boll_up and close > boll_up:
        bear.append("股价突破布林上轨，短线可能超涨")
    elif boll_low and close < boll_low:
        bull.append("股价跌破布林下轨，超跌反弹概率上升")
    else:
        pos_score += 4

    atr = _s(last.get("atr"))
    vol_pct = (atr / close * 100) if close else 0
    pos_score = max(0.0, min(10.0, pos_score))
    details.append({"name": "位置", "score": pos_score, "max": 10,
                    "desc": "区间位置与波动率"})

    # ---------------------------------------------------------------- 汇总
    total = round(sum(x["score"] for x in details), 1)

    if total >= 80:
        level, signal, color = "强烈看多", "买入", "#e63946"
    elif total >= 65:
        level, signal, color = "偏多", "增持", "#f4a261"
    elif total >= 45:
        level, signal, color = "中性", "持有观望", "#8d99ae"
    elif total >= 30:
        level, signal, color = "偏空", "减持", "#457b9d"
    else:
        level, signal, color = "强烈看空", "卖出", "#1d3557"

    sr = ind.find_support_resistance(d)

    return {
        "score": total,
        "level": level,
        "signal": signal,
        "color": color,
        "dimensions": details,
        "bullish": bull,
        "bearish": bear,
        "support_resistance": sr,
        "snapshot": {
            "close": round(close, 2),
            "pct": round(pct, 2),
            "ma5": round(_s(last.get("ma5")), 2),
            "ma10": round(_s(last.get("ma10")), 2),
            "ma20": round(ma20, 2),
            "ma60": round(ma60, 2),
            "ma120": round(ma120, 2),
            "dif": round(dif, 3),
            "dea": round(dea, 3),
            "macd": round(macd_hist, 3),
            "k": round(k, 1), "d": round(dd, 1), "j": round(jv, 1),
            "rsi6": round(_s(last.get("rsi6"), 50), 1),
            "rsi14": round(rsi14, 1),
            "boll_up": round(boll_up, 2),
            "boll_mid": round(boll_mid, 2),
            "boll_low": round(boll_low, 2),
            "atr": round(atr, 3),
            "vol_ratio": round(vol_ratio, 2),
            "bias6": round(bias6, 2),
            "position_120": round(pos * 100, 1),
        },
    }



# ============================================================================
# [原 app/core/market.py]
# ============================================================================

# -*- coding: utf-8 -*-
"""
市场数据层：新闻 / 热点板块 / 榜单
====================================
与 datasource.py（行情+基本面）分离，专注"资讯与热点"类数据。

实测可用源（本环境）：
  ✅ 新浪全球财经快讯   ak.stock_info_global_sina
  ✅ 东财全球快讯       ak.stock_info_global_em
  ✅ 新浪行业/概念/地域板块  ak.stock_sector_spot
  ✅ 新浪板块成分股      ak.stock_sector_detail
  ❌ 东财 push2 系（热点榜/板块/资金流）—— 本环境被拦截
未覆盖的部分用"新浪快照本地计算"替代（涨幅榜/成交额榜）。
"""

import logging
import re
from datetime import datetime

import pandas as pd


logger = logging.getLogger(__name__)


# ----------------------------------------------------------------------------
# 1. 金融新闻
# ----------------------------------------------------------------------------
def get_news(limit: int = 40) -> list[dict]:
    """聚合新闻快讯，按时间倒序。多源合并 + 去重。"""
    items: list[dict] = []

    # 源1：东财全球快讯（含摘要，200条）
    try:
        df = cached_frame("news_em", lambda: _em_news(), ttl=300, disk_ttl=3600)
        for _, r in df.head(limit).iterrows():
            items.append({
                "title": str(r.get("标题", "")).strip(),
                "summary": str(r.get("摘要", "")).strip(),
                "time": str(r.get("发布时间", "")).strip(),
                "url": str(r.get("链接", "")).strip(),
                "source": "东方财富",
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("东财快讯失败: %s", str(e)[:100])

    # 源2：新浪全球快讯
    try:
        df2 = cached_frame("news_sina", lambda: _sina_news(), ttl=300, disk_ttl=3600)
        for _, r in df2.head(limit).iterrows():
            content = str(r.get("内容", "")).strip()
            content = re.sub(r"\s+", " ", content)
            items.append({
                "title": content[:70] + ("…" if len(content) > 70 else ""),
                "summary": content,
                "time": str(r.get("时间", "")).strip(),
                "url": "",
                "source": "新浪财经",
            })
    except Exception as e:  # noqa: BLE001
        logger.warning("新浪快讯失败: %s", str(e)[:100])

    # 去重（同标题）并按时间倒序
    seen, uniq = set(), []
    for it in items:
        key = it["title"][:40]
        if key and key not in seen:
            seen.add(key)
            uniq.append(it)

    uniq.sort(key=lambda x: _time_key(x.get("time", "")), reverse=True)
    return uniq[:limit]


def _em_news() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_info_global_em()


def _sina_news() -> pd.DataFrame:
    import akshare as ak
    return ak.stock_info_global_sina()


def _time_key(t: str) -> str:
    """把各种时间格式规整成可排序字符串。"""
    t = (t or "").strip()
    m = re.search(r"(\d{1,2}):(\d{2})(?::(\d{2}))?", t)
    if m:
        h, mi = m.group(1).zfill(2), m.group(2)
        return f"{h}:{mi}"
    m2 = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2})", t)
    if m2:
        return f"{m2.group(4)}:{m2.group(5)}"
    return t


# ----------------------------------------------------------------------------
# 2. 热点板块
# ----------------------------------------------------------------------------
_SECTOR_CACHE: dict[str, pd.DataFrame] = {}


def get_sectors(kind: str = "industry", top: int = 12) -> list[dict]:
    """
    热点板块排行。kind: industry(行业) / concept(概念)
    按涨跌幅排序，返回领涨板块。
    """
    indicator = {"industry": "新浪行业", "concept": "概念", "region": "地域"}.get(kind, "新浪行业")
    try:
        df = cached_frame(f"sector_{kind}", lambda: _sector_spot(indicator),
                          ttl=600, disk_ttl=3600)
    except Exception as e:  # noqa: BLE001
        logger.warning("板块获取失败 %s: %s", kind, str(e)[:100])
        return []

    if df is None or len(df) == 0:
        return []

    df = df.copy()
    df["涨跌幅"] = pd.to_numeric(df.get("涨跌幅"), errors="coerce")
    df["总成交额"] = pd.to_numeric(df.get("总成交额"), errors="coerce")
    df = df.dropna(subset=["涨跌幅"]).sort_values("涨跌幅", ascending=False)
    _SECTOR_CACHE[kind] = df

    out = []
    for _, r in df.head(top).iterrows():
        out.append({
            "name": str(r.get("板块", "")),
            "label": str(r.get("label", "")),
            "pct": round(_f(r.get("涨跌幅")) or 0, 2),
            "count": int(_f(r.get("公司家数")) or 0),
            "avg_price": round(_f(r.get("平均价格")) or 0, 2),
            "amount": _f(r.get("总成交额")),
            "leader": str(r.get("领涨股票", "") or ""),
            "leader_pct": round(_f(r.get("涨跌幅")) or 0, 2),
        })
    return out


def _sector_spot(indicator: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_sector_spot(indicator=indicator)


def get_sector_stocks(label: str, top: int = 20) -> list[dict]:
    """获取某板块的成分股（按涨跌幅排序）。"""
    if not label:
        return []
    try:
        df = cached_frame(f"sector_stock_{label}", lambda: _sector_detail(label),
                          ttl=600, disk_ttl=3600)
    except Exception as e:  # noqa: BLE001
        logger.warning("板块成分股失败 %s: %s", label, str(e)[:100])
        return []

    if df is None or len(df) == 0:
        return []
    df = df.copy()
    df["changepercent"] = pd.to_numeric(df.get("changepercent"), errors="coerce")
    df = df.dropna(subset=["changepercent"]).sort_values("changepercent", ascending=False)
    out = []
    for _, r in df.head(top).iterrows():
        code = str(r.get("code", ""))
        out.append({
            "code": code,
            "name": str(r.get("name", "")),
            "price": round(_f(r.get("trade")) or 0, 2),
            "pct": round(_f(r.get("changepercent")) or 0, 2),
        })
    return out


def _sector_detail(label: str) -> pd.DataFrame:
    import akshare as ak
    return ak.stock_sector_detail(sector=label)


# ----------------------------------------------------------------------------
# 3. 市场榜单（涨幅榜 / 成交额榜 / 活跃榜）—— 基于新浪全市场快照本地计算
# ----------------------------------------------------------------------------
def get_rankings(top: int = 10, exclude_st: bool = True) -> dict:
    """
    从全市场快照计算各类榜单，替代被拦截的东财人气榜。
    """
    try:
        spot = _get_a_spot_all()
    except Exception as e:  # noqa: BLE001
        logger.warning("快照获取失败: %s", str(e)[:100])
        return {"gainers": [], "amount": [], "active": [], "losers": []}

    df = spot.copy()
    for col in ("最新价", "涨跌幅", "成交额", "成交量", "换手率", "量比"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")

    if exclude_st and "名称" in df.columns:
        df = df[~df["名称"].astype(str).str.contains("ST|退", na=False)]
    # 过滤停牌/无价
    df = df[(df["最新价"].notna()) & (df["最新价"] > 0)]

    def pack(sub, n=top):
        out = []
        for _, r in sub.head(n).iterrows():
            code = str(r.get("代码", ""))
            out.append({
                "code": code[2:] if len(code) > 6 else code,
                "ak": code,
                "name": str(r.get("名称", "")),
                "price": round(_f(r.get("最新价")) or 0, 2),
                "pct": round(_f(r.get("涨跌幅")) or 0, 2),
                "amount": _f(r.get("成交额")),
                "turnover": round(_f(r.get("换手率")) or 0, 2) if "换手率" in df.columns else None,
                "vol_ratio": round(_f(r.get("量比")) or 0, 2) if "量比" in df.columns else None,
            })
        return out

    gainers = pack(df.sort_values("涨跌幅", ascending=False))
    losers = pack(df.sort_values("涨跌幅", ascending=True))
    by_amount = pack(df.sort_values("成交额", ascending=False))
    by_turnover = pack(df.sort_values("换手率", ascending=False)) if "换手率" in df.columns else []

    return {"gainers": gainers, "losers": losers, "amount": by_amount, "active": by_turnover}


# ----------------------------------------------------------------------------
# 4. 市场情绪概览
# ----------------------------------------------------------------------------
def get_market_sentiment() -> dict:
    """基于快照统计涨跌家数，反映市场情绪。"""
    try:
        spot = _get_a_spot_all()
        s = pd.to_numeric(spot["涨跌幅"], errors="coerce").dropna()
        up = int((s > 0).sum())
        down = int((s < 0).sum())
        flat = int((s == 0).sum())
        limit_up = int((s >= 9.8).sum())
        limit_down = int((s <= -9.8).sum())
        total = len(s)
        return {
            "up": up, "down": down, "flat": flat, "total": total,
            "limit_up": limit_up, "limit_down": limit_down,
            "up_ratio": round(up / total * 100, 1) if total else 0,
            "mood": _mood(up, down, limit_up),
        }
    except Exception as e:  # noqa: BLE001
        logger.warning("情绪统计失败: %s", str(e)[:100])
        return {}


def _mood(up: int, down: int, limit_up: int) -> str:
    if up == 0 and down == 0:
        return "数据缺失"
    ratio = up / max(up + down, 1)
    if ratio > 0.7 and limit_up > 30:
        return "极度活跃"
    if ratio > 0.6:
        return "偏强"
    if ratio > 0.45:
        return "震荡"
    if ratio > 0.3:
        return "偏弱"
    return "低迷"



# ============================================================================
# [原 app/core/screener.py]
# ============================================================================

# -*- coding: utf-8 -*-
"""
技术选股引擎（Screener）
--------------------------
按技术指标条件在全市场范围内筛选股票。

设计要点：
  - 全市场逐只拉取日线成本高，因此采用「预筛 + 精算」两阶段：
      阶段1：用实时快照按价格/涨跌幅/成交额等做初筛，缩小候选池
      阶段2：对候选池逐只计算指标，套用条件
  - 条件以声明式描述（dict）传入，便于前端组合与后端复用
  - 并发拉取，带缓存，控制总耗时

支持的条件（condition key）：
  ma_bull         均线多头排列 (MA5>MA10>MA20)
  above_ma20      收盘价站上 MA20
  above_ma60      收盘价站上 MA60
  macd_golden     MACD 金叉（DIF>DEA）
  macd_cross_up   当日 MACD 金叉（昨日死叉今日金叉）
  kdj_golden      KDJ 金叉（K>D）
  kdj_oversold    KDJ 超卖（J<0）
  rsi_range       RSI14 在 [lo, hi] 区间
  vol_surge       量比 > 阈值
  boll_break_up   突破布林上轨
  boll_touch_low  触及布林下轨
  price_up_n      近 n 日累计上涨
  new_high_n      创 n 日新高
  turnover_range  换手率区间
  score_above     综合评分高于阈值
"""

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd


logger = logging.getLogger(__name__)

# 预设选股策略（前端一键套用）
PRESETS = {
    "macd_golden": {
        "name": "MACD 金叉",
        "desc": "MACD 的 DIF 上穿 DEA，动能转多",
        "conditions": {"macd_cross_up": True},
    },
    "ma_bull": {
        "name": "均线多头排列",
        "desc": "MA5 > MA10 > MA20，短期趋势向上",
        "conditions": {"ma_bull": True},
    },
    "volume_surge": {
        "name": "放量上攻",
        "desc": "量比 > 1.5 且当日上涨",
        "conditions": {"vol_surge": 1.5, "day_up": True},
    },
    "oversold_rebound": {
        "name": "超跌反弹",
        "desc": "KDJ 超卖 + 触及布林下轨",
        "conditions": {"kdj_oversold": True, "boll_touch_low": True},
    },
    "strong_trend": {
        "name": "强势趋势股",
        "desc": "站上 MA20/MA60 且均线多头排列",
        "conditions": {"above_ma20": True, "above_ma60": True, "ma_bull": True},
    },
    "high_score": {
        "name": "高评分标的",
        "desc": "综合技术评分 ≥ 70",
        "conditions": {"score_above": 70},
    },
    "breakout": {
        "name": "突破新高",
        "desc": "创 60 日新高且放量",
        "conditions": {"new_high_n": 60, "vol_surge": 1.2},
    },
}


def list_presets() -> list[dict]:
    return [{"key": k, **v} for k, v in PRESETS.items()]


def screen(conditions: dict, market: str = "A", pool_size: int = 120,
           max_results: int = 50, min_amount: float = 1e8) -> dict:
    """
    执行选股。

    参数:
      conditions  条件字典，见模块文档
      pool_size   精算阶段最多处理的候选股数量（控制耗时）
      min_amount  成交额下限（元），过滤流动性差的标的
    """
    conditions = conditions or {}

    # ---------- 阶段1：预筛候选池 ----------
    try:
        spot = _get_a_spot_all()
    except Exception as e:  # noqa: BLE001
        return {"error": f"行情快照获取失败: {str(e)[:100]}", "results": []}

    df = spot.copy()
    for col in ("最新价", "涨跌幅", "成交额", "换手率"):
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    if "名称" in df.columns:
        df = df[~df["名称"].astype(str).str.contains("ST|退|N ", na=False)]
    df = df[(df["最新价"].notna()) & (df["最新价"] >= 2)]
    if min_amount and "成交额" in df.columns:
        df = df[df["成交额"] >= min_amount]

    # 按成交额降序取候选池（流动性优先，兼顾速度）
    if "成交额" in df.columns:
        df = df.sort_values("成交额", ascending=False)
    candidates = df.head(pool_size)

    # ---------- 阶段2：逐只精算指标 ----------
    results = []
    codes = [str(r["代码"]) for _, r in candidates.iterrows()]

    def worker(code: str):
        try:
            hist, info = ds.get_history(code, "A", days=260)
            if len(hist) < 60:
                return None
            d = ind.compute_all(hist)
            ok, reasons = _match(d, conditions)
            if not ok:
                return None
            try:
                ana = scoring.analyze(hist)
                score = ana["score"]
                level = ana["level"]
            except Exception:  # noqa: BLE001
                score, level = None, ""
            last = d.iloc[-1]
            return {
                "code": info["code"],
                "display": info["display"],
                "name": info.get("name", info["code"]),
                "close": round(_f(last["close"]) or 0, 2),
                "pct": round(_f(last.get("pct")) or 0, 2),
                "score": score,
                "level": level,
                "vol_ratio": round(_f(last.get("vol_ratio")) or 0, 2),
                "rsi14": round(_f(last.get("rsi14")) or 0, 1),
                "reasons": reasons,
            }
        except Exception as e:  # noqa: BLE001
            logger.debug("选股失败 %s: %s", code, str(e)[:60])
            return None

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = [ex.submit(worker, c) for c in codes]
        for f in as_completed(futs):
            r = f.result()
            if r:
                results.append(r)

    results.sort(key=lambda x: (x.get("score") or 0), reverse=True)

    return {
        "total_scanned": len(codes),
        "matched": len(results),
        "results": results[:max_results],
        "conditions": conditions,
    }


def _match(d: pd.DataFrame, cond: dict) -> tuple[bool, list[str]]:
    """
    判断单只股票是否满足全部条件，并返回命中理由。
    """
    last = d.iloc[-1]
    prev = d.iloc[-2] if len(d) > 1 else last
    close = _f(last["close"]) or 0
    reasons = []

    def g(row, col, default=0.0):
        return _f(row.get(col)) if _f(row.get(col)) is not None else default

    # ---- 均线类 ----
    if cond.get("ma_bull"):
        ma5, ma10, ma20 = g(last, "ma5"), g(last, "ma10"), g(last, "ma20")
        if not (ma5 > ma10 > ma20 > 0):
            return False, []
        reasons.append(f"均线多头排列(MA5 {ma5:.2f}>MA10 {ma10:.2f}>MA20 {ma20:.2f})")

    if cond.get("above_ma20"):
        ma20 = g(last, "ma20")
        if not (ma20 and close > ma20):
            return False, []
        reasons.append(f"站上MA20({ma20:.2f})")

    if cond.get("above_ma60"):
        ma60 = g(last, "ma60")
        if not (ma60 and close > ma60):
            return False, []
        reasons.append(f"站上MA60({ma60:.2f})")

    # ---- MACD 类 ----
    if cond.get("macd_golden"):
        if not (g(last, "dif") > g(last, "dea")):
            return False, []
        reasons.append("MACD 处于金叉状态")

    if cond.get("macd_cross_up"):
        dif, dea = g(last, "dif"), g(last, "dea")
        pdif, pdea = g(prev, "dif"), g(prev, "dea")
        if not (dif > dea and pdif <= pdea):
            return False, []
        reasons.append("MACD 当日金叉")

    # ---- KDJ 类 ----
    if cond.get("kdj_golden"):
        if not (g(last, "k", 50) > g(last, "d", 50)):
            return False, []
        reasons.append(f"KDJ 金叉(K {g(last,'k',50):.1f}>D {g(last,'d',50):.1f})")

    if cond.get("kdj_oversold"):
        jv = g(last, "j", 50)
        if not (jv < 0):
            return False, []
        reasons.append(f"KDJ 超卖(J={jv:.1f})")

    # ---- RSI ----
    if "rsi_range" in cond:
        lo, hi = cond["rsi_range"]
        r = g(last, "rsi14", 50)
        if not (lo <= r <= hi):
            return False, []
        reasons.append(f"RSI14={r:.1f} 在[{lo},{hi}]区间")

    # ---- 量能 ----
    if cond.get("vol_surge"):
        thr = float(cond["vol_surge"])
        vr = g(last, "vol_ratio", 1)
        if not (vr > thr):
            return False, []
        reasons.append(f"量比{vr:.2f}>{thr}")

    if cond.get("day_up"):
        if not (g(last, "pct") > 0):
            return False, []
        reasons.append(f"当日上涨{g(last,'pct'):.2f}%")

    # ---- 布林 ----
    if cond.get("boll_break_up"):
        up = g(last, "boll_up")
        if not (up and close > up):
            return False, []
        reasons.append(f"突破布林上轨({up:.2f})")

    if cond.get("boll_touch_low"):
        low = g(last, "boll_low")
        if not (low and close <= low * 1.01):
            return False, []
        reasons.append(f"触及布林下轨({low:.2f})")

    # ---- 涨跌幅 / 新高 ----
    if cond.get("price_up_n"):
        n = int(cond["price_up_n"])
        if len(d) <= n:
            return False, []
        chg = (close / (_f(d["close"].iloc[-n - 1]) or close) - 1) * 100
        if not (chg > 0):
            return False, []
        reasons.append(f"近{n}日涨{chg:.1f}%")

    if cond.get("new_high_n"):
        n = int(cond["new_high_n"])
        seg = d["high"].tail(n)
        if not (len(seg) >= n and close >= float(seg.max()) * 0.99):
            return False, []
        reasons.append(f"创{n}日新高")

    # ---- 换手率 ----
    if "turnover_range" in cond:
        lo, hi = cond["turnover_range"]
        t = g(last, "turnover_pct")
        if not (lo <= t <= hi):
            return False, []
        reasons.append(f"换手率{t:.2f}%")

    # ---- 综合评分 ----
    if cond.get("score_above") is not None:
        try:
            s = scoring.analyze(d)["score"]
        except Exception:  # noqa: BLE001
            return False, []
        if not (s >= float(cond["score_above"])):
            return False, []
        reasons.append(f"综合评分{s:.0f}")

    return True, reasons



# ----------------------------------------------------------------
# 模块别名：原各 core 模块彼此 import，合并为单文件后统一指向本命名空间
# ----------------------------------------------------------------
_merged_mod = __import__(__name__)
ds = _merged_mod
ind = _merged_mod
scoring = _merged_mod
mkt = _merged_mod
screener = _merged_mod
logger_ds = logging.getLogger('stocklens.datasource')


# ============================================================================
# [前端页面 —— 原 index.html + style.css + app.js 内联]
# ============================================================================
PAGE_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>StockLens · 股票分析体系</title>
<style>
:root {
  --bg: #0e1116;
  --bg-soft: #151a21;
  --card: #1a2029;
  --card-hi: #212936;
  --border: #2a3340;
  --text: #e6edf3;
  --text-dim: #8b98a8;
  --text-mute: #5d6775;
  --accent: #4f8cff;
  --accent-soft: rgba(79, 140, 255, .12);
  --up: #ff4d5e;
  --down: #22c55e;
  --warn: #f5a623;
  --radius: 12px;
  --shadow: 0 2px 12px rgba(0, 0, 0, .35);
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  background: var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", "Microsoft YaHei",
               "Segoe UI", Roboto, sans-serif;
  font-size: 13px;
  line-height: 1.5;
  min-height: 100vh;
}

/* ================================================================ topbar */
.topbar {
  display: flex; align-items: center; gap: 24px;
  padding: 12px 22px;
  background: linear-gradient(180deg, #141a22, #10151c);
  border-bottom: 1px solid var(--border);
  position: sticky; top: 0; z-index: 100;
}
.brand { display: flex; align-items: baseline; gap: 8px; white-space: nowrap; }
.logo { color: var(--accent); font-size: 18px; }
.brand-name { font-size: 17px; font-weight: 700; letter-spacing: .5px; }
.brand-sub { font-size: 11px; color: var(--text-mute); }
.search-wrap { position: relative; flex: 1; max-width: 560px; display: flex; gap: 8px; }
#searchInput {
  flex: 1; padding: 9px 14px;
  background: var(--bg-soft); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text); font-size: 13px; outline: none;
  transition: border-color .15s;
}
#searchInput:focus { border-color: var(--accent); box-shadow: 0 0 0 3px var(--accent-soft); }
#searchBtn {
  padding: 9px 20px; background: var(--accent); color: #fff;
  border: none; border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
}
#searchBtn:hover { filter: brightness(1.12); }
.topbar-actions { display: flex; gap: 8px; }
.ghost-btn {
  padding: 8px 14px; background: transparent; border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-dim); cursor: pointer; font-size: 12px;
  white-space: nowrap;
}
.ghost-btn:hover { border-color: var(--accent); color: var(--accent); }
.suggest {
  position: absolute; top: 44px; left: 0; right: 60px;
  background: var(--card-hi); border: 1px solid var(--border);
  border-radius: 8px; box-shadow: var(--shadow); overflow: hidden; z-index: 200;
}
.suggest.hidden { display: none; }
.suggest-item {
  padding: 9px 14px; display: flex; justify-content: space-between; align-items: center;
  cursor: pointer; border-bottom: 1px solid rgba(255,255,255,.04);
}
.suggest-item:last-child { border-bottom: none; }
.suggest-item:hover { background: var(--accent-soft); }
.suggest-item .s-code { color: var(--text-mute); font-size: 11px; margin-left: 8px; }

/* ================================================================ ticker */
.ticker-tape {
  display: flex; gap: 26px; padding: 8px 22px;
  background: #10151c; border-bottom: 1px solid var(--border);
  overflow-x: auto; white-space: nowrap; font-size: 12px;
}
.tick { display: inline-flex; gap: 7px; align-items: baseline; }
.tick .t-name { color: var(--text-dim); }
.tick .t-val { font-weight: 600; font-variant-numeric: tabular-nums; }
.up { color: var(--up); }
.down { color: var(--down); }
.flat { color: var(--text-dim); }

/* ================================================================ board nav */
.board-nav {
  display: flex; gap: 8px; padding: 12px 22px 0;
  background: var(--bg); border-bottom: 1px solid var(--border);
  overflow-x: auto;
}
.bnav {
  display: flex; align-items: center; gap: 8px;
  padding: 10px 20px; background: transparent; border: none;
  border-bottom: 2px solid transparent; color: var(--text-dim);
  font-size: 13px; font-weight: 600; cursor: pointer; white-space: nowrap;
  transition: color .15s, border-color .15s;
}
.bnav:hover { color: var(--text); }
.bnav.active { color: var(--accent); border-bottom-color: var(--accent); }
.bn-ico { font-size: 15px; }

/* ================================================================ board area */
.board-area { padding: 16px 22px; }
.board { display: none; }
.board.active { display: block; animation: fadeIn .22s ease; }
@keyframes fadeIn { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }

.board-head {
  display: flex; justify-content: space-between; align-items: center;
  margin-bottom: 14px; flex-wrap: wrap; gap: 10px;
}
.board-head h2 { font-size: 16px; font-weight: 700; letter-spacing: .3px; }
.head-tools { display: flex; align-items: center; gap: 10px; }
.head-note { font-size: 12px; color: var(--text-mute); }
.live-dot {
  width: 7px; height: 7px; border-radius: 50%; background: var(--down);
  box-shadow: 0 0 0 0 rgba(34,197,94,.7); animation: pulse 2s infinite;
}
@keyframes pulse {
  0% { box-shadow: 0 0 0 0 rgba(34,197,94,.6); }
  70% { box-shadow: 0 0 0 7px rgba(34,197,94,0); }
  100% { box-shadow: 0 0 0 0 rgba(34,197,94,0); }
}

/* ================================================================ card */
.card {
  background: var(--card); border: 1px solid var(--border);
  border-radius: var(--radius); padding: 14px 16px; box-shadow: var(--shadow);
}
.card-title {
  font-size: 12px; font-weight: 600; color: var(--text-dim);
  letter-spacing: .6px; text-transform: uppercase; margin-bottom: 12px;
  padding-left: 8px; border-left: 3px solid var(--accent); line-height: 1.1;
}
.card-title .dim { text-transform: none; letter-spacing: 0; font-weight: 400; color: var(--text-mute); }
.ph { color: var(--text-mute); font-size: 12px; }

/* ================================================================ 板块1 新闻 */
.news-layout { display: grid; grid-template-columns: minmax(0, 1fr) 330px; gap: 14px; align-items: start; }
@media (max-width: 1200px) { .news-layout { grid-template-columns: 1fr; } }
.news-side { display: flex; flex-direction: column; gap: 14px; }
.news-filter { display: flex; gap: 6px; margin-bottom: 12px; }
.nf {
  padding: 5px 14px; border: 1px solid var(--border); background: transparent;
  border-radius: 6px; color: var(--text-dim); font-size: 12px; cursor: pointer;
}
.nf.active { background: var(--accent-soft); color: var(--accent); border-color: rgba(79,140,255,.35); }
.news-list { list-style: none; display: flex; flex-direction: column; max-height: 660px; overflow-y: auto; }
.news-list li {
  padding: 12px 4px; border-bottom: 1px solid rgba(255,255,255,.045); cursor: pointer;
}
.news-list li:last-child { border-bottom: none; }
.news-list li:hover { background: rgba(255,255,255,.02); }
.news-meta { display: flex; gap: 10px; align-items: center; margin-bottom: 5px; font-size: 11px; }
.news-src {
  padding: 1px 7px; border-radius: 4px; background: var(--accent-soft);
  color: var(--accent); font-weight: 600;
}
.news-time { color: var(--text-mute); font-variant-numeric: tabular-nums; }
.news-title { font-size: 13px; font-weight: 600; line-height: 1.5; margin-bottom: 4px; }
.news-summary { font-size: 12px; color: var(--text-dim); line-height: 1.6; }
.news-list li a { color: inherit; text-decoration: none; }

/* ================================================================ 榜单 */
.rank-list { list-style: none; display: flex; flex-direction: column; gap: 2px; }
.rank-list li {
  display: flex; align-items: center; gap: 8px; padding: 7px 6px;
  border-radius: 6px; font-size: 12px; cursor: pointer;
}
.rank-list li:hover { background: var(--accent-soft); }
.rank-idx {
  width: 18px; height: 18px; border-radius: 4px; flex-shrink: 0;
  display: flex; align-items: center; justify-content: center;
  font-size: 10px; font-weight: 700; background: var(--bg-soft); color: var(--text-mute);
}
.rank-idx.top { background: var(--up); color: #fff; }
.rank-name { flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.rank-code { color: var(--text-mute); font-size: 10.5px; }
.rank-val { text-align: right; font-variant-numeric: tabular-nums; font-weight: 600; }

/* ================================================================ 情绪 */
.sentiment-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.senti-item { background: var(--bg-soft); border-radius: 7px; padding: 9px 11px; }
.senti-item .s-lbl { font-size: 10.5px; color: var(--text-mute); margin-bottom: 3px; }
.senti-item .s-val { font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.senti-mood {
  margin-top: 10px; padding: 9px 12px; border-radius: 7px;
  background: var(--accent-soft); color: var(--accent);
  font-size: 13px; font-weight: 700; text-align: center;
}

/* ================================================================ 板块2 热点 */
.hot-layout { display: grid; grid-template-columns: 400px minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 1200px) { .hot-layout { grid-template-columns: 1fr; } }
.hot-layout-2 { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-top: 14px; }
@media (max-width: 1100px) { .hot-layout-2 { grid-template-columns: 1fr; } }

/* 防 grid/flex 子项被长文本撑破（min-width:auto 是溢出元凶） */
.news-layout > *, .hot-layout > *, .hot-layout-2 > *,
.tech-layout > *, .tech-metrics > *, .news-side > * { min-width: 0; }
.sector-grid, .sector-item, .sector-top, .sector-sub, .rank-name { min-width: 0; }
.sector-sub { flex-wrap: wrap; }
.sector-grid { display: flex; flex-direction: column; gap: 7px; max-height: 520px; overflow-y: auto; }
.sector-item {
  padding: 9px 11px; background: var(--bg-soft); border-radius: 8px;
  cursor: pointer; border-left: 3px solid transparent; transition: .15s;
}
.sector-item:hover { background: var(--card-hi); }
.sector-item.active { border-left-color: var(--accent); background: var(--accent-soft); }
.sector-top { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 5px; }
.sector-name { font-size: 13px; font-weight: 600; }
.sector-pct { font-size: 14px; font-weight: 700; font-variant-numeric: tabular-nums; }
.sector-sub { display: flex; justify-content: space-between; font-size: 11px; color: var(--text-mute); }
.sector-bar { height: 3px; background: var(--bg); border-radius: 2px; margin-top: 6px; overflow: hidden; }
.sector-bar > span { display: block; height: 100%; border-radius: 2px; }

/* ================================================================ 表格 */
.table-wrap { overflow-x: auto; max-height: 520px; overflow-y: auto; }
.data-table { width: 100%; border-collapse: collapse; font-size: 12px; }
.data-table th {
  position: sticky; top: 0; background: var(--card);
  text-align: left; padding: 8px 10px; color: var(--text-mute);
  font-size: 11px; font-weight: 600; border-bottom: 1px solid var(--border);
  white-space: nowrap; z-index: 2;
}
.data-table td {
  padding: 9px 10px; border-bottom: 1px solid rgba(255,255,255,.04);
  white-space: nowrap;
}
.data-table tbody tr { cursor: pointer; }
.data-table tbody tr:hover { background: var(--accent-soft); }
.data-table .num { text-align: right; font-variant-numeric: tabular-nums; }
.data-table .name-cell { font-weight: 600; }
.badge {
  display: inline-block; padding: 2px 8px; border-radius: 4px;
  font-size: 10.5px; font-weight: 700; color: #fff;
}
.chip {
  display: inline-block; padding: 1px 7px; margin: 1px 4px 1px 0;
  border-radius: 4px; background: var(--bg-soft); color: var(--text-dim); font-size: 10.5px;
}
.mini-btn {
  padding: 3px 10px; background: var(--accent-soft); color: var(--accent);
  border: none; border-radius: 5px; font-size: 11px; cursor: pointer; font-weight: 600;
}
.mini-btn:hover { background: rgba(79,140,255,.22); }
.del-btn {
  padding: 3px 9px; background: transparent; color: var(--text-mute);
  border: 1px solid var(--border); border-radius: 5px; font-size: 11px; cursor: pointer;
}
.del-btn:hover { color: var(--up); border-color: var(--up); }
.mini-input {
  padding: 6px 12px; background: var(--bg-soft); border: 1px solid var(--border);
  border-radius: 7px; color: var(--text); font-size: 12px; outline: none; width: 180px;
}
.mini-input:focus { border-color: var(--accent); }

/* ================================================================ 板块4 技术 */
.tech-layout { display: grid; grid-template-columns: 330px minmax(0, 1fr); gap: 14px; align-items: start; }
@media (max-width: 1250px) { .tech-layout { grid-template-columns: 1fr; } }
.tech-left, .tech-right { display: flex; flex-direction: column; gap: 14px; }
.tech-metrics { display: grid; grid-template-columns: repeat(2, 1fr); gap: 14px; }
@media (max-width: 780px) { .tech-metrics { grid-template-columns: 1fr; } }

.stock-head .sh-name { font-size: 19px; font-weight: 700; }
.stock-head .sh-code { font-size: 11px; color: var(--text-mute); margin-left: 8px; font-weight: 400; }
.stock-head .sh-price {
  font-size: 32px; font-weight: 700; margin: 6px 0 2px;
  font-variant-numeric: tabular-nums; line-height: 1.1;
}
.stock-head .sh-chg { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }
.stock-head .sh-extra {
  display: grid; grid-template-columns: repeat(2, 1fr); gap: 6px 14px;
  margin-top: 12px; padding-top: 12px; border-top: 1px solid var(--border);
  font-size: 11.5px;
}
.sh-extra .k { color: var(--text-mute); }
.sh-extra .v { color: var(--text); font-variant-numeric: tabular-nums; }

/* ---- score ---- */
.score-body { display: flex; gap: 10px; align-items: center; }
.gauge-wrap { width: 150px; height: 130px; flex-shrink: 0; }
.gauge-wrap #gauge { width: 100%; height: 100%; }
.score-meta { flex: 1; min-width: 0; }
.signal-badge {
  display: inline-block; padding: 5px 14px; border-radius: 6px;
  font-weight: 700; font-size: 13px; margin-bottom: 10px; color: #fff;
}
.dim-list { display: flex; flex-direction: column; gap: 7px; }
.dim-row { font-size: 11.5px; }
.dim-row .dim-top { display: flex; justify-content: space-between; margin-bottom: 3px; }
.dim-row .dim-name { color: var(--text-dim); }
.dim-row .dim-val { color: var(--text); font-variant-numeric: tabular-nums; }
.dim-bar { height: 4px; background: var(--bg-soft); border-radius: 2px; overflow: hidden; }
.dim-bar > span { display: block; height: 100%; border-radius: 2px; transition: width .5s ease; }

/* ---- support/resistance ---- */
.sr-box .sr-current {
  text-align: center; padding: 8px 0 12px;
  border-bottom: 1px dashed var(--border); margin-bottom: 12px;
}
.sr-box .sr-current .lbl { font-size: 11px; color: var(--text-mute); }
.sr-box .sr-current .val { font-size: 22px; font-weight: 700; font-variant-numeric: tabular-nums; }
.sr-group { margin-bottom: 10px; }
.sr-group .sr-title { font-size: 11px; color: var(--text-mute); margin-bottom: 6px; }
.sr-levels { display: flex; flex-wrap: wrap; gap: 6px; }
.sr-chip { padding: 4px 10px; border-radius: 6px; font-size: 12px; font-variant-numeric: tabular-nums; font-weight: 600; }
.sr-chip.res { background: rgba(255,77,94,.12); color: var(--up); border: 1px solid rgba(255,77,94,.25); }
.sr-chip.sup { background: rgba(34,197,94,.12); color: var(--down); border: 1px solid rgba(34,197,94,.25); }

/* ---- charts ---- */
.chart-card { padding: 12px 14px 8px; }
.chart-toolbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; flex-wrap: wrap; gap: 10px; }
.tabs { display: flex; gap: 4px; background: var(--bg-soft); padding: 3px; border-radius: 7px; }
.tab {
  padding: 5px 13px; border: none; background: transparent;
  color: var(--text-dim); font-size: 12px; border-radius: 5px; cursor: pointer;
}
.tab.active { background: var(--accent); color: #fff; font-weight: 600; }
.indicator-toggles { display: flex; gap: 14px; font-size: 12px; color: var(--text-dim); }
.indicator-toggles label { display: flex; align-items: center; gap: 5px; cursor: pointer; }
.indicator-toggles input { accent-color: var(--accent); }
.chart { width: 100%; }
.chart-tall { height: 340px; }
.chart-short { height: 110px; }
.chart-mid { height: 260px; }

/* ---- reasons ---- */
.tabbar { display: flex; gap: 4px; margin-bottom: 10px; }
.rtab {
  flex: 1; padding: 6px; border: none; border-radius: 6px;
  background: var(--bg-soft); color: var(--text-dim); font-size: 12px; cursor: pointer;
}
.rtab.active { background: var(--accent-soft); color: var(--accent); font-weight: 600; }
.reason-list { list-style: none; display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; }
.reason-list li {
  font-size: 12px; padding: 8px 10px; border-radius: 7px;
  background: var(--bg-soft); line-height: 1.45; border-left: 3px solid transparent;
}
.reason-list li.bull-item { border-left-color: var(--up); }
.reason-list li.bear-item { border-left-color: var(--down); }

/* ---- metrics ---- */
.metric-grid { display: grid; grid-template-columns: repeat(2, 1fr); gap: 8px; }
.metric { background: var(--bg-soft); border-radius: 7px; padding: 8px 10px; }
.metric .m-name { font-size: 10.5px; color: var(--text-mute); margin-bottom: 3px; }
.metric .m-val { font-size: 14px; font-weight: 600; font-variant-numeric: tabular-nums; }

/* ---- screener ---- */
.screener-card { margin-top: 14px; }
.screener-presets { display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 14px; }
.preset-btn {
  padding: 8px 14px; background: var(--bg-soft); border: 1px solid var(--border);
  border-radius: 8px; color: var(--text-dim); cursor: pointer; text-align: left;
  transition: .15s;
}
.preset-btn:hover { border-color: var(--accent); }
.preset-btn.active { background: var(--accent-soft); border-color: var(--accent); color: var(--accent); }
.preset-name { display: block; font-size: 12.5px; font-weight: 600; margin-bottom: 2px; }
.preset-desc { display: block; font-size: 10.5px; color: var(--text-mute); }
.preset-btn.active .preset-desc { color: var(--accent); opacity: .75; }
.screener-toolbar { display: flex; align-items: flex-end; gap: 12px; margin-bottom: 14px; flex-wrap: wrap; }
.scr-opt { display: flex; flex-direction: column; gap: 4px; }
.scr-opt label { font-size: 11px; color: var(--text-mute); }
.scr-opt select {
  padding: 7px 12px; background: var(--bg-soft); border: 1px solid var(--border);
  border-radius: 7px; color: var(--text); font-size: 12px; outline: none;
}
.primary-btn {
  padding: 9px 24px; background: var(--accent); color: #fff; border: none;
  border-radius: 8px; font-size: 13px; font-weight: 600; cursor: pointer;
}
.primary-btn:hover { filter: brightness(1.12); }
.primary-btn:disabled { opacity: .5; cursor: not-allowed; }
.scr-status { font-size: 12px; color: var(--text-mute); padding-bottom: 8px; }

/* ================================================================ misc */
.disclaimer {
  padding: 14px 22px 26px; color: var(--text-mute); font-size: 11.5px;
  text-align: center; line-height: 1.6;
}
.disclaimer b { color: var(--warn); }
.loading-overlay {
  position: fixed; inset: 0; background: rgba(14,17,22,.72);
  display: flex; align-items: center; justify-content: center;
  z-index: 999; backdrop-filter: blur(2px);
}
.loading-overlay.hidden { display: none; }
.spinner {
  width: 38px; height: 38px; border: 3px solid var(--border);
  border-top-color: var(--accent); border-radius: 50%; animation: spin .8s linear infinite;
}
@keyframes spin { to { transform: rotate(360deg); } }
.toast {
  position: fixed; bottom: 26px; left: 50%; transform: translateX(-50%);
  background: var(--card-hi); border: 1px solid var(--accent); color: var(--text);
  padding: 11px 22px; border-radius: 9px; font-size: 13px; z-index: 1000;
  box-shadow: var(--shadow); opacity: 0; transition: opacity .25s;
}
.toast.show { opacity: 1; }

::-webkit-scrollbar { width: 7px; height: 7px; }
::-webkit-scrollbar-thumb { background: #333d4c; border-radius: 4px; }

/* ================================================================
   平板 / 移动端适配（iPad 及其他触屏设备）
   —— 触摸目标 ≥44px、字号放大、布局纵向堆叠、表格横向滚动
   ================================================================ */

/* ---------- iPad 竖屏 (768-834px) 与 iPad 横屏 (1024-1194px) ---------- */
@media (max-width: 1195px) {
  /* 触屏优化：禁用 hover 依赖，加大点击区 */
  .data-table tbody tr:hover { background: transparent; }
  .data-table tbody tr:active { background: var(--accent-soft); }

  /* 顶部栏：搜索独占一行，按钮加大 */
  .topbar {
    flex-wrap: wrap; gap: 10px; padding: 10px 14px;
  }
  .brand { flex: 0 0 auto; }
  .brand-sub { display: none; }
  .search-wrap { flex: 1 1 100%; max-width: none; order: 3; }
  #searchInput { padding: 12px 16px; font-size: 16px; /* ≥16px 防 iOS 自动缩放 */ }
  #searchBtn { padding: 12px 22px; font-size: 14px; min-width: 76px; }
  .topbar-actions { margin-left: auto; order: 2; }
  .ghost-btn { padding: 11px 16px; font-size: 13px; min-height: 44px; }
  .suggest { top: 56px; right: 0; }

  /* 指数条：横向滑动更顺手 */
  .ticker-tape { gap: 20px; padding: 10px 14px; font-size: 13px; }
  .tick { gap: 6px; }

  /* 板块导航：加大触摸区，允许横向滚动 */
  .board-nav { padding: 10px 14px 0; gap: 6px; }
  .bnav {
    padding: 13px 18px; font-size: 14px; min-height: 48px;
    -webkit-tap-highlight-color: rgba(79,140,255,.15);
  }
  .bn-ico { font-size: 17px; }

  .board-area { padding: 12px 14px; }
  .board-head { margin-bottom: 12px; }
  .board-head h2 { font-size: 17px; }
  .head-tools { width: 100%; }

  /* 卡片内边距稍收，给内容让空间 */
  .card { padding: 13px 13px; }

  /* 各种两栏/三栏布局统一改为单列堆叠 */
  .news-layout { grid-template-columns: 1fr; }
  .hot-layout { grid-template-columns: 1fr; }
  .hot-layout-2 { grid-template-columns: 1fr; }
  .tech-layout { grid-template-columns: 1fr; }
  .tech-metrics { grid-template-columns: 1fr; }

  /* 新闻列表：字号与行距适应触屏阅读 */
  .news-list { max-height: none; }
  .news-list li { padding: 15px 4px; }
  .news-title { font-size: 14.5px; }
  .news-summary { font-size: 13px; }
  .news-meta { font-size: 12px; }
  .nf { padding: 9px 16px; font-size: 13px; min-height: 40px; }

  /* 表格：横向滚动 + 加大行高，字号放大 */
  .table-wrap {
    max-height: none;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;   /* iOS 惯性滚动 */
    /* 右侧渐隐提示可横滑 */
    background:
      linear-gradient(to left, var(--card) 0%, rgba(26,32,41,0) 12px) right / 24px 100% no-repeat;
  }
  .data-table { font-size: 13.5px; min-width: 640px; }
  .data-table th { padding: 11px 12px; font-size: 12px; }
  .data-table td { padding: 13px 12px; }
  .mini-btn { padding: 8px 14px; font-size: 12.5px; min-height: 36px; }
  .del-btn { padding: 8px 13px; font-size: 12.5px; min-height: 36px; }

  /* 板块卡片触摸优化 */
  .sector-item { padding: 13px 14px; }
  .sector-name { font-size: 14.5px; }
  .sector-pct { font-size: 15px; }
  .rank-list li { padding: 11px 8px; font-size: 13.5px; }
  .rank-idx { width: 22px; height: 22px; font-size: 11px; }

  /* 图表切换按钮加大 */
  .tabs { width: 100%; }
  .tab { flex: 1; padding: 10px 8px; font-size: 13px; min-height: 42px; }
  .indicator-toggles { font-size: 13px; gap: 18px; }
  .indicator-toggles input { width: 18px; height: 18px; }

  /* 评分区：仪表盘缩小，避免挤压 */
  .score-body { flex-wrap: wrap; }
  .gauge-wrap { width: 132px; height: 118px; }
  .score-meta { flex: 1 1 60%; }

  /* 选股器 */
  .preset-btn { padding: 11px 15px; min-height: 52px; flex: 1 1 46%; }
  .preset-name { font-size: 13.5px; }
  .preset-desc { font-size: 11.5px; }
  .screener-toolbar { gap: 10px; }
  .scr-opt { flex: 1 1 45%; }
  .scr-opt select { padding: 11px 12px; font-size: 14px; min-height: 44px; width: 100%; }
  .primary-btn { flex: 1 1 100%; padding: 14px 24px; font-size: 15px; min-height: 50px; }
  .mini-input { padding: 11px 14px; font-size: 16px; min-height: 44px; flex: 1; }
  .mini-btn { min-height: 44px; padding: 11px 18px; }

  /* 自选股添加区 */
  .head-tools .mini-input { width: auto; }

  /* 理由列表 */
  .reason-list { max-height: none; }
  .reason-list li { font-size: 13.5px; padding: 11px 13px; }

  /* 指标卡 */
  .metric .m-name { font-size: 11.5px; }
  .metric .m-val { font-size: 16px; }
  .metric { padding: 11px 12px; }

  /* 关键价位 */
  .sr-chip { padding: 7px 13px; font-size: 13.5px; }
  .sr-box .sr-current .val { font-size: 26px; }
}

/* ---------- 窄屏手机 (<560px) ---------- */
@media (max-width: 560px) {
  .topbar { padding: 9px 11px; gap: 8px; }
  .brand-name { font-size: 16px; }
  .board-area { padding: 10px 11px; }
  .bnav { padding: 12px 13px; font-size: 13px; }
  .bnav .bn-ico { font-size: 15px; }
  .score-body { flex-direction: column; align-items: stretch; }
  .gauge-wrap { width: 100%; height: 128px; }
  .data-table { min-width: 560px; font-size: 13px; }
  .screener-presets .preset-btn { flex: 1 1 100%; }
  .chart-tall { height: 280px; }
  .chart-short { height: 96px; }
  .metric-grid { grid-template-columns: 1fr; }
  .sentiment-grid { grid-template-columns: repeat(2, 1fr); }

  /* 防止热点榜单名称过长撑破容器 */
  .rank-name { min-width: 0; }
  .rank-code { display: none; }          /* 窄屏隐藏代码，避免挤压 */
  .rank-val { flex-shrink: 0; }
  .sector-sub { flex-wrap: wrap; gap: 2px 10px; }
  .sector-name { font-size: 14px; }
  .sector-pct { flex-shrink: 0; }
  /* 板块名可能较长，允许换行不撑破 */
  .sector-top { gap: 8px; }
  .sector-top .sector-name { min-width: 0; word-break: break-all; }
}

/* ---------- iPad 横屏及更大平板：图表高度适当增加 ---------- */
@media (min-width: 769px) and (max-width: 1195px) {
  .chart-tall { height: 320px; }
}

/* ---------- 触屏设备：去除点击高亮、优化滚动 ---------- */
@media (hover: none) and (pointer: coarse) {
  * { -webkit-tap-highlight-color: transparent; }
  .data-table tbody tr:active,
  .sector-item:active,
  .rank-list li:active,
  .bnav:active,
  .tab:active,
  .preset-btn:active { opacity: .75; }
}

::-webkit-scrollbar-track { background: transparent; }

</style>
<script src="https://cdn.jsdelivr.net/npm/echarts@5.4.3/dist/echarts.min.js"></script>
</head>
<body>

<header class="topbar">
  <div class="brand">
    <span class="logo">◈</span>
    <span class="brand-name">StockLens</span>
    <span class="brand-sub">股票分析体系</span>
  </div>
  <div class="search-wrap">
    <input id="searchInput" type="text" placeholder="输入代码或名称，如 600519 / 贵州茅台 / AAPL / 00700" autocomplete="off">
    <button id="searchBtn">分析</button>
    <div id="suggestBox" class="suggest hidden"></div>
  </div>
  <div class="topbar-actions">
    <button id="refreshBtn" class="ghost-btn" title="刷新全部数据">⟳ 刷新</button>
    <button id="favBtn" class="ghost-btn" title="加入自选">☆ 自选</button>
  </div>
</header>

<div class="ticker-tape" id="marketBar">加载大盘数据…</div>

<!-- 板块切换 -->
<nav class="board-nav" id="boardNav">
  <button class="bnav active" data-board="news">
    <span class="bn-ico">📰</span><span class="bn-txt">金融热点新闻</span>
  </button>
  <button class="bnav" data-board="hot">
    <span class="bn-ico">🔥</span><span class="bn-txt">市场热点</span>
  </button>
  <button class="bnav" data-board="watch">
    <span class="bn-ico">⭐</span><span class="bn-txt">我的自选股</span>
  </button>
  <button class="bnav" data-board="tech">
    <span class="bn-ico">📈</span><span class="bn-txt">技术分析与选股</span>
  </button>
</nav>

<main class="board-area">

  <!-- ============================================ 板块1：金融热点新闻 -->
  <section class="board active" id="board-news">
    <div class="board-head">
      <h2>① 最新金融热点新闻</h2>
      <div class="head-tools">
        <span class="live-dot"></span>
        <span class="head-note" id="newsTime">实时更新</span>
      </div>
    </div>
    <div class="news-layout">
      <div class="card news-main">
        <div class="news-filter">
          <button class="nf active" data-src="all">全部</button>
          <button class="nf" data-src="东方财富">东方财富</button>
          <button class="nf" data-src="新浪财经">新浪财经</button>
        </div>
        <ul class="news-list" id="newsList"><li class="ph">加载中…</li></ul>
      </div>
      <div class="news-side">
        <section class="card">
          <h3 class="card-title">市场情绪</h3>
          <div id="sentimentBox"><div class="ph">—</div></div>
        </section>
        <section class="card">
          <h3 class="card-title">涨幅榜</h3>
          <ul class="rank-list" id="newsGainers"><li class="ph">—</li></ul>
        </section>
      </div>
    </div>
  </section>

  <!-- ============================================ 板块2：市场热点 -->
  <section class="board" id="board-hot">
    <div class="board-head">
      <h2>② 市场关注的热点</h2>
      <div class="head-tools">
        <div class="tabs" id="sectorTabs">
          <button class="tab active" data-kind="industry">行业板块</button>
          <button class="tab" data-kind="concept">概念板块</button>
          <button class="tab" data-kind="region">地域板块</button>
        </div>
      </div>
    </div>
    <div class="hot-layout">
      <section class="card">
        <h3 class="card-title">板块涨幅排行</h3>
        <div class="sector-grid" id="sectorGrid"><div class="ph">加载中…</div></div>
      </section>
      <section class="card">
        <h3 class="card-title">板块成分股 <span class="dim" id="sectorStockTitle">（点击左侧板块查看）</span></h3>
        <div class="table-wrap">
          <table class="data-table" id="sectorStockTable">
            <thead><tr><th>代码</th><th>名称</th><th class="num">现价</th><th class="num">涨跌幅</th><th></th></tr></thead>
            <tbody><tr><td colspan="5" class="ph">—</td></tr></tbody>
          </table>
        </div>
      </section>
    </div>
    <div class="hot-layout-2">
      <section class="card">
        <h3 class="card-title">成交额榜</h3>
        <ul class="rank-list" id="rankAmount"><li class="ph">加载中…</li></ul>
      </section>
      <section class="card">
        <h3 class="card-title">涨幅榜</h3>
        <ul class="rank-list" id="rankGainers"><li class="ph">加载中…</li></ul>
      </section>
      <section class="card">
        <h3 class="card-title">跌幅榜</h3>
        <ul class="rank-list" id="rankLosers"><li class="ph">加载中…</li></ul>
      </section>
    </div>
  </section>

  <!-- ============================================ 板块3：我的自选股 -->
  <section class="board" id="board-watch">
    <div class="board-head">
      <h2>③ 我的自选股</h2>
      <div class="head-tools">
        <input id="watchAddInput" type="text" placeholder="添加代码，如 600519" class="mini-input">
        <button id="watchAddBtn" class="mini-btn">+ 添加</button>
      </div>
    </div>
    <section class="card">
      <div class="table-wrap">
        <table class="data-table" id="watchTable">
          <thead>
            <tr>
              <th>名称</th><th>代码</th><th class="num">现价</th><th class="num">涨跌额</th>
              <th class="num">涨跌幅</th><th class="num">评分</th><th>信号</th><th></th>
            </tr>
          </thead>
          <tbody><tr><td colspan="8" class="ph">加载中…</td></tr></tbody>
        </table>
      </div>
    </section>
    <section class="card">
      <h3 class="card-title">自选股评分对比</h3>
      <div id="watchChart" class="chart chart-mid"></div>
    </section>
  </section>

  <!-- ============================================ 板块4：技术分析与选股 -->
  <section class="board" id="board-tech">
    <div class="board-head">
      <h2>④ 技术分析与选股</h2>
      <div class="head-tools">
        <span class="head-note" id="techSymbolNote">当前标的</span>
      </div>
    </div>

    <div class="tech-layout">
      <!-- 左：技术分析 -->
      <div class="tech-left">
        <section class="card stock-head" id="stockHead">
          <div class="ph">在顶部搜索框输入股票代码，或从其他板块点击个股</div>
        </section>
        <section class="card score-card">
          <h3 class="card-title">综合评分</h3>
          <div class="score-body">
            <div class="gauge-wrap"><div id="gauge"></div></div>
            <div class="score-meta">
              <div class="signal-badge" id="signalBadge">—</div>
              <div class="dim-list" id="dimList"></div>
            </div>
          </div>
        </section>
        <section class="card">
          <h3 class="card-title">关键价位</h3>
          <div id="srBox" class="sr-box"><div class="ph">—</div></div>
        </section>
        <section class="card">
          <h3 class="card-title">盘面解读</h3>
          <div class="tabbar" id="reasonTabs">
            <button class="rtab active" data-tab="bull">看多理由</button>
            <button class="rtab" data-tab="bear">看空理由</button>
          </div>
          <ul class="reason-list" id="reasonList"><li class="ph">—</li></ul>
        </section>
      </div>

      <!-- 右：图表 + 指标 -->
      <div class="tech-right">
        <section class="card chart-card">
          <div class="chart-toolbar">
            <div class="tabs" id="periodTabs">
              <button class="tab" data-days="60">近3月</button>
              <button class="tab active" data-days="120">近半年</button>
              <button class="tab" data-days="250">近1年</button>
              <button class="tab" data-days="500">近2年</button>
            </div>
            <div class="indicator-toggles" id="indToggles">
              <label><input type="checkbox" data-ind="ma" checked> 均线</label>
              <label><input type="checkbox" data-ind="boll"> BOLL</label>
            </div>
          </div>
          <div id="klineChart" class="chart chart-tall"></div>
          <div id="volChart" class="chart chart-short"></div>
          <div id="macdChart" class="chart chart-short"></div>
          <div id="kdjChart" class="chart chart-short"></div>
          <div id="rsiChart" class="chart chart-short"></div>
        </section>
        <div class="tech-metrics">
          <section class="card">
            <h3 class="card-title">技术指标速览</h3>
            <div class="metric-grid" id="metricGrid"><div class="ph">—</div></div>
          </section>
          <section class="card">
            <h3 class="card-title">基本面指标</h3>
            <div class="metric-grid" id="finGrid"><div class="ph">—</div></div>
          </section>
        </div>
      </div>
    </div>

    <!-- 选股器 -->
    <section class="card screener-card">
      <h3 class="card-title">技术选股器 · 按指标筛选股票</h3>
      <div class="screener-presets" id="screenerPresets"></div>
      <div class="screener-toolbar">
        <div class="scr-opt">
          <label>候选池规模</label>
          <select id="scrPool">
            <option value="60">60 只（快）</option>
            <option value="120" selected>120 只（推荐）</option>
            <option value="200">200 只（慢）</option>
          </select>
        </div>
        <div class="scr-opt">
          <label>最低成交额</label>
          <select id="scrAmount">
            <option value="50000000">5000万</option>
            <option value="100000000" selected>1亿</option>
            <option value="500000000">5亿</option>
          </select>
        </div>
        <button id="scrRunBtn" class="primary-btn">开始选股</button>
        <span class="scr-status" id="scrStatus"></span>
      </div>
      <div class="table-wrap">
        <table class="data-table" id="screenerTable">
          <thead>
            <tr><th>代码</th><th>名称</th><th class="num">现价</th><th class="num">涨跌幅</th>
                <th class="num">评分</th><th>信号</th><th>命中条件</th><th></th></tr>
          </thead>
          <tbody><tr><td colspan="8" class="ph">选择上方策略后点击「开始选股」</td></tr></tbody>
        </table>
      </div>
    </section>
  </section>

</main>

<div class="disclaimer">
  ⚠️ 本体系基于公开行情数据自动计算，新闻来自公开媒体聚合，所有评分与信号仅供研究参考，<b>不构成任何投资建议</b>。股市有风险，投资需谨慎。
</div>

<script>
/* StockLens · 股票分析体系前端
   四大板块：①金融热点新闻 ②市场热点 ③我的自选股 ④技术分析与选股
*/
(function () {
  "use strict";

  const $ = (s) => document.querySelector(s);
  const $$ = (s) => document.querySelectorAll(s);
  const COLORS = {
    up: "#ff4d5e", down: "#22c55e", accent: "#4f8cff",
    text: "#e6edf3", dim: "#8b98a8", mute: "#5d6775", border: "#2a3340",
    ma5: "#f5a623", ma10: "#4f8cff", ma20: "#c77dff", ma60: "#2dd4bf",
  };

  const state = {
    board: "news",
    symbol: "600519",
    days: 120,
    showMA: true,
    showBoll: false,
    raw: null,
    news: [],
    newsSrc: "all",
    sectorKind: "industry",
    preset: "macd_golden",
  };

  let gaugeChart, klineChart, volChart, macdChart, kdjChart, rsiChart, watchChart;

  /* ============================================================ utils */
  const fmt = (v, nd = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(nd));
  const cls = (v) => (v > 0 ? "up" : v < 0 ? "down" : "flat");
  const sign = (v, nd = 2) => (v === null || v === undefined || Number.isNaN(v) ? "—" : (v > 0 ? "+" : "") + Number(v).toFixed(nd));
  const currencyOf = (m) => (m === "US" ? "$" : m === "HK" ? "HK$" : "¥");

  function fmtMoney(v) {
    if (v === null || v === undefined || Number.isNaN(v)) return "—";
    const n = Number(v);
    if (Math.abs(n) >= 1e8) return (n / 1e8).toFixed(2) + "亿";
    if (Math.abs(n) >= 1e4) return (n / 1e4).toFixed(2) + "万";
    return n.toFixed(2);
  }

  function escapeHtml(s) {
    return String(s || "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  function toast(msg) {
    let el = $("#toast");
    if (!el) {
      el = document.createElement("div");
      el.id = "toast"; el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = msg;
    el.classList.add("show");
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove("show"), 2400);
  }

  function showLoading(on) {
    let el = $("#loadingOverlay");
    if (!el) {
      el = document.createElement("div");
      el.id = "loadingOverlay";
      el.className = "loading-overlay hidden";
      el.innerHTML = '<div class="spinner"></div>';
      document.body.appendChild(el);
    }
    el.classList.toggle("hidden", !on);
  }

  async function api(path, opts) {
    const res = await fetch(path, opts);
    const json = await res.json();
    if (!json.ok) throw new Error(json.error || "请求失败");
    return json.data;
  }

  /* ============================================================ 板块切换 */
  function switchBoard(name) {
    state.board = name;
    $$(".bnav").forEach((b) => b.classList.toggle("active", b.dataset.board === name));
    $$(".board").forEach((b) => b.classList.toggle("active", b.id === "board-" + name));
    if (name === "news" && !state.news.length) loadNews();
    if (name === "hot" && !$("#sectorGrid").dataset.loaded) loadHotspots();
    if (name === "watch") loadWatchlist();
    if (name === "tech") {
      if (!state.raw) analyze(state.symbol);
      loadPresets();
      setTimeout(resizeAll, 60);
    }
  }

  /* ============================================================ 板块1 新闻 */
  async function loadNews() {
    const ul = $("#newsList");
    ul.innerHTML = '<li class="ph">加载中…</li>';
    try {
      state.news = await api("/api/news?limit=50");
      $("#newsTime").textContent = "更新于 " + new Date().toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
      renderNews();
    } catch (e) {
      ul.innerHTML = `<li class="ph">新闻加载失败：${e.message}</li>`;
    }
    loadSentiment();
    loadNewsGainers();
  }

  function renderNews() {
    const ul = $("#newsList");
    const list = state.newsSrc === "all" ? state.news : state.news.filter((n) => n.source === state.newsSrc);
    if (!list.length) { ul.innerHTML = '<li class="ph">暂无新闻</li>'; return; }
    ul.innerHTML = list.map((n) => `
      <li ${n.url ? `data-url="${n.url}"` : ""}>
        <div class="news-meta">
          <span class="news-src">${n.source}</span>
          <span class="news-time">${(n.time || "").slice(-8)}</span>
        </div>
        <div class="news-title">${escapeHtml(n.title)}</div>
        ${n.summary && n.summary !== n.title ? `<div class="news-summary">${escapeHtml(String(n.summary).slice(0, 160))}</div>` : ""}
      </li>`).join("");
  }

  async function loadSentiment() {
    const box = $("#sentimentBox");
    try {
      const s = await api("/api/sentiment");
      if (!s.total) { box.innerHTML = '<div class="ph">暂无数据</div>'; return; }
      const upPct = ((s.up / s.total) * 100).toFixed(0);
      box.innerHTML = `
        <div class="sentiment-grid">
          <div class="senti-item"><div class="s-lbl">上涨家数</div><div class="s-val up">${s.up}</div></div>
          <div class="senti-item"><div class="s-lbl">下跌家数</div><div class="s-val down">${s.down}</div></div>
          <div class="senti-item"><div class="s-lbl">涨停</div><div class="s-val up">${s.limit_up}</div></div>
          <div class="senti-item"><div class="s-lbl">跌停</div><div class="s-val down">${s.limit_down}</div></div>
        </div>
        <div class="senti-mood">市场情绪：${s.mood}　（上涨占比 ${upPct}%）</div>`;
    } catch (e) {
      box.innerHTML = '<div class="ph">情绪数据获取失败</div>';
    }
  }

  async function loadNewsGainers() {
    const ul = $("#newsGainers");
    try {
      const r = await api("/api/rankings?top=8");
      ul.innerHTML = renderRank(r.gainers);
    } catch (e) { ul.innerHTML = '<li class="ph">加载失败</li>'; }
  }

  function renderRank(items) {
    if (!items || !items.length) return '<li class="ph">暂无数据</li>';
    return items.map((it, i) => `
      <li data-symbol="${it.code}">
        <span class="rank-idx ${i < 3 ? "top" : ""}">${i + 1}</span>
        <span class="rank-name">${it.name} <span class="rank-code">${it.code}</span></span>
        <span class="rank-val ${cls(it.pct)}">${sign(it.pct)}%</span>
      </li>`).join("");
  }

  /* ============================================================ 板块2 热点 */
  async function loadHotspots() {
    const grid = $("#sectorGrid");
    grid.innerHTML = '<div class="ph">加载中…</div>';
    try {
      const d = await api(`/api/hotspots?kind=${state.sectorKind}&top=15`);
      renderSectors(d.sectors);
      grid.dataset.loaded = "1";
    } catch (e) {
      grid.innerHTML = `<div class="ph">板块加载失败：${e.message}</div>`;
    }
    loadRankings();
  }

  function renderSectors(list) {
    const grid = $("#sectorGrid");
    if (!list || !list.length) { grid.innerHTML = '<div class="ph">暂无数据</div>'; return; }
    const max = Math.max(...list.map((s) => Math.abs(s.pct)), 0.01);
    grid.innerHTML = list.map((s) => {
      const w = (Math.abs(s.pct) / max) * 100;
      const c = s.pct >= 0 ? COLORS.up : COLORS.down;
      return `<div class="sector-item" data-label="${s.label}" data-name="${s.name}">
        <div class="sector-top">
          <span class="sector-name">${s.name}</span>
          <span class="sector-pct" style="color:${c}">${sign(s.pct)}%</span>
        </div>
        <div class="sector-sub">
          <span>${s.count} 只成分股</span>
          <span>成交 ${fmtMoney(s.amount)}</span>
        </div>
        <div class="sector-bar"><span style="width:${w}%;background:${c}"></span></div>
      </div>`;
    }).join("");
  }

  async function loadSectorStocks(label, name) {
    const tbody = $("#sectorStockTable tbody");
    $("#sectorStockTitle").textContent = `（${name}）`;
    tbody.innerHTML = '<tr><td colspan="5" class="ph">加载中…</td></tr>';
    try {
      const list = await api(`/api/sector/stocks?label=${encodeURIComponent(label)}&top=25`);
      if (!list.length) { tbody.innerHTML = '<tr><td colspan="5" class="ph">暂无成分股数据</td></tr>'; return; }
      tbody.innerHTML = list.map((s) => `
        <tr data-symbol="${s.code}">
          <td>${s.code}</td>
          <td class="name-cell">${s.name}</td>
          <td class="num">${fmt(s.price)}</td>
          <td class="num ${cls(s.pct)}">${sign(s.pct)}%</td>
          <td><button class="mini-btn" data-add="${s.code}">+自选</button></td>
        </tr>`).join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="5" class="ph">加载失败：${e.message}</td></tr>`;
    }
  }

  async function loadRankings() {
    try {
      const r = await api("/api/rankings?top=12");
      $("#rankAmount").innerHTML = renderRank(r.amount);
      $("#rankGainers").innerHTML = renderRank(r.gainers);
      $("#rankLosers").innerHTML = renderRank(r.losers);
    } catch (e) {
      ["#rankAmount", "#rankGainers", "#rankLosers"].forEach((s) => { $(s).innerHTML = '<li class="ph">加载失败</li>'; });
    }
  }

  /* ============================================================ 板块3 自选股 */
  async function loadWatchlist() {
    const tbody = $("#watchTable tbody");
    tbody.innerHTML = '<tr><td colspan="8" class="ph">加载中…</td></tr>';
    try {
      const items = await api("/api/watchlist");
      if (!items.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="ph">暂无自选股，可在右上角添加</td></tr>';
        return;
      }
      const withScore = await Promise.all(items.map(async (q) => {
        try {
          const d = await api(`/api/analyze?symbol=${encodeURIComponent(q.display)}&days=60`);
          return { ...q, score: d.analysis.score, level: d.analysis.level, signal: d.analysis.signal, color: d.analysis.color };
        } catch (e) {
          return { ...q, score: null };
        }
      }));
      tbody.innerHTML = withScore.map((q) => `
        <tr data-symbol="${q.display}">
          <td class="name-cell">${q.name || q.display}</td>
          <td>${q.display}</td>
          <td class="num ${cls(q.pct)}">${fmt(q.price)}</td>
          <td class="num ${cls(q.change)}">${sign(q.change)}</td>
          <td class="num ${cls(q.pct)}">${sign(q.pct)}%</td>
          <td class="num">${q.score === null ? "—" : q.score.toFixed(0)}</td>
          <td>${q.score === null ? "—" : `<span class="badge" style="background:${q.color}">${q.level}</span>`}</td>
          <td><button class="del-btn" data-del="${q.display}">移除</button></td>
        </tr>`).join("");
      renderWatchChart(withScore.filter((q) => q.score !== null));
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="ph">加载失败：${e.message}</td></tr>`;
    }
  }

  function renderWatchChart(items) {
    if (!watchChart) watchChart = echarts.init($("#watchChart"));
    if (!items.length) { watchChart.clear(); return; }
    watchChart.setOption({
      animation: true,
      grid: { left: 100, right: 50, top: 20, bottom: 20 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      xAxis: {
        type: "value", max: 100, axisLine: { lineStyle: { color: COLORS.border } },
        axisLabel: { color: COLORS.mute, fontSize: 10 },
        splitLine: { lineStyle: { color: "rgba(255,255,255,.04)" } },
      },
      yAxis: {
        type: "category", data: items.map((q) => q.name || q.display),
        axisLine: { lineStyle: { color: COLORS.border } },
        axisLabel: { color: COLORS.text, fontSize: 11 }, axisTick: { show: false },
      },
      series: [{
        type: "bar", barWidth: "52%",
        data: items.map((q) => ({ value: q.score, itemStyle: { color: q.color, borderRadius: [0, 4, 4, 0] } })),
        label: { show: true, position: "right", color: COLORS.dim, fontSize: 11, formatter: (p) => Number(p.value).toFixed(0) },
      }],
    }, true);
  }

  async function addWatch(symbol) {
    try {
      await api("/api/watchlist", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ symbol }),
      });
      toast(`已添加 ${symbol} 到自选`);
      if (state.board === "watch") loadWatchlist();
    } catch (e) { toast("添加失败：" + e.message); }
  }

  async function delWatch(symbol) {
    try {
      await api(`/api/watchlist?symbol=${encodeURIComponent(symbol)}`, { method: "DELETE" });
      toast(`已移除 ${symbol}`);
      loadWatchlist();
    } catch (e) { toast("移除失败：" + e.message); }
  }

  /* ============================================================ 板块4 技术分析 */
  function renderHead(info, quote) {
    const cur = currencyOf(info.market);
    const q = quote || {};
    $("#stockHead").innerHTML = `
      <div class="sh-name">${info.name || info.code}<span class="sh-code">${info.display}</span></div>
      <div class="sh-price ${cls(q.pct)}">${cur}${fmt(q.price)}</div>
      <div class="sh-chg ${cls(q.pct)}">${sign(q.change)} &nbsp; ${sign(q.pct)}%</div>
      <div class="sh-extra">
        <div><span class="k">今开</span> <span class="v">${fmt(q.open)}</span></div>
        <div><span class="k">最高</span> <span class="v">${fmt(q.high)}</span></div>
        <div><span class="k">最低</span> <span class="v">${fmt(q.low)}</span></div>
        <div><span class="k">昨收</span> <span class="v">${fmt(q.prev_close)}</span></div>
        <div><span class="k">成交量</span> <span class="v">${fmtMoney(q.volume)}</span></div>
        <div><span class="k">数据日期</span> <span class="v">${info.last_date || "—"}</span></div>
      </div>`;
    $("#techSymbolNote").textContent = `当前标的：${info.name || info.code} (${info.display})`;
  }

  function renderGauge(a) {
    if (!gaugeChart) gaugeChart = echarts.init($("#gauge"));
    gaugeChart.setOption({
      series: [{
        type: "gauge", startAngle: 210, endAngle: -30, min: 0, max: 100,
        radius: "92%", center: ["50%", "58%"],
        progress: { show: true, width: 12, itemStyle: { color: a.color } },
        axisLine: { lineStyle: { width: 12, color: [[1, "#232b38"]] } },
        pointer: { show: false }, axisTick: { show: false }, splitLine: { show: false },
        axisLabel: { distance: -22, color: COLORS.mute, fontSize: 9, formatter: (v) => (v === 0 || v === 50 || v === 100 ? v : "") },
        detail: { valueAnimation: true, fontSize: 30, fontWeight: "bolder", color: a.color, offsetCenter: [0, "-5%"], formatter: (v) => v.toFixed(0) },
        data: [{ value: a.score }],
      }],
    }, true);
  }

  function renderScoreMeta(a) {
    $("#signalBadge").textContent = `${a.level} · ${a.signal}`;
    $("#signalBadge").style.background = a.color;
    const dimColor = { "趋势": "#4f8cff", "动量": "#f5a623", "均线": "#c77dff", "量能": "#2dd4bf", "位置": "#8b98a8" };
    $("#dimList").innerHTML = a.dimensions.map((d) => {
      const pct = (d.score / d.max) * 100;
      const c = dimColor[d.name] || COLORS.accent;
      return `<div class="dim-row">
        <div class="dim-top"><span class="dim-name">${d.name}</span>
        <span class="dim-val">${d.score.toFixed(0)} / ${d.max}</span></div>
        <div class="dim-bar"><span style="width:${pct}%;background:${c}"></span></div>
      </div>`;
    }).join("");
  }

  function renderSR(sr) {
    const res = sr.resistance.map((p) => `<span class="sr-chip res">${p.toFixed(2)}</span>`).join("");
    const sup = sr.support.map((p) => `<span class="sr-chip sup">${p.toFixed(2)}</span>`).join("");
    $("#srBox").innerHTML = `
      <div class="sr-current"><div class="lbl">当前价</div><div class="val">${sr.current.toFixed(2)}</div></div>
      <div class="sr-group"><div class="sr-title">上方压力位</div>
        <div class="sr-levels">${res || '<span class="ph">暂无</span>'}</div></div>
      <div class="sr-group"><div class="sr-title">下方支撑位</div>
        <div class="sr-levels">${sup || '<span class="ph">暂无</span>'}</div></div>
      <div class="sr-group"><div class="sr-title">120日区间</div>
        <div class="sr-levels">
          <span class="sr-chip res">高 ${sr.high_120.toFixed(2)}</span>
          <span class="sr-chip sup">低 ${sr.low_120.toFixed(2)}</span>
        </div></div>`;
  }

  function renderReasons(a, tab) {
    const list = tab === "bull" ? a.bullish : a.bearish;
    const li = $("#reasonList");
    if (!list || !list.length) { li.innerHTML = '<li class="ph">暂无相关信号</li>'; return; }
    li.innerHTML = list.map((t) => `<li class="${tab === "bull" ? "bull-item" : "bear-item"}">${escapeHtml(t)}</li>`).join("");
  }

  function renderMetrics(s) {
    const items = [
      ["MA5", s.ma5], ["MA20", s.ma20], ["MA60", s.ma60],
      ["DIF", s.dif], ["DEA", s.dea], ["MACD", s.macd],
      ["RSI6", s.rsi6], ["RSI14", s.rsi14],
      ["K", s.k], ["D", s.d], ["J", s.j],
      ["量比", s.vol_ratio], ["乖离率6", s.bias6 + "%"], ["区间位置", s.position_120 + "%"],
    ];
    $("#metricGrid").innerHTML = items.map(([n, v]) =>
      `<div class="metric"><div class="m-name">${n}</div><div class="m-val">${typeof v === "number" ? fmt(v) : v}</div></div>`).join("");
  }

  function renderFin(fin) {
    const grid = $("#finGrid");
    if (!fin || !fin.metrics || !fin.metrics.length) {
      grid.innerHTML = '<div class="ph">该标的暂无可解析的基本面数据</div>'; return;
    }
    grid.innerHTML = fin.metrics.map((m) => {
      let v = m.value;
      if (v === null || v === undefined) v = "—";
      else if (m.fmt === "money") v = fmtMoney(v);
      else v = fmt(v, 2) + (m.unit || "");
      return `<div class="metric"><div class="m-name">${m.name}</div><div class="m-val">${v}</div></div>`;
    }).join("");
  }

  function baseAxis() {
    return {
      axisLine: { lineStyle: { color: COLORS.border } },
      axisLabel: { color: COLORS.mute, fontSize: 10 },
      splitLine: { show: false }, axisTick: { show: false },
    };
  }
  function lineSeries(name, data, color, width, dash) {
    return {
      name, type: "line", data, smooth: true, showSymbol: false,
      lineStyle: { width: width || 1, color, type: dash ? "dashed" : "solid" },
      itemStyle: { color },
    };
  }
  function subAxis(dates) {
    return {
      xAxis: { type: "category", data: dates, ...baseAxis(), axisLabel: { show: false } },
      yAxis: { type: "value", scale: true, ...baseAxis(), axisLabel: { color: COLORS.mute, fontSize: 9 } },
    };
  }

  function renderKline(chart) {
    if (!chart || !chart.dates || !chart.dates.length) return;
    klineChart.setOption({
      animation: false, backgroundColor: "transparent",
      grid: [{ left: 46, right: 16, top: 22, height: 210 }],
      tooltip: {
        trigger: "axis", axisPointer: { type: "cross" },
        backgroundColor: "rgba(21,26,33,.95)", borderColor: COLORS.border,
        textStyle: { color: COLORS.text, fontSize: 11 },
      },
      xAxis: [{ type: "category", data: chart.dates, boundaryGap: true, ...baseAxis() }],
      yAxis: [{ type: "value", scale: true, ...baseAxis(),
        splitLine: { show: true, lineStyle: { color: "rgba(255,255,255,.04)" } },
        axisLabel: { color: COLORS.mute, fontSize: 10, formatter: (v) => v.toFixed(2) } }],
      series: [
        { name: "K线", type: "candlestick", data: chart.kline,
          itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down } },
        ...(state.showMA ? [
          lineSeries("MA5", chart.ma5, COLORS.ma5, 1),
          lineSeries("MA10", chart.ma10, COLORS.ma10, 1),
          lineSeries("MA20", chart.ma20, COLORS.ma20, 1),
          lineSeries("MA60", chart.ma60, COLORS.ma60, 1),
        ] : []),
        ...(state.showBoll ? [
          lineSeries("BOLL上轨", chart.boll_up, "#8b98a8", 1, [4, 3]),
          lineSeries("BOLL中轨", chart.boll_mid, "#8b98a8", 1),
          lineSeries("BOLL下轨", chart.boll_low, "#8b98a8", 1, [4, 3]),
        ] : []),
      ],
    }, true);
  }

  function renderVol(chart) {
    if (!chart || !chart.volume || !chart.volume.length) return;
    const colored = chart.volume.map((v, i) => ({
      value: v, itemStyle: { color: chart.kline[i][1] >= chart.kline[i][0] ? COLORS.up : COLORS.down },
    }));
    volChart.setOption({
      animation: false, backgroundColor: "transparent",
      grid: { left: 46, right: 16, top: 6, height: 84 },
      tooltip: { trigger: "axis", axisPointer: { type: "shadow" } },
      ...subAxis(chart.dates),
      series: [{ name: "成交量", type: "bar", data: colored, barWidth: "60%" }],
    }, true);
  }

  function renderMacd(chart) {
    if (!chart || !chart.macd || !chart.macd.length) return;
    const bars = chart.macd.map((v) => ({ value: v, itemStyle: { color: v >= 0 ? COLORS.up : COLORS.down } }));
    macdChart.setOption({
      animation: false, backgroundColor: "transparent",
      grid: { left: 46, right: 16, top: 6, height: 84 },
      tooltip: { trigger: "axis" }, ...subAxis(chart.dates),
      series: [
        { name: "MACD", type: "bar", data: bars, barWidth: "50%" },
        lineSeries("DIF", chart.dif, COLORS.ma5, 1),
        lineSeries("DEA", chart.dea, COLORS.ma10, 1),
      ],
    }, true);
  }

  function renderKdj(chart) {
    if (!chart || !chart.k || !chart.k.length) return;
    kdjChart.setOption({
      animation: false, backgroundColor: "transparent",
      grid: { left: 46, right: 16, top: 6, height: 84 },
      tooltip: { trigger: "axis" }, ...subAxis(chart.dates),
      series: [
        lineSeries("K", chart.k, COLORS.ma5, 1),
        lineSeries("D", chart.d, COLORS.ma10, 1),
        lineSeries("J", chart.j, COLORS.ma20, 1),
      ],
    }, true);
  }

  function renderRSI(chart) {
    if (!chart || !chart.rsi6 || !chart.rsi6.length) return;
    rsiChart.setOption({
      animation: false, backgroundColor: "transparent",
      grid: { left: 46, right: 16, top: 6, height: 84 },
      tooltip: { trigger: "axis" }, ...subAxis(chart.dates),
      yAxis: { type: "value", min: 0, max: 100, ...baseAxis(), axisLabel: { color: COLORS.mute, fontSize: 9 } },
      series: [
        lineSeries("RSI6", chart.rsi6, COLORS.ma5, 1),
        lineSeries("RSI14", chart.rsi14, COLORS.accent, 1.4),
      ],
    }, true);
  }

  function initCharts() {
    if (!klineChart) klineChart = echarts.init($("#klineChart"));
    if (!volChart) volChart = echarts.init($("#volChart"));
    if (!macdChart) macdChart = echarts.init($("#macdChart"));
    if (!kdjChart) kdjChart = echarts.init($("#kdjChart"));
    if (!rsiChart) rsiChart = echarts.init($("#rsiChart"));
  }

  function resizeAll() {
    [gaugeChart, klineChart, volChart, macdChart, kdjChart, rsiChart, watchChart]
      .forEach((c) => c && c.resize());
  }

  async function analyze(symbol, silent) {
    if (!symbol) return;
    if (!silent) showLoading(true);
    try {
      const data = await api(`/api/analyze?symbol=${encodeURIComponent(symbol)}&days=${state.days}`);
      state.raw = data;
      state.symbol = symbol;

      renderHead(data.info, data.quote);
      renderGauge(data.analysis);
      renderScoreMeta(data.analysis);
      renderSR(data.analysis.support_resistance);
      renderMetrics(data.analysis.snapshot);
      const activeTab = $(".rtab.active") ? $(".rtab.active").dataset.tab : "bull";
      renderReasons(data.analysis, activeTab);

      initCharts();
      renderKline(data.chart);
      renderVol(data.chart);
      renderMacd(data.chart);
      renderKdj(data.chart);
      renderRSI(data.chart);
      setTimeout(resizeAll, 60);
      loadFinancials(symbol);
    } catch (e) {
      toast("分析失败：" + e.message);
    } finally {
      if (!silent) showLoading(false);
    }
  }

  async function loadFinancials(symbol) {
    $("#finGrid").innerHTML = '<div class="ph">加载中…</div>';
    try { renderFin(await api(`/api/financials?symbol=${encodeURIComponent(symbol)}`)); }
    catch (e) { $("#finGrid").innerHTML = '<div class="ph">基本面数据获取失败</div>'; }
  }

  /* ============================================================ 选股器 */
  async function loadPresets() {
    const box = $("#screenerPresets");
    if (box.dataset.loaded) return;
    try {
      const list = await api("/api/screener/presets");
      box.innerHTML = list.map((p) => `
        <button class="preset-btn ${p.key === state.preset ? "active" : ""}" data-preset="${p.key}">
          <span class="preset-name">${p.name}</span>
          <span class="preset-desc">${p.desc}</span>
        </button>`).join("");
      box.dataset.loaded = "1";
    } catch (e) {
      box.innerHTML = '<div class="ph">策略加载失败</div>';
    }
  }

  async function runScreener() {
    const btn = $("#scrRunBtn");
    const tbody = $("#screenerTable tbody");
    btn.disabled = true;
    $("#scrStatus").textContent = "扫描中，请稍候…";
    tbody.innerHTML = '<tr><td colspan="8" class="ph">正在扫描（约 20-60 秒）…</td></tr>';
    try {
      const presets = await api("/api/screener/presets");
      const preset = presets.find((p) => p.key === state.preset);
      if (!preset) throw new Error("请先选择策略");
      const res = await fetch("/api/screener", {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          conditions: preset.conditions,
          pool_size: parseInt($("#scrPool").value, 10),
          min_amount: parseFloat($("#scrAmount").value),
          max_results: 40,
        }),
      });
      const json = await res.json();
      if (!json.ok) throw new Error(json.error);
      const d = json.data;
      $("#scrStatus").textContent = `扫描 ${d.total_scanned} 只，命中 ${d.matched} 只`;
      if (!d.results.length) {
        tbody.innerHTML = '<tr><td colspan="8" class="ph">没有符合条件的股票，可尝试更换策略或扩大候选池</td></tr>';
        return;
      }
      tbody.innerHTML = d.results.map((r) => `
        <tr data-symbol="${r.display}">
          <td>${r.display}</td>
          <td class="name-cell">${r.name}</td>
          <td class="num">${fmt(r.close)}</td>
          <td class="num ${cls(r.pct)}">${sign(r.pct)}%</td>
          <td class="num">${r.score === null ? "—" : r.score.toFixed(0)}</td>
          <td>${r.level ? `<span class="badge" style="background:${scoreColor(r.score)}">${r.level}</span>` : "—"}</td>
          <td>${r.reasons.map((x) => `<span class="chip">${escapeHtml(x)}</span>`).join("")}</td>
          <td><button class="mini-btn" data-add="${r.code}">+自选</button></td>
        </tr>`).join("");
    } catch (e) {
      $("#scrStatus").textContent = "";
      tbody.innerHTML = `<tr><td colspan="8" class="ph">选股失败：${e.message}</td></tr>`;
    } finally {
      btn.disabled = false;
    }
  }

  function scoreColor(s) {
    if (s === null || s === undefined) return "#5d6775";
    if (s >= 80) return "#e63946";
    if (s >= 65) return "#f4a261";
    if (s >= 45) return "#8d99ae";
    if (s >= 30) return "#457b9d";
    return "#1d3557";
  }

  /* ============================================================ 大盘 */
  async function loadMarket() {
    try {
      const list = await api("/api/market");
      $("#marketBar").innerHTML = list.map((m) => `
        <span class="tick">
          <span class="t-name">${m.name}</span>
          <span class="t-val ${cls(m.pct)}">${fmt(m.price)}</span>
          <span class="${cls(m.pct)}">${sign(m.change)} (${sign(m.pct)}%)</span>
        </span>`).join("");
    } catch (e) { $("#marketBar").textContent = "大盘数据暂不可用"; }
  }

  /* ============================================================ 搜索 */
  let suggestTimer = null;
  function bindSearch() {
    const input = $("#searchInput");
    const box = $("#suggestBox");

    input.addEventListener("input", () => {
      clearTimeout(suggestTimer);
      const kw = input.value.trim();
      if (!kw) { box.classList.add("hidden"); return; }
      suggestTimer = setTimeout(async () => {
        try {
          const list = await api(`/api/search?q=${encodeURIComponent(kw)}`);
          if (!list.length) { box.classList.add("hidden"); return; }
          box.innerHTML = list.map((it) => `
            <div class="suggest-item" data-code="${it.code}">
              <span>${it.name}<span class="s-code">${it.display}</span></span>
              <span class="${cls(it.pct)}">${sign(it.pct)}%</span>
            </div>`).join("");
          box.classList.remove("hidden");
        } catch (e) { box.classList.add("hidden"); }
      }, 260);
    });

    box.addEventListener("click", (e) => {
      const item = e.target.closest(".suggest-item");
      if (!item) return;
      box.classList.add("hidden");
      goAnalyze(item.dataset.code);
    });

    const submit = () => {
      const v = input.value.trim();
      box.classList.add("hidden");
      if (v) goAnalyze(v);
    };
    $("#searchBtn").addEventListener("click", submit);
    input.addEventListener("keydown", (e) => { if (e.key === "Enter") submit(); });
    document.addEventListener("click", (e) => {
      if (!e.target.closest(".search-wrap")) box.classList.add("hidden");
    });
  }

  function goAnalyze(symbol) {
    $("#searchInput").value = symbol;
    switchBoard("tech");
    analyze(symbol);
  }

  /* ============================================================ 事件绑定 */
  function bindEvents() {
    $("#boardNav").addEventListener("click", (e) => {
      const b = e.target.closest(".bnav");
      if (b) switchBoard(b.dataset.board);
    });

    $("#refreshBtn").addEventListener("click", () => {
      if (state.board === "news") loadNews();
      else if (state.board === "hot") loadHotspots();
      else if (state.board === "watch") loadWatchlist();
      else if (state.board === "tech") { analyze(state.symbol, true); loadFinancials(state.symbol); }
      loadMarket();
      toast("已刷新");
    });

    $("#favBtn").addEventListener("click", () => addWatch(state.symbol));

    $(".news-filter").addEventListener("click", (e) => {
      const b = e.target.closest(".nf");
      if (!b) return;
      $$(".nf").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.newsSrc = b.dataset.src;
      renderNews();
    });

    $("#newsList").addEventListener("click", (e) => {
      const li = e.target.closest("li[data-url]");
      if (li && li.dataset.url) window.open(li.dataset.url, "_blank");
    });

    $("#sectorTabs").addEventListener("click", (e) => {
      const b = e.target.closest(".tab");
      if (!b) return;
      $$("#sectorTabs .tab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.sectorKind = b.dataset.kind;
      loadHotspots();
    });

    $("#sectorGrid").addEventListener("click", (e) => {
      const item = e.target.closest(".sector-item");
      if (!item) return;
      $$(".sector-item").forEach((x) => x.classList.remove("active"));
      item.classList.add("active");
      loadSectorStocks(item.dataset.label, item.dataset.name);
    });

    document.addEventListener("click", (e) => {
      const addBtn = e.target.closest("[data-add]");
      if (addBtn) { e.stopPropagation(); addWatch(addBtn.dataset.add); return; }
      const delBtn = e.target.closest("[data-del]");
      if (delBtn) { e.stopPropagation(); delWatch(delBtn.dataset.del); return; }
      const row = e.target.closest("tr[data-symbol]");
      if (row) { goAnalyze(row.dataset.symbol); return; }
      const rank = e.target.closest(".rank-list li[data-symbol]");
      if (rank) { goAnalyze(rank.dataset.symbol); return; }
    });

    $("#watchAddBtn").addEventListener("click", () => {
      const v = $("#watchAddInput").value.trim();
      if (v) { addWatch(v); $("#watchAddInput").value = ""; }
    });
    $("#watchAddInput").addEventListener("keydown", (e) => {
      if (e.key === "Enter") {
        const v = e.target.value.trim();
        if (v) { addWatch(v); e.target.value = ""; }
      }
    });

    $("#periodTabs").addEventListener("click", (e) => {
      const b = e.target.closest(".tab");
      if (!b) return;
      $$("#periodTabs .tab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.days = parseInt(b.dataset.days, 10);
      analyze(state.symbol, true);
    });

    $("#indToggles").addEventListener("change", (e) => {
      const ind = e.target.dataset.ind;
      if (ind === "ma") state.showMA = e.target.checked;
      if (ind === "boll") state.showBoll = e.target.checked;
      if (state.raw && state.raw.chart) renderKline(state.raw.chart);
    });

    $("#reasonTabs").addEventListener("click", (e) => {
      const b = e.target.closest(".rtab");
      if (!b) return;
      $$(".rtab").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      if (state.raw) renderReasons(state.raw.analysis, b.dataset.tab);
    });

    $("#screenerPresets").addEventListener("click", (e) => {
      const b = e.target.closest(".preset-btn");
      if (!b) return;
      $$(".preset-btn").forEach((x) => x.classList.remove("active"));
      b.classList.add("active");
      state.preset = b.dataset.preset;
    });
    $("#scrRunBtn").addEventListener("click", runScreener);
  }

  /* ============================================================ boot */
  window.addEventListener("resize", resizeAll);

  document.addEventListener("DOMContentLoaded", () => {
    bindSearch();
    bindEvents();
    loadMarket();
    loadNews();
  });
})();

</script>
</body>
</html>
"""


# ============================================================================
# [原 app.py —— Flask 应用与全部 API]
# ============================================================================

# -*- coding: utf-8 -*-
"""
StockLens · 股票分析体系
==============================
Flask 应用：提供页面与 REST API。

本地开发启动：
    python3 app.py                 # http://127.0.0.1:8000

生产部署启动（推荐，支持并发）：
    gunicorn -c gunicorn.conf.py wsgi:application
    # 或
    gunicorn -w 2 -k gthread --threads 8 -b 0.0.0.0:8000 wsgi:application
"""
# (合并自各模块的 __future__ 已省略)

import argparse
import logging
import os
import sys
import traceback

from flask import Flask, jsonify, render_template_string, request

# 允许从项目根目录直接运行：把 app/ 加入模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))


logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("stocklens")

app = Flask(__name__)
app.config["JSON_AS_ASCII"] = False

# 自选股（内存存储，重启即清空；可扩展为持久化）
WATCHLIST: list[str] = ["600519", "000001", "300750", "00700", "AAPL"]


def ok(data=None, **extra):
    payload = {"ok": True, "data": data}
    payload.update(extra)
    return jsonify(payload)


def fail(msg: str, code: int = 400):
    return jsonify({"ok": False, "error": msg}), code


# ---------------------------------------------------------------- 页面
@app.route("/")
def index():
    return render_template_string(PAGE_HTML)


# ---------------------------------------------------------------- API
@app.route("/api/market")
def api_market():
    """大盘指数概览"""
    try:
        return ok(ds.get_market_overview())
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(f"大盘数据获取失败: {e}", 500)


@app.route("/api/quote")
def api_quote():
    symbol = request.args.get("symbol", "").strip()
    market = request.args.get("market", "").strip() or None
    if not symbol:
        return fail("缺少参数 symbol")
    try:
        return ok(ds.get_quote(symbol, market))
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/analyze")
def api_analyze():
    """
    核心分析接口：一次性返回行情 + 指标 + 评分 + 支撑压力，
    前端拿到即可渲染整个仪表盘。
    """
    symbol = request.args.get("symbol", "").strip()
    market = request.args.get("market", "").strip() or None
    days = int(request.args.get("days", 250))
    if not symbol:
        return fail("请输入股票代码")

    try:
        df, info = ds.get_history(symbol, market, days=max(days, 260))
        result = scoring.analyze(df)

        # 只回传最近 days 根，减小载荷；但指标已全量计算
        chart_df = ind.compute_all(df).tail(days)
        chart = {
            "dates": chart_df["date"].tolist(),
            "kline": [[float(o), float(c), float(l), float(h)] for o, c, l, h in
                      zip(chart_df["open"], chart_df["close"], chart_df["low"], chart_df["high"])],
            "volume": [float(v) if v == v else 0 for v in chart_df["volume"]],
            "ma5": _round_list(chart_df["ma5"]),
            "ma10": _round_list(chart_df["ma10"]),
            "ma20": _round_list(chart_df["ma20"]),
            "ma60": _round_list(chart_df["ma60"]),
            "boll_up": _round_list(chart_df["boll_up"]),
            "boll_mid": _round_list(chart_df["boll_mid"]),
            "boll_low": _round_list(chart_df["boll_low"]),
            "dif": _round_list(chart_df["dif"], 3),
            "dea": _round_list(chart_df["dea"], 3),
            "macd": _round_list(chart_df["macd"], 3),
            "k": _round_list(chart_df["k"], 1),
            "d": _round_list(chart_df["d"], 1),
            "j": _round_list(chart_df["j"], 1),
            "rsi6": _round_list(chart_df["rsi6"], 1),
            "rsi14": _round_list(chart_df["rsi14"], 1),
        }

        quote = ds.get_quote(symbol, market)
        return ok({
            "info": info,
            "quote": quote,
            "analysis": result,
            "chart": chart,
        })
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/financials")
def api_financials():
    symbol = request.args.get("symbol", "").strip()
    market = request.args.get("market", "").strip() or None
    if not symbol:
        return fail("缺少参数 symbol")
    try:
        return ok(ds.get_financials(symbol, market))
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/search")
def api_search():
    """按名称/代码搜索 A股标的（基于全市场快照，本地匹配）。"""
    kw = request.args.get("q", "").strip()
    if not kw:
        return ok([])
    try:
        spot = ds._get_a_spot_all()
        m = spot[spot["名称"].astype(str).str.contains(kw, na=False) |
                 spot["代码"].astype(str).str.contains(kw, na=False)]
        out = []
        for _, r in m.head(12).iterrows():
            code = str(r["代码"])  # sh600519
            out.append({
                "code": code[2:],
                "display": f"{code[2:]}.{ 'SH' if code.startswith('sh') else ('SZ' if code.startswith('sz') else 'BJ') }",
                "ak": code,
                "name": str(r["名称"]),
                "price": _safe(r.get("最新价")),
                "pct": _safe(r.get("涨跌幅")),
            })
        return ok(out)
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/watchlist", methods=["GET", "POST", "DELETE"])
def api_watchlist():
    global WATCHLIST
    if request.method == "GET":
        items = []
        for s in WATCHLIST:
            try:
                items.append(ds.get_quote(s))
            except Exception:  # noqa: BLE001
                items.append({"display": s, "name": s, "price": None})
        return ok(items)

    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or request.args.get("symbol") or "").strip()
    if not symbol:
        return fail("缺少 symbol")
    norm = ds.normalize_symbol(symbol)
    key = norm["display"]
    if request.method == "POST":
        if key not in WATCHLIST:
            WATCHLIST.append(key)
    else:
        WATCHLIST = [x for x in WATCHLIST if ds.normalize_symbol(x)["display"] != key]
    return ok(WATCHLIST)


@app.route("/api/health")
def api_health():
    return ok({"status": "up", "watchlist": WATCHLIST})


# ================================================================ 资讯与热点
@app.route("/api/news")
def api_news():
    """最新金融热点新闻（多源聚合）"""
    limit = int(request.args.get("limit", 40))
    try:
        return ok(mkt.get_news(limit=limit))
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(f"新闻获取失败: {e}", 500)


@app.route("/api/hotspots")
def api_hotspots():
    """市场热点：板块排行 + 榜单 + 情绪"""
    kind = request.args.get("kind", "industry")
    top = int(request.args.get("top", 10))
    try:
        return ok({
            "sectors": mkt.get_sectors(kind, top=top),
            "kind": kind,
        })
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(f"热点获取失败: {e}", 500)


@app.route("/api/sector/stocks")
def api_sector_stocks():
    """某板块的成分股"""
    label = request.args.get("label", "").strip()
    if not label:
        return fail("缺少参数 label")
    try:
        return ok(mkt.get_sector_stocks(label, top=int(request.args.get("top", 20))))
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/rankings")
def api_rankings():
    """市场榜单：涨幅/跌幅/成交额/换手率"""
    try:
        return ok(mkt.get_rankings(top=int(request.args.get("top", 10))))
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


@app.route("/api/sentiment")
def api_sentiment():
    """市场情绪概览"""
    try:
        return ok(mkt.get_market_sentiment())
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


# ================================================================ 技术选股
@app.route("/api/screener/presets")
def api_screener_presets():
    """预设选股策略列表"""
    return ok(screener.list_presets())


@app.route("/api/screener", methods=["GET", "POST"])
def api_screener():
    """
    技术选股。支持两种传参：
      GET  ?preset=macd_golden&pool=100
      POST {"conditions": {...}, "pool_size": 120}
    """
    try:
        if request.method == "POST":
            body = request.get_json(silent=True) or {}
            conditions = body.get("conditions") or {}
            pool_size = int(body.get("pool_size", 120))
            max_results = int(body.get("max_results", 50))
            min_amount = float(body.get("min_amount", 1e8))
        else:
            preset = request.args.get("preset", "").strip()
            pool_size = int(request.args.get("pool", 120))
            max_results = int(request.args.get("max", 50))
            min_amount = float(request.args.get("min_amount", 1e8))
            if preset:
                if preset not in screener.PRESETS:
                    return fail(f"未知策略: {preset}")
                conditions = screener.PRESETS[preset]["conditions"]
            else:
                conditions = {}

        if not conditions:
            return fail("请至少指定一个筛选条件或选择预设策略")

        result = screener.screen(conditions, pool_size=pool_size,
                                 max_results=max_results, min_amount=min_amount)
        if result.get("error"):
            return fail(result["error"], 500)
        return ok(result)
    except Exception as e:  # noqa: BLE001
        logger.error(traceback.format_exc())
        return fail(str(e), 500)


# ---------------------------------------------------------------- helpers
def _round_list(series, nd: int = 2):
    out = []
    for v in series:
        try:
            f = float(v)
            out.append(None if f != f else round(f, nd))
        except Exception:  # noqa: BLE001
            out.append(None)
    return out


def _safe(v):
    try:
        f = float(v)
        return None if f != f else f
    except Exception:  # noqa: BLE001
        return None


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    # 端口优先级：命令行 > 平台注入的 PORT 环境变量 > 默认 8000
    parser.add_argument("--port", type=int, default=int(os.getenv("PORT", "8000")))
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args()
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)
  
