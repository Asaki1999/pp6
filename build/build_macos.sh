#!/usr/bin/env bash
# macOS 打包：产出 dist/PP6 Studio.app（内含 Python 运行时，目标机器无需安装任何东西）
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."

echo "==> 准备虚拟环境"
if [ ! -d .venv ]; then
  python3 -m venv .venv
fi
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements-desktop.txt

echo "==> 清理旧产物"
rm -rf build/work dist

echo "==> 写入构建版本（关于页与检查更新都读它）"
VER="${PP6_VERSION:-$(git describe --tags --always --dirty 2>/dev/null || echo dev)}"
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
.venv/bin/python tools/stamp_version.py --version "$VER" --commit "$COMMIT" --channel local

echo "==> 开始打包（约 1-2 分钟）"
.venv/bin/pyinstaller build/pp6.spec --noconfirm --distpath dist --workpath build/work

APP="dist/PP6 Studio.app"
if [ ! -d "$APP" ]; then
  echo "!! 打包失败，未找到 $APP"
  exit 1
fi

echo "==> 自检：冻结包能否正常出包"
"$APP/Contents/MacOS/PP6ImportBuilder" --headless --port 8899 >/tmp/pp6-selftest.log 2>&1 &
PID=$!
sleep 5
CODE=$(curl -s --noproxy '*' -o /dev/null -w '%{http_code}' http://127.0.0.1:8899/api/template || true)
kill $PID 2>/dev/null || true
if [ "$CODE" != "200" ]; then
  echo "!! 自检失败（HTTP $CODE），请查看 /tmp/pp6-selftest.log"
  exit 1
fi

SIZE=$(du -sh "$APP" | cut -f1)
ARCH=$(uname -m)
echo ""
echo "✓ 打包完成: $APP  ($SIZE, $ARCH)"
echo ""
echo "分发方式：把整个 .app 压缩后发给同事（微信/内网盘均可）。"
echo "对方首次打开若提示「无法验证开发者」：右键点图标 → 打开 → 再点「打开」，"
echo "或让其执行： xattr -dr com.apple.quarantine \"/Applications/PP6 Studio.app\""
