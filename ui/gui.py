"""图形界面（Tkinter）

- 任务多选框：勾选本次要运行的任务
- 编辑任务：账号密码、是否有 2FA（并选择绑定哪个验证器）、是否有 CF 验证等，保存到 config/tasks/*.yaml
- 全局设置：AI / CookieCloud / 浏览器参数，保存到 config/config.yaml
所有设置本地保存，每次打开自动载入。

启动：python main.py gui（或不带参数直接运行）
"""
from __future__ import annotations

import asyncio
import logging
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

from core.config import AppConfig, load_task_configs
from core.logger import get_logger
from core.scheduler import (describe, normalize_hhmm, normalize_tasks,
                            pick_scheduled, should_run)
from core.state import (is_signed, login_fail_info, load_state, record_date,
                        reset_all_login_fail, reset_login_fail, schedule_last_run,
                        set_schedule_last_run, today_str)
from core.twofa_store import TwoFactorVault
from tasks.manager import TaskManager

LOGGER = get_logger()


def _short_msg(text: str, limit: int = 42) -> str:
    """任务结果只取首行并截断，避免 Playwright Call log 之类的长文本把界面撑爆"""
    raw = (text or "").strip()
    if not raw:
        return ""
    line = " ".join(raw.splitlines()[0].split())     # 首行 + 压掉多余空白
    return line if len(line) <= limit else line[:limit] + "…"


class TextHandler(logging.Handler):
    """把日志输出到 Tk Text 控件（线程安全）"""

    def __init__(self, text_widget: tk.Text):
        super().__init__()
        self.text = text_widget
        self.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    def emit(self, record):
        try:
            msg = self.format(record)

            def append():
                self.text.configure(state="normal")
                self.text.insert("end", msg + "\n")
                self.text.see("end")
                self.text.configure(state="disabled")

            self.text.after(0, append)
        except Exception:
            pass


class TaskEditDialog(tk.Toplevel):
    """任务配置对话框"""

    def __init__(self, master, task_id: str, on_saved=None):
        super().__init__(master)
        self.cfg = next((c for c in load_task_configs() if c.id == task_id), None)
        if self.cfg is None:
            messagebox.showerror("错误", f"任务配置不存在: {task_id}")
            self.destroy()
            return
        self.on_saved = on_saved
        self.title(f"任务配置 - {self.cfg.name}")
        self.geometry("600x700")
        self.minsize(560, 480)
        self.transient(master)
        self.grab_set()
        self._build()
        self._load()

    def _build(self):
        pad = {"padx": 8, "pady": 4}

        # ---- 底部按钮（先 pack 到底部，保证任何窗口高度下都可见）----
        btns = ttk.Frame(self)
        btns.pack(side="bottom", fill="x", padx=10, pady=8)
        ttk.Button(btns, text="保存配置", command=self._save, width=12).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy, width=10).pack(side="right", padx=4)

        # ---- 可滚动内容区 ----
        container = ttk.Frame(self)
        container.pack(fill="both", expand=True, padx=10, pady=(10, 0))
        canvas = tk.Canvas(container, borderwidth=0, highlightthickness=0)
        vbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vbar.set)
        vbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frm = ttk.Frame(canvas)
        win = canvas.create_window((0, 0), window=frm, anchor="nw")
        frm.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(win, width=e.width))
        self._canvas = canvas
        self.bind_all("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta / 120), "units"))

        # ---- 基础 ----
        base = ttk.Labelframe(frm, text="基础", padding=8)
        base.pack(fill="x", pady=(0, 8))
        self.var_name = tk.StringVar()
        self.var_url = tk.StringVar()
        self.var_enabled = tk.BooleanVar()
        ttk.Label(base, text="任务名称").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(base, textvariable=self.var_name, width=42).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(base, text="签到地址").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(base, textvariable=self.var_url, width=42).grid(row=1, column=1, sticky="w", **pad)
        ttk.Checkbutton(base, text="启用本任务（参与「运行全部启用」）",
                        variable=self.var_enabled).grid(row=2, column=0, columnspan=2, sticky="w", **pad)

        # ---- 账号 ----
        acc = ttk.Labelframe(frm, text="登录账号（CookieCloud 失效时兜底登录）", padding=8)
        acc.pack(fill="x", pady=(0, 8))
        self.var_user = tk.StringVar()
        self.var_pass = tk.StringVar()
        ttk.Label(acc, text="用户名").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(acc, textvariable=self.var_user, width=36).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(acc, text="密码").grid(row=1, column=0, sticky="w", **pad)
        self.entry_pass = ttk.Entry(acc, textvariable=self.var_pass, width=36, show="*")
        self.entry_pass.grid(row=1, column=1, sticky="w", **pad)
        self.var_show_pass = tk.BooleanVar(value=False)
        ttk.Checkbutton(acc, text="显示密码", variable=self.var_show_pass,
                        command=lambda: self.entry_pass.configure(
                            show="" if self.var_show_pass.get() else "*")
                        ).grid(row=1, column=2, sticky="w", **pad)

        # ---- 2FA ----
        two = ttk.Labelframe(frm, text="二次验证（2FA）", padding=8)
        two.pack(fill="x", pady=(0, 8))
        self.var_has_2fa = tk.BooleanVar()
        self.var_vault = tk.StringVar()
        self.var_secret = tk.StringVar()
        ttk.Checkbutton(two, text="本任务有 2FA（勾选后登录时自动填写动态验证码）",
                        variable=self.var_has_2fa,
                        command=self._toggle_2fa).grid(row=0, column=0, columnspan=3, sticky="w", **pad)
        ttk.Label(two, text="绑定验证器").grid(row=1, column=0, sticky="w", **pad)
        self.cmb_vault = ttk.Combobox(two, textvariable=self.var_vault, width=34, state="readonly")
        self.cmb_vault.grid(row=1, column=1, sticky="w", **pad)
        ttk.Button(two, text="刷新列表", command=self._refresh_vault).grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(two, text="或直接填 secret").grid(row=2, column=0, sticky="w", **pad)
        self.entry_secret = ttk.Entry(two, textvariable=self.var_secret, width=36)
        self.entry_secret.grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(two, text="（凭证库为空时先点主界面「导入2FA二维码」；两者都填时优先用 secret）",
                  foreground="gray", font=("", 8)).grid(row=3, column=0, columnspan=3, sticky="w", **pad)

        # ---- CF ----
        cff = ttk.Labelframe(frm, text="人机验证", padding=8)
        cff.pack(fill="x", pady=(0, 8))
        self.var_has_cf = tk.BooleanVar()
        tk.Checkbutton(cff, text="本任务有 Cloudflare / 人机验证（勾选后遇到挑战时由 AI 主动处理；"
                                 "未勾选则完全不处理）",
                       variable=self.var_has_cf, wraplength=480,
                       justify="left").grid(row=0, column=0, sticky="w", **pad)

        # ---- 代理 ----
        px = ttk.Labelframe(frm, text="代理", padding=8)
        px.pack(fill="x", pady=(0, 8))
        self.var_use_proxy = tk.BooleanVar()
        tk.Checkbutton(px, text="本任务走代理（使用「全局设置 → 代理」中的地址；"
                                "不勾选则本任务强制直连，不走代理）",
                       variable=self.var_use_proxy, wraplength=480,
                       justify="left").grid(row=0, column=0, sticky="w", **pad)
        self.lbl_proxy_state = ttk.Label(px, text="", foreground="gray", font=("", 8))
        self.lbl_proxy_state.grid(row=1, column=0, sticky="w", **pad)

        # ---- 其它 ----
        oth = ttk.Labelframe(frm, text="其它", padding=8)
        oth.pack(fill="x", pady=(0, 8))
        ttk.Label(oth, text="登录失败重试次数").grid(row=0, column=0, sticky="w", **pad)
        self.var_retry = tk.IntVar(value=3)
        ttk.Spinbox(oth, from_=1, to=10, textvariable=self.var_retry, width=6).grid(
            row=0, column=1, sticky="w", **pad)
        ttk.Label(oth, text="（HDSky 连续失败会禁用 IP，建议不超过 3）",
                  foreground="gray", font=("", 8)).grid(row=0, column=2, sticky="w", **pad)

    def destroy(self):
        try:
            self.unbind_all("<MouseWheel>")
        except Exception:
            pass
        super().destroy()

    def _refresh_vault(self):
        keys = ["（不绑定，按站点自动匹配）"]
        try:
            for a in TwoFactorVault().accounts:
                keys.append(a.key)
        except Exception as e:
            LOGGER.warning(f"读取 2FA 凭证库失败: {e}")
        self.cmb_vault["values"] = keys
        if self.var_vault.get() not in keys:
            self.var_vault.set(keys[0])

    def _toggle_2fa(self):
        state = "normal" if self.var_has_2fa.get() else "disabled"
        self.cmb_vault.configure(state="readonly" if self.var_has_2fa.get() else "disabled")
        self.entry_secret.configure(state=state)

    def _load(self):
        c = self.cfg
        self.var_name.set(c.name)
        self.var_url.set(c.url)
        self.var_enabled.set(c.enabled)
        self.var_user.set(c.username)
        self.var_pass.set(c.password)
        self.var_has_2fa.set(c.has_2fa)
        self.var_secret.set(c.totp_secret)
        self.var_has_cf.set(c.has_cf)
        self.var_use_proxy.set(c.use_proxy)
        self.var_retry.set(c.max_login_retry)
        self._refresh_vault()
        self.var_vault.set(c.vault_key if c.vault_key in self.cmb_vault["values"]
                           else self.cmb_vault["values"][0])
        self._toggle_2fa()
        # 显示全局代理状态
        px = AppConfig.load().proxy
        if px.ready:
            self.lbl_proxy_state.configure(
                text=f"当前全局代理: {px.server}（已启用）", foreground="gray")
        else:
            self.lbl_proxy_state.configure(
                text="当前未配置全局代理：请先在「全局设置 → 代理」中填写并启用", foreground="#b45309")

    def _save(self):
        c = self.cfg
        c.name = self.var_name.get().strip() or c.name
        c.url = self.var_url.get().strip() or c.url
        c.enabled = bool(self.var_enabled.get())
        c.account["username"] = self.var_user.get().strip()
        c.account["password"] = self.var_pass.get()
        ts = c.account.setdefault("two_step", {})
        ts["enabled"] = bool(self.var_has_2fa.get())
        ts["secret"] = self.var_secret.get().strip()
        vault_val = self.var_vault.get()
        ts["vault_key"] = "" if vault_val.startswith("（") else vault_val
        c.options["has_2fa"] = bool(self.var_has_2fa.get())
        c.options["has_cf"] = bool(self.var_has_cf.get())
        c.options["use_proxy"] = bool(self.var_use_proxy.get())
        try:
            c.options["max_login_retry"] = int(self.var_retry.get() or 3)
        except (tk.TclError, ValueError):
            c.options["max_login_retry"] = 3
        try:
            path = c.save()
            LOGGER.info(f"任务配置已保存: {path}")
            if self.on_saved:
                self.on_saved()
            messagebox.showinfo("已保存", f"任务配置已保存到：\n{path}")
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))


class SettingsDialog(tk.Toplevel):
    """全局设置对话框"""

    def __init__(self, master, on_saved=None):
        super().__init__(master)
        self.cfg = AppConfig.load()
        self.on_saved = on_saved
        self.title("全局设置")
        self.geometry("600x560")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self._build()
        self._load()

    def _build(self):
        pad = {"padx": 8, "pady": 4}
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, padx=8, pady=8)

        # --- AI ---
        tab_ai = ttk.Frame(nb, padding=8)
        nb.add(tab_ai, text="AI 模型")
        self.ai_enabled = tk.BooleanVar()
        self.ai_base = tk.StringVar()
        self.ai_key = tk.StringVar()
        self.ai_model = tk.StringVar()
        self.ai_vision = tk.StringVar()
        self.ai_proactive = tk.BooleanVar()
        self.ai_agent_fallback = tk.BooleanVar()
        ttk.Checkbutton(tab_ai, text="启用 AI（验证码识别 / CF 挑战 / 滑块需要）",
                        variable=self.ai_enabled).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        for i, (label, var) in enumerate([("接口地址 base_url", self.ai_base),
                                          ("API Key", self.ai_key),
                                          ("模型 model", self.ai_model),
                                          ("视觉模型（可留空）", self.ai_vision)], start=1):
            ttk.Label(tab_ai, text=label).grid(row=i, column=0, sticky="w", **pad)
            e = ttk.Entry(tab_ai, textvariable=var, width=48)
            e.grid(row=i, column=1, sticky="w", **pad)
            if label == "API Key":
                e.configure(show="*")
        ttk.Checkbutton(tab_ai, text="允许 AI 主动介入（遇到人机挑战时主动点击/拖动）",
                        variable=self.ai_proactive).grid(row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(tab_ai,
                        text="CF 过不去时让 AI 看图驱动浏览器兜底（AI 托管 / MCP）",
                        variable=self.ai_agent_fallback).grid(
            row=6, column=0, columnspan=2, sticky="w", **pad)

        # --- CookieCloud ---
        tab_cc = ttk.Frame(nb, padding=8)
        nb.add(tab_cc, text="CookieCloud")
        self.cc_enabled = tk.BooleanVar()
        self.cc_mode = tk.StringVar(value="overwrite")
        self.cc_server = tk.StringVar()
        self.cc_uuid = tk.StringVar()
        self.cc_pass = tk.StringVar()
        self.cc_domains = tk.StringVar()
        ttk.Checkbutton(tab_cc, text="启用 CookieCloud",
                        variable=self.cc_enabled).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(tab_cc, text="工作模式").grid(row=1, column=0, sticky="nw", **pad)
        mode_frame = ttk.Frame(tab_cc)
        mode_frame.grid(row=1, column=1, sticky="w", **pad)
        for text, val in (("覆盖到浏览器", "overwrite"),
                          ("暂停同步", "paused")):
            ttk.Radiobutton(mode_frame, text=text, value=val,
                            variable=self.cc_mode).pack(side="left", padx=(0, 12))
        ttk.Label(tab_cc,
                  text="覆盖到浏览器：打开站点前先把服务器的 cookie 注入浏览器（推荐）\n"
                       "暂停同步：不读取服务器的 cookie",
                  foreground="gray", font=("", 8), justify="left").grid(
            row=2, column=1, sticky="w", **pad)
        for i, (label, var) in enumerate([("服务器地址", self.cc_server),
                                          ("UUID", self.cc_uuid),
                                          ("同步密钥", self.cc_pass),
                                          ("同步域名（逗号分隔，留空=所有站点）", self.cc_domains)], start=3):
            ttk.Label(tab_cc, text=label).grid(row=i, column=0, sticky="w", **pad)
            e = ttk.Entry(tab_cc, textvariable=var, width=48)
            e.grid(row=i, column=1, sticky="w", **pad)
            if label == "同步密钥":
                e.configure(show="*")

        # --- 浏览器 ---
        tab_br = ttk.Frame(nb, padding=8)
        nb.add(tab_br, text="浏览器")
        self.br_mode = tk.StringVar()
        self.br_port = tk.IntVar()
        self.br_headless = tk.BooleanVar()
        self.br_slow = tk.IntVar()
        ttk.Label(tab_br, text="模式").grid(row=0, column=0, sticky="w", **pad)
        ttk.Combobox(tab_br, textvariable=self.br_mode, values=["launch", "attach"],
                     state="readonly", width=12).grid(row=0, column=1, sticky="w", **pad)
        ttk.Label(tab_br, text="launch=程序启动 Edge；attach=连接已运行的 Edge",
                  foreground="gray", font=("", 8)).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(tab_br, text="调试端口").grid(row=2, column=0, sticky="w", **pad)
        ttk.Entry(tab_br, textvariable=self.br_port, width=12).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(tab_br, text="操作间隔 slow_mo(ms)").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(tab_br, textvariable=self.br_slow, width=12).grid(row=3, column=1, sticky="w", **pad)
        ttk.Checkbutton(tab_br, text="无头模式（不显示浏览器窗口，不建议）",
                        variable=self.br_headless).grid(row=4, column=0, columnspan=2, sticky="w", **pad)
        self.br_fullscreen = tk.BooleanVar()
        self.br_topmost = tk.BooleanVar()
        ttk.Checkbutton(tab_br, text="启动后全屏", variable=self.br_fullscreen).grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(tab_br, text="窗口置顶（始终显示在其它窗口之上）",
                        variable=self.br_topmost).grid(row=6, column=0, columnspan=2, sticky="w", **pad)

        # --- 代理 ---
        tab_px = ttk.Frame(nb, padding=8)
        nb.add(tab_px, text="代理")
        self.px_enabled = tk.BooleanVar()
        self.px_server = tk.StringVar()
        self.px_user = tk.StringVar()
        self.px_pass = tk.StringVar()
        ttk.Checkbutton(tab_px, text="启用代理地址",
                        variable=self.px_enabled).grid(row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(tab_px, text="代理地址").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(tab_px, textvariable=self.px_server, width=48).grid(row=1, column=1, sticky="w", **pad)
        ttk.Label(tab_px, text="格式：http://127.0.0.1:7890 或 socks5://127.0.0.1:1080",
                  foreground="gray", font=("", 8)).grid(row=2, column=1, sticky="w", **pad)
        ttk.Label(tab_px, text="代理账号（可选）").grid(row=3, column=0, sticky="w", **pad)
        ttk.Entry(tab_px, textvariable=self.px_user, width=30).grid(row=3, column=1, sticky="w", **pad)
        ttk.Label(tab_px, text="代理密码（可选）").grid(row=4, column=0, sticky="w", **pad)
        ttk.Entry(tab_px, textvariable=self.px_pass, width=30, show="*").grid(row=4, column=1, sticky="w", **pad)
        ttk.Label(tab_px, text="说明：只有任务配置里勾选了「本任务走代理」的任务才使用此代理；\n"
                              "未勾选的任务强制直连（不会走此代理，也不会走系统代理）。",
                  foreground="gray", font=("", 8), justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)

        # --- 定时 ---
        tab_sc = ttk.Frame(nb, padding=8)
        nb.add(tab_sc, text="定时运行")
        self.sc_enabled = tk.BooleanVar()
        self.sc_time = tk.StringVar()
        self.sc_skip_signed = tk.BooleanVar()
        self.sc_catch_up = tk.BooleanVar()
        ttk.Checkbutton(tab_sc, text="启用每日定时运行（程序开着时到点自动签到）",
                        variable=self.sc_enabled).grid(
            row=0, column=0, columnspan=2, sticky="w", **pad)
        ttk.Label(tab_sc, text="每天运行时间").grid(row=1, column=0, sticky="w", **pad)
        time_row = ttk.Frame(tab_sc)
        time_row.grid(row=1, column=1, sticky="w", **pad)
        ttk.Entry(time_row, textvariable=self.sc_time, width=8).pack(side="left")
        ttk.Label(time_row, text="（24 小时制，如 09:30、21:05）",
                  foreground="gray", font=("", 8)).pack(side="left", padx=6)
        ttk.Checkbutton(tab_sc, text="跳过今天已经签到的任务",
                        variable=self.sc_skip_signed).grid(
            row=2, column=0, columnspan=2, sticky="w", **pad)
        ttk.Checkbutton(tab_sc, text="错过时间点也补跑（例如 10 点才打开程序，仍会跑一次当天）",
                        variable=self.sc_catch_up).grid(
            row=3, column=0, columnspan=2, sticky="w", **pad)

        # 定时运行哪些任务（与主界面勾选无关）
        self.sc_task_vars: dict[str, tk.BooleanVar] = {}
        picker = ttk.Labelframe(tab_sc, text="定时运行的任务（与主界面勾选无关）", padding=6)
        picker.grid(row=4, column=0, columnspan=2, sticky="nsew", **pad)
        canvas = tk.Canvas(picker, height=148, highlightthickness=0)
        sb = ttk.Scrollbar(picker, orient="vertical", command=canvas.yview)
        inner = ttk.Frame(canvas)
        inner.bind("<Configure>",
                   lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=inner, anchor="nw")
        canvas.configure(yscrollcommand=sb.set)
        canvas.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")

        def _on_wheel(event):
            """鼠标滚轮上下滚动任务列表（滚到顶/底时把事件交回上级，不卡住）"""
            delta = int(getattr(event, "delta", 0) or 0)
            if delta == 0:
                return "break"
            step = -1 if delta > 0 else 1
            units = max(1, abs(delta) // 120)
            first, last = canvas.yview()
            if (step < 0 and first <= 0.0) or (step > 0 and last >= 1.0):
                return "break"          # 已到边界：吃掉事件，别让外层容器跟着动
            canvas.yview_scroll(step * units, "units")
            return "break"

        def _bind_wheel(widget):
            widget.bind("<MouseWheel>", _on_wheel)
            widget.bind("<Button-4>", lambda e: canvas.yview_scroll(-1, "units"))
            widget.bind("<Button-5>", lambda e: canvas.yview_scroll(1, "units"))
            for child in widget.winfo_children():
                _bind_wheel(child)

        try:
            all_configs = load_task_configs()
        except Exception:
            all_configs = []
        for cfg in all_configs:
            var = tk.BooleanVar(value=False)
            label = cfg.name if cfg.enabled else f"{cfg.name}（未启用）"
            ttk.Checkbutton(inner, text=label, variable=var).pack(anchor="w")
            self.sc_task_vars[cfg.id] = var
        if not all_configs:
            ttk.Label(inner, text="config/tasks/ 下没有任务配置",
                      foreground="gray").pack(anchor="w")

        # 滚轮绑到容器、画布、内部框架和每个复选框上（鼠标停在谁上面都能滚）
        for w in (picker, canvas, inner, sb):
            _bind_wheel(w)
        row_btns = ttk.Frame(picker)
        row_btns.pack(side="bottom", fill="x", pady=(4, 0))
        ttk.Button(row_btns, text="全选", width=6,
                   command=lambda: self._set_all_sc_tasks(True)).pack(side="left")
        ttk.Button(row_btns, text="全不选", width=7,
                   command=lambda: self._set_all_sc_tasks(False)).pack(side="left", padx=6)
        ttk.Label(row_btns, text="（全选＝跟随任务启用状态）",
                  foreground="gray", font=("", 8)).pack(side="left")

        ttk.Label(tab_sc,
                  text="说明：\n"
                       "· 定时运行**只跑上面勾选的任务**，与主界面勾选无关；全选＝跑所有「启用」的任务。\n"
                       "· 勾选「跳过今天已经签到的任务」可避免白跑。\n"
                       "· 每天只自动跑一次；跑完会在状态栏显示下次运行时间。\n"
                       "· 程序需要保持打开（可最小化）；关掉程序就不会触发。\n"
                       "· 手动点「▶ 运行选中 / 运行全部启用」不受定时设置影响。",
                  foreground="gray", font=("", 8), justify="left").grid(
            row=5, column=0, columnspan=2, sticky="w", **pad)

        btns = ttk.Frame(self)
        btns.pack(fill="x", padx=8, pady=(0, 8))
        ttk.Button(btns, text="保存", command=self._save).pack(side="right", padx=4)
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)

    def _set_all_sc_tasks(self, flag: bool) -> None:
        """定时任务列表：全选 / 全不选"""
        for var in getattr(self, "sc_task_vars", {}).values():
            var.set(bool(flag))

    def _load(self):
        c = self.cfg
        self.ai_enabled.set(c.ai.enabled)
        self.ai_base.set(c.ai.base_url)
        self.ai_key.set(c.ai.api_key)
        self.ai_model.set(c.ai.model)
        self.ai_vision.set(c.ai.vision_model)
        self.ai_proactive.set(c.ai.proactive)
        self.ai_agent_fallback.set(getattr(c.ai, "agent_fallback", True))
        self.cc_enabled.set(c.cookiecloud.enabled)
        self.cc_mode.set(c.cookiecloud.mode if c.cookiecloud.mode in ("overwrite", "paused") else "overwrite")
        self.cc_server.set(c.cookiecloud.server)
        self.cc_uuid.set(c.cookiecloud.uuid)
        self.cc_pass.set(c.cookiecloud.password)
        self.cc_domains.set(",".join(c.cookiecloud.domains or []))
        self.br_mode.set(c.browser.mode)
        self.br_port.set(c.browser.debugging_port)
        self.br_headless.set(c.browser.headless)
        self.br_slow.set(c.browser.slow_mo)
        self.br_fullscreen.set(c.browser.fullscreen)
        self.br_topmost.set(c.browser.always_on_top)
        self.px_enabled.set(c.proxy.enabled)
        self.px_server.set(c.proxy.server)
        self.px_user.set(c.proxy.username)
        self.px_pass.set(c.proxy.password)
        self.sc_enabled.set(bool(getattr(c, "schedule", None) and c.schedule.enabled))
        self.sc_time.set(getattr(getattr(c, "schedule", None), "time", "09:30") or "09:30")
        self.sc_skip_signed.set(bool(getattr(getattr(c, "schedule", None), "skip_signed", True)))
        self.sc_catch_up.set(bool(getattr(getattr(c, "schedule", None), "catch_up", True)))
        saved = normalize_tasks(getattr(getattr(c, "schedule", None), "tasks", []))
        chosen = set(saved) if saved else set(self.sc_task_vars)   # 空=全部
        for tid, var in self.sc_task_vars.items():
            var.set(tid in chosen if chosen else True)

    def _save(self):
        c = self.cfg
        c.ai.enabled = bool(self.ai_enabled.get())
        c.ai.base_url = self.ai_base.get().strip()
        c.ai.api_key = self.ai_key.get().strip()
        c.ai.model = self.ai_model.get().strip()
        c.ai.vision_model = self.ai_vision.get().strip()
        c.ai.proactive = bool(self.ai_proactive.get())
        c.ai.agent_fallback = bool(self.ai_agent_fallback.get())
        c.cookiecloud.enabled = bool(self.cc_enabled.get())
        c.cookiecloud.mode = self.cc_mode.get() or "overwrite"
        c.cookiecloud.server = self.cc_server.get().strip()
        c.cookiecloud.uuid = self.cc_uuid.get().strip()
        c.cookiecloud.password = self.cc_pass.get()
        c.cookiecloud.domains = [d.strip() for d in self.cc_domains.get().split(",") if d.strip()]
        c.browser.mode = self.br_mode.get() or "launch"
        c.browser.headless = bool(self.br_headless.get())
        c.browser.fullscreen = bool(self.br_fullscreen.get())
        c.browser.always_on_top = bool(self.br_topmost.get())
        c.proxy.enabled = bool(self.px_enabled.get())
        c.proxy.server = self.px_server.get().strip()
        c.proxy.username = self.px_user.get().strip()
        c.proxy.password = self.px_pass.get()
        # 定时运行：启用时必须填合法时间（HH:MM）
        sc_time = normalize_hhmm(self.sc_time.get(), default="")
        if not sc_time:
            if self.sc_enabled.get():
                messagebox.showerror("时间格式不对", "每天运行时间请填 HH:MM，例如 09:30")
                return
            sc_time = "09:30"
        c.schedule.enabled = bool(self.sc_enabled.get())
        c.schedule.time = sc_time
        c.schedule.skip_signed = bool(self.sc_skip_signed.get())
        c.schedule.catch_up = bool(self.sc_catch_up.get())
        selected = [tid for tid, var in self.sc_task_vars.items() if var.get()]
        if self.sc_enabled.get() and self.sc_task_vars and not selected:
            messagebox.showerror("没有选择任务",
                                 "定时运行启用时需要至少勾选一个要运行的任务。")
            return
        # 全选 = 跟随任务启用状态（以后新增的任务也会自动包含）
        if self.sc_task_vars and len(selected) == len(self.sc_task_vars):
            selected = []
        c.schedule.tasks = selected
        try:
            c.browser.debugging_port = int(self.br_port.get() or 9222)
            c.browser.slow_mo = int(self.br_slow.get() or 50)
        except (tk.TclError, ValueError):
            pass
        try:
            path = c.save()
            LOGGER.info(f"全局设置已保存: {path}")
            if self.on_saved:
                self.on_saved()
            self.destroy()
        except Exception as e:
            messagebox.showerror("保存失败", str(e))


class SigninApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("网站签到助手")
        self.root.geometry("1000x660")
        self.root.minsize(820, 500)
        self.checks: dict[str, tk.BooleanVar] = {}
        self._running = False
        self._day = today_str()
        self._build()
        self.root.after(30000, self._watch_day)
        self.root.after(5000, self._schedule_tick)

    def _watch_day(self):
        """跨天自动刷新：界面开着过夜也能把「已签到」变成「未签到」"""
        try:
            cur = today_str()
            if cur != self._day:
                self._day = cur
                LOGGER.info(f"日期已变为 {cur}，刷新签到状态显示")
                self.refresh_tasks()
            self.root.after(30000, self._watch_day)
        except tk.TclError:
            pass          # 窗口已关闭

    # ---------- 每日定时运行 ----------
    def _schedule_tick(self):
        """每 20 秒检查一次是否到了定时运行时间"""
        try:
            self._update_schedule_label()
            if not self._running and self._scheduled_due():
                self._run_scheduled()
            self.root.after(20000, self._schedule_tick)
        except tk.TclError:
            pass          # 窗口已关闭

    def _scheduled_due(self) -> bool:
        try:
            sc = AppConfig.load().schedule
        except Exception as e:
            LOGGER.warning(f"读取定时配置失败: {e}")
            return False
        if not sc.enabled:
            return False
        return should_run(datetime.now(), sc.time, schedule_last_run(), sc.catch_up)

    def _run_scheduled(self):
        """到点触发：只跑「定时运行」页里勾选的任务，可按配置跳过今天已签到的"""
        try:
            sc = AppConfig.load().schedule
        except Exception as e:
            LOGGER.error(f"定时运行读取配置失败: {e}")
            return
        configs = load_task_configs()
        ids, skipped = pick_scheduled(sc.tasks, [(c.id, c.enabled) for c in configs])
        for note in skipped:
            LOGGER.warning(f"定时运行跳过 {note}")
        if sc.skip_signed:
            ids = [tid for tid in ids if not is_signed(tid)]
        if not ids:
            # 没有可跑的任务也要标记，避免每 20 秒重复判断
            set_schedule_last_run(note=f"定时 {sc.time}，无任务可跑")
            LOGGER.info(f"⏰ 定时运行（{sc.time}）：今天没有需要跑的任务（未选中或都已签到），跳过")
            self._update_schedule_label()
            return
        LOGGER.info(f"⏰ 定时运行触发（每天 {sc.time}）：本次 {len(ids)} 个任务 → {', '.join(ids)}")
        self.status.set(f"定时运行中（{sc.time}）：{len(ids)} 个任务")
        if not self._run_async(ids, silent=True):
            # 上一个队列还在跑：不写标记，20 秒后会再判断一次
            LOGGER.warning("定时运行未能启动（已有任务队列在执行），稍后重试")
            self._update_schedule_label()
            return
        set_schedule_last_run(note=f"定时 {sc.time}，待跑 {len(ids)} 个任务")

    def _update_schedule_label(self):
        try:
            sc = AppConfig.load().schedule
            text = describe(datetime.now(), sc.time, schedule_last_run(),
                            sc.enabled, sc.catch_up)
            if sc.enabled:
                configs = load_task_configs()
                ids, _ = pick_scheduled(sc.tasks, [(c.id, c.enabled) for c in configs])
                text += f"｜{len(ids)} 个任务"
        except Exception:
            text = "定时：读取配置失败"
        try:
            self.lbl_schedule.configure(text=text)
        except Exception:
            pass

    # ---------- UI ----------
    def _build(self):
        toolbar = ttk.Frame(self.root, padding=6)
        toolbar.pack(fill="x")
        ttk.Button(toolbar, text="刷新任务", command=self.refresh_tasks).pack(side="left", padx=2)
        ttk.Button(toolbar, text="重置登录失败次数",
                   command=self.reset_all_login_fail_of).pack(side="left", padx=2)
        self.btn_run_selected = ttk.Button(toolbar, text="▶ 运行选中", command=self.run_selected)
        self.btn_run_selected.pack(side="left", padx=2)
        self.btn_run_all = ttk.Button(toolbar, text="运行全部启用", command=self.run_all)
        self.btn_run_all.pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="全局设置", command=self.open_settings).pack(side="left", padx=2)
        ttk.Button(toolbar, text="导入2FA二维码", command=self.import_qr).pack(side="left", padx=2)
        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=6)
        ttk.Button(toolbar, text="测试AI", command=self.test_ai).pack(side="left", padx=2)
        ttk.Button(toolbar, text="测试CookieCloud", command=self.test_cookiecloud).pack(side="left", padx=2)
        ttk.Button(toolbar, text="打开日志目录", command=self.open_log_dir).pack(side="left", padx=2)

        paned = ttk.PanedWindow(self.root, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=6, pady=4)

        left = ttk.Labelframe(paned, text="签到任务（勾选后运行）", padding=6)
        self.task_frame = ttk.Frame(left)
        self.task_frame.pack(fill="both", expand=True)
        paned.add(left, weight=5)

        right = ttk.Labelframe(paned, text="运行日志（每次运行另存到 data/logs/run_*.log）", padding=6)
        self.log_text = tk.Text(right, state="disabled", wrap="word", font=("Consolas", 9))
        scroll = ttk.Scrollbar(right, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.log_text.pack(fill="both", expand=True)
        paned.add(right, weight=6)

        self.status = tk.StringVar(value="就绪")
        bar = ttk.Frame(self.root, relief="sunken")
        bar.pack(fill="x", side="bottom")
        ttk.Label(bar, textvariable=self.status, anchor="w", padding=4).pack(
            side="left", fill="x", expand=True)
        self.lbl_schedule = ttk.Label(bar, text="定时：未启用", anchor="e", padding=4,
                                      foreground="gray")
        self.lbl_schedule.pack(side="right")

        handler = TextHandler(self.log_text)
        LOGGER.addHandler(handler)
        LOGGER.setLevel(logging.INFO)

        self.refresh_tasks()

    # ---------- 任务列表 ----------
    def refresh_tasks(self):
        for w in self.task_frame.winfo_children():
            w.destroy()
        self.checks.clear()
        configs = load_task_configs()
        st = load_state()
        if not configs:
            ttk.Label(self.task_frame, text="config/tasks/ 下没有任务配置").pack(anchor="w")
            return
        for cfg in configs:
            info = st.get(cfg.id, {})
            rec_date = record_date(cfg.id)
            label = "已签到" if is_signed(cfg.id) else "未签到"
            # 已签到的默认不勾选（打开程序 / 刷新任务都是），避免白跑一遍
            var = tk.BooleanVar(value=cfg.enabled and label == "未签到")
            self.checks[cfg.id] = var
            row = ttk.Frame(self.task_frame)
            row.pack(fill="x", pady=3)
            ttk.Checkbutton(row, text=f"{cfg.name}({label})",
                            variable=var).pack(side="left")
            ttk.Button(row, text="配置", width=6,
                       command=lambda tid=cfg.id: self.edit_task(tid)).pack(side="left", padx=6)
            # 登录失败计数：跨天累计，只有登录成功才清零；用尽后任务会被跳过
            fail = login_fail_info(cfg.id)
            limit = cfg.max_login_retry
            if fail["count"]:
                used = fail["count"]
                if used >= limit:
                    ttk.Label(row, text=f"登录已锁定 {used}/{limit}",
                              foreground="#c62828").pack(side="left", padx=4)
                else:
                    ttk.Label(row, text=f"登录失败 {used}/{limit}",
                              foreground="#ef6c00").pack(side="left", padx=4)
                ttk.Button(row, text="重置登录次数", width=11,
                           command=lambda tid=cfg.id: self.reset_login_fail_of(tid)
                           ).pack(side="left", padx=4)
            if info.get("time"):
                # 昨天的记录标注为「上次」，避免被当成今天的签到结果
                prefix = "上次" if rec_date != today_str() else "今天"
                ttk.Label(row, text=f"{prefix}: {info['time']}  {_short_msg(info.get('message', ''))}",
                          foreground="gray").pack(side="left")
        # 2FA 凭证库概览
        try:
            vault = TwoFactorVault()
            if vault.accounts:
                ttk.Separator(self.task_frame).pack(fill="x", pady=6)
                ttk.Label(self.task_frame, text=f"2FA 凭证库（{len(vault.accounts)} 个账号）:",
                          font=("", 9, "bold")).pack(anchor="w")
                for a in vault.accounts:
                    bound = f" → 绑定[{a.bound_task}]" if a.bound_task else ""
                    ttk.Label(self.task_frame,
                              text=f"    {a.issuer or '(无issuer)'} / {a.name}{bound}",
                              foreground="gray", font=("", 8)).pack(anchor="w")
        except Exception:
            pass
        self.status.set(f"已加载 {len(configs)} 个任务")

    def edit_task(self, task_id: str):
        TaskEditDialog(self.root, task_id, on_saved=self.refresh_tasks)

    def reset_login_fail_of(self, task_id: str):
        """手动清空某任务的登录失败计数（解锁该任务）"""
        reset_login_fail(task_id)
        LOGGER.info(f"已重置任务 [{task_id}] 的登录失败次数")
        self.refresh_tasks()

    def reset_all_login_fail_of(self):
        """手动清空所有任务的登录失败计数"""
        cleared = reset_all_login_fail()
        if cleared:
            LOGGER.info(f"已重置 {len(cleared)} 个任务的登录失败次数: {', '.join(cleared)}")
        else:
            LOGGER.info("没有需要重置的登录失败计数")
        self.refresh_tasks()

    def open_settings(self):
        SettingsDialog(self.root, on_saved=self._after_settings_saved)

    def _after_settings_saved(self):
        self.refresh_tasks()
        self._update_schedule_label()

    # ---------- 运行 ----------
    def run_selected(self):
        ids = [tid for tid, var in self.checks.items() if var.get()]
        if not ids:
            messagebox.showinfo("提示", "请先勾选要运行的任务")
            return
        self._run_async(ids)

    def run_all(self):
        ids = [c.id for c in load_task_configs() if c.enabled]
        if not ids:
            messagebox.showinfo("提示", "没有启用的任务")
            return
        self._run_async(ids)

    def _run_async(self, task_ids: list[str], silent: bool = False) -> bool:
        """启动一个后台线程跑队列；已有队列在执行时返回 False。

        silent=True（定时触发）时不再弹模态框，避免打断用户操作。
        """
        if self._running:
            if not silent:
                messagebox.showinfo("提示", "任务队列正在执行中，请等待当前队列完成")
            return False
        self._running = True
        self._set_running_ui(True)
        self.status.set(f"队列执行中（共 {len(task_ids)} 个任务，串行执行）...")

        def worker():
            async def main():
                manager = TaskManager()
                try:
                    return await manager.run_tasks(task_ids)
                finally:
                    await manager.close()

            try:
                results = asyncio.run(main())
                ok = sum(1 for r in results if r.success)
                log_path = next((r.run_log for r in results if r.run_log), "")
                for r in results:
                    LOGGER.info(f"{'[成功]' if r.success else '[失败]'} [{r.task_id}] {r.message}")
                if log_path:
                    LOGGER.info(f"本次运行日志: {log_path}")
                self.root.after(0, lambda: self.status.set(
                    f"运行完成 {ok}/{len(results)} 成功" + (f" | 日志: {log_path}" if log_path else "")))
            except Exception as e:
                LOGGER.error(f"运行失败: {e}")
                err_text = f"运行失败: {e}"
                self.root.after(0, lambda t=err_text: self.status.set(t))
            finally:
                self.root.after(0, self._after_run_done)

        threading.Thread(target=worker, daemon=True).start()
        return True

    def _after_run_done(self):
        """一次运行（手动或定时）结束后：恢复按钮 + 刷新任务列表与定时状态"""
        self._set_running_ui(False)
        try:
            self.refresh_tasks()          # 刷新「已签到」状态与默认勾选
        except Exception as e:
            LOGGER.warning(f"运行结束后刷新任务列表失败: {e}")
        self._update_schedule_label()

    def _set_running_ui(self, running: bool) -> None:
        """运行中的界面状态：禁用运行按钮，避免重复点击造成并发"""
        self._running = running
        state = "disabled" if running else "normal"
        for btn in (getattr(self, "btn_run_selected", None), getattr(self, "btn_run_all", None)):
            if btn is not None:
                try:
                    btn.configure(state=state)
                except Exception:
                    pass

    # ---------- 工具 ----------
    def import_qr(self):
        path = filedialog.askopenfilename(
            title="选择谷歌验证器二维码图片",
            filetypes=[("图片", "*.png;*.jpg;*.jpeg;*.bmp")],
        )
        if not path:
            return

        def worker():
            from core.totp import load_from_qr
            try:
                accounts = load_from_qr(path)
                vault = TwoFactorVault()
                added, updated = vault.add_many(accounts)
                LOGGER.info(f"二维码导入成功：新增 {added}，更新 {updated}")
                for a in accounts:
                    LOGGER.info(f"  - {a.issuer or '(无issuer)'} / {a.account}")
                self.root.after(0, self.refresh_tasks)
                self.root.after(0, lambda: messagebox.showinfo(
                    "导入成功", f"共导入 {len(accounts)} 个 2FA 账号到凭证库。\n"
                                f"可在任务「配置」中选择绑定哪个验证器。"))
            except Exception as e:
                err_text = str(e)
                self.root.after(0, lambda t=err_text: messagebox.showerror("导入失败", t))

        threading.Thread(target=worker, daemon=True).start()

    def test_ai(self):
        def worker():
            from core.ai import AIClient
            ai = AIClient(AppConfig.load().ai)
            if not ai.available:
                self.root.after(0, lambda: messagebox.showwarning(
                    "AI 未配置", "请在「全局设置 → AI 模型」中填写 base_url / api_key / model"))
                return
            try:
                reply = ai.chat("请只回复两个字：可用")
                self.root.after(0, lambda: messagebox.showinfo("AI 测试", f"连接成功: {reply}"))
            except Exception as e:
                err_text = str(e)
                self.root.after(0, lambda t=err_text: messagebox.showerror("AI 测试失败", t))

        threading.Thread(target=worker, daemon=True).start()

    def test_cookiecloud(self):
        def worker():
            from core.cookiecloud import CookieCloudClient
            cfg = AppConfig.load().cookiecloud
            if not cfg.enabled:
                self.root.after(0, lambda: messagebox.showwarning(
                    "未启用", "请在「全局设置 → CookieCloud」中填写并启用"))
                return
            ok, msg = CookieCloudClient(cfg).test_connection()
            self.root.after(0, lambda: (messagebox.showinfo if ok else messagebox.showerror)(
                "CookieCloud 测试", msg))

        threading.Thread(target=worker, daemon=True).start()

    def open_log_dir(self):
        from core.config import LOG_DIR
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            import os
            os.startfile(str(LOG_DIR))  # noqa: S606  Windows 打开目录
        except Exception as e:
            messagebox.showerror("打开失败", str(e))


def run_gui():
    import os
    root = tk.Tk()
    SigninApp(root)
    # 便于打包冒烟测试：设置 SIGNIN_SMOKE_MS 后自动关闭
    smoke = os.environ.get("SIGNIN_SMOKE_MS")
    if smoke:
        try:
            root.after(int(smoke), root.destroy)
        except ValueError:
            pass
    root.mainloop()
