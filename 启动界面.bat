@echo off
chcp 936 >nul
setlocal
cd /d "%~dp0companion-ai"

echo ========================================
echo    未央 · 陪伴 AI    桌面窗口
echo ========================================
echo.
echo 建议先启动对话模型（一个窗口）：
echo    启动llm.bat        对话模型 127.0.0.1:8080
echo.
echo 记忆检索的嵌入服务（8081）由窗口自动拉起，不用手动开。
echo    想手动开：启动嵌入服务.bat
echo    想关掉自动拉起：set COMPANION_NO_EMBED_AUTOSTART=1
echo.
echo 服务没起也能打开窗口，窗口里会提示缺哪个。
echo.

where python >nul 2>&1
if errorlevel 1 goto no_python

python gui.py
goto end

:no_python
echo [ERROR] PATH 里找不到 python
pause
exit /b 1

:end
if errorlevel 1 pause
exit
