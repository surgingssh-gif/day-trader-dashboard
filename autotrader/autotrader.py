#!/usr/bin/env python3
"""
autotrader.py - automates the day-trader-skill pack into a paper journal,
a weekly backtest and a dashboard. It never places an order.

  python3 autotrader.py daily             # journal every finished session not logged yet
  python3 autotrader.py daily --backfill 10   # first run: also log the last 10 sessions
  python3 autotrader.py weekly            # backtest 4 strategy variants on the watchlist
  python3 autotrader.py dashboard         # rebuild dashboard.html from the saved results

The strategy is the pack's opening-range breakout: 15-minute opening range,
entry on the first 5-minute close above the range high (no entries after
3:30 PM ET), stop and target set before the entry, flat by 3:55 PM ET.

The odds come from the pack's own Time Machine (trader-timemachine), which only
ever looks at sessions BEFORE the day it is predicting, so logging a day after
the close is not hindsight: the prediction never sees that day's bars.
"""
from __future__ import print_function

import argparse
import datetime
import glob
import importlib.util
import json
import math
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT = os.path.dirname(HERE)                      # ~/Desktop/day-trader
DATA = os.environ.get("TRADER_DATA") or os.path.join(PROJECT, "trader", "auto")       # everything this writes lives here
CACHE = os.path.join(DATA, "cache")
JOURNAL = os.path.join(DATA, "journal.json")
BACKTEST = os.path.join(DATA, "backtest.json")
DASHBOARD = os.environ.get("DASHBOARD_PATH") or os.path.join(PROJECT, "dashboard.html")
CONFIG = os.path.join(HERE, "config.json")
TEMPLATE = os.path.join(HERE, "dashboard_template.html")

SLIP = 0.02            # assumed slippage per share, each side (entry and exit); set in config.json
HISTORY = os.path.join(DATA, "history")   # every full session ever downloaded, so backtests grow past 60 days
OR_BARS = 3            # 15-minute opening range = the 9:30, 9:35, 9:40 bars
LAST_ENTRY_SLOT = 71   # the 15:25 bar closes at 15:30: no entries after 3:30 PM ET
FLAT_SLOT = 76         # the 15:50 bar closes at 15:55: flat by 3:55 PM ET
VARIANTS = [           # what the weekly backtest compares
    {"id": "mid-1R", "label": "Stop at range middle, 1R target", "stop_rule": "or_mid", "target_r": 1.0},
    {"id": "mid-2R", "label": "Stop at range middle, 2R target", "stop_rule": "or_mid", "target_r": 2.0},
    {"id": "low-1R", "label": "Stop at range low, 1R target", "stop_rule": "or_low", "target_r": 1.0},
    {"id": "low-2R", "label": "Stop at range low, 2R target", "stop_rule": "or_low", "target_r": 2.0},
]


def log(msg):
    print("[%s] %s" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg))
    sys.stdout.flush()


# ------------------------------------------------------------------ setup

def load_config():
    cfg = {"watchlist": ["AAPL", "TSLA", "NVDA", "AMD", "SPY"], "scanner_picks": 5,
           "stop_rule": "or_mid", "target_r": 1.0, "risk_dollars": 50, "slippage_per_share": 0.02, "plugin_dir": ""}
    if os.path.exists(CONFIG):
        with open(CONFIG) as fh:
            cfg.update(json.load(fh))
    cfg["watchlist"] = [s.strip().upper() for s in cfg["watchlist"] if s.strip()]
    global SLIP
    SLIP = float(cfg.get("slippage_per_share") or 0)
    return cfg


def find_plugin(cfg):
    """The folder that holds trader-timemachine/, trader-scanner/ and the rest."""
    cands = []
    if os.environ.get("PLUGIN_DIR"):
        cands.append(os.environ["PLUGIN_DIR"])
    if cfg.get("plugin_dir"):
        cands.append(os.path.expanduser(cfg["plugin_dir"]))
    home = os.path.expanduser("~")
    pattern = os.path.join(home, ".claude", "plugins", "**", "trader-timemachine", "timemachine.py")
    hits = sorted(glob.glob(pattern, recursive=True), key=os.path.getmtime, reverse=True)
    cands += [os.path.dirname(os.path.dirname(h)) for h in hits]
    for c in cands:
        if os.path.exists(os.path.join(c, "trader-timemachine", "timemachine.py")):
            return c
    sys.exit("Could not find the day-trader-skill plugin. Install it in Claude Code first, or set "
             "\"plugin_dir\" in autotrader/config.json to the folder that holds trader-timemachine/.")


def load_tm(plugin):
    path = os.path.join(plugin, "trader-timemachine", "timemachine.py")
    spec = importlib.util.spec_from_file_location("timemachine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def read_json(path, default):
    if os.path.exists(path):
        with open(path) as fh:
            return json.load(fh)
    return default


def write_json(path, doc):
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as fh:
        json.dump(doc, fh, indent=1)
    os.replace(tmp, path)


def now_utc():
    return datetime.datetime.now(datetime.timezone.utc).replace(microsecond=0, tzinfo=None)


def half_up(x):
    return math.floor(x * 100 + 0.5) / 100.0


# ------------------------------------------------------------------ data

def get_chart(tm, sym, offline_dir=None):
    """60 days of 5-minute bars -> (path of the saved JSON, {date: [78 bars]} full sessions)."""
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s-60d5m.json" % sym)
    if offline_dir:
        text = tm.read_offline(offline_dir, sym)
    else:
        text = tm.fetch_chart(sym)
        time.sleep(0.5)                      # be polite to the free endpoint
    with open(path, "w") as fh:
        fh.write(text)
    full = parse_full(tm, text, sym)
    if not offline_dir:
        full = merge_history(sym, full)
    return path, full


def parse_full(tm, text, sym):
    result = tm.chart_result(text, sym)
    _, bars, _ = tm.parse_bars(result, now_utc())
    days = tm.split_sessions(bars)
    return {d: [s[i] for i in range(tm.SESSION_BARS)] for d, s in days.items() if len(s) == tm.SESSION_BARS}


def merge_history(sym, full):
    """Add the sessions saved on earlier runs, save the union. Compact: [o, h, l, c, v] per bar."""
    import gzip
    path = os.path.join(HISTORY, "%s.json.gz" % sym)
    saved = {}
    if os.path.exists(path):
        try:
            with gzip.open(path, "rt") as fh:
                saved = json.load(fh)
        except (OSError, ValueError):
            saved = {}
    for d, D in full.items():
        saved[d] = [[round(b["o"], 4), round(b["h"], 4), round(b["l"], 4), round(b["c"], 4), int(b["v"] or 0)] for b in D]
    os.makedirs(HISTORY, exist_ok=True)
    with gzip.open(path + ".tmp", "wt") as fh:
        json.dump(saved, fh, separators=(",", ":"))
    os.replace(path + ".tmp", path)
    out = {}
    for d, rows in saved.items():
        out[d] = [{"date": d, "slot": i, "start": "%02d:%02d" % divmod(570 + 5 * i, 60), "o": r[0], "h": r[1], "l": r[2], "c": r[3], "v": r[4]}
                  for i, r in enumerate(rows)]
    return out


def trade_on(tm, D, stop_rule, target_r, min_entry_min=None):
    """The pack's opening-range breakout on one session. None when it never triggered
    (or, with min_entry_min, when the first breakout came before that time of day)."""
    oh, ol = tm.opening_range(D, OR_BARS)
    k = next((i for i in range(OR_BARS, LAST_ENTRY_SLOT + 1) if D[i]["c"] > oh), None)
    if k is None:
        return None
    if min_entry_min is not None and tm.OPEN_MIN + 5 * (k + 1) < min_entry_min:
        return None
    e = round(D[k]["c"], 2)
    s = tm.rule_stop({"stop_rule": stop_rule}, oh, ol, e, True)
    risk = e - s
    if risk <= 0 or risk < 0.0005 * e:      # trader-exit refuses a stop this tight
        return None
    t = half_up(e + target_r * risk)
    outcome, exit_px, exit_time = "flat", D[FLAT_SLOT]["c"], tm.clock12(tm.OPEN_MIN + 5 * (FLAT_SLOT + 1))
    for b in D[k + 1:FLAT_SLOT + 1]:
        if b["l"] <= s:                      # a bar that touches both counts as the stop
            outcome, exit_px, exit_time = "stop", s, tm.clock12(tm.OPEN_MIN + 5 * (b["slot"] + 1))
            break
        if b["h"] >= t:
            outcome, exit_px, exit_time = "target", t, tm.clock12(tm.OPEN_MIN + 5 * (b["slot"] + 1))
            break
    R = target_r if outcome == "target" else -1.0 if outcome == "stop" else (exit_px - e) / risk
    cost = 2 * SLIP / risk                   # slippage on the way in and on the way out
    return {"entry": e, "stop": s, "target": t, "risk": round(risk, 2), "entry_slot": k,
            "entry_time": tm.clock12(tm.OPEN_MIN + 5 * (k + 1)), "or_high": round(oh, 2), "or_low": round(ol, 2),
            "outcome": outcome, "exit": round(exit_px, 2), "exit_time": exit_time,
            "R": round(R - cost, 3), "R_gross": round(R, 3), "cost_R": round(cost, 3)}


def time_machine(plugin, sym, date, tr, chart_path):
    """The pack's own Time Machine, run as its CLI, on sessions before `date` only."""
    cmd = [sys.executable, os.path.join(plugin, "trader-timemachine", "timemachine.py"), "run",
           "--symbol", sym, "--date", date, "--entry", "%.2f" % tr["entry"], "--stop", "%.2f" % tr["stop"],
           "--target", "%.2f" % tr["target"], "--entry-time", tr["entry_time"], "--until", "15:55",
           "--offline", chart_path, "--now", "%s 16:05" % date, "--json"]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=300)
    if p.returncode != 0:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1] if (p.stderr or p.stdout) else "failed")
    doc = json.loads(p.stdout)
    mc, h = doc["mc"], doc.get("history") or {}
    return {"p_target": mc["p_target"], "p_stop": mc["p_stop"], "p_neither": mc["p_neither"],
            "p_green": mc["p_green"], "avg_R": mc["avg_R"], "seed": mc["seed"], "pool": mc["pool_sessions"],
            "verdict": doc.get("verdict"),
            "history": {k: h.get(k) for k in ("triggered", "target", "stop", "close", "avg_R")}}


def scanner_picks(plugin, date, n, skip):
    """Today's top opening-range breakouts at 10:15 AM ET from the pack's Chart Scanner."""
    scan = os.path.join(plugin, "trader-scanner", "scan.py")
    uni = os.path.join(PROJECT, "trader", "universe.txt")
    if not os.path.exists(uni) or time.time() - os.path.getmtime(uni) > 7 * 86400:
        log("Refreshing the stock universe (weekly)...")
        subprocess.run([sys.executable, scan, "universe"], cwd=PROJECT, timeout=600,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log("Scanning the market for 10:15 AM breakouts (takes a few minutes)...")
    p = subprocess.run([sys.executable, scan, "orb", "--now", "%s 10:15" % date, "--json", "--no-write"],
                       cwd=PROJECT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True,
                       timeout=1800)
    try:
        doc = json.loads(p.stdout)
    except ValueError:
        log("Scanner gave no results: %s" % (p.stdout or p.stderr).strip()[:200])
        return []
    hits = [h for h in doc.get("hits") or [] if h.get("symbol") and h["symbol"].upper() not in skip]
    hits.sort(key=lambda h: -(h.get("price") or 0) * (h.get("volume") or 0))
    return [h["symbol"].upper() for h in hits[:n]]


# ------------------------------------------------------------------ jobs

def ny_now(tm):
    return tm.to_eastern(now_utc())


def run_sip(tm, cfg, plugin, a):
    """Stocks in play (see sip.py). Never lets a problem there stop the main journal."""
    if getattr(a, "no_sip", False):
        return
    try:
        sys.path.insert(0, HERE)
        import sip
        if sip.run(tm, cfg, plugin, PROJECT, DATA, parse_full, SLIP, offline_dir=a.offline):
            build_dashboard()
    except Exception as e:
        import traceback
        traceback.print_exc()
        log("Stocks-in-play step failed (the main journal is fine): %s" % e)


def cmd_daily(a):
    cfg = load_config()
    plugin = find_plugin(cfg)
    tm = load_tm(plugin)
    try:
        _daily(a, cfg, plugin, tm)
    finally:
        run_sip(tm, cfg, plugin, a)


def _daily(a, cfg, plugin, tm):
    journal = read_json(JOURNAL, {"entries": [], "days": []})
    for r in journal["entries"]:              # older entries were logged before costs were counted
        if r.get("status") == "triggered" and "R_gross" not in r and r.get("risk"):
            cost = 2 * SLIP / r["risk"]
            r["R_gross"], r["cost_R"], r["R"] = r["R"], round(cost, 3), round(r["R"] - cost, 3)
    done_days = set(journal["days"])
    ny = ny_now(tm)
    today = ny.date().isoformat()
    after_close = ny.hour * 60 + ny.minute >= 16 * 60 + 5

    charts, full = {}, {}
    syms = list(cfg["watchlist"])
    for s in syms:
        try:
            charts[s], full[s] = get_chart(tm, s, a.offline)
        except Exception as e:
            log("%s: no data (%s)" % (s, e))
    if not full:
        sys.exit("No chart data for any watchlist symbol. Is the Mac online?")

    sessions = sorted(set(d for f in full.values() for d in f))
    if not (after_close or a.offline):
        sessions = [d for d in sessions if d < today]
    todo = [d for d in sessions if d not in done_days]
    if not done_days and not a.backfill:
        todo = todo[-1:]                      # first run: just the latest session
    elif a.backfill:
        todo = [d for d in sessions[-a.backfill:] if d not in done_days]
    else:                                     # catch up on days missed while the Mac slept, at most 10
        todo = [d for d in todo if d > max(done_days)][-10:]
    if not todo:
        log("Nothing new to log (latest session %s is already in the journal)." % sessions[-1])
        if run_versions(tm, cfg, journal, full):
            write_json(JOURNAL, journal)
            log("Test versions updated.")
            build_dashboard()
        return

    picks = []
    latest = sessions[-1]
    if cfg.get("scanner_picks") and latest in todo and latest == today and not a.offline:
        try:
            picks = scanner_picks(plugin, today, int(cfg["scanner_picks"]), set(syms))
            log("Scanner picks: %s" % (", ".join(picks) or "none"))
            for s in picks:
                try:
                    charts[s], full[s] = get_chart(tm, s)
                except Exception as e:
                    log("%s: no data (%s)" % (s, e))
        except Exception as e:
            log("Scanner failed, carrying on with the watchlist: %s" % e)

    for d in todo:
        for s in syms + [p for p in picks if d == latest]:
            D = full.get(s, {}).get(d)
            if D is None:
                continue
            row = {"date": d, "symbol": s, "source": "watchlist" if s in syms else "scanner",
                   "rule": "%s-%gR" % (cfg["stop_rule"].replace("or_", ""), cfg["target_r"])}
            tr = trade_on(tm, D, cfg["stop_rule"], float(cfg["target_r"]))
            if tr is None:
                row["status"] = "no_trigger"
                journal["entries"].append(row)
                continue
            row.update(tr, status="triggered")
            try:
                row["tm"] = time_machine(plugin, s, d, tr, charts[s])
            except Exception as e:
                row["tm_error"] = str(e)[:200]
            journal["entries"].append(row)
            log("%s %s: entry %.2f at %s -> %s (%+.2fR)%s" % (
                d, s, tr["entry"], tr["entry_time"], tr["outcome"], tr["R"],
                "  TM target-first %.0f%%" % (100 * row["tm"]["p_target"]) if "tm" in row else ""))
        journal["days"].append(d)
    journal["days"] = sorted(set(journal["days"]))
    journal["entries"].sort(key=lambda r: (r["date"], r["symbol"]))
    run_versions(tm, cfg, journal, full)
    journal["updated"] = now_utc().isoformat() + "Z"
    write_json(JOURNAL, journal)
    log("Logged %d session(s): %s" % (len(todo), ", ".join(todo)))
    build_dashboard()


def hm_to_min(t):
    h, m = str(t).split(":")
    return int(h) * 60 + int(m)


def run_versions(tm, cfg, journal, full):
    """Test versions of the rule, run on every logged day next to the live rule.
    Results after a version's "since" date are the honest, out-of-sample test."""
    out = journal.setdefault("versions", {})
    changed = False
    for v in cfg.get("versions") or []:
        vid = v["id"]
        spec = {k: v.get(k) for k in ("stop_rule", "target_r", "symbols", "min_entry")}
        spec["slippage_per_share"] = SLIP
        rec = out.get(vid)
        if not rec or rec.get("spec") != spec:          # new or edited version: start over
            rec = out[vid] = {"spec": spec, "trades": [], "days": []}
        rec.update(label=v.get("label", vid), since=v.get("since"), baseline=bool(v.get("baseline")))
        syms = cfg["watchlist"] if v.get("symbols") in (None, "watchlist") else [x.upper() for x in v["symbols"]]
        me = hm_to_min(v["min_entry"]) if v.get("min_entry") else None
        done = set(rec["days"])
        for d in journal["days"]:
            if d in done or not any(d in full.get(sym, {}) for sym in syms):
                continue
            for sym in syms:
                D = full.get(sym, {}).get(d)
                if D is None:
                    continue
                tr = trade_on(tm, D, v.get("stop_rule", "or_mid"), float(v.get("target_r", 1)), me)
                if tr:
                    rec["trades"].append({"date": d, "symbol": sym, "R": tr["R"], "outcome": tr["outcome"],
                                          "entry_time": tr["entry_time"], "exit_time": tr["exit_time"]})
            rec["days"].append(d)
            changed = True
        rec["days"].sort()
        rec["trades"].sort(key=lambda t: (t["date"], t["entry_time"], t["symbol"]))
    for vid in list(out):                               # versions removed from the config
        if vid not in {v["id"] for v in cfg.get("versions") or []}:
            del out[vid]
            changed = True
    return changed


def cmd_weekly(a):
    cfg = load_config()
    plugin = find_plugin(cfg)
    tm = load_tm(plugin)
    journal = read_json(JOURNAL, {"entries": []})
    recent_picks = sorted(set(r["symbol"] for r in journal["entries"][-200:] if r.get("source") == "scanner"))
    syms = list(cfg["watchlist"])
    out = {"created": now_utc().isoformat() + "Z", "variants": VARIANTS, "symbols": {}, "trades": []}
    for s in syms:
        try:
            _, full = get_chart(tm, s, a.offline)
        except Exception as e:
            log("%s: no data (%s)" % (s, e))
            continue
        dates = sorted(full)
        out["symbols"][s] = {"sessions": len(dates), "from": dates[0], "to": dates[-1], "variants": {}}
        for v in VARIANTS:
            trades = []
            for d in dates:
                tr = trade_on(tm, full[d], v["stop_rule"], v["target_r"])
                if tr:
                    trades.append({"date": d, "symbol": s, "variant": v["id"], "outcome": tr["outcome"],
                                   "R": tr["R"], "entry": tr["entry"], "entry_time": tr["entry_time"]})
            out["symbols"][s]["variants"][v["id"]] = summarize(trades, len(dates))
            out["trades"] += trades
        best = max(VARIANTS, key=lambda v: out["symbols"][s]["variants"][v["id"]]["avg_R"] or -9)
        log("%s: %d sessions, best variant %s (avg %+.3fR)" % (
            s, len(dates), best["id"], out["symbols"][s]["variants"][best["id"]]["avg_R"] or 0))
    out["overall"] = {v["id"]: summarize([t for t in out["trades"] if t["variant"] == v["id"]],
                                         sum(x["sessions"] for x in out["symbols"].values()))
                      for v in VARIANTS}
    out["scanner_picks_seen"] = recent_picks
    write_json(BACKTEST, out)
    log("Backtest saved.")
    build_dashboard()


def summarize(trades, sessions):
    n = len(trades)
    if not n:
        return {"sessions": sessions, "fired": 0, "target": 0, "stop": 0, "flat": 0,
                "win_rate": None, "avg_R": None, "total_R": 0}
    wins = sum(1 for t in trades if t["R"] > 0)
    tot = sum(t["R"] for t in trades)
    return {"sessions": sessions, "fired": n,
            "target": sum(1 for t in trades if t["outcome"] == "target"),
            "stop": sum(1 for t in trades if t["outcome"] == "stop"),
            "flat": sum(1 for t in trades if t["outcome"] == "flat"),
            "win_rate": round(wins / float(n), 4), "avg_R": round(tot / n, 3), "total_R": round(tot, 2)}


def slim_sip(st):
    """Only what the dashboard draws: keeps the page light."""
    if not st:
        return None
    keep = ("date", "symbol", "variant", "side", "R", "R_gross", "outcome", "entry_time", "exit_time", "rvol", "entry", "exit")
    last = sorted(st.get("picks", {}))[-15:]
    return {"since": st.get("since"), "variants": st.get("variants"), "days": st.get("days"), "universe": st.get("universe"),
            "fetched": st.get("fetched"), "updated": st.get("updated"), "slip": st.get("slip"),
            "picks": {d: st["picks"][d] for d in last},
            "trades": [{k: t.get(k) for k in keep} for t in st.get("trades", [])]}


def build_dashboard():
    cfg = load_config()
    now = datetime.datetime.now(datetime.timezone.utc)
    nxt = now.replace(hour=21, minute=30, second=0, microsecond=0)   # the workflow's weekday schedule
    while nxt <= now or nxt.weekday() >= 5:
        nxt += datetime.timedelta(days=1)
    data = {"journal": read_json(JOURNAL, {"entries": [], "days": []}),
            "backtest": read_json(BACKTEST, None),
            "sip": slim_sip(read_json(os.path.join(DATA, "sip", "sip.json"), None)),
            "config": {k: cfg.get(k) for k in ("watchlist", "scanner_picks", "stop_rule", "target_r", "risk_dollars", "versions", "slippage_per_share", "sip")},
            "built": datetime.datetime.now().strftime("%a %b %-d, %-I:%M %p"),
            "built_iso": now.isoformat(),
            "next_run": nxt.astimezone().strftime("%a %b %-d, %-I:%M %p")}
    with open(TEMPLATE) as fh:
        html = fh.read()
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    os.makedirs(os.path.dirname(DASHBOARD) or ".", exist_ok=True)
    with open(DASHBOARD, "w") as fh:
        fh.write(html.replace("/*__DATA__*/null", blob))
    log("Dashboard updated: %s" % DASHBOARD)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("daily")
    p.add_argument("--backfill", type=int, default=0, help="also log the last N sessions")
    p.add_argument("--no-sip", action="store_true", help="skip the stocks-in-play step")
    p.add_argument("--offline", help="folder of saved <SYM>-60d5m.json(.gz) charts (testing)")
    p = sub.add_parser("weekly")
    p.add_argument("--offline", help="folder of saved <SYM>-60d5m.json(.gz) charts (testing)")
    sub.add_parser("dashboard")
    a = ap.parse_args()
    if a.cmd == "daily":
        cmd_daily(a)
    elif a.cmd == "weekly":
        cmd_weekly(a)
    elif a.cmd == "dashboard":
        build_dashboard()
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
