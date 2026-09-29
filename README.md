# see-balance

查看 DeepSeek / Codex / Claude Code / Antigravity (AGY) 四个 AI provider 的余额和用量。

## 安装

```bash
bash install.sh
```

安装做了三件事：
1. 复制 `provider_balance.py` 到 `~/bin/`
2. 创建配置文件 `~/.see-balance.env`（如果不存在）
3. 提示你添加 shell alias

## 配置

编辑 `~/.see-balance.env`：

```bash
DEEPSEEK_API_KEY=sk-your-key-here

# 如需代理（VPN 场景）：
# HTTPS_PROXY=http://127.0.0.1:7890
```

- **DeepSeek key**：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
- **Codex**：自动读取 `~/.codex/auth.json`（运行 `codex login` 生成）
- **Claude Code**：自动读取 macOS Keychain（`claude /login` 登录后自动写入）
- **Antigravity (AGY)**：自动读取 macOS Keychain / Google OAuth 凭据，识别 Google One Pro / AI Pro 套餐与 AI Credits，并通过本地日志自动统计每日 1,500 次模型请求进度与滑动窗口用量。

## 使用

```bash
# 推荐：加 alias 到 ~/.zshrc
alias see="python3 ~/bin/provider_balance.py --watch 15"

see                  # 每 15 分钟刷新
python3 ~/bin/provider_balance.py          # 一次性查询
python3 ~/bin/provider_balance.py --watch 30   # 每 30 分钟
python3 ~/bin/provider_balance.py --compact    # 每 provider 一行
python3 ~/bin/provider_balance.py --json       # 原始 JSON
```

## 输出示例

![screenshot](screenshot.jpg)

每个 provider 显示进度条与节奏分析：
- **5h used / reqs** — 5小时窗口用量或频次
- **7d used / daily used** — 7天窗口或每日用量（含 AGY 当日 1,500 次请求进度）
- **进度应达** — 线性时间进度应达百分比 + 当前进度超前/还差评估
- **节奏评估** — 🟢 节奏正常 / 🟡 偏快需注意 / 🔴 超速需节约 / 🔴 偏慢可加速（附已过时间、当前消耗与到期预计）
- **增量消费** — 显示两次查询之间的新增请求与花费统计

## 状态缓存

用量快照保存在 `~/.see-balance/state.json`，用于显示 DeepSeek 两次查询之间的消费金额。

## 更新

```bash
# 修改源文件后重新安装
cd ~/Dev/tools/see-balance
# 编辑 provider_balance.py
bash install.sh
```
