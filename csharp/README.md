# CampusNetAuth（C# 重写版，迁移中）

本目录是 **全量 C# 重写** 的代码，独立分支 `feat/csharp`，**暂不合并 main**。
目标：安装器化 + 单运行时 + 保留现有 HTML UI（`ui/index.html` 从 Python 版原样抽出）。

**0.7.0 绿色过渡版已于 2026-11-27 发布**（单文件自包含，0.6.4 可一键升级）；
**V1.0.0 正式版已发布；V1.0.1 补丁版**（安装器 UI/提示窗、自绘横幅与通知统一）。

## 工具链

- .NET SDK **10.0**（用户级安装即可，路径按本机 `%USERPROFILE%\.dotnet` 调整）。
  运行/构建前需要：

```bash
# Git Bash
export PATH="$HOME/.dotnet:$PATH"
export DOTNET_ROOT="$(cygpath -w "$HOME/.dotnet")"
```

## 构建与自检

```bash
dotnet build CampusNetAuth.slnx
dotnet run --project tests/CampusNetAuth.Tests          # 默认离线自检
dotnet run --project tests/CampusNetAuth.Tests -- --notify   # 手动探针：弹一条真实通知
```

自检是零依赖控制台（不走 vstest）：沙箱里 `dotnet test` 的 testhost 会因
`OpenProcess` 拒绝访问而启动失败。当前覆盖 **7 组 RSA 黄金向量**（与 Python 版
逐字节一致）、反转/编码、版本比较、配置归一、登录页判定、input 解析。

改 `ui/index.html` 后至少做一次静态校验（语法 + id 引用）：

```bash
python - <<'EOF'
import re, pathlib
html = pathlib.Path('ui/index.html').read_text(encoding='utf-8')
pathlib.Path('.ui_check.js').write_text('\n'.join(re.findall(r'<script>(.*?)</script>', html, re.S)), encoding='utf-8')
ids = set(re.findall(r'id="([^"]+)"', html))
refs = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", html))
print('缺失 id:', sorted(refs - ids) or '无')
EOF
node --check .ui_check.js
```

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
- 里程碑 4（已完成）：**代码绘制**的图标（16–256 ICO + splash）、Velopack
  安装器、安装版内置更新（UpdateManager）、数据目录迁移到 `%APPDATA%`、
  系统通知（AUMID + Shell_NotifyIcon，守护进程也能弹）、安装版卸载接管
  （`Update.exe --uninstall`）、自启命令自愈 + 卸载时移除自启、
  旧版目录自动迁移清理（导入数据 → 停旧守护 → 整目录移入回收站 → 失败重试）
- 里程碑 5（进行中）：与 Python 版行为对拍、单测移植（当前 C# 自检 19 项）；
  绿色单文件构建已完成（0.7.0 兼容升级件）

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

## 绿色单文件构建（0.6.x 的兼容升级件）

```powershell
powershell -ExecutionPolicy Bypass -File csharp/build_green.ps1 [-Version 0.7.0]
# 产物：csharp/dist/green/CampusNetAuth.exe（约 50 MB，自包含单文件）
```

- 自包含 win-x64 + 压缩单文件；首次运行把原生库解包到 `%TEMP%\.net\...`，之后启动很快
  （实测 `--version` 约 0.3 秒）；
- PE `FileVersion` / `ProductName` 与请求版本一致（脚本构建后会校验），满足 Python 0.6.x
  升级器的资产名/版本/产品名校验；配置、凭据（DPAPI，`cred.bin` 为 `DPAPI\x01` 头 +
  DPAPI blob，与 Python 版同格式）、状态、Run 自启、`daemon` 参数全部兼容，可直接替换旧 exe；
- **版本单一来源是 csproj `<Version>`**：`AppInfo.Version` 读程序集 InformationalVersion，
  构建时可用 `-p:Version=...` / 脚本 `-Version ...` 覆盖（例如用同一份最新代码构建 0.7.0 修订版）；
- 受限沙箱里验证时需把 `DOTNET_BUNDLE_EXTRACT_BASE_DIR`、`CNA_DATA_DIR`、`TEMP/TMP`
  指向工作区内目录（工作区内的 exe 不能写 `%TEMP%` / `%LOCALAPPDATA%`）；真实用户无此限制。

## 安装器（Velopack + 自绘引导器）

```powershell
powershell -ExecutionPolicy Bypass -File csharp/build_installer.ps1 [-Version 1.0.0-preview.3]
# 产物（dist\releases）：
#   CampusNetAuth-Installer.exe   自绘安装引导器（内嵌 Setup，推荐给用户）
#   CampusNetAuth-win-Setup.exe   Velopack 一键安装（IT/命令行）
#   *.nupkg / releases.win.json   应用内更新资产
```

- 引导器是独立的 WinForms 自绘窗口（不依赖 WebView2，安装前也能运行）：安装位置可改、
  右侧「选项」（安装后启动）、安装中禁止关闭；安装完成后的首次启动会自动迁移旧版配置、
  凭据与自启，并把旧目录移入回收站；
- 无界面参数：`--silent --dir <路径> --no-launch`（自动化/冒烟测试）；
- 已有安装时自动锁定安装位置并显示「更新」；
- 卸载钩子只移除**指向本安装目录**的 Run 值，不会误删另一份绿色部署的开机自启；
- 引导器 108 MB（内嵌 62 MB Setup + 自包含运行时）；主程序仍需 WebView2。

## 从绿色版（0.6.x / 0.7.x）迁移到安装版

1. 运行 `CampusNetAuth-win-Setup.exe`（单用户安装，无需管理员）。安装器会装到
   `%LOCALAPPDATA%\CampusNetAuth`，创建桌面与开始菜单快捷方式，并自动启动一次。
2. **首次启动自动迁移**：读取开机自启 Run 值指向的旧目录，把
   `config.json / cred.bin / state.json` 复制到 `%APPDATA%\CampusNetAuth`（不覆盖已有），
   把自启改指安装版，停掉旧版守护/界面，然后把**旧目录整体移入回收站**（可恢复）。
   导入失败或文件被占用时跳过删除，记录 `legacy_cleanup_pending`，下次启动重试。
3. 回收站不可用（组策略禁用等）时退化为"只删旧版已知程序文件"，未知用户文件保留。
4. 守护与界面仍是同一个 exe 的两个进程，控制通道仍是 `127.0.0.1:47667`；
   迁移期 C# 与 Python 守护互相可见（`VER` 协议互通），但不能同时跑两个守护。
5. 卸载：界面「彻底卸载」或系统设置 → 应用。卸载会停止守护、移除开机自启、删除程序
   文件与快捷方式；**`%APPDATA%\CampusNetAuth` 会保留**（重装免配置），需要彻底清理
   请手动删除该目录。

> 想保留旧目录做备份，请在安装前把旧目录移走/改名；否则安装版首次启动会按上述规则清理它。

## 守护生命周期保证（1.0.0-preview.3 起）

- **关闭界面不会停止守护**（守护是独立进程）；
- 界面启动后自动确保守护在跑：未运行且满足「已启用自启 或 已配置账号」→ 自动拉起；
- **本次开机内手动停止过守护 → 尊重该选择**，重开界面不会强行拉起（重启后按自启恢复）；
- 只接管同一 exe 的旧版本守护（同路径且版本不同才重启）；端口被另一份部署占用时不动手，
  避免测试副本/并行安装互相劫持；
- 在界面里启用开机自启时会顺带拉起守护。

> 0.7.0 绿色版没有这段自愈逻辑：关界面同样不会停守护，但守护本来就没跑时，
> 打开/关闭界面不会自动拉起。需要该保证请升级到含此修复的版本。

## 原生窗口风格原则（重要）

程序的**所有**原生窗口都应与 HTML UI 的极简风格一致：无边框自绘标题栏、
克制的单一主色、无系统渐变/大图标。包括但不限于安装器引导页、卸载器、
错误对话框——不要直接弹 Win32/WinForms 默认样式的窗口；优先复用 UI 的
HTML 页面（本地 HTTP + WebView2）或按同一套视觉规范自绘。

## 注意

- 与 Python 版**共用** `127.0.0.1:47667` 和 `VER` 控制协议，两者不能同时运行守护；
  迁移期实测 C# CLI 可以读取 Python 守护的版本（协议互通）。
- 安装版数据在 `%APPDATA%\CampusNetAuth`，绿色/开发版仍在 exe 同目录；
  安装版首次启动会从安装目录做一次性迁移（不覆盖已有文件）。