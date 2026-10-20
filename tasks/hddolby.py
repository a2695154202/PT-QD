"""HDDolby（site-b.example）签到任务。

站点为 NexusPHP，登录页 https://site-b.example/index.php
- <form action="takelogin.php">，字段：username / password / 2fa / imagestring / imagehash
- 注意：2FA 字段名是 "2fa"（不是 oneCode），且站点有 Cloudflare 人机验证
- 登录后 info_block 才有 logout.php / userdetails.php / attendance.php
- 签到入口为 attendance.php（"签到得鲸币"）

签到页结构随站点插件不同而变化，因此签到按钮支持在任务配置里用
options.checkin_btn / options.checkin_url 覆盖；未配置时自动探测。
"""
from __future__ import annotations

from .base import register
from .nexusphp import NexusPHPTask


@register
class HDDolbyTask(NexusPHPTask):
    id = "hddolby"
    name = "HDDolby"
    description = "HDDolby(site-b.example) 每日签到：NexusPHP，支持 CF 人机验证 / 2FA / AI 验证码"
    site = "https://site-b.example/index.php"

    # 登录页（2FA 字段名为 2fa）
    SEL_ONECODE = "form[action='takelogin.php'] input[name='2fa']"
    # 登录后页面标志
    LOGGED_IN_MARKERS = (
        "a[href*='logout.php']",
        "a[href*='userdetails.php']",
        "a[href*='attendance.php']",
    )
    # 签到页
    CHECKIN_URL = "https://site-b.example/index.php"
    CHECKIN_BTN_CANDIDATES = (
        "input[type='submit'][value*='签到']",
        "input[type='button'][value*='签到']",
        "button:has-text('签到')",
        "#signin",
        "input[type='submit']",
    )
    # 结果关键词（避免用"鲸币"这类常驻文本，防止误判）
    ALREADY_KEYWORDS = ("已签到", "已经签到", "今日已签到", "今天已签到", "已领取", "明天再来")
    SUCCESS_KEYWORDS = ("签到成功", "恭喜", "签到获得", "获得奖励")
