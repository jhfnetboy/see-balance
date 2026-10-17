# see-balance

查看 DeepSeek / Codex / Claude Code / Antigravity (AGY) / Grok (xAI) 五个 AI provider 的余额和用量。

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
# 可选：手动指定 XAI_API_KEY
# XAI_API_KEY=xai-your-key-here

# 如需代理（VPN 场景）：
# HTTPS_PROXY=http://127.0.0.1:7890
```

- **DeepSeek key**：[platform.deepseek.com/api_keys](https://platform.deepseek.com/api_keys)
- **Codex**：自动读取 `~/.codex/auth.json`（运行 `codex login` 生成）
- **Claude Code**：自动读取 macOS Keychain（`claude /login` 登录后自动写入）；Keychain 条目缺失或为空时回退到 `~/.claude/.credentials.json`（遵循 `CLAUDE_CONFIG_DIR`）
- **Grok (xAI)**：自动读取 `~/.grok/auth.json`（运行 `grok` / `grok login` 生成）或 `XAI_API_KEY`，直连 `cli-chat-proxy.grok.com` 查询：
  - **主周池**（Chat · Imagine · Voice · Build）：7d 使用百分比、进度与重置倒计时；
  - **Grok Bot 池**（Bot / Cursor 独立周额度）：独立周额度不占用主池；支持在 `~/.see-balance.env` 配置 `CURSOR_SESSION_TOKEN` 同步实测用量。
- **Antigravity (AGY)**：自动读取 macOS Keychain / Google OAuth 凭据，识别 `paidTier` 套餐；直连服务端 `retrieveUserQuotaSummary` 读真实配额。

两个互不相通的池，各自带 **5h + weekly** 两个窗口：

| 池 | bucketId | 覆盖 |
|---|---|---|
| Gemini 池 | `gemini-5h` / `gemini-weekly` | Gemini Flash / Pro |
| Claude·GPT 池（3P） | `3p-5h` / `3p-weekly` | Claude Opus / Sonnet / GPT-OSS |

配额单位是 `remainingFraction`（**按 token 成本扣，不是请求次数**）。官方从没公布 5h 的数值阈值，只说过 *"refreshed every five hours until the weekly limit is reached"*。
> ⚠️ 旧版本会把 free-tier 推销文案里的 “1,500 requests/day with Gemini CLI” 当成 AGY 配额 —— 那是 Gemini CLI / Code Assist 的口径，与 Antigravity agent 的配额不是一回事，已在 2026-10-03 修正。本地日志统计的请求数现在单独列为「本地计数」，仅作参考。

## 使用

```bash
# 推荐：加 alias 到 ~/.zshrc
alias see="python3 ~/bin/provider_balance.py --watch 15"

see                  # 每 15 分钟刷新
python3 ~/bin/provider_balance.py          # 一次性查询
python3 ~/bin/provider_balance.py --watch 30   # 每 30 分钟
python3 ~/bin/provider_balance.py --compact    # 每 provider 一行
python3 ~/bin/provider_balance.py --json       # 原始 JSON
python3 ~/bin/provider_balance.py --no-pet     # 不显示 Claude 宠物（或设 SEE_BALANCE_PET=0）
python3 ~/bin/provider_balance.py --pet-demo   # 不查接口，演示宠物的每种情绪
```

`--watch` 在终端里运行时支持快捷键：**`p`** 显示/隐藏宠物，**`s`** 切换宠物（猫 / 兔子 / 小熊），**`r`** 立即刷新，**`q`** 退出。

## Claude 宠物

完整视图右下角有一只 ASCII 宠物（猫 / 兔子 / 小熊，按 `s` 切换，选择记在 `state.json` 里），按 **Claude** 的用量换情绪（劝歇时会顺带看别家订阅还剩多少），优先级从高到低：

| 情绪 | 什么时候 |
|---|---|
| 开心 | 7d 周额度用完（≥ 99.5%） |
| 献爱心（提醒休息） | 7d 按当前进度预计到期用量 > 120%；或 5h 已用满；或按实测速率 5h 会在重置前见底。别的订阅有余量时，直接说先用哪家（如 `要用到 130% 啦，试试 Grok？♥`） |
| 生气 | 7d 窗口重置时，上一周最后用量 < 90%（重置后 12h 内） |
| 冲刺 | 7d 不到 24h 就重置，照现在的速度到期还会剩 ≥ 20%：`剩 45% 要过期啦，试试 Opus？` |
| 鼓励 | 7d 预计到期用量 ≤ 75% |
| 日常 | 其它情况：每隔几秒随机发呆、摇尾巴、东张西望、打哈欠、舔爪、伸懒腰、睡觉 |

- 宠物优先贴着终端右下角；那几行的正文太宽时，就近往上找一块空位；哪儿都放不下时会隐藏，并在 footer 下提示需要多宽的终端。
- 一次性查询时显示静态的一帧；`--compact`、`--json` 和管道输出不显示宠物。
- “生气”要知道上周最后的用量，所以会在 `state.json` 的 `pet` 段记下 Claude 7d 窗口的位置。
- 画风参考 [ts-animal](https://github.com/ts-animal/ts-animal)（MIT）的颜文字动物，帧为本项目自绘。

## 输出示例

![screenshot](screenshot.jpg)

每个 provider 显示进度条与节奏分析：
- **5h used / 7d used** — 各窗口用量（AGY 按池分开显示 G-5h / G-7d / 3P-5h / 3P-7d）
- **进度应达** — 线性时间进度应达百分比 + 当前进度超前/还差评估
- **节奏评估** — 🟢 节奏正常 / 🟡 偏快需注意 / 🔴 超速需节约 / 🔴 偏慢可加速（附已过时间、当前消耗与到期预计）
- **实测消耗** — 从历史快照算出的真实速率：`+1.4%/h`（额度窗口）或 `¥0.69/h`（DeepSeek 现金）
- **建议** — 把速率翻译成结论：**能不能撑到重置**（`⚠ 2.6h 后见底，比重置早 56h`）或在闸门内（`今日将花 ¥13.52（闸门 ¥9.00 ⚠ 会超）`）
- **增量消费** — 两次查询之间的新增消费
- **🧭 调度建议** — 把几家订阅（Claude / Codex / Grok 主池 / AGY 两个池）的 7d 窗口放一起看，只在有话可说时出现：
  - `⚠` 某家照这样到期要超 120%，或 `⛔` 周额度 / 5h 额度用光 → 推荐一家有余量的订阅试试（快重置还剩很多的优先，其次看到期能空出多少）
  - `⏰` 不到 24h 就重置、照现在的速度会空着 ≥ 20% → 建议放开用：高 effort、最强模型、`/fast` 加速、多开并行 agent
  - `💤` 预计到期只用 ≤ 75% 且还剩 ≥ 30% → 建议多给它点活儿
  - 「有余量」指：还剩 ≥ 30%，同池 5h 不紧（< 80% 且没在超速），并且是 `⏰` / `💤`，或窗口刚开、用量 ≤ 5%
  - 离 7d 重置不到 15 分钟的池不出 `⏰` / `💤`，也不会被推荐（马上就是新窗口）；5h 紧的池同样不出 `⏰` / `💤`

  ```
  🧭 调度建议
     ⚠ Claude 照这样到期要用 130% → 要不试试 Grok？还剩 99%，5d17h 后重置，人家也很强哦
     ⛔ Codex 周额度用完了，4d19h 后重置 → 要不试试 Grok？还剩 99%，5d17h 后重置，人家也很强哦
  ```

`--compact` 每行末尾会带速率后缀：`⇣+1.4%/h≈3h`（按此速率还有 3h 见底）。

## 状态缓存

用量快照保存在 `~/.see-balance/state.json`，用途：
- `history` 数组：每次查询追一条样本（保留 14 天 / 4000 条），**实测消耗速率就从这里算** —— 跨重启、跨终端都不断（重启进程不会丢速率）。
- `daily_baselines`：当天开头各窗口的基线，用于算「进度应达」。

可用环境变量：

| 变量 | 作用 |
|---|---|
| `SEE_BALANCE_STATE` | 换一个 state 文件（测试 / 多账号） |
| `SEE_BALANCE_CNY_DAILY_CAP` | DeepSeek 当日人民币闸门（例 `9`），设了就会给「安全 / 会超」判定 |
| `HTTPS_PROXY` | 访问 api.deepseek.com / chatgpt.com 的代理 |

速率的可信度取决于采样密度：`--watch 15` 下几小时内就很准；只跑两次相隔 1 分钟是看不到速率的（跨度 < 3 分钟直接不出值，避免噪声）。

## 更新

```bash
# 修改源文件后重新安装
cd ~/Dev/tools/see-balance
# 编辑 provider_balance.py
bash install.sh
```
