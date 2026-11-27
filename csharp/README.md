# CampusNetAuth（C# 重写版，迁移中）

本目录是 **全量 C# 重写** 的代码，独立分支 `feat/csharp`，**暂不合并 main**。
目标：安装器化 + 单运行时 + 保留现有 HTML UI（`ui/index.html` 从 Python 版原样抽出）。

## 工具链

- .NET SDK **10.0**（本机为 10.0.404，用户级安装在 `C:/Users/XiChen/.dotnet`）。
  运行/构建前需要：

```bash
export PATH="/c/Users/XiChen/.dotnet:$PATH"
export DOTNET_ROOT="C:\Users\XiChen\.dotnet"   # 运行 apphost 时必需
```

## 构建与自检

```bash
dotnet build csharp/CampusNetAuth.sln
dotnet run --project csharp/tests/CampusNetAuth.Tests
```

自检是零依赖控制台（不走 vstest）：沙箱里 `dotnet test` 的 testhost 会因
`OpenProcess` 拒绝访问而启动失败。当前覆盖 **7 组 RSA 黄金向量**（与 Python 版
逐字节一致）、反转/编码、版本比较、配置归一、登录页判定、input 解析。

## 运行 CLI

```bash
csharp/src/CampusNetAuth.App/bin/Debug/net10.0-windows/CampusNetAuth.exe --version
# 从 cmd 运行可看到输出；被重定向时同样可用
```

已实现命令：`--version / setup / login / logout / status / once / daemon / start /
stop / proxy / update / upgrade / install / uninstall / log / test`。

## 进度

- 里程碑 1：核心层（Config/State/DPAPI/Crypto/Http/PortalClient）+ 黄金向量自检
- 里程碑 2：守护循环 + 控制通道、自启、代理守卫、更新检查/下载校验、就地升级、CLI
- 里程碑 3（已完成）：本地 HTTP API（26 端点）+ WebView2 无边框窗口 + 托盘；
  页面加载后自动轮询 `/api/status`，token/回环 Host 校验生效
- 里程碑 4（进行中）：**代码绘制**的图标（16–256 ICO + splash）与 Velopack
  安装器已完成；
  安装版内置更新（Velopack UpdateManager）与数据目录迁移到 `%APPDATA%` 待做；
  与 Python 版行为对拍、迁移文档待做

## 图标（代码绘制，无 AI 水印）

图标由 `assets/icon.html`（HTML/CSS + 手写 SVG 路径）描述，经
`tools/IconRenderer`（WebView2 无头渲染，透明背景）导出为 PNG，再由
`tools/make_assets.py` 生成多尺寸 ICO 与安装引导图：

```powershell
powershell -ExecutionPolicy Bypass -File csharp/build_icon.ps1
```

- 无水印、可审阅、可复现；改颜色/形状直接改 `icon.html`；
- 产物：`assets/app.ico`（16–256）、`app-1024.png`、`app-256.png`、`splash.png`；
- 渲染器的 WebView2 用户数据目录放在工具目录下（受限沙箱里工作区内的 exe
  不能往工作区外写）。

## 数据目录

- **开发 / 绿色版**：`config.json / cred.bin / state.json / campusnet.log` 在 exe 同目录
  （与 Python 版一致，便于对拍与迁移）；
- **安装版（Velopack 布局）**：在 `%APPDATA%\CampusNetAuth`；首次启动会把 exe
  同目录的旧数据**复制**过去（不覆盖已有文件）；
- 可用 `CNA_DATA_DIR` 环境变量覆盖（测试/便携场景）。

## 安装器（Velopack）

```powershell
powershell -ExecutionPolicy Bypass -File csharp/build_installer.ps1
# 产物：csharp/dist/releases/CampusNetAuth-win-Setup.exe（PerUser / x64）
```

- 引导页使用浅色 splash + 品牌蓝进度条（与 UI 风格一致）；
- 安装版由安装器管理文件：程序内 `upgrade` 会拒绝裸 exe 换位（`IsInstalledLayout()`），
  后续接入 Velopack UpdateManager 做内置更新；
- 当前未签名：SmartScreen 会提示，正式分发需要代码签名证书。

## 原生窗口风格原则（重要）

程序的**所有**原生窗口都应与 HTML UI 的极简风格一致：无边框自绘标题栏、
克制的单一主色、无系统渐变/大图标。包括但不限于安装器引导页、卸载器、
错误对话框——不要直接弹 Win32/WinForms 默认样式的窗口；优先复用 UI 的
HTML 页面（本地 HTTP + WebView2）或按同一套视觉规范自绘。

## 注意

- 与 Python 版**共用** `127.0.0.1:47667` 和 `VER` 控制协议，两者不能同时运行守护；
  迁移期实测 C# CLI 可以读取 Python 守护的版本（协议互通）。
- `config.json / cred.bin / state.json / campusnet.log` 仍在 exe 同目录（与 Python 版一致）；
  安装器阶段再迁到 `%APPDATA%\CampusNetAuth` 并做一次性迁移。