# 网站签到助手

基于 **Edge 浏览器自动化（Playwright/CDP）+ AI 视觉识别** 的网站每日签到工具。
支持 2FA 动态验证码、CookieCloud 同步、AI 识别图形/滑块/CF 验证码，任务可插拔扩展，可打包为免环境 exe。

## 功能特性

| 功能 | 说明 |
|------|------|
| 浏览器控制 | 程序启动 Edge（带远程调试端口 9222，可 `edge://inspect` 挂接调试）或连接已运行的 Edge；支持点击、填表、截取页面指定位置图片 |
| 2FA（TOTP） | 导入谷歌身份验证器二维码（`otpauth://` 与 `otpauth-migration://` 导出格式均支持）；**任务级勾选是否有 2FA，并可指定绑定凭证库中的哪个验证器** |
| AI 能力（用户配置） | OpenAI 兼容接口；验证码识别、滑块缺口定位、CF 挑战主动点击；事件驱动「主动触发」 |
| CookieCloud | 输入服务器/UUID/密钥，本地 AES 解密同步 cookie；工作模式：**覆盖到浏览器 / 暂停同步**，默认同步全部站点；**在打开站点之前就把 cookie 注入浏览器** |
| 代理 | 全局设置代理地址（http/socks5 + 可选账号密码）；任务配置里勾选「本任务走代理」才使用，未勾选的任务强制直连 |
| 签到任务 | 任务基类 + 注册表 + YAML 配置；任务可单独勾选是否有 2FA / CF 验证 / 走代理。内置 **PT-空（site-a.example）**、**HDDolby（site-b.example）**、**TTG 听听歌（site-c.example）**、**馒头 M-Team（site-d.example）**、**人人 Audiences（site-e.example）**、**Akile（site-f.example，控制台「每日签到」）** |
| 任务队列 | 严格串行：一个任务跑完再跑下一个；队列全部结束后才关闭浏览器（任务之间浏览器保持运行）
| 运行日志 | 每次运行单独写入 `data/logs/run_日期_时间.log`；运行结束自动关闭浏览器 |
| 设置持久化 | 所有设置保存到本地 YAML，启动自动载入；图形界面内即可编辑保存 |
| GUI | Tkinter：任务多选框、任务配置对话框、全局设置对话框、实时日志 |
| exe 分发 | PyInstaller 单文件打包，目标电脑无需 Python 环境（需系统自带 Edge） |

## 两种使用方式

### 方式一：直接运行 exe（无需任何环境）

1. 双击 `签到助手.exe`（在 `dist/` 或 `release/` 目录）
2. 首次运行会在 exe 同目录释放 `config/` 配置目录
3. 在图形界面中完成设置与任务配置后，勾选任务点「▶ 运行选中」

> 目标电脑要求：Windows 10/11（自带 Microsoft Edge）。exe 已内置 Python 运行时、
> playwright 驱动、OpenCV、tkinter 等全部依赖。
> **分发给他人时只发 `release/` 里的 exe**，不要把 `dist/` 整个目录发出去
> （那里有你本地的账号密码与 2FA 密钥）。详见「打包 exe → 分发时的重要提醒」。

### 方式二：源码运行

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python main.py            # 不带参数 = 启动图形界面
python main.py run pt_kong  # 命令行运行任务
```

## 图形界面

- **任务列表**：每行一个任务，勾选框决定本次运行哪些；点「配置」打开任务配置对话框
  - 任务名后括号里显示**今天**是否已签到：`任务名称(已签到)` / `人人(未签到)`
    —— 昨天的签到结果今天会自动变回「未签到」；记录行用「今天: …」/「上次: …」区分
  - 结果记录在 `data/task_state.json`，每次运行后自动更新；跨天时界面自动刷新
- **任务配置**（保存到 `config/tasks/*.yaml`，底部固定「保存配置」按钮）：
  - 账号 / 密码（CookieCloud 失效时兜底登录）
  - ☑ **本任务有 2FA** → 可下拉选择绑定 2FA 凭证库中的哪个账号，或直接填 secret
  - ☑ **本任务有 Cloudflare / 人机验证** → 勾选后才处理挑战，未勾选完全不处理
  - ☑ **本任务走代理** → 使用「全局设置 → 代理」的地址；未勾选则本任务强制直连
  - 登录失败次数上限：**跨天累计**，只有登录成功才清零；用尽后该任务不再执行
    （界面显示「登录已锁定 x/y」，可点「重置登录失败次数」手动解锁）
- **全局设置**（保存到 `config/config.yaml`）：AI 接口、CookieCloud（含工作模式）、代理、
  浏览器模式/端口/无头、**启动后全屏**、**窗口置顶**
- **导入2FA二维码**：选择二维码图片 → 解析入凭证库 → 在任务配置里选择绑定
- **打开日志目录**：查看每次运行的独立日志

## 配置文件

所有设置也可直接编辑本地 YAML（图形界面保存的就是这些文件，启动自动载入）。

`config/config.yaml` 全局设置：

```yaml
ai:
  enabled: true
  base_url: "https://api.openai.com/v1"   # 任意 OpenAI 兼容接口
  api_key: "sk-..."
  model: "gpt-4o-mini"                    # 验证码识别需支持视觉的模型

cookiecloud:
  enabled: true
  mode: overwrite             # overwrite=覆盖到浏览器(打开站点前注入) / paused=暂停同步
  server: "http://127.0.0.1:8088"
  uuid: "..."                 # 扩展生成的 UUID
  password: "..."             # 同步密钥（仅本地加解密用）
  domains: []                 # 留空 = 同步所有站点

proxy:
  enabled: true
  server: "http://127.0.0.1:7890"    # 或 socks5://127.0.0.1:1080
  username: ""                        # 可选
  password: ""                        # 可选
```

代理生效规则：只有任务配置里勾选了「本任务走代理」的任务才走该代理；未勾选的任务以
`--no-proxy-server` 启动强制直连（不走此代理也不走系统代理）；全局代理未启用时所有任务跟随系统设置。

`config/tasks/*.yaml` 任务设置（PT-空 示例）：

```yaml
task:
  id: pt_kong
  name: "PT-空"
  enabled: true
  url: "https://site-a.example/index.php"
account:
  username: ""
  password: ""
  two_step:
    enabled: false              # 是否启用 2FA
    secret: ""                  # 直接填 base32 密钥（可选）
    vault_key: ""               # 绑定凭证库账号，格式 "issuer|name"
options:
  has_2fa: false                # 本任务有 2FA（勾选）
  has_cf: false                 # 本任务有 CF 人机验证（勾选，未勾选则不处理）
  use_proxy: false              # 本任务走代理（勾选，未勾选强制直连）
  max_login_retry: 3
```

`config/twofa.yaml` 2FA 凭证库：**默认为空**，必须自行导入二维码后才有内容。

## 目录结构

```
签到助手/
├── main.py                  # 入口（无参数=GUI，带子命令=CLI）
├── 签到助手.spec            # PyInstaller 打包配置
├── requirements.txt
├── config/
│   ├── config.yaml          # 全局配置（浏览器/AI/CookieCloud/日志）
│   ├── twofa.yaml           # 2FA 凭证库（二维码导入的账号）
│   └── tasks/
│       └── pt_kong.yaml     # PT-空 任务配置
├── core/
│   ├── browser.py           # Edge 控制器（launch/attach CDP、点击、截图、反检测、生命周期）
│   ├── ai.py                # AI 客户端（OpenAI 兼容：文本/视觉/JSON）
│   ├── captcha.py           # 验证码识别（页面元素 → AI → 文本）
│   ├── slider.py            # 滑块验证码（AI 定位缺口 + 拟人拖动）
│   ├── cf.py                # CF/人机挑战（每秒实时探测点击/滑动控件，元素定位优先，AI 兜底）
│   ├── desktop.py           # 桌面控制（Win32 屏幕截图 + 鼠标/键盘原生输入，识图点击用）
│   ├── desktop_agent.py     # 识图点击 Agent（AI 看屏幕截图，决定点哪里、填什么）
│   ├── browser_tools.py     # 浏览器工具箱（MCP 与 AI 托管共用的一套工具定义）
│   ├── mcp_server.py        # 浏览器 MCP 服务端（stdio JSON-RPC，供外部 AI 接管浏览器）
│   ├── browser_agent.py     # AI 托管（CF 过不去时由视觉模型看图驱动浏览器）
│   ├── cookiecloud.py       # CookieCloud 拉取 + 解密（fixed/legacy 双格式）
│   ├── totp.py              # TOTP + 二维码解析（含 otpauth-migration protobuf）
│   ├── twofa_store.py       # 2FA 凭证库（多账号、按任务绑定）
│   ├── events.py            # 事件总线
│   ├── state.py             # 任务状态（今日是否已签到 + 登录失败次数，跨天累计）
│   ├── config.py            # 配置加载/保存（frozen 时以 exe 目录为根）
│   └── logger.py            # 日志（控制台 + 全局 + 每次运行独立文件）
├── tasks/
│   ├── base.py              # BaseTask 模板方法 + TaskContext + 注册表
│   ├── nexusphp.py          # NexusPHP 站点通用基类（登录/2FA/CF/CookieCloud/签到）
│   ├── manager.py           # 任务发现/组装/串行执行/收尾（日志落盘 + 关浏览器）
│   ├── pt_kong.py           # PT-空（HDSky）任务实现
│   ├── hddolby.py           # HDDolby 任务实现（继承 NexusPHP 基类）
│   ├── ttg.py               # TTG 听听歌任务实现（多语言表单 + JS 签到）
│   ├── mteam.py             # 馒头 M-Team 任务实现（React 单页应用 + 2FA + 保活）
│   ├── audiences.py         # 人人 Audiences 任务（默认桌面模式：正常浏览器 + 识图点击；可退自动化）
│   └── akile.py             # Akile 任务（Vue 单页应用：登录 + 验证器 2FA + 控制台「每日签到」）
├── ui/gui.py                # Tkinter 界面（主窗口 + 任务配置 + 全局设置）
└── data/                    # 运行时生成：Edge profile、截图、日志
```

## 命令行

```bash
python main.py list                    # 列出任务（启用/账号/2FA/CF 状态）
python main.py run pt_kong             # 运行 PT-空
python main.py run --all               # 运行所有启用任务
python main.py import-qr <二维码.png>  # 导入谷歌验证器二维码
python main.py import-qr <二维码.png> --task pt_kong   # 导入并绑定到任务
python main.py test-ai                 # 测试 AI
python main.py test-cookiecloud        # 测试 CookieCloud
python main.py show-config             # 查看配置
python main.py gui                     # 启动图形界面
```

## 运行日志与浏览器关闭

- 每次运行开始创建 `data/logs/run_YYYYMMDD_HHMMSS.log`，完整记录：
  浏览器启动 → 页面加载 → 登录页检测 → CookieCloud 同步 → 登录尝试 → 验证码识别 → 签到 → 浏览器关闭
- 运行结束（无论成功/失败）自动关闭浏览器，并在控制台/GUI 状态栏输出本次日志路径
- 全局滚动日志仍在 `data/logs/app.log`

## PT-空 签到流程

1. **打开站点之前**：从 CookieCloud 拉取本站 cookie 注入浏览器上下文（先同步、后打开）
2. 打开 `https://site-a.example/index.php`（仅当任务勾选「有 CF 验证」时才处理人机挑战）
3. 登录态判定（以页面真实标志为准，不看 URL）：
   - 页面上出现 `logout.php` 链接 / `#showup` 签到入口 / `userdetails.php` 链接 → 判定已登录，直接进入签到
   - 页面出现登录表单 → 再兜底同步一次 cookie 并刷新；仍未登录才用**账号密码**登录：
     - 勾选「有 2FA」则自动填入 TOTP 动态验证码（剩余不足 5 秒时自动等下一周期）
     - **图形验证码交给 AI 识别**（`imagestring` + `imagehash`）
     - 失败自动重新打开站点重试（默认 3 次，HDSky 连续失败会禁 IP）
   - 既无登录标志也无表单 → 等跳转完成后复核，仍无法识别则重新打开站点
4. 点击导航栏 **签到**（`#showup`）→ 弹出验证码层
5. AI 识别弹窗验证码（`#showupimg`）→ 填入 `#imagestring` → 点击 `#showupbutton`
6. 轮询弹窗文本判定结果：签到成功 / 今日已签 / 失败

## HDDolby 签到流程

站点 `https://site-b.example/index.php`，NexusPHP，登录需 2FA，且有 Cloudflare 人机验证。

1. 打开前注入 CookieCloud cookie → 打开 `index.php`
2. 遇到 Cloudflare 挑战时（任务已勾选「有 CF 验证」）进入**实时处理**模式：每秒探测一次页面，
   出现可点击复选框就立刻点、出现滑块就立刻拖，详见「人机验证实时处理」一节
3. 登录态判定 → 需要登录时填 用户名 / 密码 / **两步验证码（字段名 `2fa`）** / 图形验证码 → 提交
4. 进入 `attendance.php`（签到得鲸币）：
   - 页面显示"已签到"类文本 → 今日已签到
   - 否则探测签到提交按钮（`input[type=submit][value*=签到]`、`button:has-text('签到')` 等）→ 若有图形验证码交给 AI → 点击提交
   - 结果按关键词判定；无法确认时返回"结果未识别"并保留截图，**不会猜测成功**
5. 页面结构若与预期不同，可在任务配置里指定 `checkin_btn` / `checkin_url` 选择器，无需改代码

> Cloudflare 托管挑战对自动化浏览器校验较严时可能一直无法自动通过。此时可用 `attach` 模式
> 人工完成一次验证：`cf_clearance` 会保存在 `data/edge_profile` 中，后续运行可复用。

## TTG（听听歌）签到流程

站点 `https://site-c.example/index.php`，NexusPHP 定制皮肤。

1. 打开前注入 CookieCloud cookie → 打开 `index.php`
2. 若有 Cloudflare 挑战（任务已勾选「有 CF 验证」）进入实时处理：每秒探测，出现可点击/可滑动控件立刻处理
3. 登录页特征及处理：
   - 页面有 **5 个语言版本**的登录表单，只有 `.login-screen.active` 那个可见，
     选择器统一加 `.login-screen.active` 限定，避免命中隐藏表单（真实站点实测：全部表单 5 个，活动表单 1 个）
   - 字段只有 用户名 / 密码（+ 默认隐藏的安全问题答案），**没有图形验证码、没有 2FA 输入框**
4. 登录态判定：`#signed`、`#sp_signed`、`logout.php`、`userdetails.php`
5. 签到：点击导航栏 `<a id="signed">签到</a>`
   - 点击前注册弹窗处理，alert 内容会被记录并参与判定
   - 结果支持两种呈现：DOM 更新（入口变“已签到”）与 alert 弹窗（如“签到成功，获得 X 积分”）
   - 点击前已确认入口存在、点击后入口消失，视为签到完成（会注明“未取到文字提示”）
6. 该站点连续 6 次登录失败会封 IP，`max_login_retry` 保持 3 以内

## 馒头（M-Team）保活流程

站点 `https://site-d.example/index`，React 单页应用（antd），无传统签到按钮，任务目标是**打开首页并保持 10 秒**。

1. 打开前注入 CookieCloud cookie → 打开 `/index`
2. 等 3.5 秒让单页应用渲染完成，然后判定：
   - 被重定向到 `/login` 且出现 `#username` 表单 → 视为未登录，执行登录
   - 页面出现两步验证输入框 → 直接提交动态验证码
   - URL 不含 `/login`、无登录表单、有布局元素（`header` / `.ant-menu`）→ 已登录
3. 登录流程：`#username` + `#password` → 点主按钮 → 若进入两步验证页则填 6 位验证码 → 提交
   - 2FA 输入框用多候选探测（`input[id*='code']`、`input[placeholder*='驗證']`、`input[maxlength='6']` 等）
   - 可在任务配置用 `sel_2fa_input` / `sel_2fa_submit` 指定选择器
   - 2FA 密钥留空时按站点域名自动匹配凭证库（`M-Team - TP` 那个账号）
4. 登录完成后在首页保持 `keep_seconds` 秒（默认 10 秒），保持期间若跳回登录页则判定失败
5. 该站是 SPA，判定时会先等渲染，避免把加载中的空白页当成登录成功

## 人人（Audiences）签到流程

站点 `https://site-e.example/index.php`，NexusPHP（BlasphemyOrange 皮肤），有 Cloudflare 保护。

**该任务默认走「桌面模式」**（`options.desktop_mode: true`），与其它任务不同：

### 桌面模式（默认）：正常浏览器 + 识图点击

Cloudflare 能识别 Playwright/CDP 驱动的自动化浏览器，对人机验证卡得特别死。
所以人人任务改成用**你平时那个正常浏览器**打开站点，再由 AI **看屏幕截图识图点击**：

1. 用系统默认浏览器打开 `https://site-e.example/index.php`（不带任何自动化标志 / 调试端口）
2. AI 看屏幕截图 → 返回动作 JSON（`click` / `type` / `drag` / `scroll` / `wait` / `finish`）
3. 桌面控制器用 OS 原生输入执行（`SetCursorPos` / `mouse_event` / `keybd_event`）
4. 重复直到看到「签到成功 / 今日已签到」或步数用尽

这样 CF 看到的是"真人看着屏幕点鼠标"，无从识别自动化。

**实现**：
- `core/desktop.py`：Win32 屏幕截图（GDI BitBlt → numpy → cv2 PNG）+ 鼠标/键盘原生输入，
  **零新依赖**（cv2/numpy 项目里本来就有）
- `core/desktop_agent.py`：识图点击 Agent（ReAct 循环，带历史避免重复无效动作）
- 坐标约定：截图是屏幕物理像素，AI 返回的 x/y 直接就是 `SetCursorPos` 坐标（一一对应）
- 启动时调用 `SetProcessDPIAware`，避免坐标被 Windows 虚拟化缩放

**配置**（`config/tasks/audiences.yaml`）：
```yaml
options:
  desktop_mode: true        # false 时退回下面的自动化模式
  desktop_max_steps: 20     # 识图点击最多多少步
```

### 自动化模式（`desktop_mode: false` 时）

1. 打开前注入 CookieCloud cookie → 打开 `index.php`
2. 遇到 Cloudflare 挑战时（任务已勾选「有 CF 验证」）进入实时处理：每秒探测，出现可点击/可滑动控件立刻处理
3. 登录页：`form[action='takelogin.php']`，字段 用户名 / 密码 / **两步验证码（字段名 `scode`）** / 图形验证码
   - 该站点账号未开启两步验证时 `scode` 留空即可；若以后开启，勾选「有 2FA」并绑定验证器即可自动填写
   - 图形验证码（`image.php?action=regimage`）交给 AI 识别
4. 登录态判定：`logout.php` / `userdetails.php` / `attendance.php`（userbar 的签到入口）
5. 签到：进入 `attendance.php`，该页是 **Cloudflare Turnstile 人机验证**（没有提交按钮）：
   ```html
   <form id="attendance-form" action="attendance.php" method="post">
     <input type="hidden" name="cf-token" id="cf-token" value="">
     <div class="cf-turnstile" data-sitekey="..." data-callback="cfCallback"></div>
   </form>
   ```
   `cfCallback` 在验证通过后直接 `attendance-form.submit()`，因此处理方式是**每秒轮询 + 加载即处理**：
   - 每 **1 秒**检测一次 Turnstile 控件是否加载完成（iframe 已渲染且有可见尺寸）
   - 一旦加载完成**立即处理**，优先级为：
     1. 在 CF 跨域 iframe 内直接找到复选框元素并点击（**不消耗 AI**）
     2. 找不到元素时按"复选框在控件左侧"的几何位置点击
     3. 以上都失败才把控件区域截图交给 AI 定位并立即点击
   - AI 判断"无需点击"（已自动通过 / 隐形验证）时继续每秒观察，不重复瞎点
   - 最多尝试 2 次点击（间隔 ≥3 秒），总时长受 `turnstile_timeout` 限制（默认 40 秒）
   - 验证通过后表单自动提交，按结果页文案判定（`签到成功` / `今日已签到` / `明天再来`）
6. **判定注意**：签到页规则区常驻"首次签到获得 20 粒爆米花""连续签到加成"等文案，
   因此成功判定不使用「获得 / 爆米花 / 连续签到」，只认结果页专有文案，避免误报
7. 连续 20 次登录失败会封 IP，`max_login_retry` 保持 2 以内；`turnstile_timeout` 默认 40 秒

## 人机验证（Cloudflare）实时处理

`core/cf.py` 是所有任务共用的挑战处理模块，核心原则是**每秒实时探测，绝不降级为被动等待**。

### 为什么要实时探测

Cloudflare 的挑战页初始只是「请稍候…」/「Just a moment…」，**几秒后才会渲染出真正需要交互的控件**：
可能是需要点击的复选框（Turnstile / Verify you are human），也可能是需要拖动的滑块（拼图）。
如果"当前页面上没找到控件"就改成每 10 秒重探一次，控件出现时就会白白多等好几秒；
更糟的是有些站点控件只在几秒内出现，错过窗口就再也过不去。
所以本模块**每一秒都重新探测**一次，控件一出现立刻处理。

### 处理优先级（元素定位优先，AI 只是兜底）

1. **元素定位**：直接用页面元素确定点击点，不消耗 AI
   - CF 跨域 iframe 内的 `label.cb-lb` / `.cb-lb` / `input[type=checkbox]`
   - 主文档验证容器（`div.cf-turnstile` / `#challenge-stage` 等）内部的复选框
   - 元素点不动（被遮挡）时，退化为按元素自身坐标点击
2. **几何兜底**：只有容器、找不到具体元素时，按"复选框在控件左侧"点击
3. **AI 兜底**：以上都失败才截图，且**只截控件所在区域**交给 AI 定位
4. **滑块**：识别到滑块按钮后，AI 定位缺口 → 拟人化轨迹拖动；AI 不可用时拖到轨道末端

### 截图前的居中处理

AI 截图前会先把控件 `scrollIntoView({block:'center'})`，并调整主窗口滚动量，
确保控件**位于浏览器窗口正中间**且完整可见。这样：

- 控件不会被截成一半
- 截图像素坐标（`scale=css`）与鼠标坐标严格一致，AI 返回的坐标可以直接点击

> 坐标系已用离线测试验证：Playwright 的 `bounding_box()` 与 `screenshot(clip=...)`
> 都是**主视口相对**坐标，即使元素位于跨域 iframe 内部也成立

### 日志

```
检测到人机验证挑战，开始实时处理（每 1 秒探测一次点击/滑动控件，最长 60s）
[CF 0s] 仍在验证中（点击 0 次 / 滑动 0 次），继续每秒探测可交互控件
[CF 2s] 已点击验证控件（Turnstile 复选框元素点击（label.cb-lb），第 1 次）
[CF 4s] 挑战已通过
```

## 任务执行顺序

- 队列严格串行：`队列入队 → 任务1 执行 → 任务2 执行 → … → 队列结束关闭浏览器`
- 单个任务失败不影响后续任务继续执行
- 浏览器只在**整个队列结束后**关闭一次；运行日志同样只落一份 `run_*.log`
- 任务之间若代理设置不同，会针对该任务重启浏览器（日志会注明"仅切换代理"）
- 图形界面运行期间会禁用运行按钮，避免重复点击造成并发

## 浏览器 MCP（CF 过不去时让 AI 接管浏览器）

内置的自动处理（每秒探测 + 元素定位点击 + 几何兜底）已经能应付绝大多数站点，
但仍有少数站点对自动化浏览器校验特别严。这种情况下提供两条升级路径：

### 1. 内置 AI 托管（开箱即用，无需任何配置）

全局设置 → AI 里勾选「**CF 过不去时让 AI 看图驱动浏览器兜底**」后，
内置策略失败时会自动切换到 AI 托管模式：

- AI 模型看截图，自己决定下一步：`click` / `drag` / `scroll` / `wait` / `screenshot` / `finish`
- 每一步的执行结果都会记进历史，AI 能看到"我上一步做了什么、结果如何"，不会重复无效动作
- 挑战消失（`state` 里 `挑战中=false`）即判定通过

### 2. 浏览器 MCP 服务端（让更强大的外部 AI 接管）

若内置 AI 也过不去，可启动浏览器 MCP 服务端，把浏览器控制权交给外部 AI：

```bash
python main.py mcp                      # 启动浏览器并暴露为 MCP 工具
python main.py mcp --url https://xxx    # 启动后先打开指定页面
```

在 MCP 客户端（Claude Desktop / 各类支持 MCP 的 AI 助手）里注册：

```json
{
  "command": "python",
  "args": ["D:/path/to/main.py", "mcp"],
  "cwd": "D:/path/to/签到助手"
}
```

注册后外部 AI 就能调用下列工具操作浏览器（坐标为 CSS 像素，与截图严格一致）：

| 工具 | 说明 |
|------|------|
| `screenshot` | 截取整窗或指定区域（可只截验证控件那一小块） |
| `click(x, y)` | 在指定坐标点击 |
| `drag(x1,y1,x2,y2)` | 拖动（滑块验证码 / 拼图） |
| `scroll(dx, dy)` | 滚动页面 |
| `fill(selector, text)` | 向输入框填文本 |
| `goto(url)` | 跳转 URL |
| `evaluate(script)` | 执行 JS |
| `state` | 读取 URL / 标题 / 是否仍在挑战 / Turnstile token 状态 |
| `wait(ms)` | 等待 |
| `finish(passed)` | 结束（passed=true 表示已通过） |

### 实现说明

- 两条路径共用同一套工具定义（`core/browser_tools.py`），行为完全一致
- MCP 用 stdio 传输（每行一条 JSON-RPC），日志写 stderr、不污染协议流
- 坐标统一 CSS 像素（`scale="css"` 截图），AI 返回的坐标可直接用于鼠标
- Windows 下 asyncio 无法用 `connect_read_pipe` 读 stdin（proactor 报 WinError 6），
  改用线程池阻塞读，跨平台稳定

## 浏览器调试能力

- **launch 模式**（默认）：程序启动 Edge 并开放调试端口 9222。
  地址栏输入 `edge://inspect/#devices` 可挂接调试；也可人工接管窗口，程序继续执行。
- **attach 模式**：先手动启动 `msedge --remote-debugging-port=9222`，配置 `browser.mode: attach`，
  程序直接连接你正在用的浏览器窗口。
- 持久化用户目录 `data/edge_profile`：登录态跨次保留，减少重复登录。
- **默认全屏 + 置顶**（可在「全局设置 → 浏览器」里关闭）：
  - `browser.fullscreen`：启动时带 `--start-fullscreen`，并让页面跟随窗口尺寸（不使用固定 viewport）
  - `browser.always_on_top`：页面加载后用 Windows API 把 Edge 窗口设为 `HWND_TOPMOST`，
    运行签到时不会被打到别的窗口后面去

## 添加新的签到任务

1. 在 `tasks/` 新建模块：

```python
from core.config import TaskConfig
from tasks.base import BaseTask, TaskContext, register

@register
class MySiteTask(BaseTask):
    id = "my_site"
    name = "我的站点"
    site = "https://example.com"

    async def ensure_logged_in(self) -> bool:
        await self.open_site()          # 内置：打开站点 +（勾选 CF 时）处理挑战
        ...
    async def do_checkin(self) -> tuple[bool, str]:
        ...
```

2. 在 `config/tasks/my_site.yaml` 写配置（`task.id` 与类属性 `id` 一致）
3. 在 `tasks/base.py` 的 `KNOWN_TASKS` 中登记 `"tasks.my_site"`
4. 重新打包时把 `tasks.my_site` 加入 `签到助手.spec` 的 `hiddenimports`
5. 任务会出现在 GUI 列表中，可勾选 2FA / CF 配置

框架内置能力：`ctx.browser`（点击/填表/元素截图/拟人点击）、`ctx.captcha`（AI 验证码识别）、
`ctx.slider`（AI 滑块）、`ctx.cf`（CF 挑战）、`ctx.cookiecloud`、`ctx.totp`、`ctx.events`。

## 打包 exe

```bash
.venv\Scripts\activate
pip install pyinstaller
pyinstaller --clean --noconfirm 签到助手.spec
# 产物：dist/签到助手.exe

# 生成对外分发的干净发布包（会自动校验 exe 内嵌配置不含任何凭据）
# 产物：release\签到助手.exe 与 release\签到助手-发布包.zip
```

说明：
- 打包会内置 playwright 的 node 驱动（程序使用系统 Edge，无需下载浏览器）
- 首次运行自动把内置 `config/` 模板释放到 exe 同目录
- 已关闭 UPX 压缩以降低杀毒软件误报；exe 体积较大属正常（含 Python + OpenCV + 驱动）

### 分发时的重要提醒

> **`dist/` 目录里除了 exe，还有程序运行时释放出来的 `config/` 与 `data/`，
> 里面含有使用者的账号、密码和 2FA 密钥，绝不能整个目录发出去。**

正确做法：只分发 `release/` 里的 exe（或发布包 zip）。
`config/twofa.yaml` 是否为空、各任务配置的账号密码是否为空，
任一项不满足就**直接中止**，避免误发带密钥的版本。

## 安全说明

- **2FA 凭证库默认为空**：程序不预置任何账号，必须由使用者自行导入二维码后才会出现 2FA 选项，
  因此分发给他人不会泄露任何人的 2FA 密钥
- `config/config.yaml`（AI 密钥、CookieCloud 密钥、代理密码）、`config/twofa.yaml`（2FA 密钥）、
  `config/tasks/*.yaml`（账号密码）均为**本地明文存储**，请勿提交公开仓库（已提供 `.gitignore`）
- CookieCloud 加解密全程本地完成，密钥不外发
- 打包前请确认 `config/` 目录是"模板状态"（账号密码为空、`twofa.yaml` 为 `accounts: []`），

## 已知边界

- 定时调度：配置已预留 `schedule_time`，调度器待接入
- CF Turnstile 的 AI 主动点击为尽力而为，复杂挑战可能需人工接管（attach 模式为此准备）
- 滑块验证码针对常见样式，特殊站点可能需要微调 `core/slider.py` 的坐标换算
- 二维码导入对截图类图片已做多尺度/多阈值/反色增强并配 pyzbar 后备；
  极端模糊或拍摄角度过大仍可能失败，此时可在任务配置里直接填 32 位 secret
- 代理按任务切换时需要重启浏览器（同一时刻一个浏览器实例只能有一种网络设置）
