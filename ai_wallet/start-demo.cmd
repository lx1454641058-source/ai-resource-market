@echo off
rem Source: Original code for this project. Local simulated payments only.
cd /d "%~dp0.."
python -m ai_wallet --open
if errorlevel 1 pause
