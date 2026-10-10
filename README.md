# termux-mcp 插件使用说明

> 一个跑在 Android **Termux** 里的本地 MCP（Model Context Protocol）服务器。
> 装上它之后，**AI 客户端就能"伸手"操作你的手机**——执行 shell 命令、读写文件、
> 查装/卸载软件包、读取设备信息。
>
> 纯 Python 标准库实现，**零第三方依赖**，只监听 `127.0.0.1`，不暴露到外网。

---

## 目录

- [1. 这是什么](#1-这是什么)
- [2. 目录结构](#2-目录结构)
- [3. 安装与启动](#3-安装与启动)
- [4. 七个工具详解](#4-七个工具详解)
- [5. 接入 AI 客户端](#5-接入-ai-客户端)
- [6. 通过 rish 获取 Shizuku 的 shell 权限](#6-通过-rish-获取-shizuku-的-shell-权限)
- [7. AI 借助本插件能做什么（实例）](#7-ai-借助本插件能做什么实例)
- [8. 安全须知](#8-安全须知)
- [9. 常见问题](#9-常见问题)

---

## 1. 这是什么

MCP（Model Context Protocol）是"AI 调用外部工具"的通用协议。
本插件把 **Termux 的能力** 包装成一个本地 HTTP 服务，让 AI 客户端（如各类
支持 MCP 的助手）通过标准 JSON-RPC 2.0 调用。

核心特性：

| 特性 | 说明 |
|---|---|
| 传输方式 | Streamable HTTP（**无状态**），`POST /mcp` 单次请求单次响应 |
| 协议支持 | `2024-11-05` / `2025-03-26` / `2025-06-18` |
| 依赖 | 仅需 `python3`（`pkg install python`） |
| 监听地址 | 硬编码 `127.0.0.1`，**绝不监听外网** |
| 鉴权 | 可选 Bearer Token（环境变量 `MCP_TOKEN`） |
| 默认端口 | `8000`（环境变量 `MCP_PORT` 可改） |
| 输出保护 | 单路输出截断至 64KB，目录/包列表分页上限，防止刷爆 AI 上下文 |
| 稳定性 | 单工具异常被捕获，不会杀死服务器；支持 SIGTERM 优雅退出 |

---

## 2. 目录结构

### 源码仓库（本目录）

```
termux-mcp插件/
├── mcp          # 控制脚本：on / off / status / help
├── server.py    # MCP 服务器本体（纯标准库）
├── install.sh   # 一键部署 / 卸载
└── README.md    # 本文件
```

### 安装后的文件布局

```
$PREFIX/bin/mcp                     # 控制脚本（$PREFIX/bin 已在 PATH 中，可直接敲 mcp）
$PREFIX/share/termux-mcp/server.py  # 服务器本体
$PREFIX/var/run/mcp.pid             # PID 文件（第 1 行 PID，第 2 行端口）
$PREFIX/var/log/mcp.log             # 运行日志
```

> `$PREFIX` 通常为 `/data/data/com.termux/files/usr`。
> `install.sh` 会按本机 `PREFIX` 自动修正两个文件的 shebang，换机也能用。

---

## 3. 安装与启动

### 3.1 部署

在 Termux 里，于本目录执行：

```bash
bash install.sh
```

脚本会：安装 `mcp` 与 `server.py` → 修正 shebang → 赋执行权限 → 检查 `python3`。

### 3.2 常用命令

```bash
mcp on        # 启动服务（后台运行，不阻塞当前 shell）
mcp status    # 查看运行状态（等同于无参数 mcp）
mcp off       # 停止服务（先 SIGTERM 优雅停止，超时再 SIGKILL）
mcp help      # 帮助
```

启动成功后会打印客户端连接配置，形如：

```json
{
  "mcpServers": {
    "termux": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### 3.3 环境变量

```bash
# 换端口
MCP_PORT=9000 mcp on

# 开启 Bearer 鉴权（改完必须 off/on 重启才生效）
MCP_TOKEN=你的令牌 mcp on
```

也可以写进 `~/.bashrc` 让它持久生效：

```bash
export MCP_PORT=8000
export MCP_TOKEN=termuxmcpon
```

> ⚠️ 一旦启用 `MCP_TOKEN`，客户端必须在请求头里带
> `Authorization: Bearer <令牌>`，否则会收到 `401`。
> 不会配请求头时，用 `mcp_no_token.sh` 一键切回无鉴权模式应急。

### 3.4 卸载

```bash
bash install.sh --uninstall
```

会停服务并删除程序文件，**日志保留**在 `$PREFIX/var/log/mcp.log`。

### 3.5 健康检查

```bash
# 浏览器或 curl 直接访问根路径（非 MCP 端点，不鉴权）
curl http://127.0.0.1:8000/
```

返回服务状态 JSON，含版本、PID、协议版本、鉴权方式与工具清单。

---

## 4. 七个工具详解

这一章把每个工具讲透：**参数、实际能干什么、真实可用的例子、坑在哪**。
所有例子都可以直接丢给 AI 执行。

工具总览：

| 工具 | 作用 | 风险 |
|---|---|---|
| [`exec_command`](#41-exec_command--万能执行器) | 执行任意 shell 命令 | 🔴 高 |
| [`read_file`](#42-read_file--读取文本) | 读取文本文件（≤1MB） | 🟡 中 |
| [`write_file`](#43-write_file--写入文本) | 写入/追加文本（自动建目录） | 🔴 高 |
| [`list_dir`](#44-list_dir--列目录) | 列出目录内容（目录在前） | 🟢 低 |
| [`device_info`](#45-device_info--设备体检) | 设备与 Termux 环境信息 | 🟢 低 |
| [`pkg_list`](#46-pkg_list--软件包查询) | 查询软件包（已装/仓库/可升级） | 🟢 低 |
| [`pkg_manage`](#47-pkg_manage--软件包管理) | 安装/卸载/刷新索引/升级 | 🔴 高 |

---

### 4.1 `exec_command` —— 万能执行器

**参数**

```jsonc
{
  "command": "ls -la ~",                    // 必填，整条命令
  "timeout_ms": 30000,                      // 可选，1000 ~ 300000
  "cwd": "/data/data/com.termux/files/home" // 可选，默认 $HOME
}
```

**它本质上是什么**
它是本插件 90% 战斗力的来源。因为它是**任意 shell 命令**，所以"Termux 能装的
任何命令行工具"都等于"AI 能用的能力"——`ffmpeg`、`python3`、`sqlite3`、`curl`、
`zip`、`git`、`rclone`……装上就有。

**实际能做什么（按场景列举）**

| 场景 | 真实命令 |
|---|---|
| **找文件** | `find /sdcard -type f -size +200M 2>/dev/null \| head -20` |
| **搜内容** | `grep -rn --include='*.txt' "关键词" /sdcard/Notes` |
| **看磁盘** | `du -sh /sdcard/* 2>/dev/null \| sort -h \| tail -15` |
| **批量改名** | `for f in *.jpeg; do mv "$f" "${f%.jpeg}.jpg"; done` |
| **批量压缩图片** | `for f in *.png; do convert "$f" -quality 80 "${f%.png}.jpg"; done` |
| **视频转码** | `ffmpeg -i in.mp4 -vcodec libx264 -crf 28 -preset fast out.mp4` |
| **打包归档** | `tar czf ~/bak/DCIM_$(date +%F).tgz /sdcard/DCIM` |
| **解压** | `unzip -O gbk -d ~/out chinese.zip` |
| **下载文件** | `curl -L -o ~/a.apk "https://example.com/a.apk"` |
| **算哈希** | `sha256sum ~/a.apk` |
| **看进程** | `ps -A -o PID,NAME,RSS \| sort -k3 -nr \| head -20` |
| **查端口** | `ss -tunlp 2>/dev/null \| head` |
| **网络诊断** | `ping -c 4 223.5.5.5; curl -sI https://baidu.com` |
| **跑 Python 脚本** | `python3 ~/script.py --input /sdcard/data.csv` |
| **持久化任务** | `nohup ~/loop.sh > ~/loop.log 2>&1 &` |
| **系统调用** | `getprop \| grep -i qualcomm`, `dumpsys` 类命令（需权限见第 6 章） |

**典型的多步组合（AI 自主完成）**

> 需求："把 /sdcard/Download 里重复下载的文件清掉，省点空间。"

```bash
# 第 1 步：列出文件+大小+哈希，找出重复
exec_command "cd /sdcard/Download && md5sum * 2>/dev/null | sort | uniq -w32 -d"

# 第 2 步：按结果删除多余副本
exec_command "cd /sdcard/Download && rm -f 'xxx(1).apk'"

# 第 3 步：核验释放了多少空间
exec_command "du -sh /sdcard/Download"
```

**⚠️ 坑与注意**

- 命令里**必须自己引号转义**，尤其 `$`、`"`、`|`。AI 生成命令时要注意
  shell 单双引号嵌套。
- 非零退出码 / 超时 → `isError=true`，但**输出照样返回**，AI 能据此自我纠错。
- 长命令（如 `apt-get upgrade`）务必调大 `timeout_ms`，否则会被中途杀掉。
- 后台任务用 `nohup ... &`，否则命令返回时子进程可能被清理。
- 交互式命令（`vim`、`top`、需要输入 y/n 的）**不要用**，会直接挂到超时。
  需要 y/n 的用 `-y` 或用 `yes | cmd`。

---

### 4.2 `read_file` —— 读取文本

**参数**

```jsonc
{ "path": "~/note.txt", "max_bytes": 65536 }   // max_bytes 上限 1MB
```

**实际能做什么**

| 场景 | 说明 |
|---|---|
| **读配置** | `~/.bashrc`、`~/Sillytavern/config.yaml`、各类 `.conf` |
| **读日志** | 抓 `~/mcp_switch.log`、应用导出的崩溃日志 |
| **读笔记** | `/sdcard/Notes/*.md`，让 AI 帮你总结/续写 |
| **读代码** | 读 `.py`/`.sh`/`.json` 源码，让 AI 审查或改写 |
| **读订阅/证书** | 各种文本形式的 key、token、书签导出文件 |
| **读抓包结果** | 文本化的 HTTP 报文，做协议分析 |

**真实例子**

```jsonc
// 1) 看 bashrc 里配了哪些环境变量
{ "path": "~/.bashrc" }

// 2) 读一份 JSON 配置，AI 帮你找错字段
{ "path": "~/app/config.json" }

// 3) 日志很大，只读前 200KB 做抽样
{ "path": "~/big.log", "max_bytes": 204800 }
```

**⚠️ 坑与注意**

- 只适合**文本**。二进制（图片、APK、SO）读出来是乱码，
  二进制请改用 `exec_command` 里的 `base64`、`xxd`、`file`。
- 路径是**服务端解析**的，支持 `~`；相对路径按进程 CWD 算，**建议一律写绝对路径**。
- 超过 `max_bytes` 会**截断并标注总长度**，AI 看到标注就知道要分段读。
- 读不了 `/data/data/其他应用`（沙箱），要读得走第 6 章的 rish。

---

### 4.3 `write_file` —— 写入文本

**参数**

```jsonc
{
  "path": "~/hello.txt",
  "content": "你好",
  "mode": "overwrite"      // overwrite（默认，覆盖）| append（追加）
}
```

**实际能做什么**

这是 AI "**把想法变成文件**"的工具——脚本、配置、文档、清单、报告全靠它落地。

| 场景 | 例子 |
|---|---|
| **生成脚本** | 写一个 `backup.sh`，接着 `exec_command "chmod +x …"` 跑起来 |
| **写配置** | 生成 `~/.termux/termux.properties`、改 SSH `config` |
| **批量建文件** | 一次生成几十个 `.md` 笔记骨架 |
| **存报告** | 把分析结论 `write_file /sdcard/报告.md` |
| **拼大文件** | 先 `overwrite` 写头，再多次 `append` 追内容 |
| **创建定时任务** | 写 crontab 片段、写 `~/.bashrc` 追加 alias |

**真实例子：AI 一次性给手机加个"一键备份"命令**

```jsonc
// 步骤 1：写脚本
{
  "path": "~/bin/mybak",
  "content": "#!/bin/bash\nset -e\nDEST=~/bak\nmkdir -p $DEST\ntar czf $DEST/DCIM_$(date +%F).tgz /sdcard/DCIM\necho '已备份到' $DEST\n",
  "mode": "overwrite"
}
```

```bash
# 步骤 2：赋权（AI 用 exec_command）
exec_command "chmod +x ~/bin/mybak && ~/bin/mybak"
```

**真实例子：追加一份日志**

```jsonc
{ "path": "~/todo.md", "content": "\n- [ ] 周末清理相册\n", "mode": "append" }
```

**⚠️ 坑与注意**

- `overwrite` 会**直接盖掉原文件**，没有备份、不可撤销。
  改重要文件时，让 AI **先 `read_file` 存一份原文**，或先 `cp` 备份。
- **父目录会自动创建**，所以路径写错就会在错的地方造目录，注意检查。
- `content` 里的换行必须是真实的 `\n`（JSON 转义），AI 生成时要留意。
- 只写**文本**；要生成二进制（apk、图片）需配合 `base64 -d`。

---

### 4.4 `list_dir` —— 列目录

**参数**

```jsonc
{ "path": "/sdcard/Download" }     // 省略则默认 $HOME
```

**实际能做什么**

它是 AI 的"**眼睛**"——在动手之前先看清目录里有什么。输出**目录在前**，
带类型和大小，最多 500 项。

| 场景 | 说明 |
|---|---|
| **探查结构** | 第一次接触某目录，先列出来再决定怎么处理 |
| **找目标文件** | 在一堆文件里定位 `*.apk`、最新那个文件 |
| **确认产物** | 脚本跑完，列目录看输出文件在不在 |
| **对比前后** | 操作前列一次、操作后再列一次，看增删了什么 |

**真实例子：AI 整理下载目录的标准开场**

```jsonc
// 1) 先看有什么
{ "path": "/sdcard/Download" }
```

```bash
# 2) 需要更细的信息（时间、排序）就用 exec_command
exec_command "ls -lt --time-style=long-iso /sdcard/Download | head -30"
```

**⚠️ 坑与注意**

- 只有**一层**，不递归。想看树状结构用 `exec_command "find . -maxdepth 3"`。
- 超过 500 项会被截断，大目录改用 `exec_command "ls | wc -l"` 先数一遍。
- 无权限的目录会直接报错（如未提权时 `/data/data/xxx`）。

---

### 4.5 `device_info` —— 设备体检

**参数**

```jsonc
{}    // 无参数
```

**实际能做什么**

一次调用拿到设备全貌，是 AI **了解"自己在哪台机器上"**的第一步。
内容来自 `termux-info` + `getprop`，包括：

- Termux 版本、包管理器、仓库源
- 可用包架构（本机 `arm64-v8a`）、Android ABI 列表
- Android 版本 / SDK / 内核版本 / 设备型号
- 已装软件包的简短清单、Python 版本

| 场景 | 说明 |
|---|---|
| **决定装什么包** | 看到 ABI、SDK 才知道该装哪个版本 |
| **排查兼容性** | 比如 Android 14+ 的 dex 只读限制、API 差异 |
| **环境快照** | 出问题前存一份，方便对比"后来改了什么" |
| **写环境报告** | 让 AI 把信息整理成 Markdown 存档 |

**真实例子**

```jsonc
{}
```

AI 拿到结果后可以自己推理，例如："SDK 36 → Android 16 → 需要留意
分区存储限制"、"arch 是 aarch64 → 下载 arm64 的二进制"。

**⚠️ 坑与注意**

- 只读，无风险，随便调。
- 内部有 `getprop` 等调用，个别属性在非提权下可能为空。

---

### 4.6 `pkg_list` —— 软件包查询

**参数**

```jsonc
{
  "scope": "installed",     // installed（默认）| all | upgrade
  "pattern": "python"       // 可选，子串过滤，不区分大小写
}
```

**三种 scope 的区别**

| scope | 查什么 | 是否必须给 pattern |
|---|---|---|
| `installed` | 本机已装的包（`dpkg-query`） | 否 |
| `upgrade` | 有更新可用的包（`apt list --upgradable`） | 否 |
| `all` | **仓库里全部包** | **必须给**，否则报错（防输出爆炸） |

**实际能做什么**

| 场景 | 例子 |
|---|---|
| **确认某工具装没装** | `{scope:"installed", pattern:"ffmpeg"}` |
| **看环境全貌** | `{scope:"installed"}` |
| **找包名** | `{scope:"all", pattern:"sqlite"}` → 找到确切包名再装 |
| **看有啥能升级** | `{scope:"upgrade"}` |
| **查 Python 生态** | `{scope:"installed", pattern:"python"}` |

**真实例子：AI 装工具前的"先查再装"流程**

```jsonc
// 1) 装了没？
{ "scope": "installed", "pattern": "ffmpeg" }

// 2) 没装的话，先确认仓库里有没有、叫什么
{ "scope": "all", "pattern": "ffmpeg" }

// 3) 确认后安装（用 pkg_manage）
```

**⚠️ 坑与注意**

- `scope=all` 必须带 `pattern`，这是服务端**故意的限制**，避免一次拉几万行刷爆上下文。
- 输出最多 300 行，超了会标注"仅显示前 300"。
- 查询超时上限 `installed` 60s、`all`/`upgrade` 120s，慢网可能超时。
- 只读，无风险。

---

### 4.7 `pkg_manage` —— 软件包管理

**参数**

```jsonc
{
  "action": "install",              // install | remove | update | upgrade
  "packages": ["git", "wget"]       // install / remove 必填
}
```

**四种 action**

| action | 底层命令 | 超时 | 说明 |
|---|---|---|---|
| `install` | `apt-get install -y <pkgs>` | 600s | 装包 |
| `remove` | `apt-get remove -y <pkgs>` | 120s | 卸包（**保留配置**） |
| `update` | `apt-get update` | 300s | 刷新软件源索引 |
| `upgrade` | `apt-get full-upgrade -y` | 900s | 升级**全部**可升级包 |

全部以 `DEBIAN_FRONTEND=noninteractive` 执行，不会卡在交互询问上。

**实际能做什么**

这是 AI "**自己给自己装能力**"的关键——很多需求可以现场装工具解决：

| 需求 | 装什么 |
|---|---|
| 处理视频 | `pkg_manage install ["ffmpeg"]` |
| 图片处理 | `pkg_manage install ["imagemagick"]` |
| 数据/数据库 | `pkg_manage install ["sqlite"]` |
| 代码版本管理 | `pkg_manage install ["git"]` |
| 联网下载 | `pkg_manage install ["wget", "curl"]` |
| 压缩解压 | `pkg_manage install ["zip", "unzip", "p7zip"]` |
| 编译环境 | `pkg_manage install ["clang", "make", "binutils"]` |
| Python 库 | 用 `exec_command "pip install requests pandas"`（pip 不在此工具范围） |
| 云同步 | `pkg_manage install ["rclone"]` |

**真实例子：AI 给手机搭一套"视频压缩流水线"**

```jsonc
// 1) 刷新索引
{ "action": "update" }

// 2) 装 ffmpeg
{ "action": "install", "packages": ["ffmpeg"] }
```

```bash
# 3) 批量压缩（exec_command）
exec_command "cd /sdcard/DCIM && for f in *.mp4; do ffmpeg -y -i \"$f\" -vcodec libx264 -crf 30 \"${f%.mp4}_small.mp4\"; done"

# 4) 看效果
exec_command "ls -lh /sdcard/DCIM | tail -20"
```

**⚠️ 坑与注意**（这是**最危险**的工具之一）

- `remove` 可能**连带卸掉依赖它的包**，动手前让 AI 先 `pkg_list` 看看谁依赖它。
- `upgrade` 是 **full-upgrade**，会动全局环境，**可能升级后某些工具行为变了**。
  长期稳定的环境要慎用。
- 装大包（如 `clang`）耗时较长，注意 600s 上限；网络差时可能超时。
- 安装失败通常是**软件源不通**，先 `pkg_manage update` 再看。
- `apt-get` 需要写 `$PREFIX`，权限通常是够的（Termux 用户态）。

---

### 4.8 参数对错怎么判断（错误分层）

AI 需要能区分"**我参数写错了**"和"**命令跑失败了**"：

| 返回 | 含义 | AI 该怎么做 |
|---|---|---|
| 错误码 `-32602` | 参数错误（缺必填、类型不对、超范围） | 改参数**重试** |
| 错误码 `-32601` | 未知方法 | 客户端协议问题，别重试 |
| 错误码 `-32700` | JSON 解析失败 | 客户端问题，别重试 |
| `result.isError = true` | **工具业务失败**（文件不存在、命令非零退出、超时） | 看输出**修正命令**后重试 |
| 正常返回 | 成功 | 继续下一步 |

**服务端强制配额**（防止 AI 失控 / 刷爆上下文）：

| 项 | 限制 |
|---|---|
| `exec_command` 超时 | `1000 ~ 300000 ms` |
| `read_file` | `1B ~ 1MB` |
| `list_dir` | 最多 500 项 |
| `pkg_list` | 最多 300 行；`scope=all` 必须给 pattern |
| 单路输出 | 截断至 65536 字符 |
| 请求体 | 上限 10MB；不支持 chunked 与批量请求 |

---

## 5. 接入 AI 客户端

### 5.1 无鉴权（最简单）

```json
{
  "mcpServers": {
    "termux": {
      "url": "http://127.0.0.1:8000/mcp"
    }
  }
}
```

### 5.2 带 Bearer 鉴权

```json
{
  "mcpServers": {
    "termux": {
      "url": "http://127.0.0.1:8000/mcp",
      "headers": {
        "Authorization": "Bearer termuxmcpon"
      }
    }
  }
}
```

### 5.3 手动验证服务是否正常

```bash
# 带 token 列工具
curl -s http://127.0.0.1:8000/mcp \
  -H 'Content-Type: application/json' \
  -H 'Authorization: Bearer termuxmcpon' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
```

---

## 6. 通过 rish 获取 Shizuku 的 shell 权限

### 6.1 操作步骤（4 步）

本机 `rish` 与 dex 已就位：`~/rish/rish` + `~/rish/rish_shizuku.dex`。

**① 启动 Shizuku 并授权 Termux**
打开 Shizuku → 「已授权应用」→ 勾选 Termux。
（无 root 时 Shizuku 需用无线调试配对启动）

**② 验证 rish 通不通**

```bash
~/rish/rish -c 'id'
```

看到 `uid=2000(shell)` 就算成功（普通 Termux 是 `u0_a352`）。

**③（关键）做一个 `rsh` 封装，让 AI 一条命令就能提权**

```bash
cat > $PREFIX/bin/rsh <<'EOF'
#!/data/data/com.termux/files/usr/bin/bash
exec "$HOME/rish/rish" -c "$*"
EOF
chmod +x $PREFIX/bin/rsh
```

**④ 之后 AI 只要加 `rsh` 前缀即可**

```jsonc
{ "command": "rsh pm list packages -3" }
```

> 若第 ② 步报 `app_process cannot load writable dex`，
> 执行 `chmod 400 ~/rish/rish_shizuku.dex`（Android 14+ 要求 dex 只读）。

### 6.2 拿到 shell 权限后的优势

身份从 `u0_aXXX`（普通第三方应用）变成 **`uid 2000 (shell)`**，
能力直接跃升一个档次：

| 能力 | 未提权 | 提权后（rsh） |
|---|---|---|
| 列/操作**所有**应用 | ❌ 只能看自己 | ✅ `pm list packages -3` |
| 冻结 / 恢复应用 | ❌ | ✅ `pm disable-user` / `pm enable` |
| 卸载、静默安装 | ❌ | ✅ `pm uninstall` / `pm install` |
| 强制停止、拉起指定页面 | ❌ | ✅ `am force-stop` / `am start -n` |
| 授/撤运行时权限 | ❌ | ✅ `pm grant` / `pm revoke` |
| 读系统日志 | ❌ | ✅ `logcat -d` |
| `dumpsys` 系统级信息 | ❌ 基本被拒 | ✅ 电池/网络/Activity 都能看 |
| 截图、模拟按键、改分辨率 | ❌ | ✅ `screencap` / `input` / `wm` |

**一句话总结**：不加 rish，AI 只能在 Termux 自己的小天地 + `/sdcard` 里活动；
加上 rish，AI 才真正**摸到了整个 Android 系统**，能干"应用管理、权限调整、
系统诊断、自动化操作"这类以前只有 root 才能碰的事。

> ⚠️ shell 权限 ≠ root（`/data/data` 其他应用目录、`su` 仍不可用），
> 但对绝大多数"手机运维/自动化"需求已经完全够用。
> 属准系统级能力，**仅对自己拥有的设备使用**。

---

## 7. AI 借助本插件能做什么（实例）

按"用户一句话 → AI 自己拆解执行"的形式给出，方便直接照抄。

### 7.1 信息与环境

| 需求 | AI 会怎么做 |
|---|---|
| "看看我的手机环境" | `device_info` → 整理成 Markdown 报告 |
| "我装了哪些 python 相关的包" | `pkg_list {scope:"installed", pattern:"python"}` |
| "有没有该升级的" | `pkg_list {scope:"upgrade"}` |
| 🔑 "列出我所有第三方应用" | `rsh pm list packages -3` |
| 🔑 "现在前台是哪个页面" | `rsh dumpsys activity activities \| grep mResumedActivity` |
| 🔑 "手机温度和电量" | `rsh dumpsys battery` |

### 7.2 文件与数据整理

| 需求 | AI 会怎么做 |
|---|---|
| "下载目录都有啥" | `list_dir {path:"/sdcard/Download"}` |
| "谁占了我最多空间" | `du -sh /sdcard/* \| sort -h \| tail` |
| "找出大于 200M 的文件" | `find /sdcard -type f -size +200M` |
| "按类型归档下载目录" | `mkdir -p ~/sort/{img,doc,apk}` + `find -exec mv` |
| "把这批 .jpeg 改成 .jpg" | `for f in *.jpeg; do mv "$f" "${f%.jpeg}.jpg"; done` |
| "在笔记里搜某个关键词" | `grep -rn "关键词" /sdcard/Notes` |

> 💡 AI 的通用套路：**`list_dir` 探查 → `exec_command` 操作 → `read_file` 抽查**，
> 形成"探查—操作—验证"闭环。

### 7.3 软件环境管理

| 需求 | AI 会怎么做 |
|---|---|
| "给我装 git 和 wget" | `pkg_manage {action:"install", packages:["git","wget"]}` |
| "刷新软件源" | `pkg_manage {action:"update"}` |
| "装套视频处理工具" | `pkg_manage install ["ffmpeg"]` → 试跑验证 |
| "装个 Python 库" | `exec_command "pip install pandas"` |

### 7.4 自动化脚本与工具链

AI 可以**写脚本 → 赋权 → 运行 → 读结果**，一气呵成：

```jsonc
// ① 写脚本
write_file {
  "path": "~/auto_backup.sh",
  "content": "#!/bin/bash\nset -e\ntar czf ~/bak/$(date +%F).tgz /sdcard/DCIM\n",
  "mode": "overwrite"
}
```

```bash
# ② 赋权并执行
exec_command "chmod +x ~/auto_backup.sh && ~/auto_backup.sh"

# ③ 核验产物
exec_command "ls -lh ~/bak/"
```

典型产出：相册定时归档、媒体批量压缩、文本批处理（编码转换/去重/正则清洗）、
用 `python3` 做数据分析并 `write_file` 存报告。

### 7.5 🔑 系统级操作（shell 权限）

| 需求 | AI 会怎么做 |
|---|---|
| "冻结这个总自启的应用" | `rsh pm disable-user --user 0 com.xxx` |
| "解冻它" | `rsh pm enable com.xxx` |
| "卸掉它但留数据" | `rsh pm uninstall -k --user 0 com.xxx` |
| "强停它" | `rsh am force-stop com.xxx` |
| "帮我直接打开它的某个页面" | `rsh am start -n com.xxx/.MainActivity` |
| "给它相机权限" | `rsh pm grant com.xxx android.permission.CAMERA` |
| "看最近的系统日志" | `rsh logcat -d -t 300` |
| "清掉它的缓存" | `rsh pm clear com.xxx` |
| "截个图" | `rsh screencap -p /sdcard/shot.png` |
| "把屏幕密度改成 420" | `rsh wm density 420` |

> 大量"App 精简 / 优化 / 冻结唤醒"的操作，本质就是上面这几条 `pm` / `am`。
> AI 可以先 `rsh pm list packages` 摸清现状，再批量生成方案让你确认。

### 7.6 组合实战：一句话交给 AI

1. **"看看谁在后台偷跑"**
   → `rsh dumpsys activity processes` 找进程 → `rsh pm list packages` 对包名
   → 给出处置建议 → `write_file` 存报告。
2. **"把这周拍的视频压到 1/3"**
   → `find -mtime -7 -name '*.mp4'` → `ffmpeg -crf 28` 批量 → 校验大小
   → 生成前后对比表。
3. **"给我搭一套爬虫环境"**
   → `pkg_manage install [python, git]` → `pip install requests lxml`
   → `write_file` 写爬虫 → `exec_command` 跑 → `read_file` 汇总结果。
4. **"整理我的下载目录"**
   → `list_dir` 分类 → 移动/重命名 → `write_file ~/Download/清单.md`。

---

## 8. 安全须知

> 本插件把"执行任意命令"的能力交给了 AI，**这既是它强大的原因，也是它最大的风险。**

- 🔴 **`exec_command` / `pkg_manage` / `write_file` 是高危工具**，
  删除、覆盖类命令**不可撤销**。建议：
  - 让 AI 在大批量操作前**先输出计划**，人工确认后再执行；
  - 涉及 `rm -rf`、覆盖、`pm uninstall` 时，先做**干跑（dry-run）或备份**。
- 🔒 **只监听 `127.0.0.1`**：外部网络访问不到，但**同一设备上的其他应用**理论上可访问。
  用 `MCP_TOKEN` 更稳妥。
- 🔑 **Shizuku shell 权限 ≈ 准系统权限**：能停应用、改设置、读日志。
  仅对**自己拥有的设备**使用。
- 🧹 **日志会记录命令**：`$PREFIX/var/log/mcp.log` 可能含隐私路径与内容。
- ♻️ **保留回滚手段**：改配置前先备份 `~/.bashrc`；
  鉴权出问题用 `bash ~/mcp_no_token.sh` 一键恢复。

---

## 9. 常见问题

**Q1. 客户端连接失败（Connection refused）**
服务没起。执行 `mcp status`；未运行则 `mcp on`。
Termux 被系统杀死后服务也会停，需要重新 `mcp on`。

**Q2. 返回 401 unauthorized**
启用了 `MCP_TOKEN` 但客户端没带请求头。加上
`Authorization: Bearer <token>`，或执行 `bash ~/mcp_no_token.sh` 切回无鉴权。

**Q3. 端口被占用**
```bash
MCP_PORT=8010 mcp on
```

**Q4. `mcp on` 提示找不到 python3**
```bash
pkg install python
```

**Q5. rish 报 `app_process cannot load writable dex`**
```bash
chmod 400 ~/rish/rish_shizuku.dex
```

**Q6. rish 报权限被拒绝**
Termux 没在 Shizuku 里获得授权。Shizuku → 已授权应用 → 勾选 Termux。

**Q7. rish 执行很慢**
`app_process` 冷启动开销所致，属正常现象。合并命令、减少调用次数即可。

**Q8. 服务莫名停止**
```bash
tail -n 50 $PREFIX/var/log/mcp.log
```

**Q9. 怎么临时关掉某些高危工具？**
编辑 `$PREFIX/share/termux-mcp/server.py`，从 `TOOLS` 与 `TOOL_IMPL`
中注释掉对应项，然后 `mcp off && mcp on`。

**Q10. PID 文件残留导致状态错乱**
```bash
rm -f $PREFIX/var/run/mcp.pid
```

---

## 附：HTTP 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| `POST` | `/mcp` | MCP 端点，JSON-RPC 2.0，需鉴权（若启用） |
| `GET` | `/` | 服务状态 JSON（健康检查，**不鉴权**） |
| `GET` | `/mcp` | `405`，未实现 SSE 流（规范允许） |
| `DELETE` | `/mcp` | `405`，无会话概念 |

支持的 JSON-RPC 方法：`initialize`、`ping`、`tools/list`、`tools/call`、
`notifications/*`（通知类消息返回 `202` 无响应体）。

---

*文档基于本目录源码 `mcp`、`server.py`、`install.sh` 与 Termux 实测环境整理。*

---

## 开源协议

本项目采用 **Apache License 2.0** 开源，详见 [LICENSE](LICENSE) 文件。

Copyright 2024 kangditandin

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
