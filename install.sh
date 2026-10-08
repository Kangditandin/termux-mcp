#!/data/data/com.termux/files/usr/bin/bash
# ============================================================================
# termux-mcp 一键部署/卸载脚本（在 Termux 内运行）
#   部署:   bash install.sh
#   卸载:   bash install.sh --uninstall
# 前置: 源码目录下需有 mcp 和 server.py 两个文件
# ============================================================================
set -e

PREFIX="${PREFIX:-/data/data/com.termux/files/usr}"
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"

# ---- 卸载 ----
if [ "${1:-}" = "--uninstall" ]; then
    "$PREFIX/bin/mcp" off 2>/dev/null || true
    rm -f  "$PREFIX/bin/mcp"
    rm -rf "$PREFIX/share/termux-mcp"
    rm -f  "$PREFIX/var/run/mcp.pid"
    echo "已卸载 termux-mcp（日志保留: $PREFIX/var/log/mcp.log）"
    exit 0
fi

# ---- 部署 ----
echo "部署 termux-mcp ..."
[ -f "$SRC_DIR/mcp" ]       || { echo "错误: $SRC_DIR/mcp 不存在" >&2; exit 1; }
[ -f "$SRC_DIR/server.py" ] || { echo "错误: $SRC_DIR/server.py 不存在" >&2; exit 1; }

mkdir -p "$PREFIX/bin" "$PREFIX/share/termux-mcp" "$PREFIX/var/run" "$PREFIX/var/log"
cp "$SRC_DIR/server.py" "$PREFIX/share/termux-mcp/server.py"
cp "$SRC_DIR/mcp"       "$PREFIX/bin/mcp"

# 按本机 PREFIX 修正 shebang（默认路径不适用时自动适配）
sed -i "1s|^#!.*|#!$PREFIX/bin/bash|"    "$PREFIX/bin/mcp"
sed -i "1s|^#!.*|#!$PREFIX/bin/python3|" "$PREFIX/share/termux-mcp/server.py"

chmod 755 "$PREFIX/bin/mcp" "$PREFIX/share/termux-mcp/server.py"

# 依赖检查（不自动安装，只提示）
if ! command -v python3 >/dev/null 2>&1; then
    echo "提示: 未检测到 python3，首次使用前请执行: pkg install python"
fi

echo "完成！"
echo "  命令:   $PREFIX/bin/mcp   （Termux 默认 PATH 已包含 \$PREFIX/bin）"
echo "  服务端: $PREFIX/share/termux-mcp/server.py"
echo
echo "快速开始:"
echo "  mcp on      # 启动"
echo "  mcp status  # 查看状态"
echo "  mcp off     # 停止"
