@echo off
:: Usage: run.bat paper | backtest | demo
call .venv\Scripts\activate || (echo Run setup.bat first. & exit /b 1)
if "%1"=="backtest" python backtest.py & exit /b
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
