#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
比特币周报数据站构建器 (明亮版)
纯 Python 标准库, 无第三方依赖。运行: python3 build.py
输出: index.html (自包含, 双击可看, 可直接部署到 Cloudflare Pages)
"""
import bisect
import csv
import glob
import io
import json
import math
import os
import shutil
import subprocess
import urllib.request
from datetime import date, datetime, timedelta, timezone

SITE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(SITE, 'data')
os.makedirs(DATA, exist_ok=True)
GENESIS = date(2009, 1, 3)
MODEL_START = date(2013, 4, 28)   # 走廊模型统一锚定起点, 保证本地/CI 参数一致
HALVINGS = [date(2012, 11, 28), date(2016, 7, 9), date(2020, 5, 11), date(2024, 4, 20)]
NOW = datetime.now(timezone.utc)

# ---------------- 基础工具 ----------------

def http_get(url, timeout=60):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (btc-weekly-dashboard)', 'Accept': '*/*'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        # 某些站点(如 FRED)对自定义 UA / HTTP2 不友好, 回退到系统 curl 默认 UA + HTTP/1.1
        r = subprocess.run(['curl', '-sS', '--http1.1', '-m', str(timeout), url],
                           capture_output=True)
        if r.returncode != 0 or not r.stdout:
            raise RuntimeError(f'fetch failed: {url} :: {r.stderr.decode(errors="ignore")[:150]}')
        return r.stdout

def cached(name, fetch_fn, max_age_days=7):
    path = os.path.join(DATA, f'cache_{name}.json')
    def age_of(j):
        try:
            t = datetime.fromisoformat(j['fetched_at'])
            return (NOW - t).total_seconds() / 86400
        except Exception:
            return 999
    if os.path.exists(path):
        try:
            j = json.load(open(path))
            if age_of(j) <= max_age_days:
                return j['payload'], age_of(j), 'cache'
        except Exception:
            pass
    try:
        payload = fetch_fn()
        json.dump({'fetched_at': NOW.isoformat(), 'payload': payload}, open(path, 'w'))
        return payload, 0.0, 'fresh'
    except Exception as e:
        if os.path.exists(path):
            j = json.load(open(path))
            return j['payload'], age_of(j), 'stale'
        print(f'  [!] {name} 抓取失败且无缓存: {type(e).__name__}: {e}')
        return None, None, 'unavailable'

def d(s):
    return s if isinstance(s, date) else date.fromisoformat(str(s)[:10])

def pct_of_sorted(zs, x):
    return bisect.bisect_right(zs, x) / len(zs)

def quantile(zs, q):
    pos = (len(zs) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(zs) - 1)
    f = pos - lo
    return zs[lo] * (1 - f) + zs[hi] * f

def ols(xs, ys):
    n = len(xs)
    mx = sum(xs) / n
    my = sum(ys) / n
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    sxx = sum((x - mx) ** 2 for x in xs)
    b = sxy / sxx
    return my - b * mx, b

def fmt_usd(v):
    if v >= 1e6: return f'{v/1e6:.2f}M'
    if v >= 1e3: return f'{v/1e3:.0f}k'
    return f'{v:,.0f}'

# ---------------- 价格数据 (CSV 优先, CoinMetrics 自动补齐) ----------------

CM_CSV_URL = 'https://raw.githubusercontent.com/coinmetrics/data/master/csv/btc.csv'

def fetch_coinmetrics():
    raw = http_get(CM_CSV_URL, timeout=120).decode('utf-8', 'ignore')
    rd = csv.reader(io.StringIO(raw))
    header = next(rd)
    ix = {c: i for i, c in enumerate(header)}
    mi, ei, pi = ix.get('CapMVRVCur'), ix.get('SplyExNtv'), ix.get('PriceUSD')
    mvrv, exsp, price = [], [], []
    for row in rd:
        try:
            if pi is not None and row[pi]:
                v = float(row[pi])
                if v > 0:
                    price.append((row[0], v))
            if mi is not None and row[mi]:
                mvrv.append((row[0], float(row[mi])))
            if ei is not None and row[ei]:
                exsp.append((row[0], float(row[ei])))
        except (ValueError, IndexError):
            pass
    return {'mvrv': mvrv, 'exsp': exsp, 'price': price}

def load_prices():
    """价格链: Downloads 最新 CSV -> data/btc_price_used.csv -> CoinMetrics 全史;
    若数据落后超过3天, 用 CoinMetrics PriceUSD 补齐并写回。"""
    used_path = os.path.join(DATA, 'btc_price_used.csv')
    src, src_mtime = None, -1
    if os.path.exists(used_path):
        src, src_mtime = used_path, os.path.getmtime(used_path)
    for f in glob.glob(os.path.expanduser('~/Downloads/btc-price*.csv')):
        mt = os.path.getmtime(f)
        if mt > src_mtime:
            src, src_mtime = f, mt
    def read_csv(path):
        rows = []
        with open(path, newline='') as fh:
            rd = csv.reader(fh)
            next(rd)
            for line in rd:
                if not line or not line[0].strip():
                    continue
                rows.append((d(line[0]), float(line[1])))
        return rows
    rows, source_label = [], '无'
    topped = 0
    if src:
        rows = read_csv(src)
        source_label = os.path.basename(src)
    else:
        cm, _, st = cached('coinmetrics', fetch_coinmetrics, max_age_days=2)
        if cm and cm.get('price'):
            rows = [(d(s), v) for s, v in cm['price']]
            source_label = 'CoinMetrics(自动)'
    if rows:
        rows.sort(key=lambda x: x[0])
        if src and src != used_path:
            shutil.copy(src, used_path)
        last = rows[-1][0]
        if (date.today() - last).days >= 3:
            cm, _, st = cached('coinmetrics_fresh', fetch_coinmetrics, max_age_days=0)
            if cm and cm.get('price'):
                have = {dd for dd, _ in rows}
                extra = [(d(s), v) for s, v in cm['price'] if d(s) > last and d(s) not in have]
                if extra:
                    rows += extra
                    rows.sort(key=lambda x: x[0])
                    topped = len(extra)
                    source_label += f' + CoinMetrics补齐{topped}天'
        seen, uniq = set(), []
        for dd, pp in rows:
            if dd not in seen:
                seen.add(dd)
                uniq.append((dd, pp))
        rows = [(dd, pp) for dd, pp in uniq if pp > 0 and dd >= MODEL_START]
        with open(used_path, 'w', newline='') as fh:
            w = csv.writer(fh)
            w.writerow(['date', 'btc price'])
            for dd, pp in rows:
                w.writerow([dd.isoformat(), pp])
    return rows, source_label

# ---------------- 走廊模型 ----------------

def fit_corridor(pdates, pvals):
    tt = [(x - GENESIS).days + 1 for x in pdates]
    xs = [math.log(t) for t in tt]
    ys = [math.log(p) for p in pvals]
    a, b = ols(xs, ys)
    res = [y - (a + b * x) for x, y in zip(xs, ys)]
    us = [math.log(abs(r) + 1e-4) for r in res]
    c0, c1 = ols(xs, us)
    z = sorted(r / math.exp(c0 + c1 * x) for x, r in zip(xs, res))
    return {'a': a, 'b': b, 'c0': c0, 'c1': c1, 'z': z}

def corr_mu_sig(m, dd):
    lx = math.log((dd - GENESIS).days + 1)
    return m['a'] + m['b'] * lx, math.exp(m['c0'] + m['c1'] * lx)

def corr_pct(m, dd, price):
    mu, sig = corr_mu_sig(m, dd)
    return pct_of_sorted(m['z'], (math.log(price) - mu) / sig)

def corr_band(m, dd, q):
    mu, sig = corr_mu_sig(m, dd)
    return math.exp(mu + quantile(m['z'], q) * sig)

# ---------------- 其他数据源 ----------------

def fetch_fred(sid):
    txt = http_get(f'https://fred.stlouisfed.org/graph/fredgraph.csv?id={sid}').decode()
    out = []
    for i, line in enumerate(txt.strip().splitlines()):
        if i == 0:
            continue
        parts = line.split(',')
        if len(parts) < 2 or parts[1] in ('.', ''):
            continue
        out.append((parts[0], float(parts[1])))
    return out

def fetch_stables():
    j = json.loads(http_get('https://stablecoins.llama.fi/stablecoincharts/all'))
    out = []
    for row in j:
        tc = row.get('totalCirculatingUSD') or row.get('totalCirculating') or {}
        v = tc.get('peggedUSD') or tc.get('USD')
        if v:
            out.append((datetime.fromtimestamp(int(row['date']), tz=timezone.utc).date().isoformat(), float(v)))
    return out

def fetch_hashrate():
    j = json.loads(http_get('https://api.blockchain.info/charts/hash-rate?timespan=all&format=json&sampled=false', timeout=90))
    return [(datetime.fromtimestamp(v['x'], tz=timezone.utc).date().isoformat(), float(v['y'])) for v in j['values']]

def load_etf_manual():
    p = os.path.join(SITE, 'etf_flows.csv')
    if not os.path.exists(p):
        return None
    rows = []
    for line in open(p):
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = line.split(',')
        if len(parts) >= 2:
            try:
                rows.append((d(parts[0]), float(parts[1])))
            except ValueError:
                pass
    rows.sort(key=lambda x: x[0])
    return rows or None

# ---------------- 序列工具 ----------------

def at_days_ago(series, days):
    target = (date.today() - timedelta(days=days))
    best, bv = None, None
    for s, v in series:
        if d(s) <= target:
            best, bv = s, v
    return bv

def change_pct(series, days):
    v0 = at_days_ago(series, days)
    if v0 is None or not v0:
        return None
    return series[-1][1] / v0 - 1

def slice_days(series, days):
    cutoff = date.today() - timedelta(days=days)
    return [(s, v) for s, v in series if d(s) >= cutoff]

# ---------------- SVG 绘图 (浅色主题) ----------------

C_GRID = '#e9edf4'
C_LAB = '#8b93a7'
C_PRICE = '#1f2937'
C_BLUE = '#2563eb'
C_BAND = '#3b82f6'
C_HALV = '#c8cfdb'
C_RED = '#dc2626'
C_GREEN = '#16a34a'

CSS = """
:root{--bg:#f6f7fa;--card:#ffffff;--line:#e7eaf1;--tx:#1a2233;--mut:#697386;--soft:#a3abc0;
--good:#16a34a;--warn:#d97706;--bad:#dc2626;--blue:#2563eb;
--good-bg:#e9f7ef;--warn-bg:#fdf3e3;--bad-bg:#fdeeee;--blue-bg:#eaf1fe}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--tx);font:15px/1.7 -apple-system,'SF Pro Text','PingFang SC','Hiragino Sans GB','Microsoft YaHei',sans-serif;-webkit-font-smoothing:antialiased}
.wrap{max-width:1200px;margin:0 auto;padding:0 22px 80px}
section{scroll-margin-top:86px}
a{color:var(--blue);text-decoration:none}
a:hover{text-decoration:underline}
h1{font-size:30px;font-weight:800;letter-spacing:-.5px;margin:0}
h2{font-size:21px;font-weight:750;margin:52px 0 14px;letter-spacing:-.3px}
h2 .en{color:var(--soft);font-size:13px;font-weight:500;margin-left:10px}
.hero{padding:44px 0 8px}
.hero .sub{color:var(--mut);margin-top:8px;font-size:14.5px}
nav{position:sticky;top:0;z-index:50;background:rgba(246,247,250,.82);backdrop-filter:blur(14px);-webkit-backdrop-filter:blur(14px);border-bottom:1px solid var(--line);margin:0 -22px;padding:0 22px}
nav .in{max-width:1200px;margin:0 auto;display:flex;gap:6px;align-items:center;height:54px}
nav a{color:var(--mut);font-size:14px;font-weight:550;padding:6px 13px;border-radius:9px;transition:all .25s ease}
nav a:hover{color:var(--tx);background:#eceef4;text-decoration:none}
nav .logo{font-weight:800;color:var(--tx);margin-right:12px;font-size:15px}
.banner{background:linear-gradient(135deg,#ffffff 0%,#f3f6ff 100%);border:1px solid #e3e9f8;border-radius:18px;padding:22px 26px;margin:26px 0;box-shadow:0 8px 28px -18px rgba(37,99,235,.25)}
.banner .row{display:flex;flex-wrap:wrap;gap:10px 22px;align-items:center;font-size:15px}
.banner .k{color:var(--mut);font-size:13px;margin-right:6px}
.pill{display:inline-block;padding:3px 14px;border-radius:99px;font-size:13.5px;font-weight:650;transition:transform .2s ease}
.pill:hover{transform:translateY(-1px)}
.pill.good{background:var(--good-bg);color:var(--good)}
.pill.warn{background:var(--warn-bg);color:var(--warn)}
.pill.bad{background:var(--bad-bg);color:var(--bad)}
.pill.na{background:#eef0f5;color:var(--mut)}
.alarms{display:flex;flex-wrap:wrap;gap:8px;margin-top:14px}
.alarm{padding:5px 13px;border-radius:9px;font-size:13.5px;transition:transform .2s ease}
.alarm:hover{transform:translateY(-1px)}
.alarm.good{color:#137a3d;background:var(--good-bg)}
.alarm.warn{color:#b45309;background:var(--warn-bg)}
.alarm.bad{color:#b91c1c;background:var(--bad-bg)}
.card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:20px 22px;box-shadow:0 1px 2px rgba(16,24,40,.04);transition:transform .28s cubic-bezier(.2,.7,.3,1.2),box-shadow .28s ease}
.card:hover{transform:translateY(-3px);box-shadow:0 14px 34px -18px rgba(16,24,40,.18)}
.grid{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media(max-width:920px){.grid{grid-template-columns:1fr}}
.card h3{margin:0;font-size:16.5px;font-weight:700}
.head{display:flex;align-items:center;gap:10px;margin-bottom:4px}
.dot{width:11px;height:11px;border-radius:50%;flex:none}
.dot.good{background:var(--good);box-shadow:0 0 0 0 rgba(22,163,74,.5);animation:pulse 2.4s infinite}
.dot.warn{background:var(--warn)}
.dot.bad{background:var(--bad);box-shadow:0 0 0 0 rgba(220,38,38,.5);animation:pulse 2.4s infinite}
.dot.na{background:#b6bdc9}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(37,99,235,.35)}70%{box-shadow:0 0 0 9px rgba(37,99,235,0)}100%{box-shadow:0 0 0 0 rgba(37,99,235,0)}}
.state{margin-left:auto;font-weight:700;font-size:13.5px;padding:2px 10px;border-radius:8px}
.state.good{color:var(--good);background:var(--good-bg)}
.state.warn{color:var(--warn);background:var(--warn-bg)}
.state.bad{color:var(--bad);background:var(--bad-bg)}
.state.na{color:var(--mut);background:#eef0f5}
.plain{color:#47536b;font-size:13.5px;background:#f4f6fb;border-radius:10px;padding:8px 12px;margin:8px 0}
.plain b{color:var(--blue)}
details{margin-top:10px;font-size:13.5px}
summary{cursor:pointer;color:var(--mut);user-select:none;transition:color .2s}
summary:hover{color:var(--blue)}
details p{color:var(--mut);margin:8px 0 0;line-height:1.75}
.nums{display:flex;flex-wrap:wrap;gap:4px 22px;margin:10px 0 6px;font-size:13px}
.num .l{color:var(--mut);font-size:12px}
.num b{font-size:16px;font-variant-numeric:tabular-nums;letter-spacing:-.2px}
.rules{font-size:12.5px;color:var(--mut);border-top:1px dashed var(--line);margin-top:12px;padding-top:10px;line-height:1.75}
.rules b{color:var(--tx)}
.src{font-size:11.5px;color:#a3abc0;margin-top:9px}
.chart{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 20px;margin:16px 0;box-shadow:0 1px 2px rgba(16,24,40,.04)}
.chart .cap{color:var(--mut);font-size:13px;margin:2px 0 10px}
table{border-collapse:collapse;width:100%;font-size:13.5px}
th,td{border-bottom:1px solid #eef0f5;padding:8px 10px;text-align:right;font-variant-numeric:tabular-nums}
th:first-child,td:first-child{text-align:left}
th{color:var(--mut);font-weight:550;font-size:12.5px}
tr{transition:background .15s}
tbody tr:hover{background:#f6f8fd}
.thisrow{background:var(--blue-bg)}
.check{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:18px 24px;font-size:14px}
.check li{margin:9px 0}
.gloss{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:920px){.gloss{grid-template-columns:1fr}}
.gloss details{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px 18px;margin:0;transition:transform .25s ease,box-shadow .25s ease}
.gloss details:hover{transform:translateY(-2px);box-shadow:0 10px 26px -16px rgba(16,24,40,.2)}
.gloss summary{font-weight:650;color:var(--tx);font-size:14px}
.gloss p{color:var(--mut)}
.intro{background:linear-gradient(135deg,#eef3ff 0%,#f7f4ec 100%);border:1px solid #e2e8f6;border-radius:18px;padding:22px 26px;margin:20px 0}
.intro ol{margin:10px 0 0;padding-left:20px}
.intro li{margin:7px 0;font-size:14.5px}
.foot{color:var(--mut);font-size:12.5px;margin-top:56px;border-top:1px solid var(--line);padding-top:18px;line-height:1.9}
.reveal{opacity:0;transform:translateY(16px);transition:opacity .7s ease,transform .7s cubic-bezier(.2,.7,.3,1)}
.reveal.in{opacity:1;transform:none}
@media (prefers-reduced-motion: reduce){.reveal{opacity:1;transform:none;transition:none}.dot{animation:none}html{scroll-behavior:auto}}
svg{max-width:100%;height:auto;display:block}
code{background:#eef0f5;padding:1px 7px;border-radius:6px;font-size:12.5px}
"""

def _scale_y(vals, H, pad=0.06, log=False):
    lo, hi = min(vals), max(vals)
    if log:
        lo, hi = math.log10(max(lo, 1e-9)), math.log10(max(hi, 1e-9))
    if hi - lo < 1e-12:
        hi = lo + 1
    rng = (hi - lo) * (1 + 2 * pad) or 1
    mid = (lo + hi) / 2
    lo2, hi2 = mid - rng / 2, mid + rng / 2
    def y(v):
        if log:
            v = math.log10(max(v, 1e-9))
        return H - (v - lo2) / (hi2 - lo2) * H
    return y, lo2, hi2

def spark(series, color, W=320, H=70, guides=None, glabels=None, fmt_v=None):
    if not series or len(series) < 3:
        return '<div style="color:#a3abc0;font-size:12px">暂无数据</div>'
    vals = [v for _, v in series]
    y, lo, hi = _scale_y(vals, H - 6)
    n = len(series)
    pts = ' '.join(f'{i/(n-1)*W:.1f},{y(v)+3:.1f}' for i, (_, v) in enumerate(series))
    out = [f'<svg viewBox="0 0 {W} {H+8}" width="{W}" xmlns="http://www.w3.org/2000/svg">']
    for g in (guides or []):
        gy = y(g) + 3
        if -5 <= gy <= H + 5:
            out.append(f'<line x1="0" y1="{gy:.1f}" x2="{W}" y2="{gy:.1f}" stroke="#dde2ec" stroke-dasharray="4 4" stroke-width="1"/>')
            if glabels:
                idx = (guides or []).index(g)
                out.append(f'<text x="3" y="{gy-2:.1f}" fill="#a3abc0" font-size="9">{glabels[idx]}</text>')
    out.append(f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="2" stroke-linejoin="round" stroke-linecap="round"/>')
    lx, ly = W - 2, y(vals[-1]) + 3
    lv = fmt_v(vals[-1]) if fmt_v else f'{vals[-1]:,.0f}'
    out.append(f'<circle cx="{lx-1}" cy="{ly:.1f}" r="3" fill="{color}"/>')
    out.append(f'<text x="{lx}" y="{max(ly-6,10):.1f}" fill="{color}" font-size="10.5" font-weight="600" text-anchor="end">{lv}</text>')
    out.append('</svg>')
    return ''.join(out)

def fan_chart(m, pdates, pvals, days_ahead=210, W=1120, H=430):
    t0 = pdates[0]
    t1 = pdates[-1] + timedelta(days=days_ahead)
    span = (t1 - t0).days
    step = max(1, span // 400)
    def y_of(v, lo, hi, H):
        return H - (math.log10(max(v, 1e-9)) - lo) / (hi - lo) * H
    blo10, bhi90, blo25, bhi75, mid = [], [], [], [], []
    dd = t0
    while dd <= t1:
        blo10.append((dd, corr_band(m, dd, 0.10)))
        bhi90.append((dd, corr_band(m, dd, 0.90)))
        blo25.append((dd, corr_band(m, dd, 0.25)))
        bhi75.append((dd, corr_band(m, dd, 0.75)))
        mid.append((dd, corr_band(m, dd, 0.50)))
        dd += timedelta(days=step)
    allv = [v for _, v in bhi90] + [v for _, v in blo10] + list(pvals)
    lo = math.log10(min(allv)) - 0.05
    hi = math.log10(max(allv)) + 0.08
    X = lambda dd_: (dd_ - t0).days / span * (W - 70)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    e = 2
    while e <= 8:
        for mult in (1, 2, 5):
            v = mult * 10 ** e
            if lo < math.log10(v) < hi:
                gy = y_of(v, lo, hi, H)
                out.append(f'<line x1="0" y1="{gy:.1f}" x2="{W-70}" y2="{gy:.1f}" stroke="{C_GRID}" stroke-width="1"/>')
                out.append(f'<text x="{W-64}" y="{gy+4:.1f}" fill="{C_LAB}" font-size="11">{fmt_usd(v)}</text>')
        e += 1
    def poly(lo_s, hi_s, op):
        pts = ' '.join(f'{X(dd):.1f},{y_of(v, lo, hi, H):.1f}' for dd, v in hi_s)
        pts += ' ' + ' '.join(f'{X(dd):.1f},{y_of(v, lo, hi, H):.1f}' for dd, v in reversed(lo_s))
        return f'<polygon points="{pts}" fill="{C_BAND}" fill-opacity="{op}"/>'
    out.append(poly(blo10, bhi90, 0.13))
    out.append(poly(blo25, bhi75, 0.22))
    pts = ' '.join(f'{X(dd):.1f},{y_of(v, lo, hi, H):.1f}' for dd, v in mid)
    out.append(f'<polyline points="{pts}" fill="none" stroke="{C_BLUE}" stroke-width="1.4" stroke-dasharray="6 5"/>')
    pts = ' '.join(f'{X(dd):.1f},{y_of(v, lo, hi, H):.1f}' for dd, v in zip(pdates, pvals))
    out.append(f'<polyline points="{pts}" fill="none" stroke="{C_PRICE}" stroke-width="1.4"/>')
    for hv in HALVINGS:
        if t0 <= hv <= t1:
            out.append(f'<line x1="{X(hv):.1f}" y1="0" x2="{X(hv):.1f}" y2="{H}" stroke="{C_HALV}" stroke-width="1" stroke-dasharray="2 5"/>')
            out.append(f'<text x="{X(hv)+3:.1f}" y="14" fill="{C_LAB}" font-size="10">减半</text>')
    last_dd, last_v = pdates[-1], pvals[-1]
    cy = y_of(last_v, lo, hi, H)
    out.append(f'<circle cx="{X(last_dd):.1f}" cy="{cy:.1f}" r="4" fill="{C_RED}" stroke="#fff" stroke-width="1.5"/>')
    out.append(f'<text x="{X(last_dd):.1f}" y="{cy-9:.1f}" fill="{C_RED}" font-size="11.5" font-weight="600" text-anchor="middle">{last_v:,.0f}</text>')
    for yr in range(t0.year + 1, t1.year + 1, 2):
        yy = date(yr, 1, 1)
        out.append(f'<text x="{X(yy):.1f}" y="{H-6}" fill="{C_LAB}" font-size="11" text-anchor="middle">{yr}</text>')
    out.append(f'<text x="6" y="16" fill="{C_LAB}" font-size="12">浅蓝=80%走廊(p10~p90) · 深蓝=50%走廊 · 蓝虚线=中枢 · 灰竖线=减半 · 红点=现在</text>')
    out.append('</svg>')
    return ''.join(out)

def percentile_chart(dates_, pcts, since_days=1150, W=1120, H=240):
    cut = date.today() - timedelta(days=since_days)
    pts_all = [(s, p) for s, p in zip(dates_, pcts) if s >= cut]
    if len(pts_all) < 10:
        return ''
    d0 = pts_all[0][0]
    d1 = date.today()
    span = (d1 - d0).days or 1
    X = lambda dd_: (dd_ - d0).days / span * (W - 60)
    Y = lambda v: H - 18 - v / 100 * (H - 30)
    out = [f'<svg viewBox="0 0 {W} {H}" width="100%" xmlns="http://www.w3.org/2000/svg">']
    for g, lab in [(10, 'p10 绝望区(历史大底) '), (50, 'p50 中枢 '), (90, 'p90 亢奋区(历史大顶) ')]:
        out.append(f'<line x1="0" y1="{Y(g):.1f}" x2="{W-60}" y2="{Y(g):.1f}" stroke="#dde2ec" stroke-dasharray="4 4"/>')
        out.append(f'<text x="{W-56}" y="{Y(g)+4:.1f}" fill="{C_LAB}" font-size="11">{lab}</text>')
    pts = ' '.join(f'{X(s):.1f},{Y(p*100):.1f}' for s, p in pts_all)
    out.append(f'<polyline points="{pts}" fill="none" stroke="{C_GREEN}" stroke-width="2"/>')
    s_last, p_last = pts_all[-1]
    out.append(f'<circle cx="{X(s_last):.1f}" cy="{Y(p_last*100):.1f}" r="4" fill="{C_GREEN}" stroke="#fff" stroke-width="1.5"/>')
    out.append(f'<text x="{X(s_last):.1f}" y="{Y(p_last*100)-9:.1f}" fill="{C_GREEN}" font-size="12" font-weight="600" text-anchor="middle">现在 {p_last*100:.0f}%</text>')
    yy = d0.year
    while yy <= d1.year:
        x0 = date(yy, 1, 1)
        if d0 <= x0 <= d1:
            out.append(f'<text x="{X(x0):.1f}" y="{H-2}" fill="{C_LAB}" font-size="11" text-anchor="middle">{yy}</text>')
        yy += 1
    out.append('</svg>')
    return ''.join(out)

# ---------------- 主流程 ----------------

def main():
    print('== 比特币周报构建 (明亮版) ==')
    prices, price_src = load_prices()
    pdates = [x[0] for x in prices]
    pvals = [x[1] for x in prices]
    last_date, last_price = pdates[-1], pvals[-1]
    print(f'价格数据: {price_src} ({pdates[0]} ~ {last_date}, {len(prices)} 天)')

    m = fit_corridor(pdates, pvals)
    pct_series = [corr_pct(m, dd, pp) for dd, pp in zip(pdates, pvals)]
    pct_now = pct_series[-1]

    walcl, age_w, st_w = cached('walcl', lambda: fetch_fred('WALCL'))
    m2sl, age_m, st_m = cached('m2sl', lambda: fetch_fred('M2SL'))
    dxy, age_x, st_x = cached('dtwexbgs', lambda: fetch_fred('DTWEXBGS'))
    liq = {'m2_yoy': None, 'm2_rising': None, 'walcl_90d': None, 'm2_series': None}
    if m2sl and len(m2sl) > 13:
        liq['m2_yoy'] = m2sl[-1][1] / m2sl[-13][1] - 1
        liq['m2_rising'] = m2sl[-1][1] > m2sl[-4][1]
        liq['m2_series'] = [(s, v / m2sl[max(0, i - 12)][1] - 1) for i, (s, v) in enumerate(m2sl) if i >= 12]
    if walcl and len(walcl) > 52:
        liq['walcl_90d'] = change_pct(walcl, 91)
    if dxy:
        liq['dxy_90d'] = change_pct(dxy, 91)
        liq['dxy_last'] = dxy[-1][1]
        liq['dxy_series'] = slice_days(dxy, 365)
    liq_state = 'na'
    if liq['m2_yoy'] is not None:
        if liq['m2_yoy'] > 0.05 or (liq['m2_yoy'] > 0 and liq['m2_rising'] and (liq['walcl_90d'] or 0) > 0):
            liq_state = 'good'
        elif liq['m2_yoy'] < -0.03:
            liq_state = 'bad'
        else:
            liq_state = 'warn'

    etf = load_etf_manual()
    etf30 = etf90 = None
    etf_state = 'na'
    if etf:
        recent = [v for s, v in etf if (date.today() - s).days <= 30]
        recent90 = [v for s, v in etf if (date.today() - s).days <= 90]
        if recent:
            etf30 = sum(recent)
            etf90 = sum(recent90) if recent90 else None
            etf_state = 'good' if etf30 > 0 else ('bad' if len(recent) >= 12 and etf30 < 0 else 'warn')

    stables, age_s, st_s = cached('stables', fetch_stables)
    st_state = 'na'
    st_90d = st_1y = None
    st_series = None
    if stables and len(stables) > 380:
        st_90d = change_pct(stables, 91)
        st_1y = change_pct(stables, 364)
        st_series = slice_days(stables, 730)
        if st_90d is not None and st_1y is not None:
            if st_90d > 0.03:
                st_state = 'good'
            elif st_1y < 0:
                st_state = 'bad'
            else:
                st_state = 'warn'

    hashrate, age_h, st_h = cached('hashrate', fetch_hashrate)
    cm, age_c, st_c = cached('coinmetrics', fetch_coinmetrics, max_age_days=7)
    ma30 = ma60 = None
    hash_state, hash_note = 'na', ''
    hash_spark = ''
    if hashrate and len(hashrate) > 80:
        hv = [v for _, v in hashrate]
        ma30 = sum(hv[-30:]) / 30
        ma60 = sum(hv[-60:]) / 60
        below = ma30 < ma60
        cross = None
        for i in range(len(hashrate) - 1, 60, -1):
            a30 = sum(hv[i - 29:i + 1]) / 30
            a60 = sum(hv[i - 59:i + 1]) / 60
            p30 = sum(hv[i - 30:i]) / 30
            p60 = sum(hv[i - 60:i]) / 60
            if (a30 - a60) * (p30 - p60) < 0:
                cross = (d(hashrate[i][0]), 'up' if a30 > a60 else 'down')
                break
        if cross:
            days_c = (date.today() - cross[0]).days
            if cross[1] == 'down' and days_c <= 45:
                hash_state = 'bad'
                hash_note = f'算力投降进行中({cross[0]}起, {days_c}天前转弱) — 历史上多出现在底部区域'
            elif cross[1] == 'up' and days_c <= 90:
                hash_state = 'good'
                hash_note = f'{cross[0]} 出现算力修复金叉({days_c}天前) — 投降后的历史级底部信号'
            else:
                hash_state = 'good' if not below else 'warn'
                hash_note = '矿工运行正常' if not below else 'MA30低于MA60(偏弱)'
        hash_spark = spark(slice_days([(s, v) for s, v in hashrate], 365), '#8b5cf6', fmt_v=lambda v: f'{v/1e6:.0f} EH/s')
    mvrv = mvrv_series = exsp_90d = None
    exsp_series = None
    cm_last = None
    if cm:
        if cm.get('mvrv'):
            mvrv = cm['mvrv'][-1][1]
            cm_last = d(cm['mvrv'][-1][0])
            mvrv_series = slice_days(cm['mvrv'], 730)
        if cm.get('exsp'):
            exsp_series = slice_days(cm['exsp'], 730)
            exsp_90d = change_pct(cm['exsp'], 91)

    ma200 = sum(pvals[-200:]) / 200
    ma730 = sum(pvals[-730:]) / 730 if len(pvals) >= 730 else None
    ath = max(pvals)
    ath_i = pvals.index(ath)
    ret90 = pvals[-1] / pvals[-91] - 1
    ret365 = pvals[-1] / pvals[-366] - 1 if len(pvals) > 366 else None
    ret180 = pvals[-1] / pvals[-181] - 1
    lgs = [math.log(pvals[i] / pvals[i - 1]) for i in range(len(pvals) - 90, len(pvals))]
    mu = sum(lgs) / len(lgs)
    vol90 = math.sqrt(sum((x - mu) ** 2 for x in lgs) / (len(lgs) - 1)) * math.sqrt(365)
    days_halving = (last_date - HALVINGS[-1]).days
    days_ath = (last_date - pdates[ath_i]).days
    odds_state = 'na'
    if pct_now < 0.10:
        odds_state = 'good'
    elif pct_now > 0.90:
        odds_state = 'bad'
    else:
        odds_state = 'warn'

    dir_state = 'na'
    if liq_state != 'na' and st_state != 'na':
        if liq_state == 'good' and st_state == 'good':
            dir_state = 'good'
        elif liq_state == 'bad' or st_state == 'bad':
            dir_state = 'bad'
        else:
            dir_state = 'warn'
    dir_label = {'good': '顺风', 'warn': '中性', 'bad': '逆风', 'na': '数据不足'}[dir_state]

    alarms = []
    if pct_now < 0.05:
        alarms.append(('bad', f'走廊分位 {pct_now*100:.0f}% — 历史级绝望位置: 要么是大机会, 要么是旧节律失效, 值得认真对待'))
    elif pct_now < 0.10:
        alarms.append(('good', f'走廊分位 {pct_now*100:.0f}% — 进入历史统计的绝望区/机会区'))
    elif pct_now > 0.90:
        alarms.append(('bad', f'走廊分位 {pct_now*100:.0f}% — 亢奋区, 历史上这个位置买入, 之后1-2年大多是亏的'))
    if ma730:
        prem730 = last_price / ma730 - 1
        if prem730 < 0:
            alarms.append(('warn', f'价格低于两年均线 {abs(prem730)*100:.0f}% — 历史上只在熊市出现; 收复并站稳 = 周期延续信号'))
        else:
            alarms.append(('good', f'价格站在两年均线上方 {prem730*100:.0f}%'))
    if st_1y is not None and st_1y < 0:
        alarms.append(('bad', f'稳定币总量同比收缩 {st_1y*100:.0f}% — 场内资金退潮确认'))
    if st_90d is not None and st_90d > 0.03:
        alarms.append(('good', f'稳定币 90 天增长 {st_90d*100:+.1f}% — 场内在蓄水'))
    if etf30 is not None and etf30 < 0:
        alarms.append(('warn', f'ETF 近30日净流出 {etf30:+,.0f} 百万美元 — 结构性买盘减弱'))
    if hash_state == 'bad':
        alarms.append(('warn', '矿工算力投降进行中'))
    if hash_note and '金叉' in hash_note:
        alarms.append(('good', '算力修复金叉 — 历史级底部信号'))

    pos_label = ''
    if dir_state == 'good':
        pos_label = '顺风: 趋势持有' + ('; 分位>90 前分批兑现' if pct_now > 0.75 else '')
    elif dir_state == 'bad':
        pos_label = '逆风: 防守姿态, 等仪表修复, 不接飞刀'
    else:
        pos_label = '中性: 按分位定纪律 — 低位分批攒, 高位分批减, 中位拿住不动'

    def state_txt(s):
        return {'good': '正常/顺风', 'warn': '中性/留意', 'bad': '警报', 'na': '无数据'}[s]

    liq_nums = ''
    if liq['m2_yoy'] is not None:
        liq_nums += f"<span class='num'><span class='l'>美国M2 同比</span><br><b>{liq['m2_yoy']*100:+.1f}%</b> ({'上行' if liq['m2_rising'] else '下行'})</span>"
    if liq['walcl_90d'] is not None:
        liq_nums += f"<span class='num'><span class='l'>联储资产负债表 90天</span><br><b>{liq['walcl_90d']*100:+.1f}%</b></span>"
    if liq.get('dxy_90d') is not None:
        liq_nums += f"<span class='num'><span class='l'>美元指数 90天</span><br><b>{liq['dxy_90d']*100:+.1f}%</b> (现 {liq.get('dxy_last', 0):.1f})</span>"
    liq_spark = spark(liq['m2_series'][-36:], C_BLUE, guides=[0], glabels=['0']) if liq['m2_series'] else ''

    etf_nums = f"<span class='num'><span class='l'>近30日净流入</span><br><b>{(etf30 or 0):+,.0f} M$</b></span><span class='num'><span class='l'>近90日</span><br><b>{(etf90 or 0):+,.0f} M$</b></span>" if etf else '—'
    etf_note = '尚未记录。每周把"日期,净流入(百万美元)"追加到 etf_flows.csv(本地或直接在 GitHub 网页上编辑), 数据源见页脚。' if not etf else f'共记录 {len(etf)} 周。'

    st_nums = ''
    if st_90d is not None:
        st_nums = f"<span class='num'><span class='l'>稳定币总量</span><br><b>${stables[-1][1]/1e9:,.0f}B</b></span><span class='num'><span class='l'>90天变化</span><br><b>{st_90d*100:+.1f}%</b></span><span class='num'><span class='l'>1年变化</span><br><b>{st_1y*100:+.1f}%</b></span>"
    st_spark = spark(st_series, C_GREEN, fmt_v=lambda v: f'${v/1e9:.0f}B') if st_series else ''

    hash_nums = ''
    if ma30:
        hash_nums = f"<span class='num'><span class='l'>算力 MA30 vs MA60</span><br><b>{ma30/1e6:.0f} vs {ma60/1e6:.0f} EH/s</b> ({'上方' if ma30>ma60 else '下方'})</span>"
    mvrv_nums = ''
    if mvrv is not None:
        mvrv_nums = f"<span class='num'><span class='l'>MVRV·截至{cm_last}</span><br><b>{mvrv:.2f}</b></span>"
    if exsp_90d is not None:
        mvrv_nums += f"<span class='num'><span class='l'>交易所余额90天·截至{cm_last}</span><br><b>{exsp_90d*100:+.1f}%</b></span>"
    mvrv_spark = spark(mvrv_series, '#d97706', guides=[1, 3], glabels=['MVRV=1 绝望', 'MVRV=3 亢奋']) if mvrv_series else ''

    odds_nums = f"<span class='num'><span class='l'>走廊分位</span><br><b>{pct_now*100:.0f}%</b></span>"
    if mvrv is not None:
        odds_nums += f"<span class='num'><span class='l'>MVRV·截至{cm_last}</span><br><b>{mvrv:.2f}</b></span>"
    if ma730:
        odds_nums += f"<span class='num'><span class='l'>vs 两年线</span><br><b>{(last_price/ma730-1)*100:+.0f}%</b></span>"
    odds_nums += (f"<span class='num'><span class='l'>距ATH</span><br><b>{last_price/ath-1:+.0%}</b> (顶后{days_ath}天)</span>"
                  f"<span class='num'><span class='l'>减半后</span><br><b>{days_halving}天</b></span>"
                  f"<span class='num'><span class='l'>90日年化波动</span><br><b>{vol90*100:.0f}%</b></span>"
                  f"<span class='num'><span class='l'>90/180/365日收益</span><br><b>{ret90*100:+.0f}% / {ret180*100:+.0f}% / {ret365*100:+.0f}%</b></span>")
    odds_spark = spark([(s, p * 100) for s, p in zip(pdates[-370:], pct_series[-370:])], C_GREEN, guides=[10, 90], glabels=['p10', 'p90'], fmt_v=lambda v: f'{v:.0f}%')

    stale_note = lambda *ages: max([a for a in ages if a is not None] or [0])
    liq_age = stale_note(age_w, age_m, age_x)
    sup_age = stale_note(age_h, age_c)
    st_age = age_s if age_s is not None else 0
    etf_label = {'good': '吸筹', 'warn': '走弱', 'bad': '衰竭', 'na': '待记录'}[etf_state]
    st_label = {'good': '蓄水', 'warn': '持平', 'bad': '失血', 'na': '无数据'}[st_state]
    odds_label = {'good': '绝望区/机会', 'warn': '中性区', 'bad': '亢奋区', 'na': '无数据'}[odds_state]
    etf_dot = 'good' if etf_state == 'good' else ('bad' if etf_state == 'bad' else ('warn' if etf_state == 'warn' else 'na'))

    fan = fan_chart(m, pdates, pvals)
    pchart = percentile_chart(pdates, pct_series)

    rows_tbl = []
    for k in range(-12, 0):
        i = min(len(prices) - 1, max(90, len(prices) + k * 7))
        j = i if i < len(prices) else len(prices) - 1
        r90 = pvals[j] / pvals[j - 90] - 1
        mv = ''
        if cm and cm.get('mvrv') and cm_last and (pdates[j] - cm_last).days <= 10:
            mv = f"{mvrv:.2f}"
        rows_tbl.append(f"<tr><td>{pdates[j]}</td><td>{pvals[j]:,.0f}</td><td>{pct_series[j]*100:.0f}%</td><td>{mv or '—'}</td><td>{r90*100:+.0f}%</td></tr>")

    JS = """
const io=new IntersectionObserver(es=>es.forEach(e=>{if(e.isIntersecting){e.target.classList.add('in');io.unobserve(e.target)}}),{threshold:.06});
document.querySelectorAll('.reveal').forEach(el=>io.observe(el));
"""

    html = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>比特币周报 · 五仪表盘</title><style>__CSS__</style></head><body>
<nav><div class="in"><span class="logo">₿ 周报</span>
<a href="#overview">总览</a><a href="#gauges">五仪表</a><a href="#checklist">每周清单</a><a href="#matrix">姿态表</a><a href="#data12">数据</a><a href="#gloss">名词表</a></div></nav>
<div class="wrap">

<div class="hero">
<h1>比特币周报 · 五仪表盘</h1>
<div class="sub">每周花 30 分钟, 回答一个问题: <b>现在的比特币, 贵不贵? 市场的钱, 是变多了还是变少了?</b></div>
<div class="sub" style="margin-top:4px">生成时间 __NOW__ · 价格数据截止 <b>__LASTDATE__(__LASTPRICE__)</b> · 来源: __SRC__</div>
</div>

<div class="banner reveal">
  <div class="row">
    <span><span class="k">综合方向</span><span class="pill __DIRS__">__DIR__</span></span>
    <span><span class="k">赔率</span><span class="pill __ODDS__">走廊分位 __PCTNOW__</span></span>
    <span><span class="k">姿态参考</span><span class="pill na">__POS__</span></span>
  </div>
  <div class="alarms">__ALARMS__</div>
</div>

<section id="intro">
<div class="intro reveal">
<b>写给第一次来的你</b> (看懂这三段, 后面的表就都能看懂了):
<ol>
<li>比特币的产量固定、使用者越来越多, 所以长期看, 价格沿着一条<b>缓缓上移的"走廊"</b>走。价格有时冲向天花板(大家疯狂抢购), 有时贴向地板(大家恐慌抛售)。</li>
<li>短期涨跌由"水"决定: 全球央行印了多少钱(①)、场内准备买币的钱有多少(③)、新资金管道每天买进多少(②), 以及有没有人在大批卖出(④)。</li>
<li>颜色规则全世界通用: <b style="color:var(--good)">绿 = 顺风 / 机会</b>, <b style="color:var(--warn)">黄 = 中性 / 留意</b>, <b style="color:var(--bad)">红 = 逆风 / 警报</b>。你不需要预测明天, 只需要每周确认仪表没有从绿变红。</li>
</ol>
</div>
</section>

<section id="overview">
<h2>总览图<span class="en">两张图看懂全局</span></h2>
<div class="chart reveal">__FAN__</div>
<div class="chart reveal">
<div class="cap"><b>价格在走廊内的位置</b> (0%=贴着地板最恐慌, 100%=顶到天花板最亢奋) — 这条线实时讲出周期的位置感</div>
__PCHART__</div>
</section>

<section id="gauges">
<h2>五个仪表<span class="en">每周按 1→3→2→4→5 的顺序看</span></h2>
<div class="grid">

<div class="card reveal"><div class="head"><span class="dot __LIQS__"></span><h3>① 全球美元流动性</h3><span class="state __LIQS__">__LIQT__</span></div>
<div class="plain">一句话: <b>全球央行"放水"还是"抽水"</b>。水多, 万物齐涨, 比特币涨得最凶; 抽水, 它摔得最狠。</div>
<div class="nums">__LIQNUMS__</div>__LIQSPARK__
<details><summary>为什么看它?</summary><p>比特币没有利息、没有利润, 它的价格几乎完全取决于"世界上有多少闲钱在找去处"。历史统计显示, 全球货币量的变化领先比特币约 2~3 个月——所以它是五个仪表里唯一能"提前看"的。</p></details>
<div class="rules">判断规则: <b>M2同比 &gt; +5% 或上行且联储表扩张 = 顺风(绿)</b>; M2同比 &lt; -3% = 逆风(红); 其间 = 中性(黄)。美元指数走强是减分项。</div>
<div class="src">FRED: WALCL / M2SL / DTWEXBGS · 数据龄 __LIQAGE__ 天</div></div>

<div class="card reveal"><div class="head"><span class="dot __ETFD__"></span><h3>② ETF 净流入 vs 新增供给</h3><span class="state __ETFS__">__ETFT__</span></div>
<div class="plain">一句话: <b>2024 年后, 老外能用股票账户买"比特币基金"了</b>。这个管道每天买进多少, 是账面上最清楚的一股买方力量。</div>
<div class="nums">__ETFNUMS__</div>
<details><summary>为什么看它?</summary><p>比特币每天新开采约 450 枚(每年约 16 万枚)。ETF 强势时一个月能吸收数倍于产量的量——想买的人多、能卖的币少, 价格自然被推高; 反过来, 如果这个管道连续 90 天往外流, 说明"正规军"在撤退, 是本轮周期最重要的危险信号。</p></details>
<div class="rules">判断规则: 30日净流入年化 &gt; 产量3倍 = 供给挤压(强); <b>连续90天净流出 = 结构性买盘衰竭(最重要的证伪信号)</b>。</div>
<div class="src">__ETFNOTE__</div></div>

<div class="card reveal"><div class="head"><span class="dot __STS__"></span><h3>③ 稳定币总市值</h3><span class="state __STS__">__STT__</span></div>
<div class="plain">一句话: <b>加密市场里的"美元存款"总量</b>。它变多 = 准备买币的钱在进场; 变少 = 钱在离场。做不了假。</div>
<div class="nums">__STNUMS__</div>__STSPARK__
<details><summary>为什么看它?</summary><p>几乎每笔买币的钱都要先换成 USDT/USDC 这类锚定美元的代币。所以稳定币总量就是"场内弹药库": 价格横盘+弹药增加 = 有人偷偷囤子弹(底部特征); 价格涨+弹药不动 = 全靠杠杆硬撑(脆弱); 弹药减少 = 撤退确认。</p></details>
<div class="rules">判断规则: 横盘 + 90天增 3~5% = 蓄水(绿); 总量同比收缩 = 熊市确认(红); 其间 = 中性。</div>
<div class="src">DefiLlama stablecoincharts/all · 数据龄 __STAGE__ 天</div></div>

<div class="card reveal"><div class="head"><span class="dot __HS__"></span><h3>④ 供给端: 矿工 / 交易所 / MVRV 位置</h3><span class="state __HS__">__HST__</span></div>
<div class="plain">一句话: <b>币的总量永远固定, 价格只取决于"谁在卖"</b>。这里监控的是: 卖压有没有被消化完。</div>
<div class="nums">__HASHNUMS____MVRVNUMS__</div>__HASHSPARK____MVRVSPARK__
<details><summary>为什么看它?</summary><p>两个历史规律: (1) <b>矿工投降</b>——币价低到矿工付不起电费、被迫关机卖币, 算力骤降; 它历史上总出现在大底附近, 之后算力恢复(金叉)往往是可靠的底部信号。(2) <b>交易所余额</b>——大家把币充进交易所通常是为了卖, 余额暴增要警惕。</p></details>
<div class="rules">判断规则: 算力投降后金叉 = 历史级底部信号; 交易所余额90天大增 = 分发警报; 顶部的长期持有人分发需付费数据(Glassnode), 未接入。</div>
<div class="src">blockchain.info hash-rate + CoinMetrics community · __HASHNOTE__ · 数据龄 __SUPAGE__ 天</div></div>

<div class="card reveal"><div class="head"><span class="dot __ODDS__"></span><h3>⑤ 估值定位 (赔率)</h3><span class="state __ODDS__">__ODDST__</span></div>
<div class="plain">一句话: <b>现在买入, 赔率合不合适</b>。它不预测涨跌, 只告诉你: 历史上站在这个位置买入的人, 后来是赚是亏。</div>
<div class="nums">__ODDSNUMS__</div>__ODDSSPARK__
<details><summary>为什么看它?</summary><p>"走廊分位"把过去十几年价格压成 0~100 的读数: 0=贴地板(恐慌到极点), 100=顶天花板(疯狂到极点)。历史上在 10 以下买入, 之后 1-2 年几乎都是赚的; 在 90 以上买入, 大多是亏的。MVRV 则直接衡量全体持币者平均浮盈倍数: 1=刚回本, 大于3=普遍赚很多(危险), 小于1=普遍被套(反而常是机会)。</p></details>
<div class="rules">判断规则: 分位 &lt;10 + MVRV &lt;1 = 历史机会区(绿); 分位 &gt;90 + MVRV≥5 = 亢奋区(红); 其间 = 拿住不动。</div>
<div class="src">幂律分位走廊(创世日锚点, 全样本拟合) + CoinMetrics CapMVRVCur</div></div>
</div>
</section>

<section id="checklist">
<h2>本周检查清单<span class="en">按顺序, 约 30 分钟</span></h2>
<div class="check reveal"><ol>
<li><b>流动性</b>: M2 同比拐了没? 联储表在扩还是在缩? 美元强不强? → 定"顺/逆风"基调</li>
<li><b>稳定币</b>: 90 天在涨还是在缩? 和价格方向背离吗? → 验证场内资金</li>
<li><b>ETF</b>: 本周净流入多少? 记进 <code>etf_flows.csv</code>(可在 GitHub 网页直接编辑) → 看 30 日累计的正负</li>
<li><b>供给端</b>: 算力 MA30 在 MA60 上/下? 交易所有异动? → 排雷</li>
<li><b>赔率</b>: 走廊分位现在多少? → 对照下面的姿态表定仓位, 然后关掉页面, 该干嘛干嘛</li>
</ol></div>
</section>

<section id="matrix">
<h2>姿态参考表<span class="en">方向 × 位置 → 动作</span></h2>
<div class="chart reveal"><table>
<tr><th>方向(①③)</th><th>走廊分位(⑤)</th><th>参考姿态</th></tr>
<tr><td>顺风</td><td>&lt;30%</td><td>积极持有 / 分批加仓</td></tr>
<tr><td>顺风</td><td>30~90%</td><td>持有, 不折腾</td></tr>
<tr><td>顺风</td><td>&gt;90%</td><td>开始分批兑现</td></tr>
<tr><td>中性</td><td>&lt;30%</td><td>逢低分批攒</td></tr>
<tr class="thisrow"><td>中性</td><td>30~90%</td><td>拿住不动 ← 当前位置</td></tr>
<tr><td>中性</td><td>&gt;90%</td><td>分批减仓</td></tr>
<tr><td>逆风</td><td>任意</td><td>防守: 仓位降到底, 等仪表修复再回来</td></tr>
</table></div>
</section>

<section id="data12">
<h2>最近 12 周<span class="en">每周五快照</span></h2>
<div class="chart reveal"><table><tr><th>日期</th><th>价格$</th><th>走廊分位</th><th>MVRV</th><th>90日收益</th></tr>__ROWS__</table></div>
</section>

<section id="gloss">
<h2>名词表<span class="en">点开看人话解释</span></h2>
<div class="gloss reveal">
<details><summary>走廊分位 (0~100)</summary><p>把比特币过去十几年的价格波动, 缩成一条"地板到天花板"的走廊。分位就是价格站在走廊里的高度: 0%=贴地板(恐慌到底), 100%=顶天花板(亢奋到顶)。28% 的意思≈比历史上 72% 的时间都便宜。</p></details>
<details><summary>MVRV</summary><p>全体持币者的平均"账面回报倍数"= 市价 ÷ 全市场平均买入成本。1.0 = 整个市场刚回本; 大于 3 = 大部分人赚很多(通常出现在疯狂顶部); 小于 1 = 大部分人被套(历史上反而靠近底部)。</p></details>
<details><summary>两年均线</summary><p>过去两年每天买入者的平均成本线。价格跌破它 = 两年内进场的人整体亏损, 历史上这种状态只出现在熊市; 重新站上去并站稳 = 熊市结束的信号。</p></details>
<details><summary>减半</summary><p>比特币大约每四年, 新币日产量自动减半(上次 2024-04)。类似"黄金开采量骤降", 历史上牛市顶部多出现在减半后 12~18 个月。</p></details>
<details><summary>稳定币</summary><p>锚定美元的加密代币(USDT、USDC), 1 枚 ≈ 1 美元。它是加密市场里的"银行存款", 大家买币前通常先把手上的币换成它, 或者把美元换成它。它的总量增减 = 新钱进场 / 旧钱离场。</p></details>
<details><summary>ETF 净流入</summary><p>2024 年 1 月起, 美国批准了直接持有比特币的上市基金(ETF)。全世界的人用股票账户就能买。每天"买入减卖出"的净额是公开数据——这是本周期最大的新增买方力量。</p></details>
<details><summary>算力投降 / 金叉</summary><p>挖矿需要电费。币价太低时矿工赚不回电费, 被迫关机, 全网算力(每秒计算量)骤降 = "投降"。历史上它总出现在价格大底附近; 之后算力止跌回升并上穿均线("金叉"), 常被当作底部确认。</p></details>
<details><summary>年化波动率</summary><p>价格上蹿下跳的剧烈程度(换成年度尺度)。比特币正从"过山车"(早年 60~80%)变成"丘陵"(现在约 40%), 通常说明机构和长期资金占比变高、市场变成熟。</p></details>
</div>
</section>

<div class="foot">
数据源: <a href="https://fred.stlouisfed.org">FRED</a>(WALCL/M2SL/DTWEXBGS) · <a href="https://defillama.com/stablecoins">DefiLlama</a>(稳定币) · <a href="https://www.blockchain.com/explorer/charts/hash-rate">blockchain.info</a>(算力) · <a href="https://docs.coinmetrics.io">CoinMetrics community</a>(MVRV/交易所余额/价格补齐) · 价格: 本地 CSV 优先, 落后时自动从 CoinMetrics 补齐<br>
刷新: 本地双击「刷新周报.command」或运行 <code>python3 build.py</code>; GitHub 仓库每周一自动重建并部署。抓取失败时自动使用最近缓存, 卡片标注数据龄。<br>
ETF 净流入无免费稳定接口, 需每周在 <code>etf_flows.csv</code> 记一行(来源: SoSoValue / Farside)。<br>
本页面全部内容为数据整理与研究参考, <b>不构成任何投资建议</b>。所有阈值从历史样本校准, 样本外有效性无法保证。
</div>
</div>
<script>__JS__</script>
</body></html>"""

    html = (html
            .replace('__CSS__', CSS)
            .replace('__JS__', JS)
            .replace('__NOW__', NOW.strftime('%Y-%m-%d %H:%M'))
            .replace('__LASTDATE__', str(last_date))
            .replace('__LASTPRICE__', f'{last_price:,.0f}')
            .replace('__SRC__', price_src)
            .replace('__DIRS__', dir_state)
            .replace('__DIR__', dir_label)
            .replace('__ODDS__', odds_state)
            .replace('__PCTNOW__', f'{pct_now*100:.0f}%')
            .replace('__POS__', pos_label)
            .replace('__ALARMS__', ''.join(f'<span class="alarm {c}">{t}</span>' for c, t in alarms) or '<span class="alarm good">无触发警报</span>')
            .replace('__FAN__', fan)
            .replace('__PCHART__', pchart)
            .replace('__LIQS__', liq_state)
            .replace('__LIQT__', state_txt(liq_state))
            .replace('__LIQNUMS__', liq_nums)
            .replace('__LIQSPARK__', liq_spark)
            .replace('__LIQAGE__', f'{liq_age:.0f}')
            .replace('__ETFD__', etf_dot)
            .replace('__ETFS__', etf_state)
            .replace('__ETFT__', etf_label)
            .replace('__ETFNUMS__', etf_nums)
            .replace('__ETFNOTE__', etf_note)
            .replace('__STS__', st_state)
            .replace('__STT__', st_label)
            .replace('__STNUMS__', st_nums or '—')
            .replace('__STSPARK__', st_spark)
            .replace('__STAGE__', f'{st_age:.0f}')
            .replace('__HS__', hash_state)
            .replace('__HST__', state_txt(hash_state))
            .replace('__HASHNUMS__', hash_nums)
            .replace('__MVRVNUMS__', mvrv_nums)
            .replace('__HASHSPARK__', hash_spark)
            .replace('__MVRVSPARK__', mvrv_spark)
            .replace('__HASHNOTE__', hash_note or '—')
            .replace('__SUPAGE__', f'{sup_age:.0f}')
            .replace('__ODDST__', odds_label)
            .replace('__ODDSNUMS__', odds_nums)
            .replace('__ODDSSPARK__', odds_spark)
            .replace('__ROWS__', ''.join(rows_tbl)))

    out = os.path.join(SITE, 'index.html')
    open(out, 'w').write(html)
    print(f'已生成: {out} ({os.path.getsize(out)/1024:.0f} KB)')
    my2 = f"{liq['m2_yoy']*100:+.1f}%" if liq['m2_yoy'] is not None else 'n/a'
    print(f'快照: 价格 {last_price:,.0f} | 走廊分位 {pct_now*100:.0f}% | 方向 {dir_label} | M2同比 {my2}')

if __name__ == '__main__':
    main()
