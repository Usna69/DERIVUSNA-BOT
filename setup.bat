@echo off
:: One-time setup: creates .venv and installs dependencies
where python >nul 2>nul || (echo Python not found. Install Python 3.10+ and tick "Add to PATH". & exit /b 1)
python -m venv .venv || exit /b 1
call .venv\Scripts\activate
pip install -r requirements.txt || exit /b 1
echo.
echo Setup done. Next: run.bat paper  ^|  run.bat backtest  ^|  run.bat demo
