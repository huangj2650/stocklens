# -*- coding: utf-8 -*-
"""
技术指标计算
-------------
纯 pandas/numpy 实现，不依赖 TA-Lib，保证零外部二进制依赖。
包含：MA、EMA、MACD、KDJ、RSI、BOLL、ATR、OBV、量比、乖离率。
"""
from __future__ import annotations

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
