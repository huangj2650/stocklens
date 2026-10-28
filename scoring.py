# -*- coding: utf-8 -*-
"""
智能评分与信号引擎
-------------------
把技术指标、量能、位置、趋势等多维度信息综合成一个 0-100 的评分，
并给出明确的交易信号与理由。所有规则可解释、可追溯。

评分维度（权重合计 100）：
  趋势(30) + 动量(25) + 均线(20) + 量能(15) + 位置/波动(10)
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import indicators as ind


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
