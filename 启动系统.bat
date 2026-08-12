@echo off
chcp 65001 >nul
title 车道线异常智能归因系统 v2.0
echo ============================================================
echo   🚗 车道线异常智能归因系统  v2.0
echo   D760 王者6x4 自主一代 L2+ 智驾项目
echo ============================================================
echo.
cd /d "%~dp0"

rem ==== 检查 Python ====
where python >nul 2>&1
if errorlevel 1 (
    echo [X] 未找到 Python, 请先安装 Python 3.8+
    pause
    exit /b
)

rem ==== 检查端口 ====
netstat -ano | findstr ":5000" | findstr "LISTENING" > "%TEMP%\port5000.txt" 2>&1
if %errorlevel%==0 (
    echo [!] 检测到系统已在运行 (端口5000被占用)
    echo.
    echo   1. 直接打开浏览器使用
    echo   2. 停止旧服务并重新启动
    echo.
    set /p choice=请选择 (1/2): 
    if "%choice%"=="2" (
        echo.
        echo 正在停止旧服务...
        for /f "tokens=5" %%p in (%TEMP%\port5000.txt) do (
            taskkill /f /pid %%p >nul 2>&1
        )
        del "%TEMP%\port5000.txt" >nul 2>&1
        timeout /t 2 >nul
    ) else (
        start "" http://127.0.0.1:5000/
        exit /b
    )
)
del "%TEMP%\port5000.txt" >nul 2>&1

echo 正在启动本地服务...
echo 启动完成后将自动打开浏览器
echo 关闭本窗口即停止服务
echo.

rem ==== 延迟打开浏览器 ====
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:5000/"

python app.py
echo.
echo 服务已停止。
pause