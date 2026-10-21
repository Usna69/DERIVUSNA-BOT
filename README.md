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
