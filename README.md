# 校园网无感认证 · CampusNetAuth

开机自动登录校园网，断线静默重连，全程无窗口、无弹窗、无感知。
针对本机门户 `http://172.16.54.18/`（**锐捷 ePortal / SAM+**）逆向适配，零第三方依赖。

> **面向校园网：湖北工业大学 iHBUT**（Hubei University of Technology）。
> 注意与河北工业大学缩写相同（均为 HBUT），本项目统一写作 **iHBUT**，勿混用。

---

## 快速开始（单 exe 版）

整个应用已经融合为**一个 exe**：`CampusNetAuth.exe` 无参运行 = 管理界面，
`daemon` 参数 = 后台守护，CLI 子命令 = 命令行工具。双击即可用，无需安装 Python。

| 步骤 | 操作 | 说明 |
|---|---|---|
| 1 | 双击 `CampusNetAuth.exe` | 打开无边框管理界面（自绘标题栏，可拖拽/最小化/最大化/关闭） |
| 2 | 界面里填账号密码 →「保存并连接」 | 立即认证 |
| 3 | 进入「自动化 → 工具箱 → 安装开机自启」 | 后台守护 + 开机自启，全部在 UI 内完成 |

> **首次使用建议先在 UI 工具箱点「一键体检」**：它会真的断网一次来验证自动重连是否可用，
> 全过程约 1 分钟，结果同时写入 `campusnet.log`。
> 只想看环境是否正常：在「状态与日志」页直接看实时数据，或命令行 `CampusNetAuth.exe test`。

当前配置：服务 **YD（移动）**，检测周期 20 秒。

---

## 命令一览

> 打包版直接用 `CampusNetAuth.exe`；源码开发态用 `python campusnet.py`，命令完全一致。

| 命令 | 作用 |
|---|---|
| `CampusNetAuth.exe`（无参） | 打开图形管理界面（双击即用） |
| `CampusNetAuth.exe setup` | 交互式配置（门户地址、账号、服务、密码） |
| `CampusNetAuth.exe install` | 安装开机自启（注册表 Run 键 → VBS 静默启动，登录零黑窗） |
| `CampusNetAuth.exe uninstall` | 移除开机自启 |
| `CampusNetAuth.exe start` | 启动后台守护（需先在 UI「自动化 → 组件管理」安装「值守组件」，释放守护启动器 vbs；不再复制 exe 副本） |
| `CampusNetAuth.exe stop` | 停止后台守护 |
| `CampusNetAuth.exe daemon` | 前台运行守护（调试用，一般不用） |
| `CampusNetAuth.exe once` | 检测一次，未认证就登录一次（也可给任务计划做定时轮询） |
| `CampusNetAuth.exe login` | 立即登录（`--force` 已在线也重登） |
| `CampusNetAuth.exe logout` | 下线 |
| `CampusNetAuth.exe status` | 查看状态：是否在线、服务、自启与守护进程状态 |
| `CampusNetAuth.exe log [-n N]` | 打印最近 N 行日志（UTF-8 读取，不会乱码） |
| `CampusNetAuth.exe test` | 自检：RSA、门户连通、登录页获取 |

---

## 管理界面

双击 **`CampusNetAuth.exe`** 直接打开（无需 .bat）。

界面为 **HTML 现代 Web 风格**（与小说管家 Novelist 同款方案：本地 HTTP 服务 + 原生
Edge WebView2 窗口，单进程零额外依赖）。窗口**无边框自绘**：顶部毛玻璃标题栏
（品牌 + Apple 交通灯风格的最小化 / 最大化 / 关闭按钮），按住标题栏任意位置可拖动，
双击标题栏切换最大化，边缘可拖拽调大小，支持 Aero Snap 吸附。

**视觉风格**：Apple 风格浅色主题 —— 毛玻璃侧栏与标题栏（`backdrop-filter` 模糊）、
圆角卡片、iOS 配色（`#007AFF` 主色 / `#34C759` 成功 / `#FF3B30` 危险 / `#FF9500` 警告）、
呼吸状态灯、分段控件、iOS 开关、页面切换动画与气泡通知。
启动**零黑框/零终端闪烁**：`hidden=True` 等 WebView2 初始化完成再显示窗口，
所有后端调用均零子进程化（Win32 API 直调），不再产生任何 CUI 子进程。

| 分页 | 可设置 |
|---|---|
| **连接与账号** | 门户地址、运营商（校园网/电信/移动/联通/自动）、账号、密码，并可立即连接或下线 |
| **自动化** | 开机后自动认证开关；**守护进程管理**（状态与 PID 显示，按上下文启用的「启动/停止/重启」）；**组件管理**（开机自启 / 值守 / 界面启动器三个组件按需安装与卸载，卸载有确认对话框）；检测周期、重试间隔、退避上限、超时；**工具箱**（自检、一键体检、打开目录、**抓取诊断**） |
| **状态与日志** | 实时网络状态、账号、服务、IP、自启与守护状态，刷新 / 立即检测 / 下线，以及深色终端风格日志区 |

**原 .bat 功能已全部迁入 UI 工具箱**（自动化页顶部，无需滚动即可看到）：`自检` / `一键体检` /
`打开目录` / `安装开机自启` / `卸载开机自启` / **`抓取诊断`** 六个按钮 + 守护进程的
`启动 / 停止 / 重启` 三个按钮。守护按钮会**根据守护运行状态自动启用/禁用**（未运行时
只有「启动」可点）。
原 .bat 文件（`管理界面.bat` / `一键测试.bat` / `查看状态.bat` / `安装.bat` / `卸载自启.bat` /
`抓取诊断.bat`）保留仅为兼容历史习惯，双击会**闪一个控制台黑框**（cmd.exe 不可避），
**推荐一律走 UI 工具箱**。`抓取诊断` 在 UI 内启动后会自动后台运行，持续监控门户状态，
**未认证时抓登录页证据，检测到手动认证完成后自动出报告** `probe/capture_result.json` +
`probe/capture_report.txt`。

细节：

- 密码输入默认掩码，可点「显示」临时查看；保存后仍走 **Windows DPAPI** 加密，
  界面不会回显已保存的密码（留空即表示不修改）。
- 所有联网操作都在后台线程执行，期间按钮禁用并显示**旋转进度指示**，不会卡住窗口。
- 提示信息用界面内的**气泡通知**（点击标题可展开详情），不使用系统弹窗。
- **守护进程只在 iHBUT 校园网内动作**：每次循环先探测校园网门户
  （`172.16.54.18`）是否可达，门户不可达（家里/热点/公共 WiFi）就静默等待、
  **绝不尝试认证**；只有门户可达且检测到未认证时才自动登录。
  开机自启启动的也是同一个守护，因此同样受此保护。
- **任务管理器里看到的是 exe 而不是 python**：主界面是 `CampusNetAuth.exe`；
  后台守护是**同一个 exe 以 `daemon` 参数运行**（不再复制 `CampusNetAuthDaemon.exe`
  副本），任务管理器里会出现两个 `CampusNetAuth.exe`（一个界面、一个守护），属正常。
- **单文件便携 + 组件按需安装**：交付物只有 `CampusNetAuth.exe` 一个文件，
  在 UI「自动化 → 组件管理」里点击安装才会放出对应文件 ——
  「开机自启组件」释放 `CampusNetAuthDaemon.vbs` 并写注册表 Run 键，
  「值守组件」释放同一个 `CampusNetAuthDaemon.vbs` 守护启动器（不再复制
  18.8MB 的 exe 副本），「界面启动器组件」释放 `CampusNetAuthUI.vbs`；
  点击卸载即移除对应文件（自启顺带删除注册表项）。
  全新部署且目录非空时会提醒"建议放入空文件夹"，该提醒只出现一次
  （首次运行已展开文件，第二次不再提示）。
- **所有配置文件都放在 exe 所在目录**（`config.json` / `cred.bin` / `state.json` /
  `daemon.pid` / `campusnet.log`），整个文件夹拷走即迁移，无需重装。
- **首次启动约 10~15 秒**（onefile 解压 + WebView2 初始化 + 首次建缓存），
  二次启动更快；窗口出现前无任何闪烁。需要 Edge WebView2 运行时，
  Windows 10/11 自带。
- 若 pywebview / WebView2 不可用（异常环境），界面会自动**回退 tkinter 版**（`ui.py`），
  功能不变。

---

## 配置文件 `config.json`

| 字段 | 默认 | 说明 |
|---|---|---|
| `portal_host` | `172.16.54.18` | 门户地址 |
| `portal_port` | `80` | 端口 |
| `username` | `1234567890` | 学号（示例，换成你自己的） |
| `service` | `YD` | 服务：`default`校园网 / `DX`电信 / `YD`移动 / `LT`联通 / `auto`自动 |
| `interval` | `20` | 在线时的检测周期（秒），即掉线后最长多久自动重连 |
| `retry_interval` | `30` | 登录失败后的重试间隔，会指数退避 |
| `max_interval` | `600` | 退避上限（秒） |
| `offline_interval` | `60` | 网络完全不可达时的检测周期 |
| `timeout` | `8` | 单次 HTTP 超时（秒） |
| `verify_interval` | `5` | 登录成功后即时确认的等待（秒）：防网关"假成功"，短等后立即复核一次，未通则当轮补登 |
| `detect_targets` | 3 个 | 连通性检测目标（HTTP，避免 HTTPS 证书干扰） |

密码**不写进** `config.json`，单独用 **Windows DPAPI** 加密存 `cred.bin`，
只有当前 Windows 用户能解密，换用户/换机器自动失效。

---

## 工作原理（逆向结论）

| 环节 | 结论 |
|---|---|
| 门户类型 | 锐捷 ePortal（SAM+），接口前缀 `/eportal/InterFace.do?method=` |
| 登录接口 | `POST /eportal/InterFace.do?method=login` |
| 在线检测 | 访问外网 HTTP 目标，被 302 到门户即为未认证 |
| 登录页获取 | 跟随该 302，页面隐藏字段含 RSA 公钥 |
| 密码明文 | `<密码> + ">" + <queryString 里的 mac>` |
| 加密流程 | 明文整体**反转** → **教科书 RSA**（无 PKCS#1 填充，16-bit 小端分块打包） |
| RSA 公钥 | 登录页 `publicKeyExponent` / `publicKeyModulus`，**每次登录动态读取** |
| 参数编码 | 每个字段 `encodeURIComponent` **两次** |
| 状态接口 | `getOnlineUserInfo` / `pageInfo` / `getServices` / `keepalive` |

### 为什么可信

RSA 加密是这套协议里唯一容易写错的地方（它没有用标准填充，且字节打包方式特殊）。
已用 **Node 执行门户官方 `security.js`** 与本地 Python 实现做逐字节对拍：

```
[1] 'Test@123456>7AB3405C36D0'            一致 ✔
[2] 'abc123>'                             一致 ✔
[3] 'a'                                   一致 ✔
[4] '1234567890>7AB3405C36D0'             一致 ✔
[5] 'P@ssw0rd!#$%^&*()>00-11-22-33-44-55' 一致 ✔
[6] 'x'*126  (刚好一块，不补零)             一致 ✔
[7] 'y'*127  (跨块)                        一致 ✔
```

（对拍时由 Node 载入门户的 `security.js`，两侧逐字节比对；该临时脚本未随仓库分发，
`probe/` 只保留抓包证据脚本。仓库内可随时用内置自检复核 RSA 正向加密：）

```
CampusNetAuth.exe test          # 或开发态：python campusnet.py test
```

自检会用一组固定 1024-bit 密钥跑 `eportal_rsa_encrypt`，并顺带检查门户连通性与在线状态。

---

## 即时响应机制（v0.7 起）

守护被设计为"事件驱动 + 秒级响应"。共三类机制协同工作：

### 1. 未认证当轮立即认证
`check_online()` 返回 `False`（被门户劫持）→ 守护循环**当轮零等待**调用 `login()`，不进入任何退避。这是基础闭环，**你打开校园网浏览器→下一秒就通**就是它在工作。

### 2. 状态变化的发现延迟（秒级）

| 场景 | 旧延迟 | 新机制 |
|---|---|---|
| 断网重连 / WiFi 切换 / 从校外回校 | ≤30s（轮询兜底） | **秒级** —— `NotifyAddrChange` 监听 IP/网络地址变化，独立线程收到通知立刻向守护发 `CHECK` 指令，`_wait()` 的 `select` 立即被打断，守护当轮复核 |
| 在线期被踢下线（IP 不变） | ≤30s | 不变 —— 由 30s 轮询 + `keepalive` 失败复核兜底（IP 不变 NotifyAddrChange 不触发），两者互补 |
| 登录后假成功（result=success 但会话未生效） | ≤30s | **5s 内复核** —— `verify_interval`（默认 5s）短等后立即 `check_online()`，未通则当轮补登，不污染 `login_fail` 统计 |
| UI 手动操作（登录/重启守护） | ≤30s | **秒级** —— UI 发 `CHECK` 唤醒正在等待的守护，立刻复核一轮 |

### 3. 状态事件即时反馈（前端"最近动态"卡片）

守护状态翻转（掉线→重连中 / 重连成功 / 恢复在线 / 登录成功 / 登录失败）会写入 `state.json` 的 `events` 环形缓冲（最近 8 条），同 kind+msg 在 60s 内自动去重防止失败刷屏。`/api/status` 透出，前端"后台守护"卡片下方会显示**"最近动态"**卡片，列出最近 3 条事件（iOS 色徽标 + HH:MM 时间戳）。事件未发生时卡片自动隐藏，零视觉干扰。

### 实现要点
- **`_start_addr_change_watcher()`**：ctypes 加载 `iphlpapi.NotifyAddrChange`，同步阻塞版由独立 daemon 线程循环调用；非 Windows / API 不可用静默降级返回 `None`；**节流 5s** 防 DHCP 续租事件风暴。
- **CHECK / STOP 协议**：`127.0.0.1:47667`（单例锁端口），守护用 `select` 监听 socket 数据（替代 `time.sleep`），`STOP`=优雅退出、`CHECK`=立即复核一轮。
- **`record_event()`**：去重规则 = 与最后一条同 kind+msg 且 60s 内仅更新时间戳；环形缓冲保留最近 8 条。

### 4. 自启链路容错与健康检查（v0.7 起）

- **vbs 启动器容错**：`CampusNetAuthDaemon.vbs` / `CampusNetAuthUI.vbs` 内置 `On Error Resume Next`
  + `FileExists` 前置检查——exe 缺失/改名时**不再弹原生错误框**（历史 80070002），改为在 exe
  目录写 `autostart.log` 后静默退出，UI 可在界面引导用户修复。
- **`autostart_health()` 健康检查**：解析 HKCU Run 键 → 提取引用的启动器（vbs/exe）→ 校验
  vbs 与同目录 `CampusNetAuth.exe` 是否齐全。`/api/status` 透出，前端在自启启用但链路残缺时
  显示红色警示条（如"开机自启将失败：启动器目录缺少 CampusNetAuth.exe"）。
- **安装前置校验**：`install_autostart()` 在写入 Run 键前校验 vbs/exe 就位，缺失直接拒绝，
  杜绝装出一个"开机必弹 80070002"的残缺自启。

### 5. 退出临时目录清理（PyInstaller _MEI 警告根治，v0.7 起）

现象：每次关闭程序后，Windows 弹出一个标题类似 `_MEI00059482` 的警告框
"**Failed to remove temporary directory**"。

- **根因**：单文件（onefile）exe 启动时把内置文件解包到 `%TEMP%\_MEIxxxx`。程序退出时
  PyInstaller bootloader（C 层，Python 代码无法覆盖）递归删除该目录；若其中文件仍被占用
  （本程序 WebView2 子进程 `msedgewebview2.exe` 退出滞后等），删除失败 → windowed 模式下
  弹框警告。
- **修复（双管齐下）**：
  1. **退出前等待 WebView2 子进程**（`desktop.py`）：窗口关闭后、`os._exit(0)` 前轮询
     `msedgewebview2.exe`，等命令行包含本程序 `.webview2-cache` 的进程全部退出再退出
     （只等自己的，不误等其它 WebView2 应用；wmic 不可用或超时则兜底退出，绝不阻塞）。
  2. **启动时清扫残留**（`campusnet.py`）：每次启动清扫 `%TEMP%\_MEI*` 中**超过 1 小时**的
     残留目录（崩溃/强杀留下的垃圾）。跳过当前进程自己的 `_MEIPASS`；用"改名探测"确认
     目录未被任何进程占用才删除，绝不误删运行中的实例。
- 验证：无头脚本实测 UI 冷启动/复用缓存两轮 + daemon 角色，退出均 rc=0、`%TEMP%` 无新建
  `_MEI*` 残留（= bootloader 清理成功，不弹警告）。

---

## 常见问题

| 现象 | 原因 / 处理 |
|---|---|
| 提示"尚未配置账号密码" | 先跑 `CampusNetAuth.exe setup` 或界面里填账号密码 |
| 登录失败：账号或密码错误 | 密码错了，重跑 `setup`。注意门户可能限制错误次数 |
| 登录失败：在线设备数超限 | 网页端先踢掉其它在线设备 |
| 登录失败：密码含中文 | 门户 JS 库对此处理有缺陷，建议改成纯 ASCII 密码 |
| 任务管理器里守护显示为 CampusNetAuth.exe | 属正常：守护本就是「同一 exe + `daemon` 参数」后台运行（不再复制 Daemon.exe 副本），与主界面同名，两个进程一界面一守护 |
| 一直"网络不可达" | 不在校园网环境（如家里 WiFi），属正常，程序只是等待 |
| 开机没自动登录 | 跑 `status` 看自启状态；或看 `campusnet.log` 末尾 |
| 开机/登录时弹"Windows Script Host"错误（80070002 找不到文件） | 自启 vbs 启动器找不到同目录的 `CampusNetAuth.exe`：vbs 与 exe 被分开存放、或 exe 被改名（如 `CampusNetAuth (1).exe`）。把 exe 与 `CampusNetAuthDaemon.vbs` 放同一文件夹即可。新版启动器已容错（失败写 `autostart.log` 不再弹窗），UI 也会显示红色警示条引导修复 |
| 界面出现红色"开机自启将失败"警示条 | 自启链路文件残缺（Run 键 → 启动器 → exe 某一环丢失）。按警示文字把 exe 与 vbs 放同一文件夹，或到「自动化 → 组件管理」卸载后重装「开机自启组件」 |
| 关闭程序后弹"Failed to remove temporary directory"警告框（标题 `_MEIxxxx`） | PyInstaller 单文件模式退出时清理 `%TEMP%\_MEIxxxx` 解包目录失败：WebView2 子进程退出滞后仍占用文件。新版已根治：退出前等待本程序的 WebView2 子进程结束，并在每次启动时自动清扫超 1 小时的 `_MEI*` 残留（见「即时响应机制 §5」） |
| 装开机自启报"拒绝访问" | 旧版用 `schtasks` 计划任务，受限环境下会被系统拒绝；新版已改用**注册表 Run 键**（当前用户级，无需管理员）+ VBS 静默启动，重新点一次"安装开机自启"即可 |
| 想换地方存放 | 整个文件夹一起移动（配置都在 exe 旁边），移动后重新跑 `install` 更新自启路径即可 |
| 双击 exe 闪了一下黑色 cmd 窗口 | 不应该发生。Subsystem=GUI（`--windowed`）打包 + `hide_console` 已消控制台；如仍闪，检查是否被 `cmd /c start` 或 bat 间接拉起 |
| UI 启动后看到 0.x 秒的灰矩形 | 不应发生。构造期 `withdraw()` 提前隐藏 + 16ms 步进 alpha 0→1 淡入；如仍出现，提交 `campusnet.log` + 系统 DPI |
| 启动后约 1 秒闪过一个 Windows Terminal 窗口再消失 | 不应该发生。根因：本应用是 GUI 子进程（`--windowed`），若在里面 `subprocess` 启动 CUI 程序（`netstat -ano` / `taskkill` / `schtasks`），Windows 必须为其分配控制台，默认终端为 Windows Terminal 时就会闪出终端窗口。新版已全部替换：端口查询走 IP Helper API（`GetExtendedTcpTable`，零子进程），终止进程走 `TerminateProcess`，剩余 CUI 调用都加了 `CREATE_NO_WINDOW`；HTML 版（pywebview）完全不启动任何 CUI 子进程。如仍闪，可能是个别地方遗漏，提交 `campusnet.log` |

---

## 构建（开发者）

双击 **`build_exe.bat`** 一键重建。会调用 `python -E -S _pyinst_wrap.py` 启动 PyInstaller，
产物在 `dist\CampusNetAuth.exe`（fresh build），随后 copy 到项目根（构建中间产物统一在
`build\`，输出统一在 `dist\`，`--clean` 防止缓存复用）。

`_pyinst_wrap.py` 的存在原因：WorkBuddy 的 sitecustomize shim 在 PyInstaller 的
`--clean` 阶段调用 `os.remove` 时被强制重定向到回收站 API（`SHFileOperationW`），
该 API 在沙箱里失败抛 `OSError`，导致 PyInstaller 静默退出（看似成功但 dist/ 为空）。
`-E -S` 跳过 site.py → sitecustomize 永不执行 → `os.remove` 保持原生实现；
wrapper 再手动把 venv 的 site-packages 加回 sys.path，PyInstaller 即可正常 import。

日志在 `campusnet.log`，排障先看它。

---

## 安全说明

- 密码用 Windows DPAPI（`CryptProtectData`）加密，绑定当前用户账户，文件被拷走也无法解密。
- 所有请求走校园网内网 HTTP，凭据只发给门户服务器 `172.16.54.18`。
- 不会在任何地方打印明文密码；日志里的账号默认做脱敏。
- 程序只做"自己的账号自动登录"，不做任何绕过计费或共享账号的行为。

---

## 目录结构

```
campus-net-auth/
├─ CampusNetAuth.exe      主程序（单 exe，双击=界面 / daemon 参数=守护 / CLI 子命令）
├─ CampusNetAuthUI.vbs    静默启动界面脚本（无黑窗，UI「界面启动器组件」释放）
├─ CampusNetAuthDaemon.vbs 守护静默启动器（无黑窗，UI「值守/自启组件」释放）
├─ campusnet.py           主程序源码（开发态用，零依赖）
├─ desktop.py             HTML 界面桌面壳（pywebview + Edge WebView2，优先启动）
├─ gui_server.py          HTML 界面后端（零第三方依赖 HTTP 服务 + 内嵌前端）
├─ ui.py                  tkinter 界面源码（HTML 界面不可用时的回退，零依赖）
├─ e2e_test.py            端到端测试脚本
├─ config.json            配置（不含密码）
├─ cred.bin               DPAPI 加密的密码（自动生成）
├─ state.json             最近一次登录的会话 ID（自动生成）
├─ daemon.pid             守护进程 PID（自动生成，供界面停止守护）
├─ campusnet.log          日志（自动生成）
├─ 管理界面.bat           双击：打开图形管理界面（优先 exe，回退源码）
├─ 一键测试.bat           双击：配置 + 启动守护 + 下线 + 验证自动重连
├─ 安装.bat               双击：配置 + 装开机自启
├─ 卸载自启.bat           双击：移除自启
├─ 查看状态.bat           双击：看状态 + 最近日志
├─ build_exe.bat          一键重新打包（--onefile --windowed，产物复制到根目录）
├─ dist/ build/ *.spec    打包产物与中间文件（可删，重新打包会再生成）
└─ probe/                 逆向抓包证据脚本 `capture_probe.py`（可删）
```

---

## 关于中文编码（重要，改 `.bat` 前必读）

所有中文文案都**只放在 Python 文件里**（UTF-8），**不要往 `.bat` 里写中文**。

原因：`cmd.exe` 切换代码页后，会按「字符偏移」重新定位批处理文件指针，
而文件实际是「字节偏移」。含中文的后续行会被错位读取甚至整行跳过——
实测表现为输出为空、或把 `chcp 65001` 读成 `ho`。

正确做法：

- `.bat` 保持**纯 ASCII**（现有文件已如此），中文一律交给 Python 打印。
- 双击 `CampusNetAuth.exe`（打包版为 **--windowed / GUI 子系统**）→ Windows 根本不创建控制台，
  **零黑窗**；CLI 命令（`status`/`log`/`test` 等）从 cmd 运行时通过 `AttachConsole`
  附加到父控制台输出，并按父控制台代码页匹配编码写入（936→GBK、65001→UTF-8），
  因此无论双击 `.bat` 还是直接在命令行运行，中文都能正常显示。
- 日志文件按 **UTF-8** 写入；查看最近日志一律用 `python campusnet.py log`
  （内部显式按 UTF-8 读取）。
  **不要**用 `type campusnet.log` 或 PowerShell `Get-Content` 直接读日志——
  它们会按系统 ANSI(GBK) 解码，把中文显示成乱码（如 `妫€娴嬪埌鏈吇璇`）。
