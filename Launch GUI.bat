@echo off
cd /d "%~dp0"
python BS2RandomizerGUI.py
if errorlevel 1 pause
