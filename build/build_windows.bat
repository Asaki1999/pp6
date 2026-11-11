@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0.."

echo ==^> 准备虚拟环境
if not exist .venv (
    py -3 -m venv .venv
    if errorlevel 1 (
        echo 未找到 Python，请先安装 Python 3.10+ 并勾选 Add to PATH
        pause
        exit /b 1
    )
)
call .venv\Scripts\activate.bat
python -m pip install -q --upgrade pip
python -m pip install -q -r requirements-desktop.txt
if errorlevel 1 (
    echo 依赖安装失败，请检查网络
    pause
    exit /b 1
)

echo ==^> 清理旧产物
if exist build\work rmdir /s /q build\work
if exist dist rmdir /s /q dist

echo ==^> 开始打包（约 1-3 分钟）
.venv\Scripts\pyinstaller build\pp6.spec --noconfirm --distpath dist --workpath build\work
if errorlevel 1 (
    echo 打包失败
    pause
    exit /b 1
)

echo.
echo 打包完成: dist\PP6ImportBuilder\PP6ImportBuilder.exe
echo.
echo 分发方式：把整个 dist\PP6ImportBuilder 文件夹压缩发给同事（不能只发 exe，
echo 它依赖同目录下的 _internal 文件夹）。
echo Windows 10/11 自带 Edge WebView2；若极老系统缺这个组件，程序会自动改用浏览器打开。
echo.
pause
