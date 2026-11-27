"""OurBits（OB / ourbits.club）每日签到任务。

**执行方式：桌面模式（和当年人人任务同一套）**
用用户平时那个正常浏览器打开站点，再由 AI 看屏幕截图识图点击完成登录与签到。
原因：站点在人机验证上跟 Playwright/CDP 的自动化指纹死磕，桌面模式看到的是普通浏览器 +
真人鼠标行为，CF 拦不住。

**站点结构（实测登录页 / 首页源码）**

登录页 `https://ourbits.club/login.php`（标题 `OurBits :: 登录`）：
- `form[action='takelogin.php']`，字段 username / password / **2fa_code**（页面提示「如有设置则必填」）
- **没有图形验证码**（页面里没有 `image.php?action=regimage`）
- 提交按钮 `input[type='submit'][value='登录']`（另有 type=reset 的「重置」）
- 注意页面里还有一个 `<form method="get">` 的语言切换表单，所以必须带 action 限定

登录后首页（标题 `OurBits :: 首页`）：
- `a[href*='logout.php']` / `a[href*='userdetails.php']` / `a[href*='attendance.php']`（签到得魔力）

**签到链路（用户实测）**：打开 `attendance.php` → 点「签到」按钮 →
**站点自动弹出 Cloudflare 人机验证** → 验证通过后**自动完成签到**（不需要再点第二次）。

结果文案按 NexusPHP attendance 插件通用规则判定：
- 成功：签到成功 / 签到完成 / 恭喜 / 签到获得
- 已签到：已签到 / 今日已签到 / 您今天已经签到 / 不能重复签到 / 明天再来
"""
from __future__ import annotations

from .base import register
from .desktop_signin import DesktopSigninMixin
from .nexusphp import NexusPHPTask


@register
class OurBitsTask(DesktopSigninMixin, NexusPHPTask):
    id = "ourbits"
    name = "OurBits（OB）"
    description = "OurBits(ourbits.club) 每日签到：桌面模式（正常浏览器 + AI 识图过 CF）+ 可选两步验证"
    site = "https://ourbits.club"

    # ---- 登录页（实测源码）----
    SEL_USERNAME = "form[action='takelogin.php'] input[name='username']"
    SEL_PASSWORD = "form[action='takelogin.php'] input[name='password']"
    # 该站两步验证字段名是 2fa_code（不是 NexusPHP 原生的 oneCode）
    SEL_ONECODE = "form[action='takelogin.php'] input[name='2fa_code']"
    # 登录页没有图形验证码 —— 置空，跳过验证码分支（别去等一张永远没有的图）
    SEL_CAPTCHA_IMG = ""
    SEL_CAPTCHA_INPUT = ""
    SEL_SUBMIT = "form[action='takelogin.php'] input[type='submit'][value='登录']"

    # ---- 登录后标志 ----
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
        "a[href*='attendance.php']",
    )

    # ---- 签到页 ----
    LOGIN_URL = "/login.php"
    CHECKIN_URL = "/attendance.php"
    CHECKIN_BTN_CANDIDATES = (
        "input[type='submit'][value*='签到']",
        "input[type='button'][value*='签到']",
        "button:has-text('签到')",
        "a[href*='attendance.php']",
    )

    # ---- 结果关键词（NexusPHP attendance 通用）----
    ALREADY_KEYWORDS = ("已签到", "已经签到", "今日已签到", "今天已签到",
                        "您今天已经签到", "不能重复签到", "明天再来", "明日再来")
    SUCCESS_KEYWORDS = ("签到成功", "签到完成", "恭喜", "签到获得", "获得奖励")
    FAIL_KEYWORDS = ("签到失败", "签到出错", "无法签到", "操作失败")

    # ---- 桌面模式 ----
    DESKTOP_TITLE = "OurBits"
    DESKTOP_GOAL = (
        "在 OurBits（ourbits.club）完成今日签到。\n"
        "流程：\n"
        "1. 页面已经是签到页或首页；如果显示的是登录页（有「用户名」「密码」输入框），"
        "先用下面的账号密码登录：\n"
        "   - 先点「用户名」输入框，输入用户名；再点「密码」输入框，输入密码；\n"
        "   - 「二步验证」输入框：如果后面账号信息里给了动态码就填进去，没给就留空；\n"
        "   - 点「登录」按钮。\n"
        "2. 如果当前不是签到页，就在页面顶部导航点「签到得魔力」这个链接"
        "（地址是 attendance.php），进入签到页。\n"
        "3. 在签到页点「签到」按钮。\n"
        "4. 重要：点完「签到」后，站点会自动弹出 Cloudflare 人机验证"
        "（可能显示「请稍候」「正在验证您是否是真人」，或一个复选框、滑块、转圈）。"
        "这时不要重复点签到按钮：\n"
        "   - 看到复选框就点它；看到滑块就拖到缺口；\n"
        "   - 只是在转圈/「请稍候」就 wait 2~3 秒再看；\n"
        "   - 验证通过后页面会**自动完成签到**，不需要你再点签到。\n"
        "5. 等到出现签到结果提示就结束。\n\n"
        "判定标准：\n"
        "- 看到「签到成功」「签到完成」「获得 X 魔力值」→ finish(passed=true, message=\"签到成功\")\n"
        "- 看到「今日已签到」「您今天已经签到」「不能重复签到」→ "
        "finish(passed=true, message=\"今日已签到（无需重复）\")\n"
        "- 看到「签到失败」「无法签到」等明确失败提示 → finish(passed=false, 带上失败原因)\n"
        "- 密码错误 / 账号被禁用 → finish(passed=false, message=\"登录失败：…\")\n"
        "注意：首页和签到页常驻的「签到得魔力」只是一个入口链接，"
        "页面上的「魔力值 2,218.6」也只是余额，都不代表签到成功，不要据此判定。"
    )
