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
