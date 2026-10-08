#!/data/data/com.termux/files/usr/bin/python3
# -*- coding: utf-8 -*-
"""
termux-mcp 服务器 —— 纯 Python 标准库实现的 MCP (Model Context Protocol) 服务端。

传输方式: Streamable HTTP（无状态）
  - POST   /mcp : 接收 JSON-RPC 2.0，返回单个 application/json 响应
  - GET    /    : 服务状态 JSON（健康检查/浏览器直查，非 MCP 端点，不鉴权）
  - GET    /mcp : 405（未实现 SSE 流，规范允许）
  - DELETE /mcp : 405（无会话概念，无需终止会话）

支持的 JSON-RPC 方法:
  initialize / notifications/* / ping / tools/list / tools/call

运行依赖: Python 3.8+（Termux: pkg install python）
配置（环境变量）:
  MCP_PORT   监听端口，默认 8000
  MCP_TOKEN  可选 Bearer 令牌；设置后 /mcp 请求需带
             Authorization: Bearer <token>
"""

import argparse
import json
import os
import shlex
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# 常量与全局配置
# ---------------------------------------------------------------------------
SERVER_NAME = "termux-mcp"
SERVER_VERSION = "1.0.0"
SERVER_INFO = {"name": SERVER_NAME, "version": SERVER_VERSION}

HOST = "127.0.0.1"                 # 只绑定回环地址，绝不监听外网
DEFAULT_PORT = 8000

SUPPORTED_PROTOCOL_VERSIONS = ("2024-11-05", "2025-03-26", "2025-06-18")
DEFAULT_PROTOCOL_VERSION = "2025-03-26"

MAX_BODY_BYTES = 10 * 1024 * 1024    # 请求体上限 10MB
MAX_OUTPUT_CHARS = 65536             # 单路输出(stdout/stderr/文件)截断上限
MAX_DIR_ENTRIES = 500                # list_dir 最多条目
MAX_PKG_LINES = 300                  # pkg_list 最多行

PREFIX = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
HOME = os.environ.get("HOME") or os.path.join(PREFIX, "home")
TOKEN = os.environ.get("MCP_TOKEN", "").strip()

# 服务器可能由 nohup 以精简环境启动，确保 $PREFIX/bin 在 PATH 里
# （pkg / apt / termux-info 等命令都在其中）
if PREFIX + "/bin" not in os.environ.get("PATH", ""):
    os.environ["PATH"] = PREFIX + "/bin:" + os.environ.get("PATH", "")


# ---------------------------------------------------------------------------
# 通用辅助
# ---------------------------------------------------------------------------
def log(msg):
    """带时间戳写一行日志（stdout，由 mcp 脚本重定向到日志文件）。"""
    print("[%s] %s" % (time.strftime("%Y-%m-%d %H:%M:%S"), msg), flush=True)


def clip(text, limit=MAX_OUTPUT_CHARS, label="输出"):
    """超长文本截断，末尾标注总长度，防止刷爆客户端上下文。"""
    if text is None:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[%s 已截断: 共 %d 字符，仅显示前 %d]" % (label, len(text), limit)


def to_text(data):
    """bytes/str 统一转 str（utf-8，坏字符替换）。"""
    if isinstance(data, bytes):
        return data.decode("utf-8", "replace")
    return data or ""


def run_cmd(cmd, timeout=60, env=None, cwd=None):
    """执行 shell 命令，返回 (returncode, stdout, stderr)。
    超时抛 subprocess.TimeoutExpired，由调用方决定如何呈现。"""
    p = subprocess.run(cmd, shell=True, capture_output=True, cwd=cwd,
                       timeout=timeout, env=env)
    return p.returncode, to_text(p.stdout), to_text(p.stderr)


def apt_env():
    """apt/dpkg 非交互环境（避免弹 debconf 前端导致挂死）。"""
    e = dict(os.environ)
    e["DEBIAN_FRONTEND"] = "noninteractive"
    return e


class ParamError(Exception):
    """参数不合法 → JSON-RPC -32602（协议层错误）"""


class ToolExecError(Exception):
    """工具执行期错误 → MCP result.isError=true（业务层失败）"""


# ---------------------------------------------------------------------------
# 工具实现（每个函数接收 arguments dict，返回 (文本, isError)）
# ---------------------------------------------------------------------------
def t_exec_command(args):
    """exec_command: 在 Termux 中执行 shell 命令。"""
    command = args.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ParamError("command 必须为非空字符串")
    try:
        timeout_ms = int(args.get("timeout_ms", 30000))
    except (TypeError, ValueError):
        raise ParamError("timeout_ms 必须为整数")
    timeout_ms = max(1000, min(timeout_ms, 300000))       # 1s ~ 5min

    cwd = args.get("cwd") or HOME
    if not os.path.isdir(cwd):
        raise ToolExecError("cwd 目录不存在: %s" % cwd)

    try:
        rc, out, err = run_cmd(command, timeout=timeout_ms / 1000.0, cwd=cwd)
    except subprocess.TimeoutExpired as e:
        # TimeoutExpired 携带的部分输出是 bytes
        out, err = to_text(e.stdout), to_text(e.stderr)
        text = ("exit_code: TIMEOUT (超过 %d ms 被终止)\n"
                "--- stdout(部分) ---\n%s\n--- stderr(部分) ---\n%s"
                % (timeout_ms, clip(out), clip(err)))
        return text, True

    text = ("exit_code: %d\n--- stdout ---\n%s\n--- stderr ---\n%s"
            % (rc, clip(out) or "(空)", clip(err) or "(空)"))
    return text, rc != 0


def t_read_file(args):
    """read_file: 读取文本文件。"""
    path = args.get("path")
    if not isinstance(path, str) or not path.strip():
        raise ParamError("path 必须为非空字符串")
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        raise ToolExecError("path 是目录: %s（请使用 list_dir）" % path)
    try:
        max_bytes = int(args.get("max_bytes", 65536))
    except (TypeError, ValueError):
        raise ParamError("max_bytes 必须为整数")
    max_bytes = max(1, min(max_bytes, 1024 * 1024))       # 上限 1MB

    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes + 1)                  # 多读 1 字节用于判断截断
    except FileNotFoundError:
        raise ToolExecError("文件不存在: %s" % path)
    except PermissionError:
        raise ToolExecError("无权限读取: %s" % path)
    except OSError as e:
        raise ToolExecError("读取失败: %s" % e)

    truncated = len(data) > max_bytes
    try:
        size = os.path.getsize(path)
    except OSError:
        size = len(data)
    text = data[:max_bytes].decode("utf-8", "replace")
    header = "文件: %s (%d bytes%s)\n---\n" % (
        path, size, "，已截断至 %d bytes" % max_bytes if truncated else "")
    return header + text, False


def t_write_file(args):
    """write_file: 写入文本文件（自动创建父目录）。"""
    path = args.get("path")
    content = args.get("content")
    if not isinstance(path, str) or not path.strip():
        raise ParamError("path 必须为非空字符串")
    if not isinstance(content, str):
        raise ParamError("content 必须为字符串")
    mode = args.get("mode", "overwrite")
    if mode not in ("overwrite", "append"):
        raise ParamError("mode 只能是 overwrite 或 append")

    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        raise ToolExecError("path 是目录，无法写入: %s" % path)
    try:
        parent = os.path.dirname(path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent, exist_ok=True)
        with open(path, "a" if mode == "append" else "w", encoding="utf-8") as f:
            f.write(content)
    except PermissionError:
        raise ToolExecError("无权限写入: %s" % path)
    except OSError as e:
        raise ToolExecError("写入失败: %s" % e)
    n = len(content.encode("utf-8"))
    return "已写入 %s (%d 字节, 模式: %s)" % (path, n, mode), False


def t_list_dir(args):
    """list_dir: 列出目录内容。"""
    path = args.get("path") or HOME
    path = os.path.abspath(os.path.expanduser(path))
    if not os.path.isdir(path):
        raise ToolExecError("目录不存在: %s" % path)
    try:
        entries = list(os.scandir(path))
    except PermissionError:
        raise ToolExecError("无权限访问: %s" % path)
    except OSError as e:
        raise ToolExecError("读取目录失败: %s" % e)

    def _key(e):
        try:
            return (not e.is_dir(follow_symlinks=False), e.name.lower())
        except OSError:
            return (True, e.name.lower())

    entries.sort(key=_key)                               # 目录在前

    lines, total = [], 0
    for ent in entries:
        total += 1
        if len(lines) >= MAX_DIR_ENTRIES:
            continue
        try:
            if ent.is_dir(follow_symlinks=False):
                lines.append("d %10s  %s/" % ("-", ent.name))
            elif ent.is_symlink():
                lines.append("l %10s  %s -> %s" % ("-", ent.name, os.readlink(ent.path)))
            else:
                st = ent.stat(follow_symlinks=False)
                lines.append("- %10d  %s" % (st.st_size, ent.name))
        except OSError:
            lines.append("? %10s  %s" % ("-", ent.name))

    header = "目录: %s (共 %d 项%s)\n" % (
        path, total, "，仅显示前 %d" % MAX_DIR_ENTRIES if total > MAX_DIR_ENTRIES else "")
    return header + "\n".join(lines), False


def t_device_info(args):
    """device_info: termux-info + 基本系统信息。"""
    parts = []
    # 优先 termux-info（termux-tools 自带，无需 termux-api 应用）
    try:
        rc, out, err = run_cmd("termux-info", timeout=30)
        if rc == 0 and out.strip():
            parts.append("--- termux-info ---\n" + clip(out, 20000, "termux-info"))
        else:
            parts.append("--- termux-info 不可用 (rc=%d) ---\n%s" % (rc, clip(err, 2000)))
    except subprocess.TimeoutExpired:
        parts.append("--- termux-info 超时 ---")
    # 补充/回退信息（getprop 为 Android 系统命令）
    for label, cmd in (
        ("内核", "uname -a"),
        ("Android 版本", "getprop ro.build.version.release"),
        ("SDK", "getprop ro.build.version.sdk"),
        ("型号", "getprop ro.product.model"),
    ):
        try:
            rc, out, _ = run_cmd(cmd, timeout=10)
            if rc == 0 and out.strip():
                parts.append("%s: %s" % (label, out.strip()))
        except subprocess.TimeoutExpired:
            pass
    parts.append("Python: %s" % sys.version.split()[0])
    return "\n".join(parts), False


def t_pkg_list(args):
    """pkg_list: 查询软件包列表。"""
    scope = args.get("scope", "installed")
    if scope not in ("installed", "all", "upgrade"):
        raise ParamError("scope 只能是 installed / all / upgrade")
    pattern = args.get("pattern") or ""
    if not isinstance(pattern, str):
        raise ParamError("pattern 必须为字符串")

    if scope == "installed":
        cmd = "dpkg-query -W -f='${binary:Package}\\t${Version}\\n'"
        timeout = 60
    elif scope == "upgrade":
        cmd = "apt list --upgradable 2>/dev/null"
        timeout = 120
    else:  # all
        if not pattern.strip():
            raise ParamError("scope=all 时必须提供 pattern，避免输出过大")
        cmd = "apt list 2>/dev/null"
        timeout = 120

    try:
        rc, out, err = run_cmd(cmd, timeout=timeout, env=apt_env())
    except subprocess.TimeoutExpired:
        raise ToolExecError("查询超时(>%ds)" % timeout)
    if rc != 0 and not out.strip():
        raise ToolExecError("查询失败(rc=%d): %s" % (rc, clip(err, 2000)))

    lines = [l for l in out.splitlines() if l.strip() and not l.startswith("Listing...")]
    if pattern.strip():
        p = pattern.strip().lower()
        lines = [l for l in lines if p in l.lower()]
    total = len(lines)
    shown = lines[:MAX_PKG_LINES]
    header = "scope=%s%s (匹配 %d 项%s)\n" % (
        scope, ", pattern=%s" % pattern if pattern.strip() else "",
        total, "，仅显示前 %d" % MAX_PKG_LINES if total > MAX_PKG_LINES else "")
    body = "\n".join(shown) if shown else "(无匹配)"
    return header + body, False


def t_pkg_manage(args):
    """pkg_manage: 软件包安装/删除/更新/升级（高危）。"""
    action = args.get("action")
    if action not in ("install", "remove", "update", "upgrade"):
        raise ParamError("action 只能是 install / remove / update / upgrade")
    packages = args.get("packages") or []
    if not isinstance(packages, list) or not all(isinstance(x, str) and x.strip() for x in packages):
        raise ParamError("packages 必须为非空字符串数组")
    packages = [x.strip() for x in packages]
    if action in ("install", "remove") and not packages:
        raise ParamError("action=%s 需要至少一个包名" % action)

    # 用 apt-get -y 非交互执行（等效 pkg install/remove，行为更确定）
    if action == "install":
        cmd = "apt-get install -y " + " ".join(shlex.quote(x) for x in packages)
        timeout = 600
    elif action == "remove":
        cmd = "apt-get remove -y " + " ".join(shlex.quote(x) for x in packages)
        timeout = 120
    elif action == "update":
        cmd = "apt-get update"
        timeout = 300
    else:  # upgrade
        cmd = "apt-get full-upgrade -y"
        timeout = 900

    try:
        rc, out, err = run_cmd(cmd, timeout=timeout, env=apt_env())
    except subprocess.TimeoutExpired:
        return ("操作超时(>%ds)，包管理器可能仍在运行\n命令: %s" % (timeout, cmd)), True
    text = "命令: %s\nexit_code: %d\n--- stdout ---\n%s\n--- stderr ---\n%s" % (
        cmd, rc, clip(out, 20000), clip(err, 8000))
    return text, rc != 0


# ---------------------------------------------------------------------------
# 工具 schema（MCP tools/list 的唯一事实来源）
# ---------------------------------------------------------------------------
TOOLS = [
    {
        "name": "exec_command",
        "description": (
            "在 Termux 环境中执行一条 shell 命令，返回退出码、stdout 与 stderr。"
            "可使用 $PREFIX/bin 下全部命令（pkg/apt/python 等）。"
            "高危工具：请谨慎执行删除、覆盖等破坏性命令。"
            "命令非零退出或超时时 isError=true（输出仍会附上）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的命令，如 ls -la ~"},
                "timeout_ms": {"type": "integer", "description": "超时毫秒数，默认 30000，范围 [1000, 300000]"},
                "cwd": {"type": "string", "description": "工作目录，默认 $HOME"},
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "description": "读取文本文件内容（utf-8）。默认最多 64KB，可用 max_bytes 调整（上限 1MB）。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件绝对路径或 ~ 开头路径"},
                "max_bytes": {"type": "integer", "description": "最多读取字节数，默认 65536"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "description": "写入文本文件（utf-8），父目录不存在时自动创建。mode=append 追加，默认 overwrite 覆盖。高危：可能覆盖已有文件。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目标文件路径"},
                "content": {"type": "string", "description": "要写入的完整内容"},
                "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "写入模式，默认 overwrite"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "list_dir",
        "description": "列出目录内容（目录在前，含类型/大小），默认 $HOME，最多显示 500 项。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认 $HOME"},
            },
            "required": [],
        },
    },
    {
        "name": "device_info",
        "description": "获取设备与 Termux 环境信息（termux-info 输出，附内核/Android 版本等）。无参数。",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "pkg_list",
        "description": "查询 Termux 软件包列表。scope=installed(已安装，默认)/all(全部仓库包，必须给 pattern)/upgrade(可升级)。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "scope": {"type": "string", "enum": ["installed", "all", "upgrade"], "description": "查询范围，默认 installed"},
                "pattern": {"type": "string", "description": "包名过滤子串（不区分大小写）"},
            },
            "required": [],
        },
    },
    {
        "name": "pkg_manage",
        "description": (
            "软件包管理（高危）：install/remove 需给出包名数组；"
            "update=刷新索引；upgrade=升级全部可升级包。apt-get -y 非交互执行。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "action": {"type": "string", "enum": ["install", "remove", "update", "upgrade"]},
                "packages": {"type": "array", "items": {"type": "string"}, "description": "包名数组，update/upgrade 时可省略"},
            },
            "required": ["action"],
        },
    },
]

TOOL_IMPL = {
    "exec_command": t_exec_command,
    "read_file": t_read_file,
    "write_file": t_write_file,
    "list_dir": t_list_dir,
    "device_info": t_device_info,
    "pkg_list": t_pkg_list,
    "pkg_manage": t_pkg_manage,
}
TOOL_REQUIRED = {t["name"]: t["inputSchema"].get("required", []) for t in TOOLS}

INSTRUCTIONS = (
    "本服务运行于 Termux (Android) 环境，提供 7 个工具："
    "exec_command(执行 shell 命令)、read_file / write_file / list_dir(文件操作)、"
    "device_info(termux-info 设备信息)、pkg_list / pkg_manage(软件包查询与管理)。"
    "所有工具以 Termux 用户权限（无 root）运行；"
    "exec_command 与 pkg_manage 属高危操作，请谨慎调用并向用户确认。"
)


# ---------------------------------------------------------------------------
# JSON-RPC 分发
# ---------------------------------------------------------------------------
def rpc_ok(msg_id, result):
    return {"jsonrpc": "2.0", "id": msg_id, "result": result}


def rpc_err(msg_id, code, message):
    return {"jsonrpc": "2.0", "id": msg_id, "error": {"code": code, "message": message}}


def _tools_call(msg_id, params):
    name = params.get("name")
    if not isinstance(name, str) or not name:
        return rpc_err(msg_id, -32602, "缺少参数: name")
    impl = TOOL_IMPL.get(name)
    if impl is None:
        return rpc_err(msg_id, -32602, "未知工具: %s（可用: %s）" % (name, ", ".join(TOOL_IMPL)))
    arguments = params.get("arguments") or {}
    if not isinstance(arguments, dict):
        return rpc_err(msg_id, -32602, "arguments 必须为对象")

    # 按 schema 的 required 做基础校验
    for key in TOOL_REQUIRED.get(name, []):
        v = arguments.get(key)
        if v is None or (isinstance(v, str) and not v.strip()):
            return rpc_err(msg_id, -32602, "缺少必填参数: %s" % key)

    try:
        text, is_error = impl(arguments)
        return rpc_ok(msg_id, {"content": [{"type": "text", "text": text}],
                               "isError": bool(is_error)})
    except ParamError as e:
        return rpc_err(msg_id, -32602, "参数错误: %s" % e)
    except ToolExecError as e:
        # 工具业务失败：isError=true，而非 JSON-RPC error（两者语义不同）
        return rpc_ok(msg_id, {"content": [{"type": "text", "text": "工具执行失败: %s" % e}],
                               "isError": True})
    except Exception as e:  # 兜底：单个工具异常不能杀死服务器
        return rpc_ok(msg_id, {"content": [{"type": "text",
                                            "text": "工具内部错误: %s: %s" % (type(e).__name__, e)}],
                               "isError": True})


def dispatch(msg):
    """处理单条 JSON-RPC 消息；通知（无 id）返回 None（HTTP 202 无响应体）。"""
    method = msg.get("method")
    msg_id = msg.get("id")
    params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

    # 通知：无论内容是否规范都不回包（JSON-RPC 规范）
    if "id" not in msg:
        return None
    if not isinstance(method, str):
        return rpc_err(msg_id, -32600, "无效请求: 缺少 method 字段")

    if method == "initialize":
        # 回显客户端版本（在支持列表内），否则用默认版本
        pv = params.get("protocolVersion")
        chosen = pv if pv in SUPPORTED_PROTOCOL_VERSIONS else DEFAULT_PROTOCOL_VERSION
        return rpc_ok(msg_id, {
            "protocolVersion": chosen,
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": SERVER_INFO,
            "instructions": INSTRUCTIONS,
        })

    if method == "ping":
        return rpc_ok(msg_id, {})

    if method == "tools/list":
        return rpc_ok(msg_id, {"tools": TOOLS})

    if method == "tools/call":
        return _tools_call(msg_id, params)

    if method.startswith("notifications/"):
        return rpc_ok(msg_id, {})   # 带了 id 的通知类消息（容错）

    return rpc_err(msg_id, -32601, "未知方法: %s" % method)


# ---------------------------------------------------------------------------
# HTTP 层
# ---------------------------------------------------------------------------
def status_payload(port):
    """GET / 的健康检查载荷（非 MCP 端点）。"""
    return {
        "service": SERVER_NAME,
        "version": SERVER_VERSION,
        "status": "running",
        "pid": os.getpid(),
        "url": "http://%s:%d/mcp" % (HOST, port),
        "transport": "streamable-http (stateless, JSON-RPC 2.0)",
        "protocolVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "auth": "bearer(MCP_TOKEN)" if TOKEN else "none",
        "tools": [{"name": t["name"], "description": t["description"].split("。")[0]}
                  for t in TOOLS],
    }


class McpHandler(BaseHTTPRequestHandler):
    server_version = "termux-mcp/" + SERVER_VERSION
    protocol_version = "HTTP/1.1"          # keep-alive；所有响应都带 Content-Length

    # ---- 基础响应工具 ----
    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Length", "0")
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()

    def _drain_body(self):
        """读完请求体，保证 keep-alive 连接上后续请求不错位。"""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)

    def _check_auth(self):
        """MCP_TOKEN 设置时校验 Bearer 令牌。"""
        if not TOKEN:
            return True
        return (self.headers.get("Authorization") or "") == "Bearer " + TOKEN

    # ---- HTTP 方法 ----
    def do_GET(self):
        if self.path == "/":
            self._send_json(200, status_payload(self.server.mcp_port))
        elif self.path == "/mcp":
            # 未实现 SSE 流；MCP 规范允许返回 405
            self._send_json(405, {"error": "GET /mcp 不支持（无状态 JSON 响应，请用 POST）"},
                            {"Allow": "POST"})
        else:
            self._send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path != "/mcp":
            self._drain_body()
            self._send_json(404, {"error": "not found"})
            return
        if not self._check_auth():
            self._drain_body()
            self._send_json(401, {"error": "unauthorized: 需要 Authorization: Bearer <token>"},
                            {"WWW-Authenticate": "Bearer"})
            return
        # 不支持 chunked 请求体（BaseHTTPRequestHandler 不解码 chunked）
        te = (self.headers.get("Transfer-Encoding") or "").lower()
        if "chunked" in te and not self.headers.get("Content-Length"):
            self.close_connection = True
            self._send_json(411, {"error": "请使用 Content-Length，不支持 chunked 请求体"})
            return
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            self._send_json(200, rpc_err(None, -32600, "无效请求: 缺少请求体"))
            return
        if length > MAX_BODY_BYTES:
            self._drain_body()
            self._send_json(413, {"error": "请求体过大(>%d bytes)" % MAX_BODY_BYTES})
            return

        body = self.rfile.read(length)
        try:
            msg = json.loads(body.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            self._send_json(200, rpc_err(None, -32700, "解析错误: 无效 JSON"))
            return
        if isinstance(msg, list):
            self._send_json(200, rpc_err(None, -32600, "不支持批量(batch)请求"))
            return
        if not isinstance(msg, dict):
            self._send_json(200, rpc_err(None, -32600, "无效请求: 期望 JSON 对象"))
            return

        resp = dispatch(msg)
        if resp is None:
            self._send_empty(202)           # 通知：202 Accepted，无响应体
            return
        self._send_json(200, resp)

    def do_DELETE(self):
        if self.path == "/mcp":
            # 无会话模型：没有会话可终止
            self._send_json(405, {"error": "本服务无会话概念，DELETE /mcp 不适用"}, {"Allow": "POST"})
        else:
            self._send_json(404, {"error": "not found"})

    def log_message(self, fmt, *args):
        """访问日志（精简格式，写入服务日志文件）。"""
        log("http %s %s" % (self.address_string(), fmt % args))


class McpServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    mcp_port = DEFAULT_PORT


# ---------------------------------------------------------------------------
# 服务器 / 探测 / CLI 入口
# ---------------------------------------------------------------------------
def serve(port):
    httpd = McpServer((HOST, port), McpHandler)
    httpd.mcp_port = port

    def _graceful(signum, _frame):
        log("收到信号 %s，正在关闭..." % signum)
        # shutdown() 必须在其他线程调用，否则死锁
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _graceful)
    signal.signal(signal.SIGINT, _graceful)

    log("=" * 56)
    log("termux-mcp %s 已启动" % SERVER_VERSION)
    log("监听: http://%s:%d/mcp (POST, JSON-RPC 2.0)" % (HOST, port))
    log("PID: %d | 认证: %s | 工具数: %d" % (os.getpid(), "Bearer" if TOKEN else "无", len(TOOLS)))
    log("工具: %s" % ", ".join(t["name"] for t in TOOLS))
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        log("termux-mcp 已停止")


def http_get_status(port, timeout=3.0):
    """GET / 状态接口（供 --probe / --print-status 使用）。"""
    with urllib.request.urlopen("http://%s:%d/" % (HOST, port), timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(port, timeout_s):
    """轮询直到服务就绪或超时。"""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            http_get_status(port, timeout=1.0)
            return True
        except Exception:
            time.sleep(0.15)
    return False


def print_status(port):
    """拉取 GET / 并打印人类可读摘要（供 mcp on / mcp status 使用）。"""
    st = http_get_status(port)
    print("  地址:   %s" % st["url"])
    print("  传输:   %s" % st["transport"])
    print("  认证:   %s" % st["auth"])
    print("  工具(%d):" % len(st["tools"]))
    for t in st["tools"]:
        print("    %-14s %s" % (t["name"], t["description"]))
    print("  PID:    %s" % st["pid"])


def main():
    ap = argparse.ArgumentParser(description="termux-mcp 服务器")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MCP_PORT", DEFAULT_PORT)))
    ap.add_argument("--probe", action="store_true", help="探测运行中的服务是否就绪")
    ap.add_argument("--print-status", action="store_true", help="打印运行中服务的状态摘要")
    ap.add_argument("--timeout", type=float, default=5.0, help="probe 模式超时秒数")
    args = ap.parse_args()

    if args.probe:
        sys.exit(0 if probe(args.port, args.timeout) else 1)
    if args.print_status:
        try:
            print_status(args.port)
        except Exception as e:
            print("错误: 无法获取状态: %s" % e, file=sys.stderr)
            sys.exit(1)
        sys.exit(0)

    try:
        serve(args.port)
    except OSError as e:
        # 常见：端口被占用
        print("[termux-mcp] 无法绑定 %s:%d: %s" % (HOST, args.port, e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
