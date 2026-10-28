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
from __future__ import annotations

import logging
import re
from datetime import datetime

import pandas as pd

from .datasource import cached_frame, _get_a_spot_all, _f

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
