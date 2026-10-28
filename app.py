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
from __future__ import annotations

import argparse
import logging
import os
import sys
import traceback

from flask import Flask, jsonify, render_template, request

# 允许从项目根目录直接运行：把 app/ 加入模块搜索路径
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

from core import datasource as ds
from core import indicators as ind
from core import scoring
from core import market as mkt
from core import screener

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("stocklens")

app = Flask(__name__,
            static_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "static"),
            template_folder=os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "templates"))
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
    return render_template("index.html")


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
