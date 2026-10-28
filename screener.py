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
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from . import datasource as ds
from . import indicators as ind
from . import scoring
from .datasource import _get_a_spot_all, _f

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
