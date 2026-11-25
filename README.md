# Codex 额度浮窗 (codex_monitor)

![浮窗截图](docs/screenshot.png)

Windows 置顶小浮窗 + 系统托盘，实时显示 Codex CLI 的 5 小时与周剩余额度、进度条及重置倒计时。纯 Python 标准库，无第三方依赖。

## 前置条件

- Windows 系统
- 已安装 Python 3（含标准库 `tkinter`，支持 `pythonw` 或 `pyw`）
- 已安装并登录官方 Codex CLI（`codex`）

## 启动

双击 `codex_monitor.bat` 即可无控制台启动；重复运行会自动重启旧实例，不影响其他 `pythonw` 进程。

> 如需指定特定 Python 解释器，可设置环境变量 `CODEX_MONITOR_PYTHONW` 为对应 `pythonw.exe` 路径。

## 操作

| 操作 | 效果 |
|---|---|
| 按住浮窗任意位置拖动 | 移动位置（自动记忆到 `position.json`） |
| 右键（浮窗或托盘图标） | 菜单：显示 / 隐藏 / 重置位置 / 强制刷新 / 退出 |
| 托盘图标左键 | 显示并置顶浮窗 |

- 优先通过官方 `codex app-server --stdio` 的 `account/rateLimits/read` 查询实时额度，失败时自动回退解析 `~/.codex/sessions` 最新会话日志。
- 界面每 60 秒刷新一次倒计时，实时快照缓存 5 分钟；置顶被其他窗口遮挡会自动恢复。

## 配置与代理

直接编辑同目录的 `config.json` 并重新运行 `codex_monitor.bat` 即可生效：

```json
{
  "proxy": "",
  "scale": 1.0,
  "opacity": 0.95,
  "refresh_seconds": 60,
  "live_ttl_seconds": 300
}
```

- `proxy`：默认留空，继承系统代理与当前环境变量（适合直连、TUN 模式或已开启系统代理的场景）；若代理客户端未开启系统代理，可填如 `"http://127.0.0.1:7897"`。
- 其余字段控制界面缩放、透明度、轮询与缓存间隔。
- 如需在本机覆盖配置且不改动受 Git 跟踪的 `config.json`，可新建同目录 `config.local.json`（已被 `.gitignore` 忽略）；加载优先级为 **环境变量（`CODEX_MONITOR_PROXY`） > `config.local.json` > `config.json` > 内置默认值**。
