"""CodexMonitor — Codex CLI 5 小时与周额度迷你浮窗。

数据源：本机 Codex CLI。通过 `codex app-server --stdio` 的
account/rateLimits/read 查询实时额度；查询失败时回退解析
~/.codex/sessions 下的最新会话日志。查询与合并逻辑移植自 MIT 协议的
CC Meter（github.com/silverdolphin863/claude-code-meter），不再依赖 CC Meter。

实时请求默认沿用系统代理与当前环境变量（适合直连、TUN 模式或已开启系统代理的场景）；
如需自定义代理或调整外观/刷新间隔，直接编辑同目录 config.json（本地私有覆盖可放 config.local.json）。
本程序只用 python 标准库。用 pythonw 运行（无控制台窗口）。

拖动：按住卡片任意位置移动；右键：菜单（显示 / 隐藏 / 重置位置 / 强制刷新 / 退出）。
位置自动记忆到 position.json。
"""

import json
import os
import platform
import shutil
import subprocess
import sys
import time
import ctypes
import datetime
import math
import queue
import threading
from ctypes import wintypes

import tkinter as tk

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(HERE, "config.json")
LOCAL_CONFIG_FILE = os.path.join(HERE, "config.local.json")
POS_FILE = os.path.join(HERE, "position.json")
LOG_FILE = os.path.join(HERE, "log.txt")


def _load_config():
    """按 config.json → config.local.json 顺序合并加载配置。"""
    cfg = {}
    for path in (CONFIG_FILE, LOCAL_CONFIG_FILE):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except Exception:
            pass
    return cfg


_CFG = _load_config()


def _cfg_str(key, env_keys=(), default=""):
    for env_key in env_keys:
        if env_key in os.environ:
            return str(os.environ.get(env_key, "")).strip()
    val = _CFG.get(key, default)
    return str(default if val is None else val).strip()


def _cfg_num(key, default, cast, lo, hi):
    try:
        val = cast(_CFG.get(key, default))
        if math.isfinite(val):
            return min(hi, max(lo, val))
    except (TypeError, ValueError):
        pass
    return default


# ---------------- CONFIG ----------------
SCALE = _cfg_num("scale", 1.0, float, 0.5, 3.0)          # 整体缩放，1.0 ≈ 190x60px；想更小改 0.85
OPACITY = _cfg_num("opacity", 0.95, float, 0.2, 1.0)    # 窗口透明度 0~1
REFRESH_SECONDS = _cfg_num("refresh_seconds", 60, int, 10, 3600) # 轮询间隔（实时快照 5 分钟内直接用缓存）
TOPMOST_WATCHDOG_SECONDS = 5 # 置顶兜底巡检间隔；不依赖 WinEvent 钩子存活
CODEX_LIVE_TIMEOUT_SECONDS = 10    # codex app-server 单次查询超时
CODEX_LIVE_TTL_SECONDS = _cfg_num("live_ttl_seconds", 300, int, 30, 86400) # 实时快照缓存有效期（CC Meter 同款 5 分钟）
CODEX_LIVE_RETRY_SECONDS = 60      # 实时查询失败后的重试冷却
CODEX_LIVE_MAX_RETRY_SECONDS = 300 # 连续失败时的最大重试间隔
CODEX_LIVE_RETRY_ATTEMPTS = 2      # 瞬时 app-server 启动失败时最多尝试次数
CODEX_LIVE_RETRY_DELAY_SECONDS = 0.8
CODEX_LIVE_MAX_STALE_SECONDS = 900 # 快照过期上限，超过则回退会话日志
CODEX_LIVE_MAX_OUTPUT_BYTES = 1_048_576
CODEX_PROXY = _cfg_str("proxy", ("CODEX_MONITOR_PROXY", "CODEXMINI_CODEX_PROXY"), "")
MAX_LOG_BYTES = 128 * 1024
LOG_KEEP_BYTES = 64 * 1024
FAMILY = "Microsoft YaHei UI"
# ----------------------------------------

CODEX_SESSIONS = os.path.join(os.path.expanduser("~"), ".codex", "sessions")
_log_lock = threading.Lock()

BG, CARD = "#25262a", "#303137"
FG, MUT = "#f2f2f2", "#b6b8c0"
GREEN, AMBER, RED, TRACK = "#4ade80", "#f0b44d", "#f06a6a", "#44464e"

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


def log(msg):
    try:
        with _log_lock:
            if os.path.exists(LOG_FILE) and os.path.getsize(LOG_FILE) > MAX_LOG_BYTES:
                with open(LOG_FILE, "rb") as f:
                    f.seek(-LOG_KEEP_BYTES, os.SEEK_END)
                    tail = f.read()
                with open(LOG_FILE, "wb") as f:
                    f.write(b"[earlier log entries rotated]\n" + tail)
            stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {msg}\n")
    except Exception:
        pass


def _top_level_hwnd(tk_window):
    """Return Tk's native top-level HWND rather than its TkChild HWND."""
    child = int(tk_window.winfo_id())
    try:
        user32 = ctypes.windll.user32
        user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetAncestor.restype = wintypes.HWND
        root = user32.GetAncestor(child, 2)  # GA_ROOT
        return int(root or child)
    except Exception:
        return child


def _is_cloaked(hwnd):
    """DWM-cloaked windows (e.g. UWP apps parked on other virtual desktops)
    report IsWindowVisible()==True yet render nothing; never count them."""
    try:
        value = wintypes.DWORD(0)
        if ctypes.windll.dwmapi.DwmGetWindowAttribute(
                hwnd, 14, ctypes.byref(value),  # 14 == DWMWA_CLOAKED
                ctypes.sizeof(value)) == 0:
            return bool(value.value)
    except Exception:
        pass
    return False


def _reset_seconds(value):
    """兼容 epoch 秒/毫秒与 ISO 字符串，统一成 epoch 秒（CC Meter 同款）。"""
    try:
        n = float(value)
    except (TypeError, ValueError):
        n = None
    if n is not None and n > 0:
        return int(n // 1000) if n > 1_000_000_000_000 else int(n)
    try:
        dt = datetime.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return int(dt.timestamp())
    except (TypeError, ValueError):
        return 0


def _window_label(mins):
    if mins == 300:
        return "5-hour"
    if mins == 10080:
        return "Weekly"
    return f"{round(mins / 60)}-hour"


def _iso_utc(epoch_seconds):
    return datetime.datetime.fromtimestamp(
        epoch_seconds, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _codex_command():
    """定位 codex 可执行文件；npm 的 .cmd 转接层尽量换成原生 exe。"""
    path = shutil.which("codex")
    if not path:
        return None
    if path.lower().endswith((".cmd", ".bat")):
        npm_root = os.path.dirname(path)
        arch = "arm64" if platform.machine().lower() == "arm64" else "x64"
        triple = ("aarch64-pc-windows-msvc" if arch == "arm64"
                  else "x86_64-pc-windows-msvc")
        for candidate in (
                os.path.join(npm_root, "node_modules", "@openai", "codex",
                             "node_modules", "@openai", f"codex-win32-{arch}",
                             "vendor", triple, "bin", "codex.exe"),
                os.path.join(npm_root, "node_modules", "@openai", "codex",
                             "vendor", triple, "bin", "codex.exe")):
            if os.path.exists(candidate):
                return [candidate]
        return ["cmd.exe", "/c", path]
    return [path]


def _codex_env():
    """构造 codex 子进程环境变量；仅在显式配置 CODEX_PROXY 时覆盖代理。"""
    env = os.environ.copy()
    proxy = str(CODEX_PROXY or "").strip()
    if proxy:
        for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                    "http_proxy", "https_proxy", "all_proxy"):
            env[key] = proxy
        env["NO_PROXY"] = env["no_proxy"] = "localhost,127.0.0.1,::1"
    return env


def _terminate_process(proc):
    """Terminate a short-lived app-server without turning races into errors."""
    try:
        if proc.poll() is None:
            proc.kill()
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.wait(timeout=3)
    except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
        try:
            proc.kill()
        except (OSError, ProcessLookupError):
            pass
        try:
            proc.wait(timeout=1)
        except (OSError, ProcessLookupError, subprocess.TimeoutExpired):
            pass


def _rpc_error_detail(msg):
    """提取 JSON-RPC error 的可读说明；压成单行并截断，便于进日志。"""
    error = msg.get("error")
    if not isinstance(error, dict):
        return ""
    text = " ".join(str(error.get("message") or "").split())
    if not text:
        text = f"code {error.get('code')}"
    return text[:200]


def _request_codex_live_once(cmd):
    """app-server 协议：initialize → account/rateLimits/read（CC Meter 同款）。"""
    # pythonw 没有控制台，不压掉标志的话 codex.exe 会闪出一个黑窗。
    popen_kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    proc = subprocess.Popen(
        cmd + ["app-server", "--stdio"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, env=_codex_env(), **popen_kwargs,
    )

    def send(message):
        proc.stdin.write((json.dumps(message) + "\n").encode("utf-8"))
        proc.stdin.flush()

    done = threading.Event()
    outcome = {}
    stderr_state = {"bytes": 0, "sample": ""}

    def read_stderr():
        try:
            for raw in proc.stderr:
                stderr_state["bytes"] += len(raw)
                if not stderr_state["sample"]:
                    text = " ".join(raw.decode(
                        "utf-8", "replace").split())
                    if text:
                        stderr_state["sample"] = text[:240]
        except (OSError, ValueError):
            pass

    stderr_thread = threading.Thread(target=read_stderr, daemon=True)
    stderr_thread.start()

    def reader():
        stdout_bytes = 0
        for raw in proc.stdout:
            stdout_bytes += len(raw)
            if stdout_bytes > CODEX_LIVE_MAX_OUTPUT_BYTES:
                outcome["error"] = "codex app-server 响应过大"
                done.set()
                return
            line = raw.decode("utf-8", "replace").strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("id") == 1:
                if msg.get("error"):
                    detail = _rpc_error_detail(msg)
                    outcome["error"] = (f"codex 初始化失败：{detail}"
                                        if detail else "codex 初始化失败")
                    done.set()
                    return
                try:
                    send({"method": "initialized"})
                    send({"id": 2, "method": "account/rateLimits/read",
                          "params": None})
                except OSError:
                    outcome["error"] = "codex 会话中断"
                    done.set()
                    return
            elif msg.get("id") == 2:
                if msg.get("error"):
                    detail = _rpc_error_detail(msg)
                    outcome["error"] = (f"codex 额度请求失败：{detail}"
                                        if detail else "codex 额度请求失败")
                else:
                    result = msg.get("result") or {}
                    rl = (result.get("rateLimits") or
                          (result.get("rateLimitsByLimitId") or {}).get("codex"))
                    if rl:
                        outcome["rate_limits"] = rl
                    else:
                        outcome["error"] = "codex 未返回账户额度"
                done.set()
                return
        if not done.is_set():
            outcome["error"] = "codex app-server 提前退出"
            done.set()

    reader_thread = threading.Thread(target=reader, daemon=True)
    reader_thread.start()

    def cleanup():
        try:
            proc.stdin.close()
        except OSError:
            pass
        _terminate_process(proc)
        reader_thread.join(timeout=0.5)
        stderr_thread.join(timeout=0.5)

    try:
        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "codex-monitor", "version": "1.0"}}})
    except OSError as e:
        cleanup()
        raise RuntimeError("无法启动 codex app-server") from e
    if not done.wait(CODEX_LIVE_TIMEOUT_SECONDS):
        cleanup()
        raise RuntimeError("codex app-server 超时")
    cleanup()
    if "error" in outcome:
        if stderr_state["sample"]:
            log("codex stderr: " + stderr_state["sample"])
        raise RuntimeError(outcome["error"])
    return outcome["rate_limits"]


def _request_codex_live():
    """Query live limits, retrying only transient app-server startup failures."""
    cmd = _codex_command()
    if not cmd:
        raise RuntimeError("codex CLI not found")
    last_error = None
    for attempt in range(CODEX_LIVE_RETRY_ATTEMPTS):
        try:
            return _request_codex_live_once(cmd)
        except RuntimeError as e:
            last_error = e
            transient = any(text in str(e) for text in (
                "提前退出", "无法启动", "会话中断", "超时"))
            if not transient or attempt + 1 >= CODEX_LIVE_RETRY_ATTEMPTS:
                raise
            time.sleep(CODEX_LIVE_RETRY_DELAY_SECONDS)
    raise last_error or RuntimeError("codex live query failed")


def _snapshot_section(rate_limits, stale_ms):
    now_ms = time.time() * 1000
    limits = []
    for key in ("primary", "secondary"):
        w = rate_limits.get(key) or {}
        percent = w.get("usedPercent", w.get("used_percent"))
        mins = w.get("windowDurationMins", w.get("window_minutes"))
        resets = _reset_seconds(w.get("resetsAt", w.get("resets_at")))
        try:
            percent, mins = float(percent), float(mins)
        except (TypeError, ValueError):
            continue
        if (not math.isfinite(percent) or not math.isfinite(mins) or
                mins <= 0 or not resets or resets * 1000 <= now_ms):
            continue
        percent = min(100.0, max(0.0, percent))
        limits.append({
            "label": _window_label(int(mins)),
            "percent": round(percent),
            "resets_at": _iso_utc(resets),
            "window_hours": int(mins // 60),
        })
    return {
        "limits": limits,
        "stale_ms": int(stale_ms * 1000),
        "source": "live",
        "error": None if limits else "no active Codex rate-limit window",
    }


_scan_cache = {"at": 0.0, "files": []}


def _newest_session_files(count=8):
    now = time.time()
    if _scan_cache["files"] and now - _scan_cache["at"] < 30:
        return _scan_cache["files"]
    found = []
    for dirpath, _dirs, names in os.walk(CODEX_SESSIONS):
        for name in names:
            if name.endswith(".jsonl"):
                path = os.path.join(dirpath, name)
                try:
                    found.append((os.path.getmtime(path), path))
                except OSError:
                    continue
    found.sort(reverse=True)
    files = [path for _mtime, path in found[:count]]
    _scan_cache.update(at=now, files=files)
    return files


def _tail(path, nbytes=1_000_000):
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - nbytes))
        return f.read().decode("utf-8", "replace")


def _codex_section_from_logs():
    """实时查询失败时的兜底：解析最近会话日志里的 rate_limits。

    并发的多个 Codex 会话各自上报同一个账户池，且新会话可能先写一个
    全零块，所以按"重置时间共识"合并（票数多者胜、平票取最新），
    窗口内取最大百分比 —— 直接沿用 CC Meter 的策略。
    """
    out = {"limits": [], "stale_ms": None, "error": None, "source": "logs"}
    if not os.path.isdir(CODEX_SESSIONS):
        out["error"] = "Codex not detected"
        return out
    cands = []
    for path in _newest_session_files():
        try:
            lines = _tail(path).split("\n")
        except OSError:
            continue
        for line in reversed(lines):
            if '"rate_limits"' not in line:
                continue
            try:
                o = json.loads(line)
            except ValueError:
                continue
            rl = (o.get("payload") or {}).get("rate_limits")
            if not rl:
                continue
            try:
                at = datetime.datetime.fromisoformat(
                    str(o.get("timestamp", "")).replace("Z", "+00:00")
                ).timestamp()
            except (TypeError, ValueError):
                at = 0.0
            cands.append({"at": at, "rl": rl})
            break
    if not cands:
        out["error"] = "近期会话日志中没有 rate_limits"
        return out
    cands.sort(key=lambda c: c["at"], reverse=True)
    out["stale_ms"] = max(0, int((time.time() - cands[0]["at"]) * 1000))

    now_ms = time.time() * 1000
    flat = []
    for c in cands:
        for key in ("primary", "secondary"):
            w = c["rl"].get(key) or {}
            try:
                pct, mins = float(w.get("used_percent")), float(w.get("window_minutes"))
            except (TypeError, ValueError):
                continue
            resets = _reset_seconds(w.get("resets_at"))
            if (not math.isfinite(pct) or not math.isfinite(mins) or
                    mins <= 0 or not resets or resets * 1000 <= now_ms):
                continue
            flat.append((int(mins), resets, min(100.0, max(0.0, pct)), c["at"]))
    if not flat:
        out["error"] = "近期会话日志中没有当前有效的额度窗口"
        return out

    windows = {}
    for mins in {e[0] for e in flat}:
        groups = {}
        for m, resets, pct, at in flat:
            if m != mins:
                continue
            g = groups.setdefault(resets, {"count": 0, "pct": 0.0, "at": 0.0})
            g["count"] += 1
            g["pct"] = max(g["pct"], pct)
            g["at"] = max(g["at"], at)
        resets, g = sorted(groups.items(),
                           key=lambda kv: (-kv[1]["count"], -kv[1]["at"]))[0]
        windows[mins] = (resets, g)
    for mins, (resets, g) in sorted(windows.items()):
        out["limits"].append({
            "label": _window_label(mins),
            "percent": round(g["pct"]),
            "resets_at": _iso_utc(resets),
            "window_hours": mins // 60,
        })
    return out


def _section_to_result(sec):
    """把额度窗口换算成 UI 数据；与旧版 CC Meter 接口解析完全一致。"""
    try:
        limits = {l.get("label"): l for l in sec.get("limits") or []}
        five, week = limits.get("5-hour"), limits.get("Weekly")
        if not five:
            return None, sec.get("source"), sec.get("stale_ms"), sec.get("error")
        reset = datetime.datetime.fromisoformat(
            five["resets_at"].replace("Z", "+00:00")).astimezone()
        delta = reset - datetime.datetime.now().astimezone()
        mins = max(0, int(delta.total_seconds() // 60))
        if mins >= 1440:
            left = f"{mins // 1440}天{mins % 1440 // 60}小时"
        elif mins >= 60:
            left = f"{mins // 60}小时{mins % 60:02d}分"
        else:
            left = f"{mins}分钟"
        week_txt = f"周 {100 - week['percent']}%" if week else ""
        week_left = None
        if week:
            wreset = datetime.datetime.fromisoformat(
                week["resets_at"].replace("Z", "+00:00")).astimezone()
            wmins = max(0, int((wreset - datetime.datetime.now().astimezone()
                                ).total_seconds() // 60))
            week_left = f"周（剩 {format_week_remaining(wmins)}）"
        return ({
            "remaining": 100 - int(five["percent"]),
            "reset_hm": reset.strftime("%H:%M"),
            "left": left,
            "week_txt": week_txt,
            "week_left": week_left,
            "source": sec.get("source") or "live",
            "stale_ms": sec.get("stale_ms"),
            "refresh_error": sec.get("refresh_error"),
        }, None, None, None)
    except Exception as e:
        log("parse section: " + repr(e))
        return None, sec.get("source"), sec.get("stale_ms"), repr(e)


_live_lock = threading.Lock()
_live_cache = {
    "snapshot": None,
    "at": 0.0,
    "error": None,
    "error_at": 0.0,
    "retry_seconds": CODEX_LIVE_RETRY_SECONDS,
}


def fetch(force=False):
    """查询 Codex 额度；此函数只在后台线程中运行。"""
    now = time.time()
    with _live_lock:
        snapshot = _live_cache["snapshot"]
        cached_at = _live_cache["at"]
        error, error_at = _live_cache["error"], _live_cache["error_at"]
        retry_seconds = _live_cache.get("retry_seconds", CODEX_LIVE_RETRY_SECONDS)
    age = now - cached_at

    def cached_result(refresh_error=None):
        sec = _snapshot_section(snapshot, time.time() - cached_at)
        sec["source"] = "cache"
        sec["refresh_error"] = refresh_error
        return _section_to_result(sec)

    def log_fallback(refresh_error=None):
        sec = _codex_section_from_logs()
        sec["refresh_error"] = refresh_error
        return _section_to_result(sec)

    if snapshot and age < CODEX_LIVE_TTL_SECONDS and not force:
        return cached_result("上次实时刷新失败" if error else None)
    # A manual refresh is an explicit request to retry immediately; do not
    # let the automatic retry cooldown turn it into a stale log fallback.
    if error and not force and now - error_at < retry_seconds:
        if snapshot and age < CODEX_LIVE_MAX_STALE_SECONDS:
            return cached_result("上次实时刷新失败")
        return log_fallback("上次实时刷新失败")
    try:
        rate_limits = _request_codex_live()
    except Exception as e:
        log("live query: " + repr(e))
        with _live_lock:
            _live_cache["error"] = repr(e)
            _live_cache["error_at"] = time.time()
            previous_retry = _live_cache.get(
                "retry_seconds", CODEX_LIVE_RETRY_SECONDS)
            _live_cache["retry_seconds"] = min(
                CODEX_LIVE_MAX_RETRY_SECONDS,
                max(CODEX_LIVE_RETRY_SECONDS, previous_retry * 2),
            )
        if snapshot and time.time() - cached_at < CODEX_LIVE_MAX_STALE_SECONDS:
            return cached_result("本次实时刷新失败")
        return log_fallback("本次实时刷新失败")
    with _live_lock:
        _live_cache["snapshot"] = rate_limits
        _live_cache["at"] = time.time()
        _live_cache["error"] = None
        _live_cache["error_at"] = 0.0
        _live_cache["retry_seconds"] = CODEX_LIVE_RETRY_SECONDS
    return _section_to_result(_snapshot_section(rate_limits, 0.0))


def color(remaining):
    return GREEN if remaining > 50 else (AMBER if remaining > 20 else RED)


def format_week_remaining(minutes):
    """Format the weekly reset countdown without a padded day field."""
    if minutes >= 1440:
        days, rest = divmod(minutes, 1440)
        hours, mins = divmod(rest, 60)
        return f"{days}天{hours:02d}小时{mins:02d}分"
    hours, mins = divmod(minutes, 60)
    return f"{hours}小时{mins:02d}分"


class LowLevelMouseData(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_size_t),
    ]


class TrayIcon:
    """Small native Windows notification-area icon; no third-party dependency."""

    WM_APP = 0x8000
    WM_CLOSE = 0x0010
    WM_DESTROY = 0x0002
    WM_LBUTTONUP = 0x0202
    WM_LBUTTONDBLCLK = 0x0203
    WM_RBUTTONUP = 0x0205
    WM_CONTEXTMENU = 0x007B
    EVENT_SYSTEM_FOREGROUND = 0x0003
    EVENT_SYSTEM_MOVESIZEEND = 0x000B
    EVENT_SYSTEM_SWITCHEND = 0x0015
    EVENT_SYSTEM_MINIMIZEEND = 0x0017
    EVENT_SYSTEM_DESKTOPSWITCH = 0x0020
    EVENT_OBJECT_REORDER = 0x8004
    WINEVENT_OUTOFCONTEXT = 0x0000
    NIM_ADD = 0x00000000
    NIM_DELETE = 0x00000002
    NIF_MESSAGE = 0x00000001
    NIF_ICON = 0x00000002
    NIF_TIP = 0x00000004

    # Native popup-menu support (TrackPopupMenu closes itself on outside click)
    MF_SEPARATOR = 0x00000800
    TPM_RIGHTBUTTON = 0x0002
    TPM_RETURNCMD = 0x0100
    CMD_SHOW = 1001
    CMD_HIDE = 1002
    CMD_REFRESH = 1003
    CMD_QUIT = 1004
    CMD_RESET_POSITION = 1005

    class NOTIFYICONDATAW(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT),
            ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT),
            ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128),
            ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD),
            ("szInfo", wintypes.WCHAR * 256),
            ("uTimeout", wintypes.UINT),
            ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD),
            ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    class WNDCLASSW(ctypes.Structure):
        _fields_ = [
            ("style", wintypes.UINT),
            ("lpfnWndProc", ctypes.c_void_p),
            ("cbClsExtra", ctypes.c_int),
            ("cbWndExtra", ctypes.c_int),
            ("hInstance", wintypes.HANDLE),
            ("hIcon", wintypes.HICON),
            ("hCursor", wintypes.HANDLE),
            ("hbrBackground", wintypes.HANDLE),
            ("lpszMenuName", wintypes.LPCWSTR),
            ("lpszClassName", wintypes.LPCWSTR),
        ]

    class ICONINFO(ctypes.Structure):
        _fields_ = [
            ("fIcon", wintypes.BOOL),
            ("xHotspot", wintypes.DWORD),
            ("yHotspot", wintypes.DWORD),
            ("hbmMask", wintypes.HBITMAP),
            ("hbmColor", wintypes.HBITMAP),
        ]

    def __init__(self, events, watched_hwnd, watched_child_hwnd=None):
        self.events = events
        self.watched_hwnd = watched_hwnd
        self.watched_child_hwnd = watched_child_hwnd
        self.hwnd = None
        self.stopping = False
        self.class_name = f"CodexMonitorTray_{os.getpid()}"
        self.hicon = None
        self.event_hooks = []
        self._topmost_pending = threading.Event()
        self._window_api_ready = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _ensure_window_api(self, user32):
        if self._window_api_ready:
            return
        user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        user32.GetWindow.restype = wintypes.HWND
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        self._window_api_ready = True

    def _queue_topmost(self):
        if self._topmost_pending.is_set():
            return
        self._topmost_pending.set()
        self.events.put_nowait("topmost")

    def consume_topmost_event(self):
        self._topmost_pending.clear()

    @staticmethod
    def _make_icon(user32, gdi32, hinstance):
        """Create a 32x32 white circle + dark 6-petal knot tray icon (4x4 supersampled)."""
        width = height = 32
        ss = 4
        color = bytearray(width * height * 4)
        mask_stride = ((width + 15) // 16) * 2
        mask = bytearray(mask_stride * height)

        knot_segs = []
        for k in range(6):
            a0 = math.radians(k * 60 - 90)
            a1 = math.radians(k * 60 - 90 + 60)
            x0, y0 = 16.0 + 4.0 * math.cos(a0), 16.0 + 4.0 * math.sin(a0)
            x1, y1 = 16.0 + 10.2 * math.cos(a0 + 0.52), 16.0 + 10.2 * math.sin(a0 + 0.52)
            x2, y2 = 16.0 + 10.2 * math.cos(a1 + 0.52), 16.0 + 10.2 * math.sin(a1 + 0.52)
            knot_segs.append((x0, y0, x1, y1))
            knot_segs.append((x1, y1, x2, y2))

        def dist_seg(px, py, x1, y1, x2, y2):
            sx, sy = x2 - x1, y2 - y1
            l2 = sx * sx + sy * sy
            t = max(0.0, min(1.0, ((px - x1) * sx + (py - y1) * sy) / l2))
            return math.hypot(px - (x1 + t * sx), py - (y1 + t * sy))

        samples = ss * ss
        for y in range(height):
            for x in range(width):
                r_sum = g_sum = b_sum = a_sum = 0.0
                for sy in range(ss):
                    v = y + (sy + 0.5) / ss
                    dy = v - 16.0
                    for sx in range(ss):
                        u = x + (sx + 0.5) / ss
                        dx = u - 16.0
                        if dx * dx + dy * dy <= 14.8 * 14.8:
                            d = min(dist_seg(u, v, *seg) for seg in knot_segs)
                            if d <= 1.25:
                                cr, cg, cb = 17.0, 24.0, 39.0
                            else:
                                cr, cg, cb = 244.0, 244.0, 245.0
                            r_sum += cr
                            g_sum += cg
                            b_sum += cb
                            a_sum += 255.0
                alpha = int(round(a_sum / samples))
                pos = ((height - 1 - y) * width + x) * 4
                if alpha > 0:
                    denom = a_sum / 255.0
                    r = int(round(r_sum / denom))
                    g = int(round(g_sum / denom))
                    b = int(round(b_sum / denom))
                    # CreateBitmap expects BGRA bytes on Windows.
                    color[pos:pos + 4] = bytes((b, g, r, alpha))
                else:
                    mask[(height - 1 - y) * mask_stride + (x // 8)] |= 1 << (7 - (x % 8))
        color_buf = ctypes.create_string_buffer(bytes(color))
        mask_buf = ctypes.create_string_buffer(bytes(mask))
        hbm_color = gdi32.CreateBitmap(width, height, 1, 32, color_buf)
        hbm_mask = gdi32.CreateBitmap(width, height, 1, 1, mask_buf)
        if not hbm_color or not hbm_mask:
            return user32.LoadIconW(None, ctypes.c_void_p(32512))
        info = TrayIcon.ICONINFO(True, 0, 0, hbm_mask, hbm_color)
        icon = user32.CreateIconIndirect(ctypes.byref(info))
        gdi32.DeleteObject(hbm_color)
        gdi32.DeleteObject(hbm_mask)
        return icon or user32.LoadIconW(None, ctypes.c_void_p(32512))

    def _run(self):
        try:
            user32 = ctypes.windll.user32
            shell32 = ctypes.windll.shell32
            kernel32 = ctypes.windll.kernel32
            gdi32 = ctypes.windll.gdi32
            callback_message = self.WM_APP + 1
            self._ensure_window_api(user32)

            # Native popup menu signatures
            user32.CreatePopupMenu.restype = wintypes.HMENU
            user32.AppendMenuW.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR]
            user32.AppendMenuW.restype = wintypes.BOOL
            user32.TrackPopupMenu.argtypes = [
                wintypes.HMENU, wintypes.UINT, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, wintypes.HWND, ctypes.c_void_p]
            user32.TrackPopupMenu.restype = ctypes.c_int
            user32.DestroyMenu.argtypes = [wintypes.HMENU]
            user32.DestroyMenu.restype = wintypes.BOOL

            wnd_proc_type = ctypes.WINFUNCTYPE(
                ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT,
                ctypes.c_size_t, ctypes.c_ssize_t,
            )

            @wnd_proc_type
            def wnd_proc(hwnd, msg, wparam, lparam):
                if msg == callback_message:
                    lparam = int(lparam)
                    if lparam in (self.WM_LBUTTONUP, self.WM_LBUTTONDBLCLK):
                        self.events.put("show")
                    elif lparam in (self.WM_RBUTTONUP, self.WM_CONTEXTMENU):
                        self._popup_menu(user32)
                    return 0
                if msg == self.WM_DESTROY:
                    self._delete_icon(shell32, hwnd)
                    user32.PostQuitMessage(0)
                    return 0
                user32.DefWindowProcW.argtypes = [
                    wintypes.HWND, wintypes.UINT,
                    ctypes.c_size_t, ctypes.c_ssize_t,
                ]
                user32.DefWindowProcW.restype = ctypes.c_ssize_t
                return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

            win_event_type = ctypes.WINFUNCTYPE(
                None, wintypes.HANDLE, wintypes.DWORD,
                wintypes.HWND, wintypes.LONG, wintypes.LONG,
                wintypes.DWORD, wintypes.DWORD,
            )

            @win_event_type
            def win_event(_hook, event, _hwnd, _object_id, _child_id, _thread_id, _time):
                watched = self._is_watched_window(user32, _hwnd)
                if event in (
                        self.EVENT_SYSTEM_FOREGROUND,
                        self.EVENT_SYSTEM_MOVESIZEEND,
                        self.EVENT_SYSTEM_SWITCHEND,
                        self.EVENT_SYSTEM_MINIMIZEEND,
                        self.EVENT_SYSTEM_DESKTOPSWITCH):
                    self._queue_topmost()
                elif (event == self.EVENT_OBJECT_REORDER and
                      _object_id in (0, -4) and _child_id == 0 and
                      (watched or self._window_covers(user32, _hwnd))):
                    self._queue_topmost()

            self._wnd_proc = wnd_proc
            self._win_event = win_event
            hinstance = kernel32.GetModuleHandleW(None)
            wc = self.WNDCLASSW()
            wc.lpfnWndProc = ctypes.cast(wnd_proc, ctypes.c_void_p)
            wc.hInstance = hinstance
            wc.lpszClassName = self.class_name
            user32.RegisterClassW(ctypes.byref(wc))
            hwnd = user32.CreateWindowExW(
                0, self.class_name, "CodexMonitor Tray", 0,
                0, 0, 0, 0, None, None, hinstance, None,
            )
            if not hwnd:
                raise ctypes.WinError()
            self.hwnd = hwnd
            self.hicon = self._make_icon(user32, gdi32, hinstance)
            for event_id in (
                    self.EVENT_SYSTEM_FOREGROUND,
                    self.EVENT_SYSTEM_MOVESIZEEND,
                    self.EVENT_SYSTEM_SWITCHEND,
                    self.EVENT_SYSTEM_MINIMIZEEND,
                    self.EVENT_SYSTEM_DESKTOPSWITCH,
                    self.EVENT_OBJECT_REORDER):
                hook = user32.SetWinEventHook(
                    event_id, event_id, None, self._win_event,
                    0, 0, self.WINEVENT_OUTOFCONTEXT)
                if hook:
                    self.event_hooks.append(hook)
            if not self.event_hooks:
                raise ctypes.WinError()
            nid = self.NOTIFYICONDATAW()
            nid.cbSize = ctypes.sizeof(nid)
            nid.hWnd = hwnd
            nid.uID = 1
            nid.uFlags = self.NIF_MESSAGE | self.NIF_ICON | self.NIF_TIP
            nid.uCallbackMessage = callback_message
            nid.hIcon = self.hicon
            nid.szTip = "Codex 额度浮窗"
            if not shell32.Shell_NotifyIconW(self.NIM_ADD, ctypes.byref(nid)):
                raise ctypes.WinError()

            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as e:
            log("tray: " + repr(e))
        finally:
            for hook in self.event_hooks:
                try:
                    ctypes.windll.user32.UnhookWinEvent(hook)
                except Exception:
                    pass
            if self.hicon:
                try:
                    ctypes.windll.user32.DestroyIcon(self.hicon)
                except Exception:
                    pass
            self.hwnd = None
            self.event_hooks = []
            self.hicon = None

    def _popup_menu(self, user32):
        """Show a native Win32 popup menu. TrackPopupMenu runs its own modal
        loop and dismisses itself when the user clicks outside."""
        hmenu = user32.CreatePopupMenu()
        if not hmenu:
            return
        user32.AppendMenuW(hmenu, 0, self.CMD_SHOW, "显示浮窗")
        user32.AppendMenuW(hmenu, 0, self.CMD_HIDE, "隐藏浮窗")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_RESET_POSITION, "重置位置")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_REFRESH, "强制刷新")
        user32.AppendMenuW(hmenu, self.MF_SEPARATOR, 0, None)
        user32.AppendMenuW(hmenu, 0, self.CMD_QUIT, "退出")
        pt = wintypes.POINT()
        user32.GetCursorPos(ctypes.byref(pt))
        # Required so the menu receives the outside-click that dismisses it.
        user32.SetForegroundWindow(self.hwnd)
        cmd = user32.TrackPopupMenu(
            hmenu, self.TPM_RIGHTBUTTON | self.TPM_RETURNCMD,
            pt.x, pt.y, 0, self.hwnd, None,
        )
        user32.DestroyMenu(hmenu)
        mapping = {
            self.CMD_SHOW: "show",
            self.CMD_HIDE: "hide",
            self.CMD_RESET_POSITION: "reset",
            self.CMD_REFRESH: "refresh",
            self.CMD_QUIT: "exit",
        }
        evt = mapping.get(int(cmd))
        if evt:
            self.events.put(evt)

    @staticmethod
    def _rects_overlap(a, b):
        return (a.left < b.right and a.right > b.left and
                a.top < b.bottom and a.bottom > b.top)

    def _is_watched_window(self, user32, hwnd):
        if not hwnd:
            return False
        return int(hwnd) in {
            int(self.watched_hwnd or 0),
            int(self.watched_child_hwnd or 0),
        }

    def _window_covers(self, user32, other):
        """Whether `other`, sitting above the card in the z-order, hides it.

        Deliberately NOT requiring WS_EX_TOPMOST on `other`: a topmost window
        can keep the style bit while being knocked out of the topmost band
        (display topology/DPI changes, shell restarts, z-order tools), and
        plain windows then stack above it — that state must read as covered.
        """
        self._ensure_window_api(user32)
        if not other or not user32.IsWindowVisible(other):
            return False
        if _is_cloaked(other):
            return False
        rect_type = wintypes.RECT
        root_rect, other_rect = rect_type(), rect_type()
        if not (user32.GetWindowRect(self.watched_hwnd, ctypes.byref(root_rect)) and
                user32.GetWindowRect(other, ctypes.byref(other_rect))):
            return False
        if other_rect.left >= other_rect.right or other_rect.top >= other_rect.bottom:
            return False  # zero-size helper windows cannot hide anything
        return self._rects_overlap(root_rect, other_rect)

    def _has_covering_window(self, user32):
        """Check only the small set of windows above us in the z-order."""
        self._ensure_window_api(user32)
        root = self.watched_hwnd
        if not root:
            return False
        rect_type = wintypes.RECT
        root_rect = rect_type()
        if not user32.GetWindowRect(root, ctypes.byref(root_rect)):
            return False
        other = user32.GetWindow(root, 3)  # GW_HWNDPREV
        for _ in range(128):
            if not other:
                return False
            if self._window_covers(user32, other):
                return True
            other = user32.GetWindow(other, 3)
        return False

    def _delete_icon(self, shell32, hwnd):
        nid = self.NOTIFYICONDATAW()
        nid.cbSize = ctypes.sizeof(nid)
        nid.hWnd = hwnd
        nid.uID = 1
        try:
            shell32.Shell_NotifyIconW(self.NIM_DELETE, ctypes.byref(nid))
        except Exception:
            pass

    def has_covering_window(self):
        try:
            return self._has_covering_window(ctypes.windll.user32)
        except Exception:
            return False

    def stop(self):
        self.stopping = True
        hwnd = self.hwnd
        if hwnd:
            try:
                ctypes.windll.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)
            except Exception:
                pass


class MiniCard:
    def __init__(self):
        self.tk = tk.Tk()
        self.tk.overrideredirect(True)
        self.tk.attributes("-topmost", True)
        self.tk.attributes("-alpha", OPACITY)
        self.tk.configure(bg=BG)
        f1 = (FAMILY, max(8, int(11 * SCALE)), "bold")
        f2 = (FAMILY, max(7, int(9 * SCALE)))
        pad, barw = int(8 * SCALE), int(150 * SCALE)
        self.title = tk.Label(self.tk, text="5小时额度 …", fg=FG, bg=BG, font=f1)
        self.title.pack(anchor="w", padx=pad, pady=(int(4 * SCALE), 0))
        self.bar = tk.Canvas(self.tk, width=barw, height=int(5 * SCALE),
                             bg=BG, highlightthickness=0)
        self.bar.pack(anchor="w", padx=pad, pady=int(2 * SCALE))
        self.barw = barw
        self.reset = tk.Label(self.tk, text="…", fg=MUT, bg=BG, font=f2)
        self.reset.pack(anchor="w", padx=pad, pady=(0, 0))
        self.week = tk.Label(self.tk, text="", fg=MUT, bg=BG, font=f2)
        self.week.pack(anchor="w", padx=pad, pady=(0, int(4 * SCALE)))
        self.bar_rect = None
        self.last_data = None
        self.fetching = False
        self.pending_force = False
        self.results = queue.Queue()
        self.tray_events = queue.Queue()
        self.refresh_after = None
        self.topmost_repair_after = None
        self.topmost_watchdog_after = None
        self.closing = False

        self.tk.bind("<Button-1>", self._press)
        self.tk.bind("<B1-Motion>", self._drag)
        self.tk.bind("<ButtonRelease-1>", self._release)
        self.tk.bind("<Button-3>", self._menu)

        self.menu = tk.Menu(self.tk, tearoff=0)
        self.menu.add_command(label="显示浮窗", command=self._show)
        self.menu.add_command(label="隐藏浮窗", command=self._hide)
        self.menu.add_separator()
        self.menu.add_command(label="重置位置", command=self._reset_position)
        self.menu.add_separator()
        self.menu.add_command(label="强制刷新", command=lambda: self.tick(force=True))
        self.menu.add_separator()
        self.menu.add_command(label="退出", command=self._quit)

        x, y = self._load_pos()
        self.tk.geometry(f"+{x}+{y}")
        self.hwnd = _top_level_hwnd(self.tk)
        self.tray = TrayIcon(
            self.tray_events, self.hwnd, int(self.tk.winfo_id()))
        self.tick()
        self.tk.after(100, self._drain_results)
        self.tk.after(100, self._drain_tray_events)
        self._arm_topmost_watchdog()

    def _load_pos(self):
        try:
            with open(POS_FILE, encoding="utf-8") as f:
                p = json.load(f)
        except Exception:
            p = {}
        self.tk.update_idletasks()
        width = max(190, self.tk.winfo_reqwidth())
        height = max(70, self.tk.winfo_reqheight())
        try:
            user32 = ctypes.windll.user32
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            screen_w = user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            screen_h = user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
        except Exception:
            left, top = 0, 0
            screen_w = self.tk.winfo_screenwidth()
            screen_h = self.tk.winfo_screenheight()
        right = left + max(screen_w, width + 20)
        bottom = top + max(screen_h, height + 20)
        default_x, default_y = right - width - 20, bottom - height - 60
        x = int(p.get("x", default_x))
        y = int(p.get("y", default_y))
        x = min(max(x, left + 10), max(left + 10, right - width - 10))
        y = min(max(y, top + 10), max(top + 10, bottom - height - 10))
        return x, y

    def _save_pos(self):
        try:
            with open(POS_FILE, "w", encoding="utf-8") as f:
                json.dump({"x": self.tk.winfo_x(), "y": self.tk.winfo_y()}, f)
        except Exception:
            pass

    def _press(self, e):
        self._dx = e.x_root - self.tk.winfo_x()
        self._dy = e.y_root - self.tk.winfo_y()

    def _drag(self, e):
        self.tk.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def _release(self, e):
        self._save_pos()

    def _menu(self, e):
        try:
            self.menu.tk_popup(e.x_root, e.y_root)
        finally:
            self.menu.grab_release()

    def _quit(self):
        self.closing = True
        for attr in ("topmost_repair_after", "topmost_watchdog_after"):
            handle = getattr(self, attr, None)
            if handle is not None:
                try:
                    self.tk.after_cancel(handle)
                except Exception:
                    pass
                setattr(self, attr, None)
        try:
            self.tray.stop()
        except Exception:
            pass
        self._save_pos()
        self.tk.destroy()

    def _show(self):
        self.tk.deiconify()
        self.tk.attributes("-alpha", OPACITY)
        self._clamp_current_position()
        self._enforce_topmost()

    def _hide(self):
        self.tk.withdraw()

    def _drain_tray_events(self):
        try:
            while True:
                event = self.tray_events.get_nowait()
                if event == "show":
                    self._show()
                elif event == "hide":
                    self._hide()
                elif event == "reset":
                    self._reset_position()
                elif event == "refresh":
                    self.tick(force=True)
                elif event == "exit":
                    self._quit()
                elif event == "topmost":
                    self.tray.consume_topmost_event()
                    self._schedule_topmost_repair()
        except queue.Empty:
            pass
        if not self.closing and self.tk.winfo_exists():
            self.tk.after(100, self._drain_tray_events)

    def _schedule_topmost_repair(self):
        """Coalesce reorder notifications and repair after the Z-order settles."""
        if self.closing or self.topmost_repair_after is not None:
            return
        self.topmost_repair_after = self.tk.after(100, self._repair_topmost)

    def _repair_topmost(self):
        self.topmost_repair_after = None
        self._enforce_topmost()

    def _arm_topmost_watchdog(self):
        """Periodically re-check topmost even when no WinEvent arrives.

        The tray hook thread can die silently (GetMessageW error exits its
        loop without logging), and some re-banding events never deliver a
        WinEvent at all; the event-driven repair alone is not a guarantee.
        """
        if self.closing:
            return
        self.topmost_watchdog_after = self.tk.after(
            TOPMOST_WATCHDOG_SECONDS * 1000, self._topmost_watchdog)

    def _topmost_watchdog(self):
        self.topmost_watchdog_after = None
        self._schedule_topmost_repair()
        self._arm_topmost_watchdog()

    def _clamp_current_position(self):
        x, y = self._load_pos()
        self.tk.geometry(f"+{x}+{y}")

    def _reset_position(self):
        """Move the card to the lower-right corner of the active desktop."""
        try:
            user32 = ctypes.windll.user32
            rect = wintypes.RECT()
            if not user32.GetWindowRect(self.hwnd, ctypes.byref(rect)):
                return
            left = user32.GetSystemMetrics(76)   # SM_XVIRTUALSCREEN
            top = user32.GetSystemMetrics(77)    # SM_YVIRTUALSCREEN
            right = left + user32.GetSystemMetrics(78)  # SM_CXVIRTUALSCREEN
            bottom = top + user32.GetSystemMetrics(79)  # SM_CYVIRTUALSCREEN
            width = max(1, rect.right - rect.left)
            height = max(1, rect.bottom - rect.top)
            x = max(left + 10, right - width - 20)
            y = max(top + 10, bottom - height - 60)
            self.tk.deiconify()
            self.tk.geometry(f"+{x}+{y}")
            self.tk.attributes("-alpha", OPACITY)
            self._save_pos()
            self._enforce_topmost()
        except Exception as e:
            log("reset position: " + repr(e))

    def _enforce_topmost(self):
        if self.closing:
            return
        try:
            if self.tk.state() == "withdrawn":
                return
            hwnd = self.hwnd
            user32 = ctypes.windll.user32
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowPos.argtypes = [
                wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int,
                ctypes.c_int, ctypes.c_int, wintypes.UINT]
            user32.SetWindowPos.restype = wintypes.BOOL
            ex_style = user32.GetWindowLongPtrW(hwnd, -20)
            covered = self.tray.has_covering_window()
            banded = bool(int(ex_style) & 0x00000008)  # WS_EX_TOPMOST
            if banded and not covered:
                return
            self.tk.attributes("-topmost", True)
            flags = 0x0001 | 0x0002 | 0x0010 | 0x0040  # NOSIZE|NOMOVE|NOACTIVATE|SHOWWINDOW
            if not user32.SetWindowPos(
                    hwnd, wintypes.HWND(-1), 0, 0, 0, 0, flags):
                raise ctypes.WinError()
            log("topmost: re-applied (" +
                ("style-lost" if not banded else "covered") + ")")
        except Exception as e:
            log("topmost: " + repr(e))

    @staticmethod
    def _age_text(stale_ms, source):
        if source == "logs":
            label = "本地历史"
        elif source == "live":
            label = "已更新"
        else:
            label = "缓存"
        if not isinstance(stale_ms, (int, float)):
            return label
        mins = max(0, int(stale_ms // 60_000))
        return f"{label} {mins}分钟前" if mins else f"{label} 刚刚"

    def _schedule_refresh(self):
        if self.refresh_after is None:
            self.refresh_after = self.tk.after(REFRESH_SECONDS * 1000, self._loop)

    def _start_fetch(self, force):
        if self.fetching:
            self.pending_force = self.pending_force or force
            return
        self.fetching = True
        threading.Thread(
            target=lambda: self.results.put(fetch(force)), daemon=True
        ).start()

    def tick(self, force=False):
        """启动一次后台本地读取；不会重复创建轮询链。"""
        self._start_fetch(force)
        self._schedule_refresh()
        if self.last_data:
            self.reset.config(text="更新中… " + self._age_text(
                self.last_data["stale_ms"], self.last_data["source"]), fg=MUT)
        else:
            self.title.config(text="5小时额度 更新中…", fg=MUT)
            self.reset.config(text="正在查询 Codex 额度", fg=MUT)

    def _render(self, data):
        self.last_data = data
        remaining = data["remaining"]
        c = color(remaining)
        self.title.config(text=f"5小时额度 剩余 {remaining}%  {data['week_txt']}", fg=c)
        refresh_error = data.get("refresh_error")
        reset_prefix = "更新失败 · " if refresh_error else ""
        self.reset.config(
            text=f"{reset_prefix}重置 {data['reset_hm']}（剩 {data['left']}）· "
                 f"{self._age_text(data['stale_ms'], data['source'])}",
            fg=AMBER if refresh_error else MUT,
        )
        self.week.config(text=data["week_left"] or "", fg=MUT)
        self.bar.delete("all")
        self.bar.create_rectangle(0, 0, self.barw, int(5 * SCALE), fill=TRACK, width=0)
        self.bar_rect = self.bar.create_rectangle(
            0, 0, max(2, int(self.barw * remaining / 100)), int(5 * SCALE),
            fill=c, width=0)

    def _render_error(self, source, stale_ms, error):
        if self.last_data:
            self.reset.config(
                text="更新失败 · " + self._age_text(
                    self.last_data["stale_ms"], self.last_data["source"]),
                fg=AMBER,
            )
            return
        if source == "logs":
            message = "仅有本地历史额度"
        elif error and "超时" in str(error):
            message = "Codex 查询超时"
        else:
            message = "Codex 不可用"
        self.title.config(text="5小时额度 —", fg=MUT)
        self.reset.config(text=message, fg=MUT)
        self.week.config(text="", fg=MUT)
        self.bar.delete("all")

    def _drain_results(self):
        try:
            while True:
                data, source, stale_ms, error = self.results.get_nowait()
                self.fetching = False
                if data is not None:
                    self._render(data)
                else:
                    self._render_error(source, stale_ms, error)
                if self.pending_force:
                    self.pending_force = False
                    self._start_fetch(True)
        except queue.Empty:
            pass
        if self.tk.winfo_exists():
            self.tk.after(100, self._drain_results)

    def _loop(self):
        self.refresh_after = None
        try:
            self.tick()
        except Exception as e:
            log("loop: " + repr(e))
            self._schedule_refresh()

    def run(self):
        self.tk.mainloop()


if __name__ == "__main__":
    try:
        MiniCard().run()
    except Exception as e:
        log("fatal: " + repr(e))
        sys.exit(1)
