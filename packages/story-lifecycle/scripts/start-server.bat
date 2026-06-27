@echo off
title Story Lifecycle - Install
cd /d "%~dp0\.."

echo ==> Stopping running story process...
taskkill /F /IM story.exe >nul 2>&1

echo ==> Installing story-lifecycle...
pip install -e .
echo ==> Done.
pause
