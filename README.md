# Day-trader dashboard

A paper journal for the opening-range breakout from the
[day-trader-skill](https://github.com/Jakeschincariol/day-trader-skill) pack,
run automatically by GitHub Actions and published with GitHub Pages.
**It never places an order and never connects to a broker.**

- **Weekdays after the close:** for each watchlist stock plus the day's top 5
  scanner breakouts, checks whether the setup fired (first 5-minute close above
  the 15-minute opening range, before 3:30 PM ET). If it did, it sets the stop
  (range middle) and the 1R target, gets the pack's Time Machine odds from
  *earlier* sessions only, and records what really happened by 3:55 PM ET.
- **Saturdays:** backtests four versions (stop at range middle or low, 1R or
  2R target) over the last ~59 sessions.
- **Dashboard:** `docs/index.html`, served by GitHub Pages.

## Settings

Edit [`autotrader/config.json`](autotrader/config.json) on GitHub:
`watchlist`, `scanner_picks`, `stop_rule` (`or_mid` or `or_low`), `target_r`.

## Run it by hand

Actions tab, then **Update journal and dashboard**, then **Run workflow**.

## Notes

- Data is free Yahoo Finance 5-minute bars: unofficial, and can lag or fail.
- Simulated odds are not predictions. Education and tooling, not financial advice.
- The pack is pinned to a reviewed commit (`PLUGIN_SHA` in the workflow).
