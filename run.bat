@echo off
:: Usage: run.bat paper | backtest | sweep | collect | test | demo | mt5diag | mt5scan | mt5check | mt5backtest | mt5 | mt5live
call .venv\Scripts\activate || (echo Run setup.bat first. & exit /b 1)
if "%1"=="backtest" python backtest.py & exit /b
if "%1"=="sweep" set SWEEP=1& python backtest.py & exit /b
if "%1"=="collect" python collect.py & exit /b
if "%1"=="test" python -m unittest -v & exit /b
if "%1"=="mt5diag" python mt5_bot.py diag & exit /b
if "%1"=="mt5scan" python mt5_bot.py scan & exit /b
if "%1"=="mt5check" python mt5_bot.py check & exit /b
if "%1"=="mt5backtest" python mt5_backtest.py & exit /b
if "%1"=="mt5" python mt5_bot.py run & exit /b
if "%1"=="mt5live" goto mt5live
if "%1"=="demo" goto demo
set BOT_MODE=paper
python deriv_bot.py
exit /b
:demo
:: Token is typed here and kept only for this window, never written to disk
if "%DERIV_APP_ID%"=="" set /p DERIV_APP_ID=Deriv app id: 
if "%DERIV_TOKEN%"=="" set /p DERIV_TOKEN=Deriv token: 
set BOT_MODE=demo
python deriv_bot.py
exit /b
:mt5live
:: Deliberate extra step: orders are only enabled for this window, after you type the phrase
echo This will place REAL orders on your MT5 account, risking up to MT5_RISK_USD per trade.
set /p CONFIRM=Type PLACE ORDERS to continue: 
if not "%CONFIRM%"=="PLACE ORDERS" (echo Cancelled. & exit /b 1)
set MT5_ORDERS=1
set ALLOW_REAL=1
python mt5_bot.py run
