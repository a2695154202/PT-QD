# 网站签到助手

基于 **Edge 浏览器自动化（Playwright/CDP）+ AI 视觉识别** 的网站每日签到工具。
支持 2FA 动态验证码、CookieCloud 同步、AI 识别图形验证码，任务可插拔扩展，可打包为免环境 exe。

## 功能特性

| 功能 | 说明 |
|------|------|
| 浏览器控制 | 程序启动 Edge（带远程调试端口 9222，可 `edge://inspect` 挂接调试）或连接已运行的 Edge；支持点击、填表、截取页面指定位置图片 |
| 2FA（TOTP） | 导入谷歌身份验证器二维码（`otpauth://` 与 `otpauth-migration://` 导出格式均支持）；**任务级勾选是否有 2FA，并可指定绑定凭证库中的哪个验证器** |
| AI 能力（用户配置） | OpenAI 兼容接口；识别登录/签到页的图形验证码（CF 人机验证不再调用 AI） |
| CookieCloud | 输入服务器/UUID/密钥，本地 AES 解密同步 cookie；工作模式：**覆盖到浏览器 / 暂停同步**，默认同步全部站点；**在打开站点之前就把 cookie 注入浏览器** |
| 代理 | 全局设置代理地址（http/socks5 + 可选账号密码）；任务配置里勾选「本任务走代理」才使用，未勾选的任务强制直连（**本程序从不使用 Windows 系统代理**） |
| Telegram 通知 | 跑完一遍任务后把签到汇总推到手机：成功/失败数量 + 每个任务一行结论；默认**走代理**（`use_proxy: true`），可设「只在失败时推送」；有失败时附「🔁 重试失败项」按钮，支持 `/status` `/run` `/retry` 菜单交互 |
| 签到任务 | 任务基类 + 注册表 + YAML 配置；任务可单独勾选是否有 2FA / CF 验证 / 走代理。内置 **PT-空（site-a.example）**、**HDDolby（site-b.example）**、**TTG 听听歌（site-c.example）**、**馒头 M-Team（site-d.example）**、**Akile（site-f.example，控制台「每日签到」）**、**憨憨 HHCLUB（site-g.example，attendance.php 停留即签到）** |
| 任务队列 | 严格串行：一个任务跑完再跑下一个；队列全部结束后才关闭浏览器（任务之间浏览器保持运行） |
| 定时运行 | 全局设置里设「每天 HH:MM」自动签到；**在定时页里单独勾选要跑哪些任务**（与主界面勾选无关）；可勾选**跳过当天已签到的任务**、**错过时间点是否补跑**；每天只自动跑一次，状态栏显示下次运行时间 + 任务数（程序需保持打开） |
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
  - 任务名后括号里显示**今天**是否已签到：`任务名称(已签到)` / `HDDolby(未签到)`
    —— 昨天的签到结果今天会自动变回「未签到」；记录行用「今天: …」/「上次: …」区分
  - **已签到的任务默认不勾选**（打开程序 / 刷新任务都一样），避免白跑一遍
  - 结果记录在 `data/task_state.json`，每次运行后自动更新；跨天时界面自动刷新
  - 任务行的结果文本只显示首行摘要（超长自动截断），完整报错看右侧运行日志
- **任务配置**（保存到 `config/tasks/*.yaml`，底部固定「保存配置」按钮）：
  - 账号 / 密码（CookieCloud 失效时兜底登录）
  - ☑ **本任务有 2FA** → 可下拉选择绑定 2FA 凭证库中的哪个账号，或直接填 secret
  - ☑ **本任务有 Cloudflare / 人机验证** → 勾选后才处理挑战，未勾选完全不处理
  - ☑ **本任务走代理** → 使用「全局设置 → 代理」的地址；未勾选则本任务强制直连
  - 登录失败次数上限：**跨天累计**，只有登录成功才清零；用尽后该任务不再执行
    （界面显示「登录已锁定 x/y」，可点「重置登录失败次数」手动解锁）
- **全局设置**（保存到 `config/config.yaml`）：AI 接口、CookieCloud（含工作模式）、代理、
  浏览器模式/端口/无头、**启动后全屏**、**窗口置顶**、**定时运行（每天 HH:MM 自动签到 + 自选要跑的任务）**
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

`browser` 段（动作节奏与超时，慢机器/被人机验证卡住时主要调这里）：

```yaml
browser:
  slow_mo: 50               # Playwright 自带的每步间隔(ms)
  action_delay_ms: 150      # 【新增】每个动作前的拟人小延时(ms)，带 0.6~1.5 倍随机抖动；0=关闭
  default_timeout: 20000    # 【默认调大】等元素/点击的单步超时(ms)
  nav_timeout: 45000        # 【新增】打开页面的超时(ms)；CF 挑战页常迟迟不触发 domcontentloaded
```

- **动作延时**：点击、填表、打开页面之前都会先停顿一小会儿再操作（`action_delay_ms`），
  避免连点过快被站点判定为机器人；数值越大越慢，0 表示关闭
- **导航超时不再判失败**：`goto` 超过 `nav_timeout` 但页面地址已经是 `http(s)`（说明其实已经打开了），
  只记一条 WARNING 就继续后续判定 —— 这正是之前「页面明明打开了却报 Timeout 15000ms」的原因
- 慢机器上仍报超时，把 `default_timeout` / `nav_timeout` 再调大即可，不用改代码

代理生效规则：**本程序从不使用 Windows 系统代理**。任务勾选了「本任务走代理」且「全局设置 → 代理」
里填了地址，才走你配置的那个代理；没配置代理、或本任务没勾选，一律以 `--no-proxy-server` 启动强制直连。
AI 识别与 CookieCloud 请求同样遵守这条规则（`trust_env=False`，不吃系统代理与 `HTTP_PROXY` 环境变量）。

> 为什么要这样：如果电脑上开着系统代理（VPN、加速器、公司网络），Chromium 和 `requests`
> 默认都会走它，表现就是「浏览器里页面明明能打开，任务却一直报超时 / 点不动」。

任务失败时如果原因是浏览器网络错误，结果里会多一行**网络诊断**，把 `net::ERR_XXX` 翻译成结论：

| 报错 | 含义 | 怎么办 |
|---|---|---|
| `ERR_CONNECTION_REFUSED` | 本机主动拒绝连接 | 查 `C:\Windows\System32\drivers\etc\hosts` 是否把域名指到 `127.0.0.1`；勾了「走代理」时确认代理软件在跑（端口没起来就是拒连） |
| `ERR_CONNECTION_RESET` / `ERR_TIMED_OUT` | 线路被阻断 | 给这个任务勾「走代理」 |
| `ERR_NAME_NOT_RESOLVED` | 域名解析失败 | 换 DNS；站点改了域名就改 `task.url` |
| `ERR_PROXY_CONNECTION_FAILED` | 连不上代理 | 核对代理地址/端口，确认代理软件已启动 |

诊断里还会附上「本次为直连 / 本次走代理」和域名的实际解析结果，便于判断是站点问题还是网络问题。

### Telegram 通知（跑完自动汇报）

一遍任务跑完后，把汇总结果推到 Telegram：成功/失败数量 + 每个任务一行结论。

```yaml
telegram:
  enabled: true
  bot_token: "123456789:AA..."   # @BotFather 建 bot 后拿到的 token
  chat_id: "123456789"           # 个人数字 id；群组填负数（-100...）
  use_proxy: true                # 默认走代理：api.telegram.org 国内直连基本不通
  only_on_failure: false         # true = 只有出现失败才推送
```

配置步骤：找 **@BotFather** 发 `/newbot` 拿到 token → **先给 bot 发一条消息**（否则 bot 不能主动找你）
→ 找 **@userinfobot** 拿到自己的数字 id → 填进「全局设置 → 通知」→ 点「发送测试消息」验证。

线路优先级：**「全局设置 → 代理」里填的地址** → **自动发现的本机代理** → 直连。
之所以有第二档：国内直连 `api.telegram.org` 基本必然超时，而很多人只是开着 Clash / v2rayN
却没把地址填进本程序。程序会先读 Windows 系统代理（只在开关打开时才算数），再探测本机常见端口
（`7897 / 7890 / 10809 / 10808 / 1080 / 2080 …`），命中就用，日志里会写明
`Telegram 交互网络：自动发现的本机代理 http://127.0.0.1:7897`。
**这一档只给 Telegram 用**，签到任务仍然严格按「勾了走代理才走代理」执行。

命令行也能单独测：`签到助手.exe test-telegram`。推送失败只写日志，不影响签到结果。

在手机上也能操作（程序开着就行，走的是同一条长轮询）：

| 命令 | 作用 |
|---|---|
| `/status` | 今日各任务签到状态（已签到 x/y） |
| `/run` | 运行全部启用任务 |
| `/retry` | 重试今天还没签到的任务 |
| `/menu` / `/help` | 菜单按钮 / 帮助 |

汇总消息里**只要有任务失败，就会多出一个「🔁 重试失败项」按钮**，点一下直接重跑那几个任务
（按钮里带的是失败任务的 id，所以只重试它们，不会把成功的又跑一遍）。点按钮后 Telegram 会立刻回执，
不会一直转圈。机器人只理会配置里那个 `chat_id`，别人搜到也操作不了。

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
│   ├── slider.py            # 拟人化鼠标拖动轨迹（滑块用）
│   ├── cf.py                # CF/人机挑战（每秒实时探测点击/滑动控件，元素定位优先，AI 兜底）
│   ├── desktop.py           # 桌面控制（Win32 屏幕截图 + 鼠标/键盘原生输入 + 窗口最大化/置顶）
│   ├── browser_bridge.py    # profile 桥接（调试浏览器写 cookie ↔ 正常浏览器签到，共用同一 profile）
│   ├── desktop_agent.py     # 识图点击 Agent（AI 看屏幕截图，决定点哪里、填什么）
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
│   ├── akile.py             # Akile 任务（Vue 单页应用：登录 + 验证器 2FA + 控制台「每日签到」）
│   └── hhan.py              # 憨憨 HHCLUB 任务（NexusPHP：图形验证码 + two_step_code，attendance.php 停留即签到）
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

站点 `https://site-b.example/`，NexusPHP，登录需 2FA，且有 Cloudflare 人机验证。

1. 打开前注入 CookieCloud cookie → 打开 `index.php`
2. 遇到 Cloudflare 挑战时（任务已勾选「有 CF 验证」）进入**实时处理**模式：每秒探测一次页面，
   出现可点击复选框就立刻点、出现滑块就立刻拖，详见「人机验证实时处理」一节
3. 登录态判定 → 需要登录时填 用户名 / 密码 / **两步验证码（字段名 `2fa`）** / 图形验证码 → 提交
   - 现在站点可能把 2FA 做成**独立一页**（提交账号密码后跳转）：程序会自动识别该页
     （`input[name='2fa']`、`oneCode`、`two_step_code`、`autocomplete=one-time-code` 等候选），
     填入动态码并提交；如果上次停在验证页，重跑也会直接补填
   - 页面结构与预期不同时，可用任务配置 `sel_2fa_input` / `sel_2fa_submit` 覆盖选择器
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
4. **滑块**：识别到滑块按钮后，用拟人化轨迹拖到轨道末端（不做 AI 缺口定位）

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
