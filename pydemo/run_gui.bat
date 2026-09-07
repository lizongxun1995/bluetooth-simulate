@echo off
cd /d "%~dp0.."
python pydemo\gui.py
if errorlevel 1 pause
