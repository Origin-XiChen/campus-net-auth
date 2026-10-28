# AGENTS.md — 接管本项目的 AI 必读（最高优先级）

> **地位**：本文件是本仓库的**最高优先级**工程指令。任何 AI（新会话、继任模型、协作者）
> 在修改本仓库前**必须完整读完本文件与 [README.md](README.md)**，并按下文流程执行。
> 与其它文档冲突时以本文件为准；**用户的当面指令高于本文件**。
>
> 本文件对外公开，**不要在这里写个人路径、账号、学号、MAC、凭据或本机内部笔记**。

---

## 0. 接手第一步（30 秒，必做）

不要相信任何文档或记忆里的"已推送 / 未发布 / 待合并"结论，**先核实真实状态**：

```bash
git status -sb                 # 工作区是否干净、与远端是否一致
git log --oneline -8           # 最近提交
gh release list                # 最新 Release（gh 可能不在 PATH，见 §4）
python -m unittest discover -s tests   # 全量离线单测（当前 200 项，应全绿）
```

任何一项不符合预期（有未提交改动、单测红、版本对不上），先查清原因再动手。

---

## 1. 这是什么

锐捷 **ePortal / SAM+** 校园网（iHBUT）无感认证工具。用户装一个 exe，程序自动检测
"是否在校园网 + 是否已认证"，未认证时自动提交账号密码完成认证；开机自启、断线自愈。

- **交付形态**：单文件 `CampusNetAuth.exe`（PyInstaller `--onefile --windowed`），
  绿色部署，配置/凭据/状态都在 exe 同目录。
- **运行时零第三方依赖**（仅标准库）；`pywebview` 只用于构建 GUI 外壳。
- **仅 Windows**（DPAPI、winreg、win32 API）。

## 2. 架构与边界

| 文件 | 行数（0.6.1） | 职责 | 改它要注意 |
|---|---|---|---|
| `campusnet.py` | ~4400 | 协议/加密/DPAPI/HTTP/守护循环/自启/升级/更新检查/CLI | 核心。改登录或判定逻辑前先读对应段落与单测 |
| `gui_server.py` | ~1870 | 本地 HTTP 后端 + **内嵌 HTML/CSS/JS 前端**（约 900 行） | 改前端后必须跑 `check_ui.py` |
| `desktop.py` | ~630 | pywebview + Edge WebView2 桌面壳（优先启动） | 失败自动回退 tkinter |
| `ui.py` | ~2070 | tkinter 回退界面 | 功能对齐 `gui_server`，两边都要改 |
| `diagnose_probe.py` | ~425 | 抓取诊断（排障用，只读） | 不打凭据 |
| `smoke_regression.py` | ~656 | 端到端冒烟（UI/守护/自启/组件/退出清理） | 需要先构建出 `dist\CampusNetAuth.exe` |
| `tests/` | — | 离线单测（不联网、不碰真注册表/真实 state） | 见 §5 测试约定 |
| `build_exe.bat` / `_pyinst_wrap.py` | — | 构建（见 §4） | 纯 ASCII，勿写中文 |
| `check_ui.py` | ~125 | 内嵌前端静态校验（JS 语法 / 标签 / id 引用） | 改前端后必跑 |

运行时要点（0.6.x）：

- **守护不是独立程序**：`CampusNetAuth.exe daemon` 的子进程；控制通道是
  `127.0.0.1:47667` 上的 `STOP` / `CHECK` / `VER` 指令。
- **自启** = HKCU `...\CurrentVersion\Run` 值 `CampusNetAuth` = `"<exe>" daemon`
  （0.6.0 起不再经 VBS）。
- **组件**：0.6.0 起自启/守护/界面合并为同一个 exe；`CampusNetAuthDaemon.vbs` /
  `CampusNetAuthUI.vbs` 仅作历史残留清理，**不要恢复成主路径**。
- **版本单一来源**：`campusnet.py` 顶部 `APP_VERSION`；exe 版本资源由
  `_pyinst_wrap.py` 从它生成。发布说明、`--version`、升级判定都以它为准。

## 3. 安全红线（不可违反）

1. **凭据只发门户**：只发给 `config.json` 的 `portal_host`（校园内网 IP），请求必须同主机同端口。
2. **绝不明文存/打印密码**：DPAPI 加密存 `cred.bin`；日志、异常、UI 均不得出现明文密码。
3. **公钥 pinning（TOFU）语义不得削弱**：首次成功登录记录指纹；指纹变化、或已 pin 但门户
   突然不加密，都要**拒绝发送凭据**。要改必须先补单测。
4. **不执行来路不明的 exe**：读候选版本用 `pe_version()`（Win32 版本 API 静态读取）。
5. **提交前先看 `git status`**：`config.json`、`cred.bin`、`state.json`、`campusnet.log`、
   `HANDOVER.md`、`_build_local.bat`、构建产物均在 `.gitignore` 里，**绝不提交**。
6. **不把任意 URL 交给浏览器**：Release 链接只允许本仓库 releases 前缀。
7. **不擅自做破坏性验证**：冒烟会临时停掉用户真实守护（结束自动恢复）；「一键体检」会真实
   断网约 75 秒——**必须先征得用户同意**。
8. 公开评论/文档不写个人路径、账号、学号、MAC、token。

## 4. 环境与工具链陷阱（都踩过）

- **仓库目录里的 exe 可能无法直接运行**：在受限沙箱（WorkBuddy 等）中，工作区内构建的
  exe 会被拦截 `%TEMP%` 解包，弹 `Could not create temporary directory!`。验证时把 exe
  **复制到工作区外**运行，或直接跑冒烟（它在 `%TEMP%` 解包）。
- **`.bat` 必须纯 ASCII**：cmd.exe 切换代码页后按字符偏移读批处理，含中文会错位。中文文案
  只放 Python 文件（UTF-8）。
- **日志是 UTF-8**：不要用 `type` / `Get-Content` 读（会按 GBK 解成乱码），用
  `CampusNetAuth.exe log`。
- **子进程输出编码**：捕获子进程输出必须 `PYTHONIOENCODING=utf-8` + GBK 兜底
  （见 `_task_self_test`），否则中文乱码。
- **CLI `print` 字面量必须 GBK 可编码**：`✓/✗` 这类字符会让 GBK 控制台抛
  `UnicodeEncodeError`（有单测 `TestCliOutputEncoding` 兜底）。
- **构建要用 `_pyinst_wrap.py`**：开发沙箱的 `sitecustomize` 把 `os.remove/rmdir` 重定向到
  回收站 API，会让 PyInstaller `--clean` 静默失败。`build_exe.bat` 已用
  `python -E -S _pyinst_wrap.py` 绕过，并支持 `CNA_BUILD_PYTHON` / `CNA_PY_SITE` 覆盖。
- **`gh` 可能不在 PATH**：先 `command -v gh`；找不到时检查常见安装位置（如
  `~/.workbuddy/binaries/gh/bin/gh.exe`），**不要**因此改用裸 API + token。
- **Python 版本**：构建环境为 3.14；代码须保持 3.9+ 语法（不要用 3.10+ 的 match 等）。

## 5. 改动与验收清单（每次提交前）

| 改了什么 | 必须执行 |
|---|---|
| 任意代码 | `python -m unittest discover -s tests`（全绿） |
| 任意 `.py` | `python -m py_compile campusnet.py gui_server.py desktop.py ui.py diagnose_probe.py smoke_regression.py smoke_state_write.py smoke_uninstall.py e2e_test.py` |
| `gui_server.py` 内嵌前端 | `python check_ui.py`（JS 语法 / 标签配对 / id 引用） |
| 协议 / 登录 / 发现 / 判定 | 新增或更新单测；必要时真机**只读**探测（绝不发凭据） |
| 打包 / 版本 / 自启 / 升级 / 守护 | 重建 exe + `python smoke_regression.py`（全 PASS） |

测试约定：

- **离线**：不联网、不碰真实注册表（用内存版 `winreg`）、不写真实 `state.json`
  （用 `tests/test_core.py` 的 `_StateDirMixin`）。
- 测试用例写在对应主题的文件里（`test_core` / `test_login_page_fallback` /
  `test_portal_discovery` / `test_portal_recovery` / `test_runtime_hygiene` /
  `test_update_check`），不要全堆进 `test_core.py`。
- 涉及时间/网络/进程的用例，优先注入依赖（例如 `DaemonReplay`）而不是 sleep。

代码风格：注释写"为什么"（中文），不写复述代码的废话；不引入新依赖；不加没要求的抽象。

## 6. 发布 Release 流程

1. 改 `campusnet.py` 的 `APP_VERSION`（含上方的版本说明注释，简述本版内容）。
2. 重建：Windows 下双击 `build_exe.bat`（本机可用 `_build_local.bat` 指定 Python）；
   产物 `dist\CampusNetAuth.exe`（fresh build），同时复制到仓库根作为开发副本。
3. 校验：PE 版本资源与 `--version` 都应为新版本；跑 §5 的全套验收。
4. 提交（中文 Conventional Commits：`feat:` / `fix:` / `refactor:` / `test:` / `docs:`，
   标题带版本号，正文写清"为什么 + 做了什么 + 怎么验证"）→ `git push origin main`。
5. 打标签：`git tag -a V<版本> -m "..."` → `git push origin V<版本>`。
6. 发布：
   ```bash
   gh release create V<版本> --repo Origin-XiChen/campus-net-auth \
     --title "CampusNetAuth V<版本>" --notes-file <说明.md> dist/CampusNetAuth.exe
   ```
   说明文档写：背景 / 修复或新增（表格）/ 行为变化与风险 / 验证 / 升级方式 / 变更对比链接。
7. 发布后：更新 §9 版本进度表；把 Release 链接补到相关 PR / issue 评论（见 §7）。
8. tag 命名沿用 `V0.6.1`（大写 V，三段版本号）；Release 标题 `CampusNetAuth V0.6.1`。

## 7. 对外沟通规范（GitHub）

- **合并 PR / 回复 issue**：正文第一行必须是
  `【DeepSeek经过审核后发布以下内容】`，以 DeepSeek 的口径写（本项目 README 已公开
  "代码由 AI 编写、参与 AI 含 DeepSeek"）。
- 合并 PR 的提交标题：`Merge PR #N: <PR 标题>`；正文写清冲突解决与验证结果。
  PR 与 main 冲突（常见于版本号/测试数据）时：**本地解冲突后推送**，GitHub 会自动标记 MERGED。
- **审查 PR 的标准动作**：读完整 diff → 在 PR 分支跑单测 / 编译 / `check_ui` → 关键路径
  真机只读验证 → 写结论（区分"必须修"与"小瑕疵"）→ 合并后补一个跟进提交修掉发现的问题。
- 发布 Release 后把链接补进相关 PR/issue；不要跨 issue 刷屏。
- 评论里不贴日志中的凭据、账号、MAC、个人路径。

## 8. 常见任务速查

```bash
# 配置 / 登录 / 状态 / 日志
CampusNetAuth.exe setup | login | logout | status | log
CampusNetAuth.exe once          # 单次检测（尊重"手动下线"静默期）
CampusNetAuth.exe daemon        # 前台跑守护（通常由自启/界面拉起）

# 维护
CampusNetAuth.exe upgrade --check     # 守护版本 + 本地升级候选 + 上次远端检查结论
CampusNetAuth.exe upgrade             # 就地升级（停守护→换文件→重启守护）
CampusNetAuth.exe update [--force] [--open]   # 查 GitHub 最新 Release（只检查）
CampusNetAuth.exe uninstaller         # 彻底卸载（独立 exe，界面里也可触发）
CampusNetAuth.exe test                # 自检（离线协议自测）
```

关键配置项（`config.json`，全部有默认值）：`portal_host/portal_port`、`username`、
`service`（default/DX/LT/YD/auto）、`detect_targets`、`login_page_hints`（网关不劫持时的
登录页兜底参数）、`pin_pubkey`、`logout_pause`（手动下线静默期，默认 600s，0=关闭）。

## 9. 版本进度（按版本汇报最新进度）

> **规则**：每发一次 Release 必须在表里补一行；未发布的 main 改动写在"当前开发线"。
> 版本号以 `campusnet.py` 的 `APP_VERSION` 为准，本表只做摘要。

**当前开发线**：`main`，`APP_VERSION = 0.6.1`，**与最新 Release V0.6.1 一致（无未发布的功能改动）**。
具体提交以 `git log` / `git status -sb` 为准——本节不钉死提交号，避免文档一提交就过期。

| 版本 | 日期 | 状态 | 要点 |
|---|---|---|---|
| V0.1 | 2026-09-01 | 已发布 | 首个公开版本：ePortal 认证、守护自动重连、单 exe 交付；MIT → GPL-3.0、SECURITY.md |
| V0.2 | 2026-09-05 | 已发布 | P0/P1 安全健壮性（本地 API 鉴权、单响应、pinning、原子写）+ 单测与 CI；P2 清理与版本体系；state 并发锁 |
| V0.3 | 2026-09-06 | 已发布 | 根治退出时 "Failed to remove temporary directory"（`_MEIPASS2` 继承） |
| V0.4 | 2026-09-11 | 已发布 | 门户残留会话自愈；系统通知与通知自检（三条通道） |
| V0.5 | 2026-09-11 | 已发布 | 守护版本握手 + 陈旧守护自动重启 + 一键就地升级 |
| V0.5.1 | 2026-09-25 | 已发布 | 自启 Run 键 `//B` 静默化 + 目录搬走后自启自愈（当时的 VBS 方案） |
| V0.5.2 | 2026-10-15 | 已发布 | 内置彻底卸载器（开主程序自动释放）+ 守护停机/进程归属硬化；CI 中文输出 UTF-8；`.bat` 纯 ASCII |
| V0.5.3 | 2026-10-19 | 已发布 | 公网探测无结论时的认证阻塞与退避修复（PR #3）+ 登录页发现优先重定向（PR #2）+ 状态文案不再误报 |
| V0.5.4 | 2026-10-22 | 已发布 | 网关不劫持时的登录页兜底发现（PR #5）+ 排除管理台页面 + 加密参数对齐 + `upgrade` GBK 修复 |
| V0.6.0 | 2026-10-24 | 已发布 | 自启/守护/界面合并为同一 exe（不再释放 VBS 组件）+ GitHub Release 只检查更新 |
| V0.6.1 | 2026-10-27 | 已发布（Latest） | 检查更新补强（结果落盘 / 在线自动补查 / 界面与 CLI 透出）+ PR #6 八项运行时修复（下线静默期、验证码拦截、累计计数落盘、按网卡取 MAC 等）+ 审查跟进 |

## 10. 已知残留与下一版候选

- **P2 大件未做**：`campusnet.py` 拆包（portal/crypt/cred/daemon/autostart/cli）、
  删除 `ui.py`（约 2000 行）改浏览器回退、6 个中文管理 `.bat` 的去留。
- **检查更新无资产完整性校验**：只靠 GitHub HTTPS + PE 版本 + ProductName；可考虑
  Release 附 `SHA256SUMS` 并在 `apply_upgrade` 校验（需改发布流程）。
- **自启悬空行为待复核**：0.6.0 起 Run 键直接指向 exe（不再经 `wscript //B`），
  "用户绿色删除整个目录后开机是否弹错"需要一次真机复核。
- **`once` 是任务计划场景**：已改为尊重手动下线静默期；若未来加定时任务要沿用该语义。
- **坑 5.8（`SINGLETON_PORT` 按用户派生）**：已评估**不做**（防不了同用户抢占、迁移成本高），
  保留为已知残余风险。
- **校外手动登录**会多发一次带超时的兜底请求（守护路径不受影响），属可接受行为。
- 检查更新在"当前版本高于线上"时显示"已是最新"，语义可接受。

## 11. 维护本文件

- 改动**规则、流程、红线、陷阱**时，**同一次提交**更新本文件。
- 每次发布 Release：更新 §9 表格与"当前开发线"。
- 本文件与 README 的分工：README 面向用户（功能、配置、FAQ、升级），本文件面向 AI 与维护者
  （流程、验收、红线、版本进度）。两边冲突时以本文件为准，并顺手修掉 README 的过期口径。
