<div align="center">

# DanmakuGuard

**基于 AI 的 B站弹幕自动审核与举报工具，守护弹幕环境。**

[![Python](https://img.shields.io/badge/Python-3.11+-blue?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-async-009688?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![Platform](https://img.shields.io/badge/Platform-Windows%20%7C%20macOS%20%7C%20Linux-lightgrey)]()
[![GitHub Stars](https://img.shields.io/github/stars/CirnoSalt/DanmakuGuard?style=social)](https://github.com/CirnoSalt/DanmakuGuard)
[![GitHub Forks](https://img.shields.io/github/forks/CirnoSalt/DanmakuGuard?style=social)](https://github.com/CirnoSalt/DanmakuGuard)
[![GitHub Issues](https://img.shields.io/github/issues/CirnoSalt/DanmakuGuard)](https://github.com/CirnoSalt/DanmakuGuard/issues)

</div>

---

> 基于 Web + 后端的 B站弹幕自动审核举报工具。通过接入 OpenAI 兼容的 AI API，自动分析视频内全部弹幕，识别违规内容并批量提交举报。支持源码运行与一键打包为 Windows 可执行文件。

## 目录

- [功能特性](#功能特性)
- [环境要求](#环境要求)
- [快速开始](#快速开始)
  - [源码运行](#方式一源码运行)
  - [打包 exe](#方式二使用打包好的-exe)
- [配置项说明](#配置项说明)
- [风控与冷却策略](#风控与冷却策略)
- [举报理由代码](#举报理由代码)
- [日志](#日志)
- [注意事项](#注意事项)
- [目录结构](#目录结构)
- [Star 历史](#star-历史)

## 功能特性

- **一键处理**：输入视频链接，自动完成 拉取弹幕 → AI 分析 → 批量举报 全流程
- **OpenAI 兼容**：支持任意 OpenAI 标准 API（LM Studio / Ollama / DeepSeek / 官方等）
- **智能分析**：AI 按固定提示词判断每条弹幕是否违规，返回违规类型与置信度
- **去重送审**：相同内容弹幕自动合并送审，命中后举报全部重复弹幕
- **本地预过滤**：违禁词词典命中直接举报，节约 AI 开销
- **多账号轮换**：配置多个 B站账号，触发风控自动切换下一个，全部风控则进入等待循环
- **拟人化冷却**：每次举报前在 5~10 秒之间随机等待，避免节奏固定被风控
- **自适应退避**：触发风控自动指数退避，连续成功后间隔回落
- **实时推送**：Web 控制台通过 SSE 实时显示统计与日志
- **可随时停止**：能即时中断进行中的 AI 调用
- **完善日志**：控制台 + 文件滚动记录，敏感信息自动脱敏
- **一键打包**：支持 PyInstaller 打包为 `exe`，免 Python 环境分发

## 环境要求

| 运行方式 | 环境要求 |
|----------|----------|
| 源码运行 | Python 3.11+，Windows / macOS / Linux |
| 打包产物 | Windows 10/11，无需 Python 环境 |

## 快速开始

### 方式一：源码运行

#### 1. 安装依赖

建议使用虚拟环境：

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

#### 2. 配置

复制示例配置并填写真实信息：

```powershell
Copy-Item config.example.yaml config.yaml
Copy-Item accounts.example.yaml accounts.yaml
```

需要填写两部分：

**B站账号 Cookie**（必填，在 `accounts.yaml`）

打开浏览器登录 B站，F12 → Application/存储 → Cookies → `bilibili.com`，复制以下两个值填入 `accounts.yaml`：
- `SESSDATA`
- `bili_jct`（CSRF Token，举报时必需）

支持配置多个账号轮换，触发风控自动切换下一个账号：

```yaml
accounts:
  - name: 账号1
    sessdata: "xxx"
    bili_jct: "yyy"
  - name: 账号2
    sessdata: "xxx"
    bili_jct: "yyy"
```

**AI API**（必填，在 `config.yaml`）

填写任意 OpenAI 兼容 API：
- `ai.base_url`：API 地址，如 `https://api.openai.com/v1`、`http://localhost:1234/v1`
- `ai.api_key`：API Key（本地部署可填任意值）
- `ai.model`：模型名，如 `gpt-4o-mini`、`qwen3-...`

**举报限制**（建议）

- `report.max_reports`：单任务举报上限，`0` 为不限。

完整配置示例见 [config.example.yaml](config.example.yaml)。

#### 3. 启动

```powershell
python run.py
```

启动成功后访问：<http://127.0.0.1:8000>

### 方式二：使用打包好的 exe

#### 1. 打包

```powershell
.\.venv\Scripts\python.exe -m pip install pyinstaller
.\.venv\Scripts\python.exe -m PyInstaller build_exe.spec --clean --noconfirm
```

产物位于 `dist/bili_report/`：
- `bili_report.exe`：入口程序
- `_internal/`：依赖与内置资源（前端页面、违禁词词典、示例配置）

#### 2. 配置

从 `dist/bili_report/_internal/config.example.yaml` 复制一份到 `dist/bili_report/config.yaml`，填写真实 Cookie 与 AI 配置。

> exe 同级目录的 `config.yaml` 优先于内置资源，方便用户编辑而不必重新打包。

#### 3. 运行

双击 `bili_report.exe`，或命令行启动：

```powershell
.\dist\bili_report\bili_report.exe
```

浏览器访问 <http://127.0.0.1:8000>。日志会写入 exe 同级目录的 `logs/`。

### 使用流程

1. 在网页输入框粘贴视频链接（支持 `https://www.bilibili.com/video/BVxxxx` 或纯 BV 号）
2. 点击「开始」
3. 实时查看进度：弹幕总数、已分析、违规数、举报成功/失败
4. 需要时可点击「停止」中断任务（能即时取消进行中的 AI 调用）

## 配置项说明

<details>
<summary>点击展开完整配置表</summary>

| 配置项 | 默认值 | 说明 |
|--------|--------|------|
| `bilibili.accounts_path` | `accounts.yaml` | 账号列表文件路径（多账号轮换） |
| `bilibili.all_limited_wait` | 300.0 | 所有账号都风控时的等待循环间隔（秒） |
| `bilibili.request_interval` | 7.5 | 举报冷却中心值（秒），实际等待为该值 ±2.5 秒随机 |
| `bilibili.max_retries` | 3 | 请求失败重试次数 |
| `bilibili.segment_cap` | 30 | 弹幕分段拉取上限 |
| `ai.batch_size` | 20 | 每批送审弹幕条数 |
| `ai.max_tokens` | 2000 | AI 输出 token 上限，仅容纳 JSON 输出；过大会鼓励思考型模型展开推理 |
| `ai.reasoning_effort` | minimal | 思考型模型推理深度：minimal/low/medium/high；留空则不发送 |
| `ai.timeout` | 120 | 单次 AI 调用超时秒数 |
| `ai.confidence_threshold` | 0.6 | 低于该置信度不举报 |
| `ai.temperature` | 0.0 | AI 温度，0 为确定输出 |
| `report.dedup_by_content` | true | 相同内容合并送审 |
| `report.default_reason` | 7 | AI 未给理由时的默认举报理由（7=引战）|
| `report.max_reports` | 0 | 单任务举报上限，0 不限 |
| `report.dictionary_path` | `dict/banned_words.yaml` | 违禁词词典路径，不存在则禁用预过滤 |
| `server.host` | 127.0.0.1 | 监听地址 |
| `server.port` | 8000 | 监听端口 |

</details>

## 风控与冷却策略

为避免被识别为机器行为，举报冷却采用三层机制：

```
正常节奏 ──┬── 随机抖动：5~10 秒随机等待（模拟人工）
           │
           ├── 触发风控 ──→ 间隔 ×3 升级，2^n 指数退避（上限 5 分钟）
           │
           └── 连续成功 5 次 ──→ 间隔减半回落到基准值
```

1. **随机抖动**：每次举报前在 `request_interval ± 2.5` 秒之间随机等待（默认 5~10 秒），模拟人工节奏
2. **风控退避**：触发风控时，间隔自动 ×3 升级，重试按 2^n 指数退避，单次最长 5 分钟
3. **成功回落**：连续成功 5 次后，间隔自动减半回落到基准值

随机抖动围绕当前冷却中心值浮动，风控升级后抖动也会同步放大，整体节奏自然不固定。

## 举报理由代码

| 代码 | 含义 | 代码 | 含义 |
|:----:|------|:----:|------|
| 1 | 违法违禁 | 7 | 引战 |
| 2 | 色情低俗 | 8 | 剧透 |
| 3 | 非法交易 | 9 | 恶意刷屏 |
| 4 | 人身攻击 | 10 | 视频无关 |
| 5 | 侵犯隐私 | 11 | 其他 |
| 6 | 垃圾广告 | 12 | 青少年不良 |

> 注：AI 送审不包含 `8 剧透` 和 `10 视频无关`（AI 无法获知视频内容，无法判断这两类）。违禁词词典预过滤仍可使用全部代码。

## 日志

日志位于 `logs/` 目录（打包后位于 exe 同级目录），按 5MB 大小滚动、保留 5 份。包含：

- 任务生命周期（入队、开始、完成、停止、失败）
- AI 请求耗时与 finish_reason
- 举报结果（成功/失败/风控）
- 敏感信息（Cookie、Token）自动脱敏

## 注意事项

- **思考型模型**（如 qwen3）默认会展开长推理，已通过 `reasoning_effort: minimal` 与提示词约束抑制；若仍出现空内容，可适当调大 `ai.max_tokens` 或将 `reasoning_effort` 设为 `low`
- **本地部署模型**推理较慢时，调小 `ai.batch_size` 或加大 `ai.timeout`
- **举报过频**会触发风控，程序会自动退避；仍建议设置 `report.max_reports` 限制单任务举报量
- **Cookie 失效**会标记任务失败，需重新获取填入配置重启
- 统计数据仅保存在内存，重启后清空
- 打包后修改 `config.yaml` 无需重新打包，重启 exe 即可生效

## 目录结构

<details>
<summary>点击展开目录树</summary>

```
bili_report/
├── app/
│   ├── ai.py          # AI 分析（提示词 + 批量分析 + JSON 解析）
│   ├── bilibili.py    # B站 API 客户端（HTTP 重试、视频、弹幕、protobuf、举报）
│   ├── core.py        # 任务模型、事件总线、冷却、违禁词词典、任务管理器
│   ├── paths.py       # 资源路径工具（兼容开发环境与 PyInstaller 打包环境）
│   ├── web/           # FastAPI 路由与前端页面
│   ├── config.py      # 配置加载与校验
│   ├── logger.py      # 日志配置
│   └── main.py        # FastAPI 应用入口
├── dict/              # 违禁词词典
├── config.yaml        # 真实配置（需自建）
├── config.example.yaml
├── requirements.txt
├── build_exe.spec     # PyInstaller 打包配置
└── run.py             # 启动入口
```

打包产物结构：

```
dist/bili_report/
├── bili_report.exe    # 入口程序
├── config.yaml        # 用户配置（从 _internal 复制并填写）
├── logs/              # 运行时自动生成
└── _internal/
    ├── app/web/static/  # 前端页面（只读资源）
    ├── dict/            # 违禁词词典（只读资源）
    ├── config.example.yaml
    └── *.pyd / *.dll    # Python 运行时与依赖
```

</details>

## Star 历史

如果这个项目对你有帮助，欢迎点个 Star 支持一下！

[![Star History Chart](https://api.star-history.com/svg?repos=CirnoSalt/DanmakuGuard&type=Date)](https://star-history.com/#CirnoSalt/DanmakuGuard&Date)

---

<div align="center">

**DanmakuGuard** · 守护弹幕环境

</div>
