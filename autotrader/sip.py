"""
Stocks in play: the opening-range breakout on the day's most unusually active stocks.

Each trading day, about 600 of the most-traded US stocks are ranked by RELATIVE
VOLUME of their first 5-minute bar (9:30-9:35 ET) against the same bar's average
over their previous 14 sessions. The top 20 are that day's "stocks in play".
Only data up to 9:35 AM is used to pick them, so there is no hindsight.

Several versions of the breakout are then run on those 20 stocks, with costs. The
5-minute versions follow the published stocks-in-play research: the first 5-minute
candle sets the direction, a stop order at the range edge enters, the stop is 10%
of the stock's 14-day average daily range, and the trade is held to the close.
Days before `since` in the config are the backtest; days from `since` on are the
fair test. The day's bars for every pick are saved under trader/auto/sip/days/
so the dataset keeps growing past Yahoo's 60-day limit.
"""
from __future__ import print_function

import datetime
import gzip
import json
import os
import statistics
import subprocess
import sys
import time

LAST_ENTRY_SLOT = 71   # entries close by 3:30 PM ET
FLAT_SLOT = 76         # flat by 3:55 PM ET
LOOKBACK = 14          # sessions for the relative-volume and ATR baselines
STRESS_SLIP = 0.05     # the "what if slippage is worse" check: 5 cents a share each way
MEASURE = 2            # bump when what is measured changes, so stored days are recomputed

DEFAULT = {
    "enabled": True,
    "since": "2026-11-03",
    "universe_size": 600,
    "picks_per_day": 20,
    "min_price": 5.0,
    "min_atr": 0.50,
    "min_rvol": 1.0,
    "variants": [
        {"id": "5m-both-atr-close", "short": "5-min · both ways · hold", "label": "5-min range · both directions · tight stop · hold to close",
         "or_bars": 1, "side": "first_candle", "stop": "atr10", "target_r": None, "entry": "stop_order"},
        {"id": "5m-long-atr-close", "short": "5-min · long only · hold", "label": "5-min range · long only · tight stop · hold to close",
         "or_bars": 1, "side": "long_first_candle", "stop": "atr10", "target_r": None, "entry": "stop_order"},
        {"id": "5m-both-atr-2R", "short": "5-min · both ways · 2R", "label": "5-min range · both directions · tight stop · 2R target",
         "or_bars": 1, "side": "first_candle", "stop": "atr10", "target_r": 2.0, "entry": "stop_order"},
        {"id": "15m-both-mid-1R", "short": "15-min classic · 1R", "label": "15-min range · both directions · middle stop · 1R (classic rule)",
         "or_bars": 3, "side": "both", "stop": "or_mid", "target_r": 1.0, "entry": "close"},
    ],
}


def log(msg):
    print("[%s] sip: %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg))
    sys.stdout.flush()


def cfg_of(cfg):
    out = dict(DEFAULT)
    out.update(cfg.get("sip") or {})
    return out


# ------------------------------------------------------------------ universe

def build_universe(plugin, project, out_path, size, min_price):
    """The most-traded US stocks by dollar volume (price x 20-day average volume)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("scan", os.path.join(plugin, "trader-scanner", "scan.py"))
    scan = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(scan)
    uni = os.path.join(project, "trader", "universe.txt")
    if not os.path.exists(uni) or time.time() - os.path.getmtime(uni) > 6 * 86400:
        log("downloading the list of US stocks")
        subprocess.run([sys.executable, os.path.join(plugin, "trader-scanner", "scan.py"), "universe"], cwd=project,
                       timeout=600, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    syms = [ln.split("|")[0].strip() for ln in open(uni) if ln.strip() and not ln.startswith("#")]
    log("ranking %d stocks by dollar volume (spark, 20 a request)" % len(syms))
    data, failed = scan.fetch_spark(syms, "1mo", "1d", 0.3)
    rows = []
    for s, ser in data.items():
        c = [x for x in ser.get("c") or [] if x is not None]
        v = [x for x in (ser.get("v") or []) if x is not None]
        meta = ser.get("meta") or {}
        price = meta.get("regularMarketPrice") or (c[-1] if c else None)
        if v:
            vol = statistics.mean(v[-20:])
        else:
            vol = meta.get("regularMarketVolume") or 0
        if price and price >= min_price and vol:
            rows.append((price * vol, s, price, vol))
    rows.sort(reverse=True)
    top = [{"symbol": s, "price": round(p, 2), "avg_volume": int(v), "dollar_volume": int(dv)} for dv, s, p, v in rows[:size]]
    doc = {"created": datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), "count": len(top),
           "scanned": len(syms), "failed": len(failed), "symbols": top}
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w") as fh:
        json.dump(doc, fh, indent=1)
    log("universe: %d stocks (of %d with data)" % (len(top), len(rows)))
    return doc


# ------------------------------------------------------------------ features

def daily_bar(D):
    return {"o": D[0]["o"], "h": max(b["h"] for b in D), "l": min(b["l"] for b in D), "c": D[-1]["c"]}


def features(full):
    """{date: {rvol, atr, open, v0}} for every session with LOOKBACK sessions before it."""
    dates = sorted(full)
    out = {}
    dl = [daily_bar(full[d]) for d in dates]
    v0 = [full[d][0]["v"] or 0 for d in dates]
    for i in range(LOOKBACK + 1, len(dates)):
        base = [x for x in v0[i - LOOKBACK:i] if x > 0]
        if len(base) < LOOKBACK - 2 or not v0[i]:
            continue
        trs = []
        for j in range(i - LOOKBACK, i):
            pc = dl[j - 1]["c"]
            trs.append(max(dl[j]["h"] - dl[j]["l"], abs(dl[j]["h"] - pc), abs(dl[j]["l"] - pc)))
        out[dates[i]] = {"rvol": v0[i] / statistics.mean(base), "atr": statistics.mean(trs),
                         "open": full[dates[i]][0]["o"], "v0": v0[i], "prev_close": dl[i - 1]["c"],
                         # short-sale rule carries over: yesterday's low was 10%+ under the close before it
                         "prev_ssr": dl[i - 1]["l"] <= 0.9 * dl[i - 2]["c"]}
    return out


# ------------------------------------------------------------------ the trade

def cents(x, down):
    import math
    c = round(x * 100, 4)
    return (math.floor(c) if down else math.ceil(c)) / 100.0


def clock12(minutes):
    h, m = divmod(minutes, 60)
    return "%d:%02d %s" % (h % 12 or 12, m, "AM" if h < 12 else "PM")


def simulate(D, v, atr, slip):
    """One version on one session. Long and short. None when it never triggered."""
    n = v["or_bars"]
    oh, ol = max(b["h"] for b in D[:n]), min(b["l"] for b in D[:n])
    first = D[0]
    side_mode = v["side"]
    if side_mode in ("first_candle", "long_first_candle"):
        want = 1 if first["c"] > first["o"] else -1 if first["c"] < first["o"] else 0
        if want == 0 or (side_mode == "long_first_candle" and want < 0):
            return None
    else:
        want = None                                   # "both": whichever side breaks first; "long": long only
    k = side = None
    stop_order = v.get("entry") == "stop_order"
    allow_long = want in (None, 1) and side_mode != "short"
    allow_short = want in (None, -1) and side_mode not in ("long", "long_first_candle")
    for i in range(n, LAST_ENTRY_SLOT + 1):
        b = D[i]
        up = (b["h"] > oh) if stop_order else (b["c"] > oh)
        dn = (b["l"] < ol) if stop_order else (b["c"] < ol)
        if up and allow_long:
            k, side = i, 1
            break
        if dn and allow_short:
            k, side = i, -1
            break
    if k is None:
        return None
    if stop_order:      # a resting stop order fills at the range edge, or at the open if the bar gaps past it
        e = round(max(oh, D[k]["o"]) if side > 0 else min(ol, D[k]["o"]), 2)
    else:               # wait for the 5-minute candle to close beyond the range
        e = round(D[k]["c"], 2)
    if v["stop"] == "atr10":
        s = cents(e - 0.10 * atr, True) if side > 0 else cents(e + 0.10 * atr, False)
    else:
        s = cents((oh + ol) / 2, side > 0)
    risk = (e - s) * side
    if risk <= 0 or risk < 0.0005 * e:
        return None
    tr = v.get("target_r")
    t = round(e + side * tr * risk, 2) if tr else None
    outcome, xp, xt = "flat", D[FLAT_SLOT]["c"], FLAT_SLOT
    first_check = k + 1
    if stop_order and ((D[k]["c"] <= s) if side > 0 else (D[k]["c"] >= s)):
        outcome, xp, xt = "stop", s, k        # fell back through the stop within the entry candle
        first_check = FLAT_SLOT + 1
    for b in D[first_check:FLAT_SLOT + 1]:
        if (b["l"] <= s) if side > 0 else (b["h"] >= s):
            outcome, xp, xt = "stop", s, b["slot"]
            break
        if t is not None and ((b["h"] >= t) if side > 0 else (b["l"] <= t)):
            outcome, xp, xt = "target", t, b["slot"]
            break
    R = tr if outcome == "target" else -1.0 if outcome == "stop" else (xp - e) * side / risk
    cost = 2 * slip / risk
    return {"side": "long" if side > 0 else "short", "entry": e, "stop": s, "target": t, "risk": round(risk, 3),
            "entry_slot": k, "exit_slot": xt, "R_5c": round(R - 2 * STRESS_SLIP / risk, 3),
            "entry_time": clock12(570 + 5 * (k if stop_order else k + 1)), "exit_time": clock12(570 + 5 * (xt + 1)), "exit": round(xp, 2),
            "outcome": outcome, "R_gross": round(R, 3), "cost_R": round(cost, 3), "R": round(R - cost, 3)}


# ------------------------------------------------------------------ the daily job

def expand(rows, d):
    return [{"date": d, "slot": i, "o": r[0], "h": r[1], "l": r[2], "c": r[3], "v": r[4]} for i, r in enumerate(rows)]


def compact(D):
    return [[round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), int(b["v"] or 0)] for b in D]


def run(tm, cfg, plugin, project, data_dir, parse_full, slip, offline_dir=None, max_new_days=None):
    sc = cfg_of(cfg)
    if not sc.get("enabled"):
        return False
    sdir = os.path.join(data_dir, "sip")
    store_path = os.path.join(sdir, "sip.json")
    store = json.load(open(store_path)) if os.path.exists(store_path) else {"days": [], "picks": {}, "trades": [], "spec": None}
    spec = {"variants": sc["variants"], "slip": slip, "picks_per_day": sc["picks_per_day"], "min_price": sc["min_price"],
            "min_atr": sc["min_atr"], "min_rvol": sc["min_rvol"], "measure": MEASURE}
    if store.get("spec") != spec:                    # rules changed: recompute everything from the saved days
        log("rules changed: recomputing")
        store = {"days": [], "picks": {}, "trades": [], "spec": spec}
    uni_path = os.path.join(sdir, "universe.json")
    uni = json.load(open(uni_path)) if os.path.exists(uni_path) else None
    if offline_dir:
        syms = sorted(f.split("-60d5m")[0] for f in os.listdir(offline_dir) if "-60d5m.json" in f)
    else:
        if not uni or (datetime.datetime.utcnow() - datetime.datetime.strptime(uni["created"], "%Y-%m-%dT%H:%M:%SZ")).days >= 7:
            uni = build_universe(plugin, project, uni_path, sc["universe_size"], sc["min_price"])
        syms = [r["symbol"] for r in uni["symbols"]]
    done = set(store["days"])
    log("downloading 60 days of 5-minute bars for %d stocks" % len(syms))
    feats, fulls, failed, t0 = {}, {}, 0, time.time()
    for n, s in enumerate(syms):
        try:
            text = tm.read_offline(offline_dir, s) if offline_dir else tm.fetch_chart(s)
            full = parse_full(tm, text, s)
        except Exception:
            failed += 1
            continue
        if not offline_dir:
            time.sleep(0.25)
        feats[s] = features(full)
        fulls[s] = {d: compact(D) for d, D in full.items() if d in feats[s] and d not in done}   # keep memory small
        if (n + 1) % 100 == 0:
            log("  %d of %d (%d failed, %.0fs)" % (n + 1, len(syms), failed, time.time() - t0))
    log("got %d stocks, %d failed" % (len(fulls), failed))
    if not fulls:
        return False
    all_days = sorted(set(d for f in feats.values() for d in f))
    todo = [d for d in all_days if d not in done]
    if max_new_days:
        todo = todo[-max_new_days:]
    vids = [v["id"] for v in sc["variants"]]
    added = 0
    for d in todo:
        ranked = []
        for s, f in feats.items():
            x = f.get(d)
            if not x or d not in fulls.get(s, {}) or x["open"] < sc["min_price"] or x["atr"] < sc["min_atr"] or x["rvol"] < sc["min_rvol"]:
                continue
            ranked.append((x["rvol"], s, x))
        ranked.sort(reverse=True)
        picks = ranked[:sc["picks_per_day"]]
        if len(picks) < 5:                         # too little data that day to call it a fair day
            continue
        snap = []
        store["picks"][d] = []
        for rv, s, x in picks:
            D = expand(fulls[s][d], d)
            pick = {"symbol": s, "rvol": round(rv, 2), "atr": round(x["atr"], 2), "open": round(x["open"], 2),
                    "first": "up" if D[0]["c"] > D[0]["o"] else "down" if D[0]["c"] < D[0]["o"] else "flat",
                    "day_change": round((D[-1]["c"] / D[0]["o"] - 1) * 100, 2)}
            store["picks"][d].append(pick)
            snap.append({"symbol": s, "rvol": pick["rvol"], "atr": pick["atr"], "bars": fulls[s][d]})
            ssr_slot = next((j for j, b in enumerate(D) if b["l"] <= 0.9 * x["prev_close"]), None)
            for v in sc["variants"]:
                t = simulate(D, v, x["atr"], slip)
                if t:
                    # short-sale rule: after a 10% drop a stock can only be shorted on an uptick, so a
                    # stop order selling into the fall would not fill the way the backtest assumes
                    t["ssr"] = t["side"] == "short" and (x["prev_ssr"] or (ssr_slot is not None and ssr_slot <= t["entry_slot"]))
                    t.update(date=d, symbol=s, variant=v["id"], rvol=pick["rvol"], rank=len(store["picks"][d]))
                    store["trades"].append(t)
        os.makedirs(os.path.join(sdir, "days"), exist_ok=True)
        with gzip.open(os.path.join(sdir, "days", "%s.json.gz" % d), "wt") as fh:
            json.dump({"date": d, "picks": snap}, fh, separators=(",", ":"))
        store["days"].append(d)
        added += 1
    store["days"] = sorted(set(store["days"]))
    store["trades"].sort(key=lambda t: (t["date"], t["variant"], t["symbol"]))
    store.update(since=sc["since"], variants=sc["variants"], universe=len(syms), fetched=len(fulls),
                 updated=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"), spec=spec, slip=slip)
    os.makedirs(sdir, exist_ok=True)
    with open(store_path + ".tmp", "w") as fh:
        json.dump(store, fh, separators=(",", ":"))
    os.replace(store_path + ".tmp", store_path)
    for vid in vids:
        rs = [t["R"] for t in store["trades"] if t["variant"] == vid]
        if rs:
            log("%-20s %4d trades  avg %+.3fR net  (%+.3fR before costs)" % (
                vid, len(rs), statistics.mean(rs), statistics.mean(t["R_gross"] for t in store["trades"] if t["variant"] == vid)))
    log("logged %d new day(s)" % added)
    return bool(added)
