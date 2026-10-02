@echo off
rem Source: Original code for this project. Separate local task-budget demo ledger.
cd /d "%~dp0.."
python -m ai_wallet --task-budgets --open
if errorlevel 1 pause
