# DERIVUSNA-BOT

Simple Deriv bot: SMA crossover on ticks (default R_100), fixed stake, hard daily loss limit.
An experiment, not a money machine. Most short-term traders lose.

## Run
```
pip install -r requirements.txt
python deriv_bot.py                 # paper mode: no account, no token
```

## Demo / real (needs developers.deriv.com app id + Personal Access Token, "trade" scope only)
```
export DERIV_APP_ID=...
export DERIV_TOKEN=...              # never commit or share this
BOT_MODE=demo python deriv_bot.py
```
`real` needs `BOT_MODE=real ALLOW_REAL=1`. Test on paper, then demo for weeks, before real.

Settings (env): SYMBOL, STAKE, DURATION_TICKS, MAX_TRADES, MAX_DAILY_LOSS. Trades log to trades.csv.

## Windows (CMD)
```
setup.bat            :: once
run.bat backtest
run.bat paper
run.bat demo        :: asks for app id + token
```

## Backtest
```
python backtest.py                      # last 24h of ticks (the feed's history limit)
PAYOUT=0.95 python backtest.py
```
Replays the same SMA signal and limits on past ticks. Break-even win rate is 1/(1+payout), about 51.3% at 95%.

## Telegram alerts (optional)
Create a bot with @BotFather, message it once, then get your chat id from `https://api.telegram.org/bot<TOKEN>/getUpdates`.
```
set TELEGRAM_TOKEN=...     :: keep secret, never commit
set TELEGRAM_CHAT_ID=...
```
You get alerts for start, each trade result, errors, disconnects and "daily limit hit". No token set = no alerts.

## Measuring
`trades.csv` now records `ask_price` and `payout_ratio` per trade. `backtest.py` uses the mean real payout from
demo/real rows when it exists (else `PAYOUT` env, else 95%). An old-layout `trades.csv` is kept as `trades_old_<time>.csv`.
`FAST`, `SLOW` and `COOLDOWN_S` are now env settings.

## Research tools
```
run.bat collect     :: leave running; saves every tick to ticks_R_100.csv (feed only keeps ~24h)
run.bat backtest    :: win-rate CI + p-value vs break-even, randomness check
run.bat sweep       :: walk-forward: tune on first half, judge on second half
run.bat test        :: offline unit tests
```
On 24h of R_100: tick changes show no autocorrelation (random), and the best SMA setting on the first half
lost on the second half (46% vs 52% needed). That is overfitting, not an edge. Judge only on out-of-sample data.

## Safety
- `state_<mode>.json` keeps the day's P&L, trade count and open contracts across crashes/restarts.
- Create a file named `STOP` in the folder to halt new trades; delete it to resume.
- No message for `STALE_S` (30) seconds = dead feed, the bot reconnects. Disconnect alerts include Deriv's close code.
- A locked `trades.csv` (open in Excel) no longer breaks a trade; the row is printed instead.

## Dashboard (Postgres + Next.js on Vercel)
```
bot (your PC) --writes--> Postgres (Neon/Supabase) <--reads-- dashboard/ (Vercel)
```
The bot logs trades, events and a 30s heartbeat to Postgres when `DATABASE_URL` is set (unset = no change in behaviour,
and a database outage never stops the bot; `trades.csv` stays the primary log).

1. Create a free Postgres at neon.tech (or Supabase). Copy the connection string. Treat it like a password.
2. Bot: `pip install -r requirements.txt`, then in CMD `set DATABASE_URL=...` before `run.bat demo`. Tables are created automatically.
3. Dashboard: import the repo in Vercel, set **Root Directory = `dashboard`**, add env vars `DATABASE_URL` (same string)
   and `DASHBOARD_PASSWORD`. The site asks for a login (any username) and refuses to load without a password.
4. Local try-out: `cd dashboard && npm install && npm run dev` with a `.env.local` copied from `.env.example`.

Shows live status (running / limit / stopped / offline), today and all-time P&L, win rate with 95% CI, break-even,
p(edge) and a plain-English verdict, equity curve, recent trades and events. Tabs switch paper / demo / real.
`cd dashboard && npm test` checks the stats against the same formulas as `backtest.py`.

## MT5 account (Deriv CFDs | Standard, Windows)
The options bot cannot trade an MT5 account. `mt5_bot.py` drives the MT5 **desktop** app (installed, logged in, symbols visible in Market Watch)
through the `MetaTrader5` Python package. Run these in order:
```
run.bat mt5scan       :: ranks symbols by spread; shows what a stop would risk at the minimum lot. Places nothing.
run.bat mt5check      :: connects, builds the next order, validates it with MT5's order_check. Places nothing.
run.bat mt5backtest   :: strategy on your terminal's history, in R, with CI and a 1st/2nd-half split.
run.bat mt5           :: signals only, no orders.
run.bat mt5live       :: places orders after you type PLACE ORDERS (enabled for that window only).
```
Safeguards in code: stop-loss and take-profit on every order (set at the broker), lot rounded down so risk <= `MT5_RISK_USD` (0.50),
no trade if even the minimum lot risks more, one position at a time, daily loss cap `MT5_DAILY_LOSS_USD` (1.00), equity floor
`MT5_EQUITY_FLOOR` (5.00, halts), max 6 trades/day, spread filter, `STOP` file, halt after 3 failed orders.
`MT5_SYMBOL=auto` picks the lowest-spread symbol that fits the risk limit. Untested against a live MT5 terminal: validate with
`mt5check` first. Trades appear on the dashboard under the `mt5` tab.

## Strategy lab (no money, no MT5 needed)
Tests ideas on ~4 months of real forex candles from Deriv's public feed (cached as `candles_<symbol>_<seconds>.csv`).
```
run.bat lab compare                         :: all strategies on USDJPY M5 (default)
run.bat lab compare --session 12-17         :: only UTC 12:00-17:00 (London/NY overlap)
run.bat lab compare --trend 200             :: only trade with the 200-bar average
run.bat lab sweep sma_cross                 :: tune on the 1st half, judge on the 2nd (honest) half
run.bat lab compare --symbol frxEURUSD --tf 900
```
Strategies: `sma_cross`, `rsi_revert`, `bb_revert`, `donchian`, `trend_pullback`. Entry is the open of the bar AFTER the signal,
the spread is charged as a share of the stop (`--spread-pct`, 0.009 = your USDJPY), both-hit-in-one-bar counts as a loss.
Verdict: PASS needs 300+ trades and a 95% interval above zero in BOTH halves; PROMISING = positive in both halves but not
significant yet; `random>=` shows how often random entries with the same exits beat the strategy. Tests: `python -m unittest`.

## MT5: several pairs, status line, why "no trades"
- `set MT5_SYMBOLS=USDJPY,EURUSD,GBPUSD` runs all of them in one window. The daily loss cap (`MT5_DAILY_LOSS_USD`) and the
  daily trade cap (`MT5_MAX_TRADES`) are shared across the pairs; each pair keeps its own `state_mt5_<pair>.json`.
- Every 15 minutes (`MT5_STATUS_S`) it prints a line such as `[status] USDJPY M5: bars 90, signals 3, opened 0, skipped: spread too wide x3`,
  so a quiet bot is explained. Each skipped signal is also printed when it happens.
- Common reasons for no trade: signals are rare (a 10/30 crossover fires about every 2-3 hours on M5), the spread is above
  `MT5_MAX_SPREAD_FRAC` (0.25) of the stop, or the minimum lot risks more than `MT5_RISK_USD`.
- The dashboard's mt5 tab now shows the real symbols (it used to show the options bot's symbol).
