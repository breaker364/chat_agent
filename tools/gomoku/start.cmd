@echo off
chcp 65001 >nul
title Gomoku Server - 五子棋
echo ============================================
echo        Gomoku Server Starting...
echo ============================================
echo.

cd /d "%~dp0"
echo Starting backend on port 8888 ...
start "" "http://localhost:8888"
python backend.py

pause
