# see-balance

查看 DeepSeek / Codex / Claude Code 三个 AI provider 的余额和用量。

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

```
══════════  Provider Balance  •  2025-06-13 11:30:00  ══════════

  🔵 DeepSeek API
     status      ✓ online
     CNY left    42.50   (topped_up: 40.00  granted: 2.50)

  🟢 Codex (Plus)
     5h used      23.4%  ████░░░░░░░░░░░░░░░░  resets in 2h15m
     7d used       8.1%  █░░░░░░░░░░░░░░░░░░░  resets in 4d12h00m

  🟣 Claude Code
     5h used      45.0%  █████████░░░░░░░░░░░  resets in 1h30m
     7d used      31.2%  ██████░░░░░░░░░░░░░░  resets in 5d08h00m

══════════════════════════════════════════════════════
```

## 状态缓存

用量快照保存在 `~/.see-balance/state.json`，用于显示 DeepSeek 两次查询之间的消费金额。

## 更新

```bash
# 修改源文件后重新安装
cd ~/Dev/tools/see-balance
# 编辑 provider_balance.py
bash install.sh
```
