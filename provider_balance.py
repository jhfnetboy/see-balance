#!/usr/bin/env python3
"""
see-balance — query usage/balance for AI providers.

  DeepSeek    : GET  api.deepseek.com/user/balance            (API key)
  Codex       : GET  chatgpt.com/backend-api/wham/usage       (~/.codex/auth.json)
  Claude      : GET  api.anthropic.com/api/oauth/usage        (Keychain OAuth)
  Antigravity : POST daily-cloudcode-pa.googleapis.com        (Keychain / Google OAuth)
  Grok (xAI)  : GET  cli-chat-proxy.grok.com/v1/billing       (~/.grok/auth.json or XAI_API_KEY)
  OpenCode    : GET  api.anthropic.com/v1/models + local opencode.db spend  (opencode auth.json)

Usage:
  python3 provider_balance.py                  # one-shot, human-readable
  python3 provider_balance.py --watch          # refresh every 30 min (default)
  python3 provider_balance.py --watch 15       # refresh every 15 min
  python3 provider_balance.py --compact        # one-line per provider
  python3 provider_balance.py --json           # raw JSON
  python3 provider_balance.py --no-pet         # hide the Claude pet (or SEE_BALANCE_PET=0)
  python3 provider_balance.py --pet-demo       # preview every pet mood, no API calls
  python3 provider_balance.py --sessions       # print the live session table once, no API calls
  python3 provider_balance.py --update         # install the latest version from upstream main now
  python3 provider_balance.py --version        # print the version
  python3 provider_balance.py --slack-test     # send a test message to SLACK_WEBHOOK_URL
  python3 provider_balance.py --no-update      # skip the daily update check (or SEE_BALANCE_UPDATE=0)
  python3 provider_balance.py --install-hooks  # pet reminds you when a Claude Code / Grok session waits for you
  python3 provider_balance.py --uninstall-hooks

  In --watch on a terminal: p toggles the pet, s switches pet (cat/bunny/bear),
  t switches the 用量 / 会话 tab, r refreshes now,
  u installs a downloaded update and restarts, q quits.

Config:
  ~/.see-balance.env   — put DEEPSEEK_API_KEY, XAI_API_KEY, SEE_BALANCE_SESSIONS, SLACK_WEBHOOK_URL, and proxy settings here
  State cached at:     ~/.see-balance/state.json

No secrets ever leave this machine.
"""

import base64, hashlib, http.client, io, json, math, os, random, re, select, shlex, shutil, signal, socket, ssl, stat, subprocess, sys, tempfile, threading, time, unicodedata
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
try:
    import termios, tty      # watch 模式读快捷键；没有（如 Windows）就退回普通 sleep
except ImportError:
    termios = tty = None

__version__ = "1.8.0"   # 自动更新拿上游 main 上的这个号比新旧：要推给用户的改动记得调大

# ── Paths ─────────────────────────────────────────────────────────────────────
STATE_FILE = Path(os.environ.get("SEE_BALANCE_STATE") or (Path.home() / ".see-balance" / "state.json"))   # 可用 SEE_BALANCE_STATE 覆盖（测试/多账号）
ENV_FILE   = Path.home() / ".see-balance.env"

# ── Proxy ─────────────────────────────────────────────────────────────────────
_PROXY_URL = (os.environ.get("HTTPS_PROXY")
              or os.environ.get("SEE_BALANCE_HTTPS_PROXY")
              or "")
if not _PROXY_URL and ENV_FILE.exists():
    for _line in ENV_FILE.read_text().splitlines():
        _line = _line.strip()
        if _line.startswith("HTTPS_PROXY=") or _line.startswith("SEE_BALANCE_HTTPS_PROXY="):
            _PROXY_URL = _line.split("=", 1)[1].strip().strip('"').strip("'")
            if _PROXY_URL: break

# ── SSL ───────────────────────────────────────────────────────────────────────
try:
    import certifi
    SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL_CTX = ssl.create_default_context()

# Build a single opener combining proxy + SSL context so urlopen() calls stay
# proxy-aware even when an SSL context is needed (passing context= to urlopen
# directly would bypass install_opener and create a proxy-less opener).
from urllib.request import ProxyHandler, HTTPSHandler, build_opener, install_opener
_handlers = [HTTPSHandler(context=SSL_CTX)]
if _PROXY_URL:
    _handlers.insert(0, ProxyHandler({"https": _PROXY_URL, "http": _PROXY_URL}))
install_opener(build_opener(*_handlers))

HTTP_TIMEOUT           = 15
CLAUDE_OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
CLAUDE_CODE_USER_AGENT = "claude-code/2.1.121"
AGY_CLIENT_ID          = os.environ.get("AGY_CLIENT_ID") or bytes([b ^ 0x5a for b in bytes.fromhex("6b6a6d6b6a6a6c6a6c6a6f636b772e3732292933346832686b3639283f68696f2c2e35363530326e3d6e6a693f2a743b2a2a29743d35353d363f2f293f283935342e3f342e74393537")]).decode()
AGY_CLIENT_SECRET      = os.environ.get("AGY_CLIENT_SECRET") or bytes([b ^ 0x5a for b in bytes.fromhex("1d1519090a0277116f621c0d086e626c163e16106b371618622902196e206c2b1e1b3c")]).decode()
GROK_CLIENT_ID         = "b1a00492-073a-47ea-816f-4c329264a828"
GROK_CLI_USER_AGENT    = "grok-cli"
GROK_AUTH_FILE         = Path(os.environ.get("GROK_AUTH_FILE") or (Path.home() / ".grok" / "auth.json"))

# ── HTTP helpers ──────────────────────────────────────────────────────────────

def http_get(url, headers=None):
    req = Request(url, headers=headers or {})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            try:    return resp.status, json.loads(raw)
            except: return resp.status, None
    except HTTPError as e:
        raw = e.read()
        try:    return e.code, json.loads(raw)
        except: return e.code, None
    except (URLError, TimeoutError, socket.timeout, OSError) as e:
        return 0, {"_transport_error": str(e)}

def http_post(url, headers=None, body=None):
    data = json.dumps(body).encode() if body else None
    req  = Request(url, data=data, method="POST", headers=headers or {})
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            raw = resp.read()
            try:    return resp.status, json.loads(raw)
            except: return resp.status, None
    except HTTPError as e:
        raw = e.read()
        try:    return e.code, json.loads(raw)
        except: return e.code, None
    except (URLError, TimeoutError, socket.timeout, OSError) as e:
        return 0, {"_transport_error": str(e)}

# ── Config helpers ────────────────────────────────────────────────────────────

def load_env_key():
    """Load DEEPSEEK_API_KEY from env → ~/.see-balance.env."""
    key = os.environ.get("DEEPSEEK_API_KEY", "")
    if key and key.startswith("sk-"):
        return key.strip('"').strip("'")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("DEEPSEEK_API_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val and val.startswith("sk-"):
                    return val
    return ""

def load_env_grok_key():
    """Load XAI_API_KEY / GROK_API_KEY from env → ~/.see-balance.env."""
    for var in ("XAI_API_KEY", "GROK_API_KEY", "GROK_ACCESS_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            return val.strip('"').strip("'")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            for prefix in ("XAI_API_KEY=", "GROK_API_KEY=", "GROK_ACCESS_TOKEN="):
                if line.startswith(prefix):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return ""

def load_env_anthropic_admin_key():
    """Load ANTHROPIC_ADMIN_KEY from env → ~/.see-balance.env（查 opencode 那个 key 的过期时间用，可选）。"""
    val = os.environ.get("ANTHROPIC_ADMIN_KEY", "")
    if val:
        return val.strip('"').strip("'")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("ANTHROPIC_ADMIN_KEY="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                if val:
                    return val
    return ""

def load_env_grok_bot_token():
    """Load CURSOR_SESSION_TOKEN / GROK_BOT_TOKEN from env → ~/.see-balance.env."""
    for var in ("CURSOR_SESSION_TOKEN", "GROK_BOT_TOKEN"):
        val = os.environ.get(var, "")
        if val:
            return val.strip('"').strip("'")
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            for prefix in ("CURSOR_SESSION_TOKEN=", "GROK_BOT_TOKEN="):
                if line.startswith(prefix):
                    val = line.split("=", 1)[1].strip().strip('"').strip("'")
                    if val:
                        return val
    return ""

def load_env_sessions():
    """SEE_BALANCE_SESSIONS：逗号分隔的目录子串。环境变量优先，空着再看 ~/.see-balance.env。

    两边都没设或空着返回 []，会话页不过滤。某一段两边的空白会去掉。
    """
    val = os.environ.get("SEE_BALANCE_SESSIONS", "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("SEE_BALANCE_SESSIONS="):
                val = line.split("=", 1)[1].strip().strip('"').strip("'")
                break
    return [p.strip() for p in (val or "").split(",") if p.strip()]

def load_slack_webhook():
    """SLACK_WEBHOOK_URL。环境变量优先，空着再看 ~/.see-balance.env。不是 https://hooks.slack.com/ 开头就当没配。"""
    val = os.environ.get("SLACK_WEBHOOK_URL", "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("SLACK_WEBHOOK_URL="):
                val = line.split("=", 1)[1]
                break
    val = (val or "").strip().strip('"').strip("'")
    return val if val.startswith("https://hooks.slack.com/") else ""

def load_slack_delay():
    """SEE_BALANCE_SLACK_DELAY（秒，整数 ≥ 0）。没设或写错就用 SLACK_DELAY_DEFAULT。"""
    val = os.environ.get("SEE_BALANCE_SLACK_DELAY", "")
    if not val and ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line.startswith("SEE_BALANCE_SLACK_DELAY="):
                val = line.split("=", 1)[1]
                break
    try:
        n = int((val or "").strip().strip('"').strip("'"))
    except (TypeError, ValueError):
        return SLACK_DELAY_DEFAULT
    return n if n >= 0 else SLACK_DELAY_DEFAULT

def load_state():
    if STATE_FILE.exists():
        try:    return json.loads(STATE_FILE.read_text(encoding="utf-8"))   # 和 save_state 同一编码，别随 locale
        except: pass
    return {}

def save_state(snapshot):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    # 原子写：几个终端同时跑时，别人不会读到写了一半的 state（读到半截会当成空 state）
    _write_atomic(STATE_FILE, json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

# ── 实测消耗（历史采样 + 速率）───────────────────────────────────────────────
#
# state.json 里维护一个 history 数组（每次查询追一条压扁的样本）。速率只从
# 历史里算 —— 不靠猜测，也不依赖上次查询的差值（那个只能看“最近几分钟”）。

HISTORY_MAX_SEC  = 14 * 86400   # 历史保留 14 天
HISTORY_MAX_ROWS = 4000         # 并限制条数
RATE_LOOKBACK_MAX_SEC = 12 * 3600   # 速率最多回看 12h（更早的窗口已重置）
WINDOW_AVG_MIN_SEC    = 24 * 3600   # 不短于 1 天的窗口（7d），建议改用窗口平均速率：跨昼夜，不被一时高峰带偏


def history_sample(data: dict) -> dict:
    """把一次快照压成历史样本（只留算速率要用的字段）。"""
    def pct(prov: str, key: str):
        return ((data.get(prov) or {}).get(key) or {}).get("pct")
    return {
        "ts":             int(data.get("ts") or time.time()),
        "deepseek_cny":   (data.get("deepseek") or {}).get("cny_left"),
        "codex_weekly":   pct("codex", "weekly"),
        "claude_5h":      pct("claude", "five_hour"),
        "claude_weekly":  pct("claude", "weekly"),
        "agy_gemini_5h":  pct("agy", "gemini_5h"),
        "agy_gemini_weekly": pct("agy", "gemini_weekly"),
        "agy_3p_5h":      pct("agy", "3p_5h"),
        "agy_3p_weekly":  pct("agy", "3p_weekly"),
        "grok_weekly":    pct("grok", "main_weekly") if pct("grok", "main_weekly") is not None else pct("grok", "weekly"),
        "grok_bot_weekly": pct("grok", "bot_weekly"),
    }


def _hist_series(hist, key, lookback_sec, increasing=True):
    """取 key 在 lookback 内的样本序列；碰到“重置”（值反向跳）就只留那之后的一段。"""
    now   = time.time()
    pts   = []
    for h in (hist or []):
        ts = h.get("ts")
        v  = h.get(key)
        if ts is None or v is None or now - float(ts) > lookback_sec:
            continue
        pts.append((float(ts), float(v)))
    pts.sort()
    if len(pts) < 2:
        return []
    seg = [pts[-1]]
    for i in range(len(pts) - 2, -1, -1):
        prev_v, last_v = pts[i][1], seg[-1][1]
        reset = (prev_v > last_v + 1e-9) if increasing else (prev_v < last_v - 1e-9)
        if reset:
            break
        seg.append(pts[i])
    seg.reverse()
    return seg


def burn_rate(hist, key, window_sec, increasing=True):
    """窗口内实测消耗速率（%/h 或 CNY/h）。没有足够样本就返回 None。"""
    lookback = min(window_sec, RATE_LOOKBACK_MAX_SEC)
    seg = _hist_series(hist, key, lookback, increasing=increasing)
    if len(seg) < 2:
        return None
    (t0, v0), (t1, v1) = seg[0], seg[-1]
    hours = (t1 - t0) / 3600.0
    if hours < 0.05:          # 跨度 < 3 分钟，噪声大于信号
        return None
    delta    = v1 - v0
    per_hour = (-delta if not increasing else delta) / hours
    return {"per_hour": per_hour, "delta": delta, "hours": hours,
            "from": v0, "to": v1, "samples": len(seg), "since_ts": int(t0)}


def window_avg_rate(w, window_sec):
    """窗口开始以来的平均速率（%/h）= 已用 ÷ 已过小时。窗口起点用量是 0，不需要历史样本。"""
    if not w or w.get("pct") is None or not w.get("reset_at"):
        return None
    elapsed = window_sec - max(0.0, w["reset_at"] - time.time())
    if elapsed < window_sec * 0.05:   # 与 pace_line 同一门槛：刚开窗时分母太小，平均值不稳
        return None
    hours = elapsed / 3600.0
    return {"per_hour": float(w["pct"]) / hours, "hours": hours, "kind": "window"}


def advice_rate(w, hist, key, window_sec):
    """建议用哪个速率：5h 窗口看近期实测；7d 窗口看窗口平均，近期实测只作参考。

    7d 窗口刚开不久（平均值还不稳）时，返回近期速率并标记 provisional —— 只展示，不下结论。
    """
    recent = burn_rate(hist, key, window_sec)
    if window_sec < WINDOW_AVG_MIN_SEC:
        return recent
    if not (w or {}).get("reset_at"):   # 不知道窗口位置，只能看近期
        return recent
    avg = window_avg_rate(w, window_sec)
    if avg:
        avg["recent"] = recent
        return avg
    return dict(recent, provisional=True) if recent else None


EXHAUSTED_PCT = 99.5   # 用量 ≥ 此值视为额度已用完：不再谈速率/节奏，只说什么时候重置


def burn_advice(w, rate, alt_hint=None):
    """返回 (实测消耗行, 进展建议行) 二元组（已是完整行，含缩进）；没有则 None。

    建议就是把速率翻译成“能不能撑到重置”—— 这是唯一有用的结论。
    """
    if not w or w.get("pct") is None:
        return None, None
    pct  = float(w["pct"])
    left = max(0.0, 100.0 - pct)
    reset_in_h = (max(0.0, w["reset_at"] - time.time()) / 3600.0) if w.get("reset_at") else None

    if pct >= EXHAUSTED_PCT:   # 已经用完：“按此速率 0h00m 后见底 / 建议降速”都没意义了
        when = f"{fmt_countdown(reset_in_h * 3600)} 后重置" if reset_in_h is not None else "等待重置"
        tail = f" —— {alt_hint}" if alt_hint else ""
        return None, f"     建议         ⛔ 已用完，{when}{tail}"

    if not rate or rate["per_hour"] <= 0.005:
        if rate and rate.get("kind") == "window":
            return (f"     实测消耗     窗口平均几乎为 0（已过 {rate['hours']:.0f}h，用了 {pct:.1f}%）",
                    "     建议         可以放心用" if reset_in_h and reset_in_h > 1 else None)
        if rate:
            line = f"     实测消耗     近 {rate['hours']:.1f}h 几乎没动（{rate['samples']} 样本，{rate['from']:.1f}%→{rate['to']:.1f}%）"
            if rate.get("provisional"):   # 窗口刚开：近期没动也不代表整窗安全
                return line, "     建议         窗口刚开始，先不下结论"
            return line, ("     建议         可以放心用" if reset_in_h and reset_in_h > 1 else None)
        return None, None

    ph        = rate["per_hour"]
    exhaust_h = left / ph
    if rate.get("kind") == "window":
        line = f"     实测消耗     窗口平均 +{ph:.1f}%/h（已过 {rate['hours']:.0f}h，剩余 {left:.1f}%）"
        recent = rate.get("recent")
        if recent:
            line += f"；近 {recent['hours']:.1f}h +{max(0.0, recent['per_hour']):.1f}%/h"
    else:
        line = (f"     实测消耗     +{ph:.1f}%/h（剩余 {left:.1f}%，近 {rate['hours']:.1f}h，"
                f"{rate['samples']} 样本）")
    if rate.get("provisional"):   # 7d 窗口刚开：近期速率会被一时高峰带偏，不据此下结论
        return line, "     建议         窗口刚开始，先不下结论"

    if reset_in_h is None:
        return line, f"     建议         按此速率还能撑 {fmt_countdown(exhaust_h * 3600)}"
    if reset_in_h < 0.25:   # 还剩 <15 分钟，建议已无意义（马上就是新窗口）
        return line, None
    if exhaust_h < reset_in_h:
        tail = alt_hint if alt_hint else "建议降速或换池"
        return line, (f"     建议         ⚠ 按此速率 {fmt_countdown(exhaust_h * 3600)} 后见底，"
                      f"比重置早 {fmt_countdown((reset_in_h - exhaust_h) * 3600)} —— {tail}")
    projected = min(100.0, pct + ph * reset_in_h)
    verdict   = "✓ 撑得到重置" if projected < 97 else "⚠ 刚好卡在重置前"
    return line, (f"     建议         {verdict}（还剩 {fmt_countdown(reset_in_h * 3600)}，届时约用 {projected:.0f}%）")


HIST_KEY_BY_WINDOW = {
    ("codex",  "weekly"):        "codex_weekly",
    ("claude", "five_hour"):    "claude_5h",
    ("claude", "weekly"):       "claude_weekly",
    ("agy",    "gemini_5h"):    "agy_gemini_5h",
    ("agy",    "gemini_weekly"): "agy_gemini_weekly",
    ("agy",    "3p_5h"):        "agy_3p_5h",
    ("agy",    "3p_weekly"):    "agy_3p_weekly",
    ("grok",   "weekly"):       "grok_weekly",
    ("grok",   "main_weekly"):  "grok_weekly",
    ("grok",   "bot_weekly"):   "grok_bot_weekly",
}


def history_summary(hist):
    """给 --json / 其它脚本用的速率摘要（不含渲染）。"""
    out = {"samples": len(hist or []), "span_hours": None, "burns": {}}
    ts_all = sorted(float(h["ts"]) for h in (hist or []) if h.get("ts"))
    if len(ts_all) >= 2:
        out["span_hours"] = round((ts_all[-1] - ts_all[0]) / 3600.0, 2)
    for key, window_sec, inc in (("deepseek_cny", 24 * 3600, False),
                                 ("codex_weekly", 7 * 24 * 3600, True),
                                 ("claude_5h", 5 * 3600, True),
                                 ("claude_weekly", 7 * 24 * 3600, True),
                                 ("agy_gemini_5h", 5 * 3600, True),
                                 ("agy_gemini_weekly", 7 * 24 * 3600, True),
                                 ("agy_3p_5h", 5 * 3600, True),
                                 ("agy_3p_weekly", 7 * 24 * 3600, True),
                                 ("grok_weekly", 7 * 24 * 3600, True),
                                 ("grok_bot_weekly", 7 * 24 * 3600, True)):
        r = burn_rate(hist, key, window_sec, increasing=inc)
        if not r:
            continue
        out["burns"][key] = {"per_hour": round(r["per_hour"], 4), "hours": round(r["hours"], 2),
                             "samples": r["samples"], "from": r["from"], "to": r["to"]}
    return out


def rate_suffix(hist, pkey, key, w=None):
    """紧凑模式的一行后缀：` ⇣+1.2%/h≈12h`（按实测速率还有 12h 见底；满 1 天显示为 `1d02h`）。"""
    hk = HIST_KEY_BY_WINDOW.get((pkey, key))
    if not hk:
        return ""
    window_sec = 5 * 3600 if (key.endswith("5h") or key == "five_hour") else 7 * 24 * 3600
    if float((w or {}).get("pct") or 0) >= EXHAUSTED_PCT:   # 已用完，没有“还剩多久见底”
        return ""
    r = advice_rate(w, hist, hk, window_sec)
    if not r or r.get("provisional") or r["per_hour"] <= 0.005:
        return ""
    text = f" ⇣+{r['per_hour']:.1f}%/h"
    pct = (w or {}).get("pct")
    if pct is not None:
        text += f"≈{fmt_countdown((100.0 - float(pct)) / r['per_hour'] * 3600)}"
    return text


def deepseek_lines(ds, hist):
    """DeepSeek 是现金：看的是 ¥/h 和“今日将花多少”。"""
    if not ds or "cny_left" not in ds:
        return None, None
    rate = burn_rate(hist, "deepseek_cny", 24 * 3600, increasing=False)
    if not rate or rate["per_hour"] <= 0.0005:
        return None, None
    ph  = rate["per_hour"]
    now = time.time()
    day_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    left_today_h = max(0.0, (day_start + 86400 - now) / 3600.0)
    spent_today, best_ts = None, None
    for h in (hist or []):
        ts, v = h.get("ts"), h.get("deepseek_cny")
        if ts is None or v is None or float(ts) < day_start:
            continue
        if best_ts is None or float(ts) < best_ts:
            best_ts, spent_today = float(ts), float(v)
    spent = (spent_today - float(ds["cny_left"])) if spent_today is not None else None
    line  = f"     实测消耗     ¥{ph:.3f}/h（近 {rate['hours']:.1f}h，{rate['samples']} 样本）"
    if spent is None:
        return line, None
    projected = spent + ph * left_today_h
    cap = os.environ.get("SEE_BALANCE_CNY_DAILY_CAP")
    if cap:
        try:
            capv = float(cap)
            verdict = "✓ 安全" if projected < capv else "⚠ 会超"
            return line, f"     建议         今日已花 ¥{spent:.2f}，按此速率将花 ¥{projected:.2f}（闸门 ¥{capv:.2f} {verdict}）"
        except ValueError:
            pass
    return line, (f"     建议         今日已花 ¥{spent:.2f}，按此速率今日将花 ¥{projected:.2f}"
                  "（设 SEE_BALANCE_CNY_DAILY_CAP 后会给闸门判定）")

# ── Render helpers ────────────────────────────────────────────────────────────

def fmt_countdown(seconds):
    """倒计时：满 1 天显示 `5d03h`，不足 1 天显示 `3h26m`（向下取整）。"""
    mins = max(0, int(seconds / 60))
    if mins >= 24 * 60:
        return f"{mins // 1440}d{mins % 1440 // 60:02d}h"
    return f"{mins // 60}h{mins % 60:02d}m"

def window_pct_bar(w):
    if not w: return "—"
    pct = max(0.0, min(100.0, float(w.get("pct", 0))))
    if w.get("reset_at"):
        reset = f"resets in {fmt_countdown(w['reset_at'] - time.time())}"
    else:
        reset = "reset ?"
    bar_n = int(round(pct / 5))
    bar   = "█" * bar_n + "░" * (20 - bar_n)
    extra = f"  {w['used']}/{w['limit']} reqs" if "used" in w and "limit" in w else ""
    return f"{pct:5.1f}%  {bar}{extra}  {reset}"

def today_target_line(w: dict, baseline_pct=None, fixed_daily_target=None, total_sec=7*24*3600, daily_rate=None) -> str:
    """Third row: 进度应达 = elapsed_frac × 100% (linear schedule position right now).

    Tells you where you *should* be on a straight 100% curve across total_sec.
    Completely independent of actual usage or baseline.
    """
    if not w or w.get("pct") is None or not w.get("reset_at"):
        return ""
    pct           = float(w["pct"])
    remaining_sec = max(0.0, w["reset_at"] - time.time())
    if remaining_sec > total_sec * 0.99:
        return ""

    elapsed_frac = max(0.0, (total_sec - remaining_sec) / total_sec)
    target_now   = elapsed_frac * 100          # linear schedule: should be here right now
    if daily_rate is None:
        daily_rate = 100.0 / (total_sec / 86400.0)

    # Bar: target_now on the same 0–100% scale as the "used" bars above, so the two line up
    bar_n = int(round(min(100.0, target_now) / 5))
    bar   = "█" * bar_n + "░" * (20 - bar_n)

    # 只陈述差值，快慢判断统一交给 pace_line（按到期推算），避免两行结论打架
    diff = pct - target_now
    if diff >= 0.05:
        status = f"多用{diff:.1f}%"
    elif diff <= -0.05:
        status = f"少用{abs(diff):.1f}%"
    else:
        status = "持平"

    rate_str = f"日均+{daily_rate:.1f}%" if total_sec > 86400 else "今日进度"
    return (f"     进度应达    {target_now:5.1f}%  {bar}"
            f"  {rate_str}  {status}")

PACE_SLOW_PCT = 75    # 预计到期用量 ≤ 此值：偏慢可加速
PACE_FAST_PCT = 120   # 预计到期用量 > 此值：超速需节约

def pace_projection(w: dict, total_sec: int):
    """按已过时间线性推算到期用量，返回 (已过时间比例, 预计到期用量%)；太早或快重置时返回 None。"""
    if not w or w.get("pct") is None or not w.get("reset_at"):
        return None
    remaining = max(0.0, w["reset_at"] - time.time())
    elapsed   = max(0.0, total_sec - remaining)
    if elapsed < total_sec * 0.05:   # < 5% elapsed — too early to judge
        return None
    if remaining < total_sec * 0.03:  # 窗口快重置了，此刻的节奏判断没意义
        return None
    frac = elapsed / total_sec
    return frac, float(w["pct"]) / frac

def pace_line(w: dict, total_sec: int) -> str:
    """Return a pace-assessment line for a rate-limited usage window.

    Compares actual usage against how much should have been used given elapsed
    time, then projects what the final usage will be at the current rate.
    """
    if w and w.get("pct") is not None and w.get("reset_at") and float(w["pct"]) >= EXHAUSTED_PCT:
        elapsed = max(0.0, total_sec - max(0.0, w["reset_at"] - time.time()))
        return f"       ⛔ 已用完  已过{elapsed / total_sec * 100:.0f}%时间，等待重置"
    proj = pace_projection(w, total_sec)
    if not proj:
        return ""
    pct             = float(w["pct"])
    frac, projected = proj           # estimated usage at end of window

    if projected <= PACE_SLOW_PCT:
        icon, verdict = "🔴", f"偏慢可加速"
    elif projected <= 100:
        icon, verdict = "🟢", f"节奏正常"
    elif projected <= PACE_FAST_PCT:
        icon, verdict = "🟡", f"偏快需注意"
    else:
        icon, verdict = "🔴", f"超速需节约"

    return (f"       {icon} {verdict}"
            f"  已过{frac*100:.0f}%时间 用了{pct:.1f}%"
            f"  预计到期用{projected:.0f}%")

def parse_reset(value):
    if value is None: return None
    if isinstance(value, (int, float)): return int(value)
    try:
        s = str(value).replace("Z", "+00:00")
        from datetime import datetime, timezone
        return int(datetime.fromisoformat(s).timestamp())
    except: return None

# ── DeepSeek ──────────────────────────────────────────────────────────────────

def fetch_deepseek():
    key = load_env_key()
    if not key: return {"error": "no DEEPSEEK_API_KEY (set in ~/.see-balance.env)"}

    status, obj = http_get("https://api.deepseek.com/user/balance",
                           headers={"Authorization": f"Bearer {key}"})
    if status != 200 or not isinstance(obj, dict):
        err = obj.get("_transport_error", f"http {status}") if isinstance(obj, dict) else f"http {status}"
        return {"error": err}

    info_list  = obj.get("balance_infos", [])
    cny        = next((b for b in info_list if b.get("currency") == "CNY"), {})
    return {
        "available":   obj.get("is_available", False),
        "cny_left":    cny.get("total_balance", "0.00"),
        "cny_topped":  cny.get("topped_up_balance", "0.00"),
        "cny_granted": cny.get("granted_balance", "0.00"),
    }

# ── Codex ─────────────────────────────────────────────────────────────────────

def fetch_codex():
    path = os.path.expanduser("~/.codex/auth.json")
    try:
        with open(path) as f:
            tokens = json.load(f).get("tokens") or {}
        token = tokens.get("access_token")
    except (OSError, ValueError, AttributeError):
        tokens, token = {}, None
    if not token: return {"error": "no codex auth (run: codex login)"}

    status, obj = http_get("https://chatgpt.com/backend-api/wham/usage",
                           headers={"Authorization": f"Bearer {token}"})
    if status == 401: return {"error": "codex auth expired (run: codex login)"}
    if status != 200 or not isinstance(obj, dict):
        raw = obj.get("_transport_error", f"http {status}") if isinstance(obj, dict) else f"http {status}"
        err = "timeout (需要代理访问 chatgpt.com)" if "timed out" in raw else raw
        return {"error": err}

    # Classify windows by their length, not by position: plans differ in which
    # windows they have (e.g. prolite returns only a 7d window as primary_window
    # and secondary_window=null), so primary/secondary ≠ 5h/7d.
    rl = obj.get("rate_limit") or {}
    windows = {}
    for key in ("primary_window", "secondary_window"):
        d = rl.get(key)
        if not isinstance(d, dict): continue
        secs = d.get("limit_window_seconds")
        if secs == 5 * 3600:        slot = "five_hour"
        elif secs == 7 * 24 * 3600: slot = "weekly"
        elif secs is None:          slot = "five_hour" if key == "primary_window" else "weekly"
        else:                       continue
        pct = d.get("used_percent", 0) or 0
        windows[slot] = {"pct": round(max(0.0, min(100.0, float(pct))), 1),
                         "reset_at": parse_reset(d.get("reset_at"))}
    return {"plan": obj.get("plan_type"), "account": obj.get("email") or _jwt_email(tokens.get("id_token")),
            "five_hour": windows.get("five_hour"), "weekly": windows.get("weekly")}

def _jwt_email(token):
    """只解码 JWT payload 取 email 用于显示，不校验签名。"""
    try:
        payload = token.split(".")[1]
        claims  = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
        return claims.get("email") if isinstance(claims, dict) else None
    except (AttributeError, IndexError, ValueError):
        return None

# ── Claude ────────────────────────────────────────────────────────────────────

def _security(args):
    try:
        return subprocess.run(["/usr/bin/security", *args],
                              capture_output=True, text=True, timeout=10)
    except (OSError, ValueError, subprocess.TimeoutExpired):   # ValueError：输出不是合法 UTF-8
        return None

def _claude_keychain_account():
    out = _security(["find-generic-password", "-s", "Claude Code-credentials"])
    if not out or out.returncode != 0: return None
    for line in out.stdout.splitlines():
        line = line.strip()
        if line.startswith('"acct"') and "=" in line:
            val = line.split("=", 1)[1]
            if val.startswith('"') and val.endswith('"') and len(val) >= 2:
                return val[1:-1] or None
    return None

def _claude_creds_file():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, ".credentials.json")

def _claude_account_email():
    # Claude Code 把登录账号缓存在 ~/.claude.json（设了 CLAUDE_CONFIG_DIR 时在该目录下）
    base = os.environ.get("CLAUDE_CONFIG_DIR")
    path = os.path.join(base, ".claude.json") if base else os.path.expanduser("~/.claude.json")
    try:
        with open(path) as f:
            acct = (json.load(f) or {}).get("oauthAccount") or {}
        return acct.get("emailAddress") if isinstance(acct, dict) else None
    except (OSError, ValueError, AttributeError):
        return None

def _parse_claude_oauth(raw):
    try:
        oauth = (json.loads(raw) or {}).get("claudeAiOauth") or {}
    except (json.JSONDecodeError, AttributeError):
        return None
    if not isinstance(oauth, dict):
        return None
    if not oauth.get("accessToken") or not oauth.get("refreshToken"):
        return None
    return oauth

def _read_claude_creds():
    # Keychain 优先；条目缺失或为空（如 SSH 登录 / 迁移后的机器）时回退到 ~/.claude/.credentials.json
    account = _claude_keychain_account()
    if account:
        out = _security(["find-generic-password", "-s", "Claude Code-credentials",
                         "-a", account, "-w"])
        if out and out.returncode == 0:
            oauth = _parse_claude_oauth(out.stdout.strip())
            if oauth: return {"account": account, "oauth": oauth}
    path = _claude_creds_file()
    try:
        with open(path) as f:
            oauth = _parse_claude_oauth(f.read())
    except (OSError, ValueError):   # ValueError：文件被写到一半、截断在多字节字符中间
        return None
    if not oauth: return None
    return {"file": path, "oauth": oauth}

def _probe_claude(token):
    status, obj = http_get(
        "https://api.anthropic.com/api/oauth/usage",
        headers={
            "Authorization":   f"Bearer {token}",
            "anthropic-beta":  "oauth-2025-04-20",
            "User-Agent":      CLAUDE_CODE_USER_AGENT,
        })
    if status in (401, 403, 429): return None, f"http {status}"
    if status != 200 or not isinstance(obj, dict): return None, f"http {status}"

    def w(key):
        d   = obj.get(key) or {}
        raw = d.get("utilization", d.get("used_percent", 0)) or 0
        return {"pct": round(max(0.0, min(100.0, float(raw))), 1),
                "reset_at": parse_reset(d.get("resets_at"))}
    return {"plan": None, "five_hour": w("five_hour"), "weekly": w("seven_day")}, "ok"

def _refresh_claude_oauth(refresh_token):
    status, obj = http_post(
        "https://platform.claude.com/v1/oauth/token",
        headers={"Content-Type": "application/json"},
        body={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": CLAUDE_OAUTH_CLIENT_ID})
    if status != 200 or not isinstance(obj, dict): return None
    if not obj.get("access_token"): return None
    expires_in = obj.get("expires_in") or 28800
    return {"access_token":  obj["access_token"],
            "refresh_token": obj.get("refresh_token", refresh_token),
            "expires_at":    int((time.time() + expires_in) * 1000)}

def _write_claude_creds(creds, oauth):
    if creds.get("file"):
        # 保留文件里 claudeAiOauth 以外的字段，原子替换并保持 0600。
        # 读不到或解析失败（可能正被 Claude Code 改写）就放弃写回，绝不拿空对象覆盖。
        path  = creds["file"]
        outer = None
        for attempt in range(3):   # 读到半截多半是 Claude Code 正在改写，稍等重试
            try:
                with open(path) as f: outer = json.load(f)
                break
            except (OSError, ValueError):   # ValueError 涵盖 JSONDecodeError 和读到半截多字节字符的 UnicodeDecodeError
                if attempt < 2: time.sleep(0.2)
        if not isinstance(outer, dict):
            return False
        outer["claudeAiOauth"] = oauth
        tmp = None
        try:
            # mkstemp：每个进程一个唯一临时文件，权限固定 0600，并发刷新互不踩
            fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path) or ".", prefix=".credentials.", suffix=".tmp")
            with os.fdopen(fd, "w") as f: json.dump(outer, f)
            os.replace(tmp, path)
            return True
        except OSError:
            if tmp:
                try: os.unlink(tmp)
                except OSError: pass
            return False
    payload = json.dumps({"claudeAiOauth": oauth})
    out = _security(["add-generic-password", "-U",
                     "-s", "Claude Code-credentials", "-a", creds["account"], "-w", payload])
    return bool(out and out.returncode == 0)

def fetch_claude():
    usage = _fetch_claude_usage()
    if "error" not in usage:
        usage["account"] = _claude_account_email()
    return usage

def _fetch_claude_usage():
    last_err = "auth required"
    creds    = _read_claude_creds()

    env_token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN")
    if env_token:
        usage, err = _probe_claude(env_token)
        if usage: return usage
        if err:   last_err = err

    if not creds:
        return {"error": last_err}

    oauth = creds["oauth"]
    usage, err = _probe_claude(oauth["accessToken"])
    if usage: return usage
    if err and "403" in err:
        return {"error": "scope missing — re-login: claude /login"}
    if err: last_err = err

    refreshed = _refresh_claude_oauth(oauth["refreshToken"])
    if refreshed:
        new_oauth = dict(oauth)
        new_oauth["accessToken"]  = refreshed["access_token"]
        new_oauth["refreshToken"] = refreshed["refresh_token"]
        new_oauth["expiresAt"]    = refreshed["expires_at"]
        saved = _write_claude_creds(creds, new_oauth)
        unsaved = "token 已刷新但未能写回凭据，如遇登录失效请 claude /login"
        usage, err = _probe_claude(refreshed["access_token"])
        if usage:
            if not saved:   # 新 token 只在本进程内存里；旧 refresh token 若已作废，下次就得重新登录
                usage["warning"] = unsaved
            return usage
        if err:   last_err = err
        if not saved:   # 查询也失败时同样要提示，否则看起来只是一次网络错误
            last_err = f"{last_err}；{unsaved}"

    return {"error": last_err}

# ── Antigravity (AGY) ─────────────────────────────────────────────────────────

def _read_agy_creds():
    """Read AGY/Gemini OAuth credentials from Keychain or ~/.gemini/oauth_creds.json."""
    account = "antigravity"
    out = _security(["find-generic-password", "-s", "gemini", "-a", account, "-w"])
    if out and out.returncode == 0:
        val = out.stdout.strip()
        if val.startswith("go-keyring-base64:"):
            try:
                raw = base64.b64decode(val[len("go-keyring-base64:"):])
                data = json.loads(raw)
                tok = data.get("token") or {}
                if tok.get("access_token") or tok.get("refresh_token"):
                    email = None
                    id_token = data.get("id_token")
                    if id_token and "." in id_token:
                        try:
                            payload_seg = id_token.split(".")[1]
                            payload_seg += "=" * ((4 - len(payload_seg) % 4) % 4)
                            jwt_payload = json.loads(base64.b64decode(payload_seg))
                            email = jwt_payload.get("email")
                        except Exception:
                            pass
                    return {
                        "source": "keychain",
                        "account": account,
                        "raw_data": data,
                        "access_token": tok.get("access_token"),
                        "refresh_token": tok.get("refresh_token"),
                        "expiry": tok.get("expiry"),
                        "email": email,
                    }
            except Exception:
                pass

    fpath = os.path.expanduser("~/.gemini/oauth_creds.json")
    if os.path.exists(fpath):
        try:
            with open(fpath) as f:
                d = json.load(f)
            if d.get("access_token") or d.get("refresh_token"):
                email = None
                acc_path = os.path.expanduser("~/.gemini/google_accounts.json")
                if os.path.exists(acc_path):
                    try:
                        with open(acc_path) as af:
                            email = json.load(af).get("active")
                    except Exception:
                        pass
                return {
                    "source": "file",
                    "file_path": fpath,
                    "access_token": d.get("access_token"),
                    "refresh_token": d.get("refresh_token"),
                    "expiry": d.get("expiry_date"),
                    "email": email,
                }
        except Exception:
            pass

    return None

def _write_agy_creds(creds, new_token, expires_in=3600):
    creds["access_token"] = new_token
    if creds.get("source") == "keychain" and creds.get("raw_data"):
        raw_data = creds["raw_data"]
        raw_data.setdefault("token", {})["access_token"] = new_token
        exp_iso = datetime.fromtimestamp(time.time() + expires_in).isoformat()
        raw_data["token"]["expiry"] = exp_iso
        raw_str = json.dumps(raw_data)
        encoded = "go-keyring-base64:" + base64.b64encode(raw_str.encode()).decode()
        _security(["add-generic-password", "-U", "-s", "gemini", "-a", creds["account"], "-w", encoded])
    elif creds.get("source") == "file":
        try:
            fpath = creds["file_path"]
            with open(fpath, "r") as f:
                d = json.load(f)
            d["access_token"] = new_token
            d["expiry_date"] = int((time.time() + expires_in) * 1000)
            with open(fpath, "w") as f:
                json.dump(d, f, indent=2)
        except Exception:
            pass

def _refresh_agy_oauth(creds):
    rf = creds.get("refresh_token")
    if not rf: return None
    body = {
        "client_id": AGY_CLIENT_ID,
        "client_secret": AGY_CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": rf
    }
    status, obj = http_post("https://oauth2.googleapis.com/token",
                            headers={"Content-Type": "application/json"},
                            body=body)
    if status != 200 or not isinstance(obj, dict): return None
    new_token = obj.get("access_token")
    if not new_token: return None
    expires_in = obj.get("expires_in") or 3600
    _write_agy_creds(creds, new_token, expires_in)
    return new_token

def _get_agy_local_usage():
    """Scan local Antigravity CLI logs to calculate daily and 5h sliding window usage."""
    log_dir = os.path.expanduser("~/.gemini/antigravity-cli/log")
    if not os.path.exists(log_dir):
        return None

    now = datetime.now()
    today_prefix = "I" + now.strftime("%m%d")

    import glob, re
    files = glob.glob(os.path.join(log_dir, "cli-*.log"))
    recent_files = [f for f in files if (now.timestamp() - os.path.getmtime(f)) < 86400 * 2]

    today_reqs = 0
    five_hour_reqs = 0
    now_ts = now.timestamp()
    five_hours_ago = now_ts - 5 * 3600

    pattern = re.compile(r"^I(\d{2})(\d{2}) (\d{2}):(\d{2}):(\d{2})\.\d+.*?v1internal:(streamGenerateContent|generateContent)")

    for fpath in recent_files:
        fname = os.path.basename(fpath)
        m_year = re.search(r"cli-(\d{4})", fname)
        file_year = int(m_year.group(1)) if m_year else now.year

        try:
            with open(fpath, "r", errors="ignore") as f:
                for line in f:
                    if "v1internal:streamGenerateContent" not in line and "v1internal:generateContent" not in line:
                        continue
                    m = pattern.search(line)
                    if m:
                        mm, dd, HH, MM, SS, _ = m.groups()
                        try:
                            req_dt = datetime(file_year, int(mm), int(dd), int(HH), int(MM), int(SS))
                            req_ts = req_dt.timestamp()
                            if req_dt.date() == now.date():
                                today_reqs += 1
                            if req_ts >= five_hours_ago:
                                five_hour_reqs += 1
                        except Exception:
                            if line.startswith(today_prefix):
                                today_reqs += 1
                    elif line.startswith(today_prefix):
                        today_reqs += 1
        except Exception:
            pass

    end_of_day = datetime(now.year, now.month, now.day, 23, 59, 59)
    daily_reset_at = int(end_of_day.timestamp())
    daily_limit = 1500
    daily_pct = round(min(100.0, (today_reqs / float(daily_limit)) * 100), 1)

    return {
        "daily": {
            "pct": daily_pct,
            "used": today_reqs,
            "limit": daily_limit,
            "reset_at": daily_reset_at,
            "window_sec": 86400,
        },
        "five_hour_reqs": five_hour_reqs,
    }

def _probe_agy(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Antigravity/1.0",
    }
    tier_info = {}
    project = "aicode-consumers"
    for host in ["https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"]:
        url = host + "/v1internal:loadCodeAssist"
        body = {
            "mode": "HEALTH_CHECK",
            "metadata": {"ideType": "ANTIGRAVITY", "platform": "DARWIN_ARM64"}
        }
        status, data = http_post(url, headers=headers, body=body)
        if status == 401:
            return None, "http 401"
        if status == 200 and isinstance(data, dict):
            ct = data.get("currentTier") or {}
            pt = data.get("paidTier") or {}
            credits_obj = pt.get("availableCredits") or ct.get("availableCredits") or {}
            tier_info = {
                "tier_id": pt.get("id") or ct.get("id"),
                "tier_name": pt.get("name") or ct.get("name"),
                "paid_tier_id": pt.get("id"),
                "current_tier_id": ct.get("id"),
                "credits": credits_obj.get("creditAmount"),
                "upgrade_text": ct.get("upgradeSubscriptionText") or "",
                "project": data.get("cloudaicompanionProject") or project,
            }
            project = tier_info["project"]
            break

    if not tier_info:
        return None, "backend connect failed"

    windows = {}
    groups_meta = {}
    for host in ["https://daily-cloudcode-pa.googleapis.com", "https://cloudcode-pa.googleapis.com"]:
        url = host + "/v1internal:retrieveUserQuotaSummary"
        body = {"project": project}
        status, qdata = http_post(url, headers=headers, body=body)
        if status == 401:
            return None, "http 401"
        if status == 200 and isinstance(qdata, dict):
            # 真实结构是 groups[].buckets[]，不是顶层 buckets —— 读错路径会一个桶
            # 都拿不到，然后退回本地日志的假配额（2026-10-03 修）。
            found = []
            for grp in (qdata.get("groups") or []):
                gname = grp.get("displayName") or "?"
                groups_meta[gname] = grp.get("description") or ""
                for b in (grp.get("buckets") or []):
                    found.append((gname, b))
            for b in (qdata.get("buckets") or []):   # 兼容极少数顶层返回
                found.append((None, b))

            for gname, b in found:
                bid  = str(b.get("bucketId") or "").lower()
                wstr = str(b.get("window") or b.get("bucketId") or "").lower()
                # 组归属：3p-* = Claude/GPT 模型池，其余算 Gemini 池
                pool = "3p" if bid.startswith("3p") or "claude" in (gname or "").lower() else "gemini"
                if "5h" in wstr or "five" in wstr:
                    win = "5h"
                elif "week" in wstr or "7d" in wstr or "seven" in wstr:
                    win = "weekly"
                elif "day" in wstr or "24h" in wstr:
                    win = "daily"
                else:
                    continue
                rem = b.get("remainingFraction")
                pct = round(max(0.0, min(100.0, (1.0 - float(rem)) * 100)), 1) if rem is not None else None
                windows[f"{pool}_{win}"] = {
                    "pct": pct,
                    "reset_at": parse_reset(b.get("resetTime") or b.get("reset_time")),
                    "bucket_id": b.get("bucketId"),
                    "display_name": b.get("displayName"),
                    "remaining_fraction": rem,
                    "note": b.get("description"),
                }
            break

    local_usage = _get_agy_local_usage()

    # 别名：five_hour / weekly 指向 Gemini 池（agy 默认烧的那个），保持
    # render_compact 与每日基线统计不炸。Antigravity 服务端没有 daily 桶，
    # 所以 daily 别名通常为 None —— 本地计数另走 local_reqs_today。
    for alias, key in (("five_hour", "gemini_5h"), ("weekly", "gemini_weekly"), ("daily", "gemini_daily")):
        if key in windows:
            windows[alias] = windows[key]

    # 别再拿 upgrade_text 里的 "1,500 reqs/day" 当套餐配额：那是 free-tier 推销
    # Gemini CLI / Code Assist 的话术，跟 Antigravity agent 的真实配额不是一回事。
    # 真实配额 = 服务端 5h + weekly 的 remainingFraction 桶（按 token 成本扣）。
    plan_name = tier_info.get("tier_name") or "Antigravity"
    if not tier_info.get("paid_tier_id"):
        plan_name += "（free）"

    return {
        "plan": plan_name,
        "tier_id": tier_info.get("tier_id"),
        "paid_tier_id": tier_info.get("paid_tier_id"),
        "current_tier_id": tier_info.get("current_tier_id"),
        "groups": groups_meta,
        "credits": tier_info.get("credits"),
        "upgrade_text": tier_info.get("upgrade_text"),
        "five_hour_reqs": (local_usage or {}).get("five_hour_reqs", 0),
        "local_reqs_today": ((local_usage or {}).get("daily") or {}).get("used"),
        "gemini_5h": windows.get("gemini_5h"),
        "gemini_weekly": windows.get("gemini_weekly"),
        "3p_5h": windows.get("3p_5h"),
        "3p_weekly": windows.get("3p_weekly"),
        "five_hour": windows.get("five_hour"),
        "weekly": windows.get("weekly"),
        "daily": windows.get("daily"),
    }, "ok"

def fetch_agy():
    creds = _read_agy_creds()
    env_token = os.environ.get("AGY_ACCESS_TOKEN") or os.environ.get("GEMINI_OAUTH_TOKEN")
    if env_token:
        usage, err = _probe_agy(env_token)
        if usage:
            usage["account"] = creds.get("email") if creds else None
            return usage

    if not creds:
        return {"error": "no agy auth (run agy once or check keychain)"}

    usage, err = _probe_agy(creds["access_token"])
    if usage:
        usage["account"] = creds.get("email")
        return usage

    if err == "http 401" and creds.get("refresh_token"):
        new_tok = _refresh_agy_oauth(creds)
        if new_tok:
            usage, err = _probe_agy(new_tok)
            if usage:
                usage["account"] = creds.get("email")
                return usage

    return {"error": err or "auth failed"}

# ── Grok (xAI) ────────────────────────────────────────────────────────────────

def _read_grok_creds():
    """Read Grok OAuth credentials from ~/.grok/auth.json."""
    if not GROK_AUTH_FILE.exists():
        return None
    try:
        data = json.loads(GROK_AUTH_FILE.read_text())
        if not isinstance(data, dict):
            return None
        for scope_key, entry in data.items():
            if isinstance(entry, dict) and entry.get("key"):
                return {
                    "scope_key": scope_key,
                    "access_token": entry.get("key"),
                    "refresh_token": entry.get("refresh_token"),
                    "expires_at": entry.get("expires_at"),
                    "email": entry.get("email"),
                    "oidc_issuer": entry.get("oidc_issuer") or "https://auth.x.ai",
                    "oidc_client_id": entry.get("oidc_client_id") or GROK_CLIENT_ID,
                }
    except Exception:
        pass
    return None

def _write_grok_creds(creds, new_token, new_refresh_token=None, expires_in=21600):
    scope_key = creds.get("scope_key")
    if not scope_key or not GROK_AUTH_FILE.exists():
        return
    try:
        with open(GROK_AUTH_FILE, "r") as f:
            full_auth = json.load(f)
        if scope_key in full_auth:
            exp_iso = datetime.fromtimestamp(time.time() + expires_in, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            full_auth[scope_key]["key"] = new_token
            if new_refresh_token:
                full_auth[scope_key]["refresh_token"] = new_refresh_token
            full_auth[scope_key]["expires_at"] = exp_iso
            with open(GROK_AUTH_FILE, "w") as f:
                json.dump(full_auth, f, indent=2)
    except Exception:
        pass

def _refresh_grok_oauth(creds):
    rf = creds.get("refresh_token")
    if not rf:
        return None
    client_id = creds.get("oidc_client_id") or GROK_CLIENT_ID
    issuer = (creds.get("oidc_issuer") or "https://auth.x.ai").rstrip("/")
    url = f"{issuer}/oauth2/token"

    import urllib.parse
    body_data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "refresh_token": rf,
        "client_id": client_id,
    }).encode("utf-8")

    req = Request(url, data=body_data, method="POST", headers={
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": GROK_CLI_USER_AGENT,
    })
    try:
        with urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            data = json.loads(resp.read())
    except Exception:
        return None

    new_token = data.get("access_token")
    if not new_token:
        return None
    new_rf = data.get("refresh_token", rf)
    expires_in = data.get("expires_in", 21600)
    _write_grok_creds(creds, new_token, new_rf, expires_in)
    return new_token

def _probe_grok_bot():
    """Fetch Grok Bot independent usage if CURSOR_SESSION_TOKEN / GROK_BOT_TOKEN is set."""
    token = load_env_grok_bot_token()
    if token:
        import urllib.parse
        cookie_val = urllib.parse.quote(token) if not token.startswith("ey") and "%" not in token else token
        status, data = http_get(
            "https://www.cursor.com/api/usage-summary",
            headers={
                "Cookie": f"WorkosCursorSessionToken={cookie_val}",
                "User-Agent": "Mozilla/5.0",
            }
        )
        if status == 200 and isinstance(data, dict):
            fast_used = data.get("numFastRequests") or data.get("numRequests") or 0
            fast_max = data.get("maxFastRequests") or data.get("maxRequests") or 500
            pct = round(min(100.0, (float(fast_used) / float(fast_max)) * 100), 1) if fast_max else 0.0
            return {
                "pct": pct,
                "used": fast_used,
                "limit": fast_max,
                "display_name": "Grok Bot 池 (Bot / Cursor 独立周额度)",
                "note": "从 Cursor API 同步",
            }
    return None

def _probe_grok(token):
    headers = {
        "Authorization": f"Bearer {token}",
        "User-Agent": GROK_CLI_USER_AGENT,
        "Accept": "application/json",
    }
    status, bdata = http_get("https://cli-chat-proxy.grok.com/v1/billing?format=credits", headers=headers)
    if status == 401:
        return None, "http 401"
    if status != 200 or not isinstance(bdata, dict):
        status, bdata = http_get("https://cli-chat-proxy.grok.com/v1/billing", headers=headers)
        if status == 401:
            return None, "http 401"
        if status != 200 or not isinstance(bdata, dict):
            err = bdata.get("_transport_error", f"http {status}") if isinstance(bdata, dict) else f"http {status}"
            return None, err

    _, sdata = http_get("https://cli-chat-proxy.grok.com/v1/settings", headers=headers)
    plan_name = None
    if isinstance(sdata, dict):
        plan_name = sdata.get("subscription_tier_display") or sdata.get("subscription_tier")

    _, udata = http_get("https://cli-chat-proxy.grok.com/v1/user", headers=headers)
    email = None
    if isinstance(udata, dict):
        email = udata.get("email")

    cfg = bdata.get("config") or {}
    cur_period = cfg.get("currentPeriod") or {}

    pct_raw = cfg.get("creditUsagePercent")
    if isinstance(pct_raw, dict):
        pct_val = pct_raw.get("val", 0)
    elif isinstance(pct_raw, (int, float)):
        pct_val = pct_raw
    else:
        pct_val = 0.0

    reset_str = cur_period.get("end") or cfg.get("billingPeriodEnd")
    reset_at = parse_reset(reset_str)

    def _get_val(obj):
        if isinstance(obj, dict): return obj.get("val", 0)
        if isinstance(obj, (int, float)): return obj
        return 0

    prepaid = _get_val(cfg.get("prepaidBalance"))
    ondemand_used = _get_val(cfg.get("onDemandUsed"))
    ondemand_cap = _get_val(cfg.get("onDemandCap"))

    main_weekly = {
        "pct": round(max(0.0, min(100.0, float(pct_val))), 1),
        "reset_at": reset_at,
        "display_name": "主周池 (Chat · Imagine · Voice · Build)",
    }
    bot_weekly = _probe_grok_bot()

    return {
        "plan": plan_name or "SuperGrok Plus",
        "account": email,
        "weekly": main_weekly,
        "main_weekly": main_weekly,
        "bot_weekly": bot_weekly,
        "prepaid_balance": prepaid,
        "on_demand_used": ondemand_used,
        "on_demand_cap": ondemand_cap,
    }, "ok"

def fetch_grok():
    creds = _read_grok_creds()
    env_token = os.environ.get("GROK_ACCESS_TOKEN") or os.environ.get("XAI_API_KEY") or load_env_grok_key()
    if env_token:
        usage, err = _probe_grok(env_token)
        if usage:
            if not usage.get("account") and creds:
                usage["account"] = creds.get("email")
            return usage

    if not creds:
        return {"error": "no grok auth (run grok once or set XAI_API_KEY in ~/.see-balance.env)"}

    # Proactive refresh if within 3 minutes of expiry
    exp_ts = parse_reset(creds.get("expires_at"))
    if exp_ts and time.time() > (exp_ts - 180) and creds.get("refresh_token"):
        new_tok = _refresh_grok_oauth(creds)
        if new_tok:
            creds["access_token"] = new_tok

    usage, err = _probe_grok(creds["access_token"])
    if usage:
        if not usage.get("account"):
            usage["account"] = creds.get("email")
        return usage

    if err == "http 401" and creds.get("refresh_token"):
        new_tok = _refresh_grok_oauth(creds)
        if new_tok:
            usage, err = _probe_grok(new_tok)
            if usage:
                if not usage.get("account"):
                    usage["account"] = creds.get("email")
                return usage

    return {"error": err or "auth failed"}

# ── OpenCode（Anthropic API key）────────────────────────────────────────────
#
# Anthropic 没有给普通 API key 查余额的接口：这里只验 key 是否还能用（GET /v1/models，不耗 token），
# 花了多少从 opencode 自己的本地库里按消息累加（opencode 每条回复都记了 cost 和 tokens）。

OPENCODE_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "opencode"

def _opencode_auth():
    try:
        with open(OPENCODE_DATA_DIR / "auth.json") as f:
            entry = json.load(f).get("anthropic")
    except (OSError, ValueError, AttributeError):
        return None
    return entry if isinstance(entry, dict) else None

def _opencode_spend(now=None):
    """opencode 本地库里 anthropic 回复的花费：今天 / 近 7 天 / 近 30 天。读不了返回 None。"""
    import sqlite3
    db = OPENCODE_DATA_DIR / "opencode.db"
    if not db.exists():
        return None
    now       = now or time.time()
    day_start = datetime.fromtimestamp(now).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    spans     = {"today": day_start, "7d": now - 7 * 86400, "30d": now - 30 * 86400}
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=2)
        try:
            rows = conn.execute(
                "SELECT time_created, json_extract(data, '$.cost'),"
                " json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'), json_extract(data, '$.tokens.reasoning'),"
                " json_extract(data, '$.tokens.cache.read'), json_extract(data, '$.tokens.cache.write')"
                " FROM message WHERE time_created >= ?"
                " AND json_extract(data, '$.role') = 'assistant'"
                " AND json_extract(data, '$.providerID') = 'anthropic'",
                (int(spans["30d"] * 1000),)).fetchall()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    out = {k: {"cost": 0.0, "tokens": 0, "msgs": 0} for k in spans}
    for ts_ms, cost, *toks in rows:
        for k, start in spans.items():
            if (ts_ms or 0) / 1000 >= start:
                out[k]["cost"]   += float(cost or 0)
                out[k]["tokens"] += sum(int(t or 0) for t in toks)
                out[k]["msgs"]   += 1
    for v in out.values():
        v["cost"] = round(v["cost"], 4)
    return out

OPENCODE_KEY_EXPIRY_WARN_SEC = 7 * 86400   # key 离过期不足此时长就提醒

def _anthropic_key_info(key, admin_key):
    """用 Admin API 列出组织里的 key，按 partial_key_hint（如 sk-ant-api03-R2D...igAA）对上这个 key。"""
    after = None
    for _ in range(10):   # 最多翻 10 页（1000 个 key）
        url = "https://api.anthropic.com/v1/organizations/api_keys?limit=100" + (f"&after_id={after}" if after else "")
        status, obj = http_get(url, headers={"x-api-key": admin_key, "anthropic-version": "2023-06-01"})
        if status != 200 or not isinstance(obj, dict):
            err = obj.get("_transport_error") if isinstance(obj, dict) and obj.get("_transport_error") else f"http {status}"
            return None, f"Admin API 查询失败：{err}"
        for k in obj.get("data") or []:
            head, sep, tail = (k.get("partial_key_hint") or "").partition("...")
            if sep and key.startswith(head) and key.endswith(tail):
                return k, None
        if not obj.get("has_more") or not obj.get("last_id"):
            break
        after = obj["last_id"]
    return None, "Admin API 里没找到这个 key（是不是别的组织的？）"

def fetch_opencode():
    auth = _opencode_auth()
    if not auth:
        return {"error": "no anthropic auth in opencode (run `opencode auth login`)"}
    out = {"spend": _opencode_spend()}
    if auth.get("type") == "oauth":
        out["auth"] = "Claude 订阅 (OAuth)"
        out["note"] = "走的是 Claude 订阅，额度和上面的 Claude Code 共用"
        return out
    key = auth.get("key") or ""
    if not key:
        return {"error": "opencode anthropic auth has no key"}
    out["auth"] = f"API key …{key[-4:]}"
    status, obj = http_get("https://api.anthropic.com/v1/models?limit=1",
                           headers={"x-api-key": key, "anthropic-version": "2023-06-01"})
    if status == 200:
        out["key_ok"] = True
    elif status in (401, 403):
        out["key_ok"] = False
        out["warning"] = f"key 无效或已停用 (http {status})"
    else:
        err = obj.get("_transport_error") if isinstance(obj, dict) and obj.get("_transport_error") else f"http {status}"
        out["warning"] = f"key 校验失败：{err}"
    admin_key = load_env_anthropic_admin_key()
    if admin_key:
        info, err = _anthropic_key_info(key, admin_key)
        if info:
            out["key_name"]   = info.get("name")
            out["key_status"] = info.get("status")
            out["expires_at"] = parse_reset(info.get("expires_at"))   # None = 永不过期
            out["expiry_known"] = True
        else:
            out["expiry_error"] = err
    return out

# ── Render ────────────────────────────────────────────────────────────────────

# ── 跨订阅调度建议 ───────────────────────────────────────────────────────────
#
# 把几家订阅的 7d 窗口放在一起看：哪家要超、哪家用完了、哪家闲着、哪家快重置还剩一大截。
# 要超 / 用完 → 指一家有余量的去分担；快重置还剩很多 → 劝它放开用（高 effort、强模型、加速、并行）。

SPARE_MIN_LEFT = 30.0         # 剩余 ≥ 此值才算“有余量”，能接别家的活儿
SPRINT_SEC     = 24 * 3600    # 距 7d 重置不足此时长 ……
SPRINT_WASTE   = 20.0         # …… 且照现在的速度到期会剩下 ≥ 此值：放开用
FRESH_MAX_PCT  = 5.0          # 窗口刚开、还没法推算时，用量 ≤ 此值才算闲着（刚开窗就猛烧的不算）
FH_TIGHT_PCT   = 80.0         # 同池 5h 用量 ≥ 此值，或 5h 预计到期超速：眼下不宜再加活儿
SOON_SEC       = 900          # 离 7d 重置不足 15 分钟：马上就是新窗口，不再谈节奏

# (provider, 7d 窗口, 同池 5h 窗口, 全称, 宠物气泡里的短名, 放开用的办法)
SUBSCRIPTIONS = [
    ("claude", "weekly",        "five_hour", "Claude", "Claude",
     "可以切 Opus、调高 effort、开 /fast，或多开几个并行 agent"),
    ("codex",  "weekly",        "five_hour", "Codex",  "Codex",
     "可以换最强模型、把 model_reasoning_effort 调到 xhigh，或多开几个并行任务"),
    ("grok",   "main_weekly",   None,        "Grok",   "Grok",
     "可以换最强模型，或多跑几个并行任务"),
    ("agy",    "gemini_weekly", "gemini_5h", "AGY Gemini 池", "Gemini",
     "可以换 Gemini Pro，或多开几个并行 agent"),
    ("agy",    "3p_weekly",     "3p_5h",     "AGY Claude·GPT 池", "AGY",
     "可以换 Opus，或多开几个并行 agent"),
]


def _fh_runs_out(fh, hist, hist_key, now):
    """5h 窗口会不会在重置前用完：看近期实测速率（有历史时），以及窗口开头就猛烧的情况。

    pace_projection 在窗口前 5% 不出值，开窗十分钟就用掉七成也看不出来，这里不设那个门槛。
    """
    if fh.get("pct") is None or not fh.get("reset_at"):
        return False
    pct, reset_in = float(fh["pct"]), max(0.0, fh["reset_at"] - now)
    if reset_in < SOON_SEC:
        return False
    elapsed = 5 * 3600 - reset_in
    if elapsed >= 60 and pct / elapsed * reset_in + pct > 100.0:
        return True
    r = burn_rate(hist, hist_key, 5 * 3600) if hist_key else None
    return bool(r and r["per_hour"] > 0.005 and (100.0 - pct) / r["per_hour"] * 3600 < reset_in)


def sub_status(data, now=None, hist=None):
    """每个订阅池一条：用量、离重置多久、预计到期用量，以及归类 out / hot / sprint / slow / ok。"""
    now, out = now or time.time(), []
    for prov, wkey, fkey, name, short, boost in SUBSCRIPTIONS:
        p = data.get(prov) or {}
        if "error" in p:
            continue
        wk = p.get(wkey) or (p.get("weekly") if prov == "grok" else None) or {}
        if wk.get("pct") is None:
            continue
        pct   = float(wk["pct"])
        fh    = (p.get(fkey) or {}) if fkey else {}
        st    = {"prov": prov, "name": name, "short": short, "boost": boost, "pct": pct,
                 "left": max(0.0, 100.0 - pct), "reset_in": None, "proj": None, "kind": "ok",
                 "fh_out": fh.get("pct") is not None and float(fh["pct"]) >= EXHAUSTED_PCT,
                 "fh_reset_in": max(0.0, fh["reset_at"] - now) if fh.get("reset_at") else None}
        # 5h 紧：已经 ≥80%，或照近期速度 / 开窗以来的速度会在重置前用完（和 5h 的“建议”行同一口径）
        st["fh_tight"] = st["fh_out"] or (fh.get("pct") is not None and (
            float(fh["pct"]) >= FH_TIGHT_PCT
            or _fh_runs_out(fh, hist, HIST_KEY_BY_WINDOW.get((prov, fkey)), now)))
        if wk.get("reset_at"):
            st["reset_in"] = max(0.0, wk["reset_at"] - now)
            elapsed = 7 * 24 * 3600 - st["reset_in"]
            if elapsed >= 7 * 24 * 3600 * 0.05:    # 和 pace_projection 同一门槛，但快重置时照样推算
                st["proj"] = pct + pct / elapsed * st["reset_in"]
        proj = st["proj"]
        if pct >= EXHAUSTED_PCT:
            st["kind"] = "out"
        elif proj is not None and proj > PACE_FAST_PCT:
            st["kind"] = "hot"
        elif proj is not None and st["reset_in"] < SOON_SEC:
            pass                                   # 马上重置：既不劝放开用，也不劝多给活儿
        elif proj is not None and st["reset_in"] <= SPRINT_SEC and 100.0 - proj >= SPRINT_WASTE:
            st["kind"] = "sprint"
        elif proj is not None and proj <= PACE_SLOW_PCT and st["left"] >= SPARE_MIN_LEFT:
            st["kind"] = "slow"
        out.append(st)
    return out


def spare_candidates(stats, exclude):
    """能接活儿的别的订阅池（exclude 是要排除的池名，或整个 provider）：5h 不紧、剩得够多，且 7d 闲着 ——
    快重置还剩一大截（sprint）、节奏偏慢（slow），或窗口刚开、几乎没用。

    和 ⏰ / 💤 行用同一套标准，免得一边说“别家也不宽裕”、一边又劝多用某家。
    """
    return [s for s in stats if exclude not in (s["name"], s["prov"]) and not s["fh_tight"]
            and s["left"] >= SPARE_MIN_LEFT
            and (s["kind"] in ("sprint", "slow")
                 or (s["kind"] == "ok" and s["proj"] is None and s["reset_in"] is not None
                     and s["pct"] <= FRESH_MAX_PCT))]   # 不知道窗口在哪（没有重置时间）的不算


def best_spare(stats, exclude):
    """挑一个来分担：sprint 优先 —— 不用就过期了；其余按到期还能空出多少排。"""
    def headroom(s):
        return 100.0 - (s["proj"] if s["proj"] is not None else s["pct"])
    return max(spare_candidates(stats, exclude), key=lambda s: (s["kind"] == "sprint", headroom(s)), default=None)


def dispatch_advice(data, now=None, hist=None):
    """调度建议行（已含缩进）；没什么可说时返回空列表。"""
    stats, lines, helpers = sub_status(data, now, hist), [], set()

    def spare_text(s):
        alt = best_spare(stats, s["name"])   # 按池排除：AGY 两个池互不相通
        if not alt:
            return None
        helpers.add(alt["name"])
        when = f"，{fmt_countdown(alt['reset_in'])} 后重置" if alt["reset_in"] is not None else ""
        return f"要不试试 {alt['name']}？还剩 {alt['left']:.0f}%{when}，人家也很强哦"

    for s in stats:
        if s["kind"] == "out":
            alt  = spare_text(s)
            when = f"，{fmt_countdown(s['reset_in'])} 后重置" if s["reset_in"] is not None else ""
            lines.append(f"     ⛔ {s['name']} 周额度用完了{when}"
                         + (f" → {alt}" if alt else ""))
            continue
        if s["fh_out"]:
            alt  = spare_text(s)
            when = f"，{fmt_countdown(s['fh_reset_in'])} 后恢复" if s["fh_reset_in"] is not None else ""
            lines.append(f"     ⛔ {s['name']} 5h 额度用光了{when}"
                         + (f" → 等的时候{alt}" if alt else ""))
        if s["kind"] == "hot":   # 5h 用光时照样说：5h 一恢复，周额度还是会超
            if s["fh_out"]:      # 上一行已经推荐过别家
                tail = " → 5h 恢复后也悠着点，可以试试调低 effort 或换个小模型"
            else:
                alt  = spare_text(s)
                tail = f" → {alt}" if alt else " → 别家也不太宽裕，可以试试调低 effort 或换个小模型"
            lines.append(f"     ⚠ {s['name']} 照这样到期要用 {s['proj']:.0f}%{tail}")
    for s in stats:
        if s["fh_tight"]:   # 5h 紧或已卡住，眼下不宜“放开用 / 多给活儿”
            continue
        if s["kind"] == "sprint":
            lines.append(f"     ⏰ {s['name']} {fmt_countdown(s['reset_in'])} 后重置，还剩 {s['left']:.0f}%，"
                         f"照这样会空着约 {100.0 - s['proj']:.0f}% → 不如放开用？{s['boost']}")
        elif s["kind"] == "slow" and s["name"] not in helpers:
            lines.append(f"     💤 {s['name']} 预计到期只用 {s['proj']:.0f}%（还剩 {s['left']:.0f}%）→ 闲着也是闲着，要不多给它点活儿？")
    return lines


def render_all(ds, cx, cl, agy, gr, prev_snap, today_bl: dict, hist=None, oc=None):
    now   = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"══════════  Provider Balance  •  {now}  ══════════", ""]

    lines.append("  🔵 DeepSeek API")
    if "error" in ds:
        lines.append(f"     ⚠ {ds['error']}")
    else:
        lines.append(f"     status      {'✓ online' if ds.get('available') else '✗ offline'}")
        lines.append(f"     CNY left    {ds['cny_left']}   (topped_up: {ds['cny_topped']}  granted: {ds['cny_granted']})")
        if prev_snap and "error" not in prev_snap.get("deepseek", {"error": ""}):
            pv = float(prev_snap["deepseek"].get("cny_left", 0))
            cv = float(ds.get("cny_left", 0))
            if pv > cv:
                spent = round(pv - cv, 4)
                lines.append(f"     spent       {spent} CNY  ≈  ${round(spent * 0.14, 4)} USD since last check")
        b, a = deepseek_lines(ds, hist)
        if b: lines.append(b)
        if a: lines.append(a)
    lines.append("")

    lines.append("  🟢 Codex")
    if "error" in cx:
        lines.append(f"     ⚠ {cx['error']}")
    else:
        if cx.get("account"): lines.append(f"     account     {cx['account']}")
        if cx.get("plan"): lines.append(f"     plan        {cx['plan']}")
        lines.append(f"     5h used     {window_pct_bar(cx.get('five_hour')) if cx.get('five_hour') else '— (当前套餐无 5h 窗口)'}")
        lines.append(f"     7d used     {window_pct_bar(cx.get('weekly')) if cx.get('weekly') else '— (当前套餐无 7d 窗口)'}")
        d = today_target_line(cx.get("weekly") or {}, today_bl.get("codex_weekly"), today_bl.get("codex_weekly_daily_target"))
        if d: lines.append(d)
        p = pace_line(cx.get("weekly") or {}, 7 * 24 * 3600)
        if p: lines.append(p)
        b, a = burn_advice(cx.get("weekly"), advice_rate(cx.get("weekly"), hist, "codex_weekly", 7 * 24 * 3600))
        if b: lines.append(b)
        if a: lines.append(a)
    lines.append("")

    lines.append("  🟣 Claude Code (Max 200)")
    if "error" in cl:
        lines.append(f"     ⚠ {cl['error']}")
    else:
        if cl.get("account"): lines.append(f"     account     {cl['account']}")
        if cl.get("plan"): lines.append(f"     plan        {cl['plan']}")
        if cl.get("warning"): lines.append(f"     ⚠ {cl['warning']}")
        # 每个窗口的实测消耗 / 建议紧跟在它自己那行下面，免得 5h 和 7d 的结论看着像在打架
        lines.append(f"     5h used     {window_pct_bar(cl.get('five_hour'))}")
        b, a = burn_advice(cl.get("five_hour"), burn_rate(hist, "claude_5h", 5 * 3600))
        if b: lines.append(b)
        if a: lines.append(a)
        lines.append(f"     7d used     {window_pct_bar(cl.get('weekly'))}")
        d = today_target_line(cl.get("weekly", {}), today_bl.get("claude_weekly"), today_bl.get("claude_weekly_daily_target"))
        if d: lines.append(d)
        p = pace_line(cl.get("weekly", {}), 7 * 24 * 3600)
        if p: lines.append(p)
        b, a = burn_advice(cl.get("weekly"), advice_rate(cl.get("weekly"), hist, "claude_weekly", 7 * 24 * 3600))
        if b: lines.append(b)
        if a: lines.append(a)
    lines.append("")

    lines.append("  🔴 Antigravity (AGY)")
    if "error" in agy:
        lines.append(f"     ⚠ {agy['error']}")
    else:
        if agy.get("account"):
            lines.append(f"     account     {agy['account']}")
        if agy.get("plan"):
            lines.append(f"     plan        {agy['plan']}")
        if agy.get("credits") is not None:
            lines.append(f"     credits     {agy['credits']} available")
        else:
            lines.append("     credits     0 available (/credits to add more)")
        def _pool(title, key5h, keywk, w5h, wk):
            if not w5h and not wk:
                return
            lines.append("     ▸ " + title)
            if w5h:
                lines.append("     5h used     " + window_pct_bar(w5h))
                p = pace_line(w5h, 5 * 3600)
                if p: lines.append(p)
                b, a = burn_advice(w5h, burn_rate(hist, key5h, 5 * 3600))
                if b: lines.append(b)
                if a: lines.append(a)
            if wk:
                lines.append("     7d used     " + window_pct_bar(wk))
                d = today_target_line(wk, today_bl.get("agy_weekly"),
                                      today_bl.get("agy_weekly_daily_target"))
                if d: lines.append(d)
                p = pace_line(wk, 7 * 24 * 3600)
                if p: lines.append(p)
                b, a = burn_advice(wk, advice_rate(wk, hist, keywk, 7 * 24 * 3600), alt_hint=alt_hint)
                if b: lines.append(b)
                if a: lines.append(a)

        # 两个池互不相通：Gemini 池见底时，3P 池还是完整的
        # 3P 池算不算有余量，和调度建议用同一套标准，免得两处说法打架
        g_left   = 100.0 - float((agy.get("gemini_weekly") or {}).get("pct") or 0.0)
        p3_spare = any(s["name"] == "AGY Claude·GPT 池" for s in spare_candidates(sub_status({"agy": agy}, None, hist), "AGY Gemini 池"))
        alt_hint = None
        if g_left < 30.0 and p3_spare:
            alt_hint = "agy 的 Claude·GPT 池仍有余量 → `--agent antigravity --model <claude/gpt 模型 id>`"
        _pool("Gemini 池 (Gemini Flash / Pro)", "agy_gemini_5h", "agy_gemini_weekly",
              agy.get("gemini_5h"), agy.get("gemini_weekly"))
        _pool("Claude·GPT 池 (3p: Opus / Sonnet / GPT-OSS)", "agy_3p_5h", "agy_3p_weekly",
              agy.get("3p_5h"), agy.get("3p_weekly"))
        if agy.get("local_reqs_today") is not None:
            lines.append("     本地计数     " + str(agy["local_reqs_today"]) + " reqs today / "
                         + str(agy.get("five_hour_reqs", 0)) + " in past 5h"
                         + "   (仅本地日志，非服务端配额)")
        if prev_snap and "error" not in prev_snap.get("agy", {"error": ""}):
            p_used = (prev_snap.get("agy") or {}).get("local_reqs_today")
            c_used = agy.get("local_reqs_today")
            if p_used is not None and c_used is not None and c_used > p_used:
                lines.append(f"     consumed    +{c_used - p_used} reqs since last check")
        if agy.get("note"):
            lines.append(f"     note        {agy['note']}")
    lines.append("")

    lines.append("  🟠 Grok (xAI)")
    if "error" in gr:
        lines.append(f"     ⚠ {gr['error']}")
    else:
        if gr.get("account"):
            lines.append(f"     account     {gr['account']}")
        if gr.get("plan"):
            lines.append(f"     plan        {gr['plan']}")

        # 主周池 (Chat · Imagine · Voice · Build)
        main_wk = gr.get("main_weekly") or gr.get("weekly")
        bot_wk  = gr.get("bot_weekly")
        has_bot_pct = bool(bot_wk and bot_wk.get("pct") is not None)

        if main_wk:
            if has_bot_pct:
                lines.append("     ▸ 主周池 (Chat · Imagine · Voice · Build)")
            lines.append(f"     7d used     {window_pct_bar(main_wk)}")
            d = today_target_line(main_wk, today_bl.get("grok_weekly"), today_bl.get("grok_weekly_daily_target"))
            if d: lines.append(d)
            p = pace_line(main_wk, 7 * 24 * 3600)
            if p: lines.append(p)
            b, a = burn_advice(main_wk, advice_rate(main_wk, hist, "grok_weekly", 7 * 24 * 3600))
            if b: lines.append(b)
            if a: lines.append(a)

        # Grok Bot 独立周池（仅在获取到实测用量时展开）
        if has_bot_pct:
            lines.append("     ▸ Grok Bot 池 (Bot / Cursor 独立周额度)")
            lines.append(f"     7d used     {window_pct_bar(bot_wk)}")
            d = today_target_line(bot_wk, today_bl.get("grok_bot_weekly"), today_bl.get("grok_bot_weekly_daily_target"))
            if d: lines.append(d)
            p = pace_line(bot_wk, 7 * 24 * 3600)
            if p: lines.append(p)
            b, a = burn_advice(bot_wk, advice_rate(bot_wk, hist, "grok_bot_weekly", 7 * 24 * 3600))
            if b: lines.append(b)
            if a: lines.append(a)
        else:
            lines.append("     note        周池涵盖 Chat·Imagine·Voice·Build（Bot 为独立额度，不扣本池）")

        if gr.get("prepaid_balance") is not None and gr.get("prepaid_balance") > 0:
            lines.append(f"     balance     ${gr['prepaid_balance']:.2f}")
        if gr.get("on_demand_used") is not None and gr.get("on_demand_used") > 0:
            lines.append(f"     on-demand   ${gr['on_demand_used']:.2f} / ${gr.get('on_demand_cap', 0):.2f}")
    lines.append("")

    if oc is not None:
        lines += opencode_lines(oc)
        lines.append("")

    advice = dispatch_advice({"codex": cx, "claude": cl, "agy": agy, "grok": gr}, hist=hist)
    if advice:
        lines.append("  🧭 调度建议")
        lines += advice
        lines.append("")
    lines.append("═" * 54)
    return "\n".join(lines)

def _fmt_tokens(n):
    return f"{n / 1e6:.1f}M" if n >= 1e6 else f"{n / 1e3:.0f}k" if n >= 1e3 else str(n)

def opencode_expiry_text(oc, now=None):
    """key 过期时间：配了 ANTHROPIC_ADMIN_KEY 才有；没配返回 None。"""
    if oc.get("expiry_error"):
        return f"— ({oc['expiry_error']})"
    if not oc.get("expiry_known"):
        return None
    name = f"「{oc['key_name']}」" if oc.get("key_name") else ""
    if oc.get("key_status") not in (None, "active"):
        return f"⚠ {name}状态 {oc['key_status']}"
    ts = oc.get("expires_at")
    if not ts:
        return f"{name}永不过期"
    left = ts - (now or time.time())
    when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    if left <= 0:
        return f"⚠ {name}已于 {when} 过期"
    flag = "⚠ " if left < OPENCODE_KEY_EXPIRY_WARN_SEC else ""
    return f"{flag}{name}{when}（还剩 {fmt_countdown(left)}）"

def opencode_lines(oc):
    lines = ["  ⚪ OpenCode (Anthropic)"]
    if "error" in oc:
        return lines + [f"     ⚠ {oc['error']}"]
    status = {True: " ✓ 可用", False: " ✗ 不可用"}.get(oc.get("key_ok"), "")
    lines.append(f"     auth        {oc.get('auth', '?')}{status}")
    if oc.get("warning"): lines.append(f"     ⚠ {oc['warning']}")
    exp = opencode_expiry_text(oc)
    if exp: lines.append(f"     expires     {exp}")
    sp = oc.get("spend")
    if sp:
        lines.append("     spent       " + "   ".join(
            f"{lbl} ${sp[k]['cost']:.2f} ({_fmt_tokens(sp[k]['tokens'])} tok)"
            for k, lbl in (("today", "今天"), ("7d", "7天"), ("30d", "30天"))))
    else:
        lines.append("     spent       — (读不到 opencode 本地库)")
    lines.append("     note        " + (oc.get("note") or "按 opencode 本地记录估算；余额请到 console.anthropic.com 查看"))
    return lines

def render_compact(data, prev_snap, today_bl: dict, hist=None):
    ds  = data.get("deepseek", {})
    cx  = data.get("codex", {})
    cl  = data.get("claude", {})
    agy = data.get("agy", {})
    gr  = data.get("grok", {})

    if "error" in ds:
        print(f"DS ⚠ {ds['error']}")
    else:
        parts = [f"DS 💰 CNY {ds.get('cny_left','?')} left"]
        if prev_snap and "error" not in prev_snap.get("deepseek", {"error": ""}):
            pv = float(prev_snap["deepseek"].get("cny_left", 0))
            cv = float(ds.get("cny_left", 0))
            if pv > cv:
                spent = round(pv - cv, 4)
                parts.append(f"(spent {spent} CNY ≈ ${round(spent*0.14,6)})")
        print("  ".join(parts))

    totals    = {"five_hour": 5 * 3600, "weekly": 7 * 24 * 3600, "daily": 24 * 3600}
    providers = [("CX", cx, "codex"), ("CL", cl, "claude"), ("AGY", agy, "agy"), ("GROK", gr, "grok")]
    for label, provider, pkey in providers:
        if "error" in provider:
            print(f"{label} ⚠ {provider['error']}")
        else:
            plan  = f"[{provider.get('plan','?')}]" if provider.get("plan") else ""
            parts = [f"{label} {plan}".strip()]
            has_window = False
            if pkey == "agy":
                # 两个独立池分开报，别把 Gemini 池的余量当成整个 agy 的余量
                spec = [("gemini_5h", "G-5h"), ("gemini_weekly", "G-7d"),
                        ("3p_5h", "3P-5h"), ("3p_weekly", "3P-7d")]
            elif pkey == "grok":
                bot_wk = provider.get("bot_weekly")
                if bot_wk and bot_wk.get("pct") is not None:
                    spec = [("main_weekly", "主池-7d"), ("bot_weekly", "Bot-7d")]
                else:
                    spec = [("weekly", "7d")]
            else:
                spec = [("five_hour", "5h"), ("daily", "day"), ("weekly", "7d")]
            for key, lbl in spec:
                w = provider.get(key)
                if w:
                    if w.get("pct") is None:
                        continue
                    has_window = True
                    left = fmt_countdown(w["reset_at"] - time.time() if w.get("reset_at") else 0)
                    if pkey == "agy":
                        window_sec = 5 * 3600 if key.endswith("5h") else 7 * 24 * 3600
                        p    = pace_line(w, window_sec)
                        pace = (" " + p.strip()) if p else ""
                        parts.append(f"{lbl}: {w['pct']}% ({left}){pace}"
                                     + rate_suffix(hist, pkey, key, w))
                        continue
                    total_window_sec = 7 * 24 * 3600 if (key in ("weekly", "main_weekly", "bot_weekly")) else totals.get(key, 7 * 24 * 3600)
                    p    = pace_line(w, total_window_sec)
                    pace = (" " + p.strip()) if p else ""
                    if key in ("weekly", "main_weekly", "bot_weekly"):
                        hist_key = HIST_KEY_BY_WINDOW.get((pkey, key), f"{pkey}_{key}")
                        d = today_target_line(w, today_bl.get(hist_key), today_bl.get(f"{hist_key}_daily_target"), total_sec=total_window_sec)
                        pace += (" " + " ".join(d.split())) if d else ""
                    elif key == "daily":
                        d = today_target_line(w, today_bl.get(f"{pkey}_daily"), total_sec=total_window_sec, daily_rate=100.0)
                        pace += (" " + " ".join(d.split())) if d else ""
                    req_str = f" ({w['used']}/{w['limit']} reqs)" if "used" in w and "limit" in w else ""
                    parts.append(f"{lbl}: {w['pct']}%{req_str} ({left}){pace}"
                                 + rate_suffix(hist, pkey, key, w))
            if not has_window:
                if provider.get("credits") is not None:
                    parts.append(f"credits: {provider['credits']}")
                else:
                    parts.append("✓ online")
            if provider.get("warning"):
                parts.append(f"⚠ {provider['warning']}")
            print("  ".join(parts))

    oc = data.get("opencode")
    if oc is not None:
        if "error" in oc:
            print(f"OC ⚠ {oc['error']}")
        else:
            sp    = oc.get("spend") or {}
            parts = [f"OC [{oc.get('auth', '?')}]"]
            if sp:
                parts.append(f"today ${sp['today']['cost']:.2f}  7d ${sp['7d']['cost']:.2f}  30d ${sp['30d']['cost']:.2f}")
            exp = opencode_expiry_text(oc)
            if exp:
                parts.append(f"expires {exp}")
            if oc.get("warning"):
                parts.append(f"⚠ {oc['warning']}")
            print("  ".join(parts))

# ── Pet（Claude 用量宠物）─────────────────────────────────────────────────────
#
# 一只 ASCII 宠物（猫 / 兔子 / 小熊，按 s 切换），按 Claude 的用量节奏换情绪，画在终端右下角。
# 画风参考 ts-animal（MIT, https://github.com/ts-animal/ts-animal）的颜文字动物，帧为本项目自绘。
# 精灵图里不用 emoji：各终端里 emoji 的宽度不一致，会把右侧对齐弄乱。

PET_HAPPY_PCT     = EXHAUSTED_PCT  # 7d 用量 ≥ 此值：订阅用完了，开心
PET_ANGRY_MAX_PCT = 90.0        # 上一窗口最后用量 < 此值：一周没用完，生气
PET_ANGRY_SEC     = 12 * 3600   # 生气从窗口重置起持续多久
PET_TICK_SEC      = 0.5         # 动画帧间隔
PET_MSG_MAX       = 30          # 气泡文字最多占几列
PET_SPRITE_H      = 8           # 精灵图 7 行 + 1 行留给上下跳
PET_BUBBLE_LINES  = 2           # 气泡最多几行字（升级选项要两行）
PET_W, PET_H      = 36, 3 + PET_BUBBLE_LINES + PET_SPRITE_H   # 整块（气泡 + 精灵图）固定大小，重画时能完整盖住上一帧

# 精灵图模板：{el}{er} 眼睛、{m} 嘴巴各 1 列；{hl}{h}{hr} 双手和手里的东西各 2 列；{x1}{x2} 头旁装饰；{t} 尾巴
PET_SPECIES = {
    "cat":   ("猫", [
        r"   /\_______/\   {x1}",
        r"  /  {el}     {er}  \  {x2}",
        r" (  =   {m}   =  )",
        r"  \  {hl}{h}{hr}   /",
        r"  /           \ ",
        r" (  |       |  ){t}",
        r"  \_|_)___(_|_/"]),
    "bunny": ("兔子", [
        r"    (\     /)   {x1}",
        r"    ( \   / )  {x2}",
        r"   (  {el}   {er}  )",
        r"   (    {m}    )",
        r"  o( {hl}{h}{hr}  )o",
        r"   (         ){t}",
        r"    (_)   (_)"]),
    "bear":  ("小熊", [
        r"  .-.     .-.  {x1}",
        r" ( (_.---._) ) {x2}",
        r"  /  {el}   {er}  \ ",
        r" |    ({m})    |",
        r"  \ {hl}{h}{hr}  /",
        r"  /`-------'\ {t}",
        r" (_/       \_)"]),
}
PET_SPECIES_ORDER = list(PET_SPECIES)
_PET_SLOT_W  = {"el": 1, "er": 1, "m": 1, "hl": 2, "h": 2, "hr": 2}
_PET_DEFAULT = dict(el="•", er="•", m="ω", hl="  ", h="  ", hr="  ", x1="", x2="", t="~", dx=0, dy=0)

def _pf(**kw):
    """一帧的部件：没写的用默认值。dx 左右挪一列（东张西望/发抖），dy=-1 往上跳一行。"""
    return {**_PET_DEFAULT, **kw}

_BLINK = _pf(el="-", er="-")

# 每个情绪一组帧，按 PET_TICK_SEC 循环；重复的帧 = 停留更久
PET_FRAMES = {
    "caring":    [_pf(hl="つ", h="♡ ", hr="と")] * 2
                 + [_pf(el="˘", er="˘", hl="つ", h="♥ ", hr="と", x1="♥")] * 2,
    "happy":     [_pf(el="^", er="^", m="▽", hl="\\ ", hr=" /", x1="✧", dy=-1),
                  _pf(el="^", er="^", m="▽", hl="\\ ", hr=" /", x2=" ✧")],
    "angry":     [_pf(el="`", er="´", m="Д", hl="つ", hr="つ", x1="#", t="!"),
                  _pf(el="`", er="´", m="Д", hl="つ", hr="つ", x1="# #", x2=" ぷんぷん", t="!", dx=1)],
    "encourage": [_pf(el=">", er="<", m="o", hl="ノ", h="☆ ", x1="☆"),
                  _pf(el="^", er="^", m="▽", hl="ノ", h="☆ ", x2="  ☆", dy=-1)],
    # 有会话在等你确认 / 回复：挥手叫你
    "call":      [_pf(el="°", er="°", m="o", hl="ノ", h="! ", x1="!"),
                  _pf(el="°", er="°", m="o", hr="ノ", x2="  !", dx=1)],
    # 有新版本，举着牌子等你选
    "update":    [_pf(m="▽", hl="ノ", h="! ", x1="NEW"),
                  _pf(el="^", er="^", m="▽", hl="ノ", h="! ", x2="  NEW", dy=-1)],
    # 有会话在干活：敲键盘，气泡轮播各会话在做什么
    "work":      [_pf(hl="つ", h="==", hr="と", x1="ｶﾀ"),
                  _pf(hl="つ", h="==", hr="と", x2="  ｶﾀｶﾀ"),
                  _pf(hl="つ", h="==", hr="と", x1="ｶﾀ", dx=1),
                  _pf(el="-", er="-", hl="つ", h="==", hr="と")],
    # 刷新取数中（后台线程在拉数据，动画照常跑）
    "busy":      [_pf(m="o", x1="."), _pf(x1=".."), _pf(m="o", x1="..."), _pf(x1="...")],
}

# 日常小动作：(气泡文字, 帧序列)；没别的情绪时每隔几秒随机换一个
# 用量情绪（劝歇、鼓励、生气……）会一连挂好几天，一直显示就把别的小动作都挡住了：
# 变了先说 USAGE_MOOD_FIRST 秒，之后每 USAGE_MOOD_EVERY 秒再说 USAGE_MOOD_SHOW 秒，其余时间做日常小动作
USAGE_MOOD_FIRST, USAGE_MOOD_EVERY, USAGE_MOOD_SHOW = 20, 180, 10

PET_IDLE = [
    ("发呆中…",         [_pf()] * 5 + [_BLINK]),
    ("摇尾巴～",        [_pf(), _pf(t="∽"), _pf(), _pf(t=" ~")]),
    ("东张西望",        [_pf(dx=-1)] * 3 + [_pf(dx=1)] * 3),
    ("哈～欠",          [_pf(), _pf(el="-", er="-", m="o"), _pf(el=">", er="<", m="O", x2=" ふぁ～")] + [_pf(el=">", er="<", m="O", x2=" ふぁ～")] + [_BLINK]),
    ("舔舔爪子",        [_pf(el="-", er="-", hr="ノ"), _pf(el="-", er="-", hr="っ", x2=" ペロ")]),
    ("伸个懒腰",        [_pf(), _pf()] + [_pf(el=">", er="<", hl="\\ ", hr=" /", dy=-1)] * 2),
    ("Zzz…",            [_pf(el="-", er="-", x1="z"), _pf(el="-", er="-", x1="zZ"), _pf(el="-", er="-", x1="zZz", x2="z")]),
]

def display_width(s: str) -> int:
    """终端显示宽度：全角/宽字符算 2 列，组合字符算 0 列。"""
    return sum(0 if unicodedata.combining(c) else (2 if unicodedata.east_asian_width(c) in "WF" else 1)
               for c in s)

def _pad(s: str, width: int) -> str:
    return s + " " * max(0, width - display_width(s))

def _rpad(s: str, width: int) -> str:
    """右对齐。调用方保证 s 的显示宽度不超过 width。"""
    return " " * max(0, width - display_width(s)) + s

def _clip(s: str, width: int) -> str:
    if display_width(s) <= width:
        return s
    out = ""
    for c in s:
        if display_width(out + c) > width - 1:
            break
        out += c
    return out + "…"


def pet_sprite(species: str, parts: dict):
    """按物种模板和一帧的部件拼出精灵图（PET_SPRITE_H 行）。槽位按固定列宽居中，宽字符不会把身体挤歪。"""
    _, tpl = PET_SPECIES.get(species) or PET_SPECIES[PET_SPECIES_ORDER[0]]
    vals = {}
    for k, v in parts.items():
        if k in _PET_SLOT_W:
            v = _pad(v, _PET_SLOT_W[k])
        vals[k] = v
    indent = " " * (1 + int(parts.get("dx", 0)))
    lines  = [indent + line.format(**vals).rstrip() for line in tpl]
    lines  = (lines + [""]) if parts.get("dy", 0) < 0 else ([""] + lines)
    return (lines + [""] * PET_SPRITE_H)[:PET_SPRITE_H]


def pet_track_reset(cl, pet_state, now=None):
    """记录 Claude 7d 窗口位置；发现窗口已滚动、且上一窗口没用完，就记一笔“生气”。

    用 for_reset_at 去重：多个终端各自检测到同一次重置，只算一次。
    """
    now = now or time.time()
    st  = dict(pet_state or {})
    wk  = (cl or {}).get("weekly") or {}
    if "error" in (cl or {}) or wk.get("pct") is None or not wk.get("reset_at"):
        return st
    prev = st.get("claude_weekly") or {}
    p_reset, p_pct = prev.get("reset_at"), prev.get("pct")
    if (p_reset and p_pct is not None and wk["reset_at"] > p_reset + 86400
            and p_pct < PET_ANGRY_MAX_PCT and (st.get("angry") or {}).get("for_reset_at") != p_reset):
        st["angry"] = {"until": p_reset + PET_ANGRY_SEC, "wasted": round(100.0 - p_pct, 1),
                       "for_reset_at": p_reset}
    st["claude_weekly"] = {"reset_at": wk["reset_at"], "pct": float(wk["pct"])}
    return st


def claude_pet_mood(cl, hist, pet_state, now=None, data=None):
    """返回 (情绪, 气泡文字)。看 Claude 的用量；优先级：开心 > 献爱心 > 生气 > 冲刺 > 鼓励 > 日常。

    周额度用完排最前：用完了就该庆祝，再提醒“慢点用”已没有意义。
    劝歇只提醒，不推荐别家（换哪家看底下的调度建议）；传了 data（全部订阅）时用来判断要不要劝冲刺 / 多用。
    """
    now = now or time.time()
    if not cl or "error" in cl:
        return "idle", None
    wk, fh = cl.get("weekly") or {}, cl.get("five_hour") or {}
    proj   = pace_projection(wk, 7 * 24 * 3600)
    fh_pct = fh.get("pct")
    pct    = wk.get("pct")
    stats  = sub_status(data, now, hist) if data else []

    if pct is not None and float(pct) >= PET_HAPPY_PCT:
        return "happy", "Claude 周额度用完啦！好棒！"
    if fh_pct is not None and float(fh_pct) >= EXHAUSTED_PCT:   # 和调度建议同一口径
        return "caring", "5h 额度用光啦，起来走走吧 ♥"
    if proj and proj[1] > PACE_FAST_PCT:   # 只提醒；换哪家看底下的调度建议
        return "caring", f"照这样到期要用 {proj[1]:.0f}% 啦 ♥"
    if fh_pct is not None and fh.get("reset_at"):
        r          = burn_rate(hist, "claude_5h", 5 * 3600)
        reset_in_h = max(0.0, fh["reset_at"] - now) / 3600.0
        if r and r["per_hour"] > 0.005 and reset_in_h >= 0.25:
            exhaust_h = max(0.0, 100.0 - float(fh_pct)) / r["per_hour"]
            if exhaust_h < reset_in_h:
                return "caring", f"5h 额度 {fmt_countdown(exhaust_h * 3600)} 后见底，慢点 ♥"

    angry = (pet_state or {}).get("angry") or {}
    if float(angry.get("until") or 0) > now:
        return "angry", f"上周白白浪费了 {float(angry.get('wasted') or 0):.0f}%！哼！"
    mine = next((s for s in stats if s["prov"] == "claude"), None)
    if mine and mine["kind"] == "sprint" and not mine["fh_tight"]:   # 5h 紧时不劝放开用，和调度建议一致
        return "encourage", f"剩 {mine['left']:.0f}% 要过期啦，试试 Opus？"
    if proj and proj[1] <= PACE_SLOW_PCT and not (mine and mine["fh_tight"]):   # 5h 紧时不劝多用
        return "encourage", f"还剩 {100.0 - float(pct):.0f}% 呢，多用用嘛～加油！"
    return "idle", None


def pet_bubble(text: str):
    """气泡：text 里的换行就是气泡里的换行，最多 PET_BUBBLE_LINES 行。"""
    rows = [_clip(t, PET_MSG_MAX) for t in text.split("\n")[:PET_BUBBLE_LINES]]
    w    = max(2, *(display_width(t) for t in rows))
    return ([" ." + "-" * (w + 2) + "."]
            + ["( " + _pad(t, w) + " )" for t in rows]
            + [" '---." + "-" * (w - 2) + "'", "     \\"])


class PetView:
    """宠物动画状态：当前情绪 + 帧序号；日常状态下每隔几秒随机换一个小动作。"""

    def __init__(self, rng=None, species="cat"):
        self.rng, self.mood, self.msg = rng or random.Random(), "idle", None
        self.tick, self.idle_i, self.idle_until = 0, 0, 0.0
        self.species = species if species in PET_SPECIES else PET_SPECIES_ORDER[0]

    def next_species(self):
        i = PET_SPECIES_ORDER.index(self.species)
        self.species = PET_SPECIES_ORDER[(i + 1) % len(PET_SPECIES_ORDER)]
        return self.species

    def set_mood(self, mood, msg=None):
        if (mood, msg) != (self.mood, self.msg):
            self.mood, self.msg, self.tick = mood, msg, 0

    def set_idle(self, i, hold_sec=1e9):
        self.mood, self.msg, self.tick = "idle", None, 0
        self.idle_i, self.idle_until = i, time.time() + hold_sec

    def frame(self, now=None):
        """下一帧：PET_H 行，每行补齐到 PET_W 列。"""
        now = now or time.time()
        if self.mood == "idle":
            if now >= self.idle_until:
                choices = [i for i in range(len(PET_IDLE)) if i != self.idle_i]
                self.idle_i, self.tick = self.rng.choice(choices), 0
                self.idle_until = now + self.rng.uniform(6, 10)
            caption, seq = PET_IDLE[self.idle_i]
        else:
            caption, seq = self.msg or "", PET_FRAMES[self.mood]
        sprite = pet_sprite(self.species, seq[self.tick % len(seq)])
        self.tick += 1
        lines = pet_bubble(caption) + sprite
        lines = [""] * max(0, PET_H - len(lines)) + lines   # 气泡字少时上面补空行，整块高度不变
        return [_pad(line, PET_W) for line in lines][:PET_H]



def screen_rows(text: str, cols: int):
    """把文本按终端宽度折行，返回每个屏幕行已占的列数。"""
    rows = []
    for line in text.split("\n"):
        w = display_width(line)
        while w > cols:
            rows.append(cols)
            w -= cols
        rows.append(w)
    return rows


def pet_place(widths, cols, pref_top):
    """在每行已占列数 widths 里，找离 pref_top（期望的起始行）最近、右侧放得下宠物的位置。

    返回 0 起算的 (起始行, 起始列)；哪儿都放不下返回 None。宠物贴右边、右侧留 1 列，左侧和正文至少隔 2 列。
    """
    col, n = cols - PET_W - 1, len(widths)
    if col < 0 or n < PET_H:
        return None
    top0 = max(0, min(pref_top, n - PET_H))
    for d in range(n):
        for top in (top0 - d, top0 + d):
            if 0 <= top <= n - PET_H and all(w + 2 <= col for w in widths[top:top + PET_H]):
                return top, col
    return None


def pet_merge_static(text: str, pet_lines, cols: int) -> str:
    """一次性查询：把一帧宠物拼在正文右下角；右侧放不下就接在正文下面。"""
    lines = text.split("\n")
    lines += [""] * max(0, PET_H - len(lines))
    place = pet_place([display_width(l) for l in lines], cols, len(lines) - PET_H)
    if not place:
        return text + "\n" + "\n".join(l.rstrip() for l in pet_lines)
    top, col = place
    for i, pl in enumerate(pet_lines):
        lines[top + i] = (_pad(lines[top + i], col) + pl).rstrip()
    return "\n".join(lines)


# ── 会话表（ps / htop 那种进程表，不画宠物）────────────────────────────────
#
# 用量页右下角的宠物不动。这一页只有表格：状态、会话名、在做什么、时长。
# 每个单元格只按自己的列宽裁一次（至多一个「…」），拼好的行不再整行裁。
# 名称列按这一屏里最宽的名字定宽（上限约 50 列），剩下的全给「在做什么」。

# 等你 > 运行 > 思考 > 自己空闲但还在等子 agent > 空闲 > 完成
# 「等子」不是某一行的状态符号，只参与会话头的排序。
# 「命令 / 外部」和运行同级：下面有 ⚙ 行的会话按运行排，行本身不算子 agent、也不算会话。
_KIND_RANK = {"等你": 0, "运行": 1, "命令": 1, "外部": 1, "思考": 2, "等子": 3, "空闲": 4, "完成": 5}
_KIND_LABEL = {
    "等你": "◆ 等你",
    "运行": "● 运行",
    "思考": "○ 思考",
    "空闲": "○ 空闲",
    "完成": "✓ 完成",
    "命令": "⚙ 命令",
    "外部": "⚙ 外部",
}
# 标题行右侧的页签。窄到放不下就只留左边的标题，不把这一截裁出省略号
_SESSION_HINT = "[用量] ‹会话› · t 切换 · q 退出"


def _filt_parts(filt):
    """逗号分隔的过滤词。None / 空串 → []，表示不过滤。不读环境变量。"""
    if filt is None:
        return []
    if isinstance(filt, (list, tuple)):
        items = filt
    else:
        items = str(filt).split(",")
    return [str(p).strip() for p in items if str(p).strip()]


def _cwd_ok(session, parts):
    """cwd 里含任意一个子串（不区分大小写）才显示。parts 空则全部通过。"""
    if not parts:
        return True
    cwd = str(session.get("cwd") or "").lower()
    return any(p.lower() in cwd for p in parts)


def _node_kind(node):
    """◆ 等你 / ● 运行（工具在跑）/ ○ 思考 / ○ 空闲 / ✓ 完成 / ⚙ 命令 / ⚙ 外部。

    注册表 status（node["reg"]）是会话自己这一行的权威：busy / shell 算运行，idle 算空闲。
    「等你」仍盖过它。没有 reg 的旧会话，继续看 state / doing。
    """
    state = node.get("state") or "空闲"
    if state == "等你":
        return "等你"
    if state in ("命令", "外部"):
        return state
    reg = node.get("reg")
    if reg in ("busy", "shell"):
        return "运行"
    if reg == "idle":
        return "空闲"
    doing = node.get("doing") or ""
    if state == "完成" or (isinstance(doing, str) and doing.startswith("✓")):
        return "完成"
    if doing == "思考中":
        return "思考"
    if state == "工作中":
        return "运行"
    return "空闲"


def _living_subagents(nodes):
    """还没做完的子 agent 有几个。嵌套的一起算；完成的节点本身不算，接着数它下面的。
    ⚙ 命令 / ⚙ 外部不是子 agent。"""
    total = 0
    for node in nodes or []:
        kind = _node_kind(node)
        if kind in ("命令", "外部"):
            total += _living_subagents(node.get("children"))
            continue
        if kind != "完成":
            total += 1
        total += _living_subagents(node.get("children"))
    return total


def _session_rank(session):
    """等你 > 运行 > 思考 > 等子 agent > 空闲。

    子树里更紧急的那个算在会话头上；做完的不抬优先级。
    ⚙ 命令 / ⚙ 外部和运行同级，所以下面有命令在跑的会话按运行排。
    主会话自己是空闲、子树里也没有更紧急的，但还有活着的子 agent 时，
    排在运行 / 思考之后、普通空闲之前。
    """
    best = _node_kind(session)
    if best == "完成":
        best = "空闲"

    def walk(nodes):
        nonlocal best
        for node in nodes or []:
            kind = _node_kind(node)
            if kind != "完成" and _KIND_RANK[kind] < _KIND_RANK[best]:
                best = kind
            walk(node.get("children"))

    walk(session.get("children"))
    if best == "空闲" and _living_subagents(session.get("children")):
        best = "等子"
    return _KIND_RANK[best]


def _count_nodes(nodes):
    """子 agent 个数。⚙ 命令 / ⚙ 外部 挂在树里，但不算子 agent。"""
    n = 0
    for node in nodes or []:
        if _node_kind(node) not in ("命令", "外部"):
            n += 1
        n += _count_nodes(node.get("children"))
    return n


def _has_command(nodes):
    """这棵子树里有没有正在跑的 ⚙ 命令 / ⚙ 外部。"""
    for node in nodes or []:
        if _node_kind(node) in ("命令", "外部"):
            return True
        if _has_command(node.get("children")):
            return True
    return False


def _dur_text(ts, done=False, now=None):
    """进行中写 14s / 3m / 2h05m；刚做完的子 agent 加「前」（1m 前）。"""
    now = time.time() if now is None else now
    try:
        sec = int(now - float(ts))
    except (TypeError, ValueError):
        sec = 0
    sec = max(0, sec)
    if sec < 60:
        text = f"{sec}s"
    elif sec < 3600:
        text = f"{sec // 60}m"
    elif sec < 86400:
        hours, mins = divmod(sec // 60, 60)
        text = f"{hours}h{mins:02d}m" if mins else f"{hours}h"
    else:
        days, hours = divmod(sec // 3600, 24)
        text = f"{days}d{hours:02d}h" if hours else f"{days}d"
    return f"{text} 前" if done else text


def _doing_text(node):
    """在做什么。思考 / 空闲是一横；做完了留空（状态列已经是 ✓）；等你时不再重复「等你」。"""
    kind = _node_kind(node)
    doing = _clean(node.get("doing"))
    if kind == "完成":
        return ""
    if kind in ("思考", "空闲"):
        return "—"
    if kind == "等你":
        # 状态列已经是「等你」。去掉这个前缀，并把「确认：Bash：…」收成「确认 Bash：…」
        if doing.startswith("等你"):
            doing = doing[len("等你"):].lstrip()
        head, sep, rest = doing.partition("：")
        if sep and rest and len(head) <= 8 and " " not in head:
            doing = f"{head} {rest}"
        return doing.strip() or "—"
    return doing or "—"


def _row_name(node):
    """会话名。Grok 写成「项目 的 Grok」，和底部提醒同一句式；有标题就嵌在中间。
    这里不写省略号，名字列按宽度只裁一次。"""
    if node.get("sid"):
        title = _clean(node.get("title"))
        cwd = node.get("cwd") or ""
        proj = _project({"cwd": cwd}) if cwd else "~"
        if node.get("src") == "grok":
            return f"{proj}「{title}」的 Grok" if title else f"{proj} 的 Grok"
        return proj + (f"「{title}」" if title else "")
    return _clean(node.get("name")) or "agent"


def _row_of(node, prefix):
    kind = _node_kind(node)
    return {
        "stat": prefix + _KIND_LABEL[kind],
        "name": _row_name(node),
        "doing": _doing_text(node),
        "dur": _dur_text(node.get("ts"), done=(kind == "完成")),
    }


def _child_rows(nodes, ancestors_last):
    """树前缀：├ / └ 分叉，│ 把上面没完的枝接下来。ancestors_last 记下每一层是不是最后一个。"""
    out, nodes = [], list(nodes or [])
    for i, node in enumerate(nodes):
        last = i == len(nodes) - 1
        prefix = "".join("  " if was_last else "│ " for was_last in ancestors_last)
        prefix += "└ " if last else "├ "
        out.append(_row_of(node, prefix))
        out.extend(_child_rows(node.get("children"), ancestors_last + [last]))
    return out


def _session_group(session):
    head = _row_of(session, "")
    # 自己是空闲时状态仍是 ○。下面还有活着的子 agent，就在「在做什么」里写等它们
    if _node_kind(session) == "空闲":
        alive = _living_subagents(session.get("children"))
        if alive:
            head["doing"] = f"等 {alive} 个子 agent"
    return [head] + _child_rows(session.get("children"), [])


def _fit_groups(groups, slots):
    """按顺序放会话。返回 (放进去的行组, 没放进去的会话数)。

    后面还有会话放不下时留一行给「…还有 N 个」。第一个会话比空位还高，就先露出上面几行。
    """
    if slots <= 0 or not groups:
        return [], len(groups)
    if sum(len(group) for group in groups) <= slots:
        return [list(group) for group in groups], 0
    shown, used = [], 0
    for i, group in enumerate(groups):
        later = len(groups) - i - 1
        left = slots - used
        if later == 0:
            if left > 0:
                shown.append(group[:left])
            return shown, 0
        if len(group) <= left - 1:
            shown.append(group)
            used += len(group)
            continue
        room = left - 1
        if room > 0:
            shown.append(group[:room])
            return shown, later
        return shown, later + 1
    return shown, 0


def _table_widths(cols, stat_need, dur_need, name_need=0):
    """(indent, stat_w, name_w, doing_w, dur_w, gap)。拼起来正好是 cols 列。

    名称列等于这一屏 row["name"] 里最宽的一格（树前缀在状态列，不重复算）：
    不超过 50，至少表头「会话 / agent」。剩下的宽度全部给「在做什么」。

    窄到放不下时，先把名称收到约 20（name_need 更小就收到 name_need），
    让「在做什么」保持 16 列；还不够再把「在做什么」收到 16 以下，名称停在这个下限。
    两列都还要至少 4 列——为了守住这 4 列，名称可以再往下收。
    再守不住就裁状态 / 时长，最后才拿掉「在做什么」。
    """
    # 宽屏名称最多 50 列；再窄先收到约 20，然后才把「在做什么」收到 16 以下
    name_cap, name_floor_at, doing_keep, side_min = 50, 20, 16, 4
    header_w = display_width("会话 / agent")
    name_need = max(0, int(name_need))
    name_want = min(name_cap, max(name_need, header_w, 1))
    if name_need <= 0:
        name_floor = 1
    elif name_need < name_floor_at:
        name_floor = name_need
    else:
        name_floor = name_floor_at
    name_floor = max(1, min(name_floor, name_want))

    def split(elastic):
        """elastic >= 2。名称和「在做什么」加起来正好是 elastic。"""
        if elastic >= name_want + doing_keep:
            name_w = name_want
        elif elastic >= name_floor + doing_keep:
            name_w = elastic - doing_keep
        else:
            name_w = min(name_floor, elastic - 1)
        doing_w = elastic - name_w
        # 原来的下限：两列都至少 4，先不裁状态 / 时长
        if doing_w < side_min and elastic >= side_min * 2:
            name_w = elastic - side_min
            doing_w = side_min
        if name_w < 1:
            name_w, doing_w = 1, elastic - 1
        elif doing_w < 1:
            name_w, doing_w = elastic - 1, 1
        return name_w, doing_w

    def layout(indent, stat_w, dur_w, gap, doing_on):
        # 状态 | 名称 | [在做什么 |] 时长。间隔数是 2 或 3
        gaps_n = 2 + int(doing_on)
        elastic = cols - indent - stat_w - dur_w - gap * gaps_n
        if elastic < (2 if doing_on else 1) or stat_w < 1 or dur_w < 1:
            return None
        if doing_on:
            name_w, doing_w = split(elastic)
        else:
            name_w, doing_w = elastic, 0
        return (indent, stat_w, name_w, doing_w, dur_w, gap)

    gap_opts = (2, 1) if cols >= 72 else (1,)
    indent_opts = (2, 0) if cols >= 48 else (0,)
    stat_cap = min(max(int(stat_need), 1), max(1, cols))
    dur_cap = min(max(int(dur_need), 1), max(1, cols))

    def search(doing_on, min_side, shrink):
        stats = range(stat_cap, 0, -1) if shrink else (stat_cap,)
        durs = range(dur_cap, 0, -1) if shrink else (dur_cap,)
        for stat_w in stats:
            for dur_w in durs:
                for gap in gap_opts:
                    for indent in indent_opts:
                        spec = layout(indent, stat_w, dur_w, gap, doing_on)
                        if not spec:
                            continue
                        name_w, doing_w = spec[2], spec[3]
                        side = min(name_w, doing_w) if doing_on else name_w
                        if side >= min_side:
                            return spec
        return None

    for shrink in (False, True):
        spec = search(True, 4, shrink)
        if spec:
            return spec
    spec = search(True, 1, True)
    if spec:
        return spec
    for shrink in (False, True):
        spec = search(False, 1, shrink)
        if spec:
            return spec
    return (0, 0, max(1, cols), 0, 0, 0)


def _needs(rows):
    """这一屏状态、时长、名称的内容宽度。名称只量 row["name"]。

    树前缀写在状态单元格上，已经算进状态列，不加进名称。
    """
    stat, dur, name = display_width("状态"), display_width("时长"), 0
    for row in rows:
        stat = max(stat, display_width(row["stat"]))
        dur = max(dur, display_width(row["dur"]))
        name = max(name, display_width(row["name"]))
    return stat, dur, name


def _table_cells(stat, name, doing, dur, spec):
    """四个单元格，各裁一次、补到列宽。宽度是 0 的列不出现。"""
    _indent, stat_w, name_w, doing_w, dur_w, _gap = spec
    cells = []
    if stat_w:
        cells.append(_pad(_clip(stat, stat_w), stat_w))
    if name_w:
        cells.append(_pad(_clip(name, name_w), name_w))
    if doing_w:
        cells.append(_pad(_clip(doing, doing_w), doing_w))
    if dur_w:
        cells.append(_rpad(_clip(dur, dur_w), dur_w))
    return cells


def _join_cells(cells, spec):
    indent, _stat_w, _name_w, _doing_w, _dur_w, gap = spec
    body = (" " * gap).join(cells) if gap else "".join(cells)
    return (" " * indent) + body


def _title_line(title, cols):
    hint = _SESSION_HINT
    if display_width(hint) + 2 < cols and display_width(title) + 2 + display_width(hint) <= cols:
        return _pad(title, cols - display_width(hint)) + hint
    if display_width(title) <= cols:
        return title
    return _clip(title, cols)


def _overflow_line(hidden, cols):
    text = f"…还有 {hidden} 个会话"
    if display_width(text) <= cols:
        return text
    # 前导 … 是这句话本身；太窄时改由裁剪加唯一的省略号，避免叠成两个
    return _clip(f"还有 {hidden} 个会话", max(1, cols))


def _clip_lines(text, width):
    """一段文字里的每一行收到 width 列以内。没超的原样留着，超了的用 _clip 裁一次。"""
    return "\n".join(line if display_width(line) <= width else _clip(line, width)
                     for line in str(text).split("\n"))


def _headline_bucket(session):
    """表头括号里这一会话算哪一类。

    等你优先。自己在跑工具或在思考，算运行。自己空闲、但下面有 ⚙ 行，也算运行。
    ⚙ 行本身不是会话，这里不单另计数。
    """
    kind = _node_kind(session)
    if kind == "等你":
        return "等你"
    if kind in ("运行", "思考"):
        return "运行"
    if _has_command(session.get("children")):
        return "运行"
    return ""


def _sessions_title(sessions, parts):
    """会话 · [过滤词 ·] N 个会话[（N 等你 · N 运行）][· N 个子 agent]。

    括号只数会话：等你，以及正在跑工具、在思考、或下面有 ⚙ 命令的（都记成「运行」）。
    ⚙ 行不加进会话数，也不加进子 agent 数。
    哪一项是 0 就不写；两项都是 0 就没有括号。子 agent 是 0 也不写。
    有括号时右括号后直接接「· N」（「）· 3 个子 agent」）；没有括号时仍用「 · 」。
    """
    n_wait = n_run = 0
    for session in sessions:
        bucket = _headline_bucket(session)
        if bucket == "等你":
            n_wait += 1
        elif bucket == "运行":
            n_run += 1
    n_sub = sum(_count_nodes(session.get("children")) for session in sessions)
    title = "会话"
    if parts:
        title += " · " + ",".join(parts)
    title += f" · {len(sessions)} 个会话"
    detail = []
    if n_wait:
        detail.append(f"{n_wait} 等你")
    if n_run:
        detail.append(f"{n_run} 运行")
    if detail:
        title += "（" + " · ".join(detail) + "）"
    if n_sub:
        title += f"{'' if detail else ' '}· {n_sub} 个子 agent"
    return title


def session_lines(sessions, cols, rows, filt=None):
    """会话页要画的行（标题、表头、进程树）。不读文件、不画宠物。

    filt 是逗号分隔的目录子串：传了就只留 cwd 含其中任一个的会话（不区分大小写），
    并写进标题（「会话 · Agent24 · …」）。None 或空串不过滤，也不读环境变量。
    一屏放不下时等你 > 运行 > 思考 > 等子 agent > 空闲，多出来的会话收成一行。
    返回的每行显示宽度不超过 cols。每个单元格至多一个「…」。
    名称列按这一屏里最宽的名字定宽（不超过约 50 列，至少表头），剩下的给「在做什么」。
    """
    cols, rows = max(1, int(cols or 1)), max(1, int(rows or 1))
    parts = _filt_parts(filt)
    sessions = [s for s in (sessions or []) if _cwd_ok(s, parts)]
    head = _title_line(_sessions_title(sessions, parts), cols)
    if not sessions:
        lines = [head]
        if rows >= 3:
            lines += ["", "没有进行中的会话"]
        elif rows >= 2:
            lines.append("没有进行中的会话")
        return lines[:rows]
    if rows <= 1:
        return [head]

    ranked = sorted(sessions, key=lambda s: (
        _session_rank(s), -float(s.get("ts") or 0), str(s.get("sid") or "")))
    groups = [_session_group(s) for s in ranked]
    remain = rows - 1
    if remain >= 3:
        use_blank, use_head, slots = True, True, remain - 2
    elif remain == 2:
        use_blank, use_head, slots = False, True, 1
    else:
        use_blank, use_head, slots = False, False, remain
    shown, hidden = _fit_groups(groups, slots)
    flat = [row for group in shown for row in group]
    spec = _table_widths(cols, *_needs(flat))
    lines = [head]
    if use_blank:
        lines.append("")
    if use_head:
        lines.append(_join_cells(_table_cells("状态", "会话 / agent", "在做什么", "时长", spec), spec))
    for row in flat:
        lines.append(_join_cells(
            _table_cells(row["stat"], row["name"], row["doing"], row["dur"], spec), spec))
    if hidden and slots > 0:
        lines.append(_overflow_line(hidden, cols))
    return lines[:rows]


def _demo_sessions(now=None):
    """--pet-demo 用的假会话：一个在等你的、一棵子 agent（含 ⚙ 外部）、一个 ⚙ 命令、一个空闲的、一个 Grok。"""
    now = now or time.time()
    return [
        {"src": "claude", "sid": "demo-wait", "cwd": "/work/Agent24", "title": "CI 修复",
         "state": "等你", "doing": "等你确认：Bash: git push origin feat/x", "ts": now - 180,
         "children": []},
        {"src": "claude", "sid": "demo-run", "cwd": "/work/Agent24-wt-02b", "title": "知识组件集成",
         "state": "工作中", "doing": "Bash: cargo test -p kb", "ts": now - 14,
         "children": [
             {"aid": "a1", "name": "codex-review「双审」", "state": "工作中",
              "doing": "Bash: codex exec review", "ts": now - 6 * 60, "children": [
                  {"aid": "a2", "name": "general-purpose「核实发现 #2」", "state": "工作中",
                   "doing": "思考中", "ts": now - 12, "children": []},
                  {"name": "外部 Codex", "state": "外部", "doing": "codex exec review",
                   "ts": now - 6 * 60, "children": []},
              ]},
             {"aid": "a3", "name": "grok-review「双审」", "state": "完成",
              "doing": "✓ 完成", "ts": now - 60, "children": []},
             {"name": "等 PR checks", "state": "命令", "doing": "gh pr checks --watch",
              "ts": now - 45, "children": []},
         ]},
        {"src": "claude", "sid": "demo-idle", "cwd": "/work/Agent24", "title": "HANDOFF 文档",
         "state": "空闲", "doing": "空闲", "ts": now - 18 * 60, "children": []},
        {"src": "grok", "sid": "demo-grok", "cwd": "/work/WeKnora", "title": "",
         "state": "空闲", "doing": "空闲", "ts": now - 40 * 60, "children": []},
    ]



def _watch_frame(text, clear):
    """watch 里一帧要写到终端的内容。clear 时整屏清掉再画：用量页每次都这样，
    会话页只在切换 tab 或终端尺寸变了时清一次。平时会话页把光标拉回左上，
    每行清到行尾，最后清掉下面缩下去的残留。"""
    if clear:
        return "\033[H\033[2J" + text
    parts = ["\033[H"]
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if i:
            parts.append("\n")
        parts.append(line + "\033[K")
    parts.append("\033[J")
    return "".join(parts)


def pet_draw(pet_lines, top, col):
    """在屏幕绝对位置 (0 起算) 画一帧，画完把光标放回原处。"""
    out = ["\0337"]
    for i, line in enumerate(pet_lines):
        out.append(f"\033[{top + i + 1};{col + 1}H{line}")
    out.append("\0338")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def pet_demo():
    """--pet-demo：不查接口，依次演示每种情绪和日常小动作（宠物轮流上场），再印一帧会话表。"""
    view  = PetView()
    shows = [("caring", "照这样到期要用 135% 啦 ♥"), ("happy", "Claude 周额度用完啦！好棒！"),
             ("angry", "上周白白浪费了 35%！哼！"), ("encourage", "剩 45% 要过期啦，试试 Opus？"),
             ("encourage", "还剩 62% 呢，多用用嘛～加油！"),
             ("update", "新版本 v1.3.0！u 立即升级\nk 跳过本次 · n 不要提醒"),
             ("call", "see-balance 等你确认 1/2\nBash：git push origin main"),
             ("work", "工作中 1/2：see-balance·宠物轮播\nBash：python3 -m py_compile")]
    shows += [("idle", i) for i in range(len(PET_IDLE))]
    if not sys.stdout.isatty():
        for i, (mood, arg) in enumerate(shows):
            view.species = PET_SPECIES_ORDER[i % len(PET_SPECIES_ORDER)]
            view.set_idle(arg) if mood == "idle" else view.set_mood(mood, arg)
            print(f"── {PET_SPECIES[view.species][0]} · {mood}\n" + "\n".join(l.rstrip() for l in view.frame()) + "\n")
        print("── 会话\n" + "\n".join(session_lines(_demo_sessions(), 80, 40)) + "\n")
        return
    sys.stdout.write("\033[?25l")
    try:
        print("\n" * (PET_H - 1))
        for i, (mood, arg) in enumerate(shows):
            view.species = PET_SPECIES_ORDER[i % len(PET_SPECIES_ORDER)]
            view.set_idle(arg) if mood == "idle" else view.set_mood(mood, arg)
            for _ in range(8):
                sys.stdout.write(f"\033[{PET_H}F" + "\n".join(view.frame()) + "\n")
                sys.stdout.flush()
                time.sleep(PET_TICK_SEC)
        size = shutil.get_terminal_size()
        print("\n".join(session_lines(_demo_sessions(), max(1, size.columns), max(8, size.lines - 1))))
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout.write("\033[?25h")
        sys.stdout.flush()

# ── 自动更新 ──────────────────────────────────────────────────────────────────
#
# 每 10 分钟查一次上游 main 上的 provider_balance.py：__version__ 比本地新，就先下载到
# ~/.see-balance/update/ 暂存，再提示装上（watch 里按 u，或运行 --update）。
# 从 git checkout 里跑时只读上游版本号、提示 git pull，不暂存、不覆盖工作区。

UPDATE_URL       = (os.environ.get("SEE_BALANCE_UPDATE_URL")
                    or "https://raw.githubusercontent.com/jhfnetboy/see-balance/main/provider_balance.py")
UPDATE_CHECK_SEC = 10 * 60         # 多个终端共用 state 里的检查时间，不会叠加请求；没变时只是一次 304
UPDATE_RETRY_SEC = UPDATE_CHECK_SEC
UPDATE_MAX_BYTES = 4 * 1024 * 1024
UPDATE_DEADLINE  = 60              # 下载总时限（秒）：urlopen 的 timeout 只管单次读写
UPDATE_STAGED    = STATE_FILE.parent / "update" / "provider_balance.py"
# 文件得以入口结尾才算完整；正则里写成 \n，源码里这一行本身不会被当成入口
_MAIN_TAIL       = re.compile(r'\nif __name__ == "__main__":\n[ \t]+main\(\)\s*\Z')


def _version_of(text):
    m = re.search(r"""^__version__\s*=\s*["']([0-9]+(?:\.[0-9]+)*)["']""", text or "", re.M)
    return m.group(1) if m else None


def _version_key(v):
    key = [int(x) for x in v.split(".")]
    while key and key[-1] == 0:   # 1.2 和 1.2.0 算同一版
        key.pop()
    return tuple(key)


def _installed_version():
    """磁盘上这份脚本的版本：别的终端可能已经装了更新的，正在跑的进程还是旧的。"""
    try:
        return _version_of(Path(__file__).resolve().read_text(encoding="utf-8"))
    except (OSError, UnicodeError):
        return None


def _current_version():
    disk = _installed_version()
    return disk if disk and _version_key(disk) > _version_key(__version__) else __version__


def _newer(v):
    """v 比本机已有的（正在跑的、磁盘上的，取新的那个）还新。"""
    return bool(v) and _version_key(v) > _version_key(_current_version())


def _from_git_checkout():
    """是不是直接从 see-balance 的 git checkout 里跑：往上找 .git，且仓库根就是本脚本所在目录。

    ~/bin 被别的仓库（如 dotfiles）管着时不算：那是一份装好的副本，照常自更新。
    """
    here = Path(__file__).resolve().parent
    for d in (here, *here.parents):
        if (d / ".git").exists():
            return d == here and (d / "install.sh").exists()
    return False


def _owned_by_me(path, private=False):
    """path 是自己的真实文件/目录（不是符号链接）；private=True 时还要求别人写不进。

    SEE_BALANCE_STATE 指到 /tmp 这类共享目录时，别让人预先塞文件或放个链接指到别处。
    没有 getuid 的平台（Windows）只查不是符号链接。
    """
    try:
        st = os.lstat(path)
    except OSError:
        return False
    if stat.S_ISLNK(st.st_mode):
        return False
    if not hasattr(os, "getuid"):
        return True
    return st.st_uid == os.getuid() and not (private and st.st_mode & 0o022)


def _private_dir(d):
    return _owned_by_me(d, private=True)


def _valid_version(text):
    """内容是不是一份完整的本程序：以入口结尾、能编译、带版本号。是就返回版本号。

    只靠能编译不够：半截文件常常照样能编译，装上去却什么都不做。
    """
    if not text or not _MAIN_TAIL.search(text):
        return None
    try:
        compile(text, "provider_balance.py", "exec")
    except (SyntaxError, ValueError):
        return None
    return _version_of(text)


def _write_atomic(path, text, mode=None):
    """先写同目录下的独一份临时文件再改名：别的进程要么读到旧文件，要么读到完整的新文件。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())   # 先落盘再改名：断电也不会留下空文件
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _download(url, etag=None):
    """下载完整内容，返回 (文本, ETag)；带上次的 ETag 且上游没变时返回 (None, ETag)，不重复下载。

    超时、截断、过大都抛 ValueError / OSError。
    """
    deadline = time.time() + UPDATE_DEADLINE
    chunks, total = [], 0
    headers = {"User-Agent": "see-balance"}
    if etag:
        headers["If-None-Match"] = etag
    try:
        resp = urlopen(Request(url, headers=headers), timeout=HTTP_TIMEOUT)
    except HTTPError as e:
        if e.code == 304:
            return None, etag
        raise
    with resp:
        size, etag = resp.headers.get("Content-Length"), resp.headers.get("ETag")
        while True:
            if time.time() > deadline:
                raise ValueError("下载太慢，超时了")
            chunk = resp.read(64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > UPDATE_MAX_BYTES:
                raise ValueError("文件大得不正常")
    raw = b"".join(chunks)
    if size and size.isdigit() and int(size) != len(raw):   # 连接中途断了，read() 不报错只返回半截
        raise ValueError(f"只下载到 {len(raw)}/{size} 字节")
    return raw.decode("utf-8"), etag


def check_update(upd=None, force=False, now=None):
    """到点就查一次上游，返回新的 update 状态（存进 state.json）。

    每 10 分钟查一次（没查成也是 10 分钟后再试）；上游没变时只拿到一个 304，不重复下载。
    别的副本（比如 git checkout）查到了新版本、这份装好的副本却还没暂存时，不等到点，马上补下载。
    """
    now, upd = now or time.time(), dict(upd or {})
    staged = staged_update()[0]
    latest = upd.get("latest")
    if staged and latest and _version_key(staged) > _version_key(latest):
        # 暂存之后又有一次检查（可能是别的副本做的）说上游比它低：上游回滚了，这份别再装
        try:
            if os.lstat(UPDATE_STAGED).st_mtime < float(upd.get("checked_at") or 0):
                UPDATE_STAGED.unlink()
                staged = None
        except OSError:
            pass
    # 上游有比本机和暂存都新的版本，这份装好的副本却还没拿到：不等到点，马上补下载
    behind = (not _from_git_checkout() and _newer(latest)
              and (not staged or _version_key(staged) < _version_key(latest)))
    if upd.get("error"):
        wait = UPDATE_RETRY_SEC       # 上次没成：到点再试（补下载也一样，别每次刷新都下）
    else:
        wait = 0 if behind else UPDATE_CHECK_SEC
    if not force and now - float(upd.get("checked_at") or 0) < wait:
        return upd
    upd["checked_at"] = int(now)
    # 需要拿到文件内容（还没查到过版本、要补下载）时不带 ETag，其余时候上游没变就只回一个 304
    etag = upd.get("etag") if latest and not behind else None
    try:
        text, etag = _download(UPDATE_URL, etag)
    except (URLError, http.client.HTTPException, TimeoutError, socket.timeout, OSError, ValueError) as e:
        upd["error"] = str(e)[:200]
        return upd
    if text is None:   # 304：上游没变，上次的结论照旧
        upd.pop("error", None)
        return upd
    latest = _valid_version(text)
    if not latest:   # 坏内容不覆盖上次查到的版本号，已暂存的好版本照样提示
        upd["error"] = "上游文件不完整（编译不过、没有版本号或缺入口），先不更新"
        upd.pop("etag", None)
        return upd
    upd.pop("error", None)
    upd["latest"] = latest
    if etag:
        upd["etag"] = etag
    else:
        upd.pop("etag", None)
    if _from_git_checkout():
        return upd
    staged = staged_update()[0]
    if staged and _version_key(staged) > _version_key(latest):   # 上游回滚了：暂存的那版别再装
        try:   # 下载期间别的终端刚暂存的更新版本不算回滚（那是它查到的更新的上游）
            if os.lstat(UPDATE_STAGED).st_mtime < now:
                UPDATE_STAGED.unlink()
        except OSError:
            pass
    staged = staged_update()[0]
    if _newer(latest) and not (staged and _version_key(staged) >= _version_key(latest)):   # 别人刚暂存的更新版本不覆盖
        try:
            UPDATE_STAGED.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not _private_dir(UPDATE_STAGED.parent):
                raise OSError(f"{UPDATE_STAGED.parent} 不是你自己的私有目录，不往里放新版本")
            _write_atomic(UPDATE_STAGED, text)
        except (OSError, UnicodeError) as e:
            upd["error"] = f"下载的新版本存不下来：{e}"[:200]
    return upd


def staged_update():
    """暂存好、确实比本机已有的新的 (版本号, 内容)；没有返回 (None, None)。"""
    if not _private_dir(UPDATE_STAGED.parent):
        return None, None
    try:
        if not _owned_by_me(UPDATE_STAGED):
            return None, None
        text = UPDATE_STAGED.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return None, None
    v = _valid_version(text)
    return (v, text) if _newer(v) else (None, None)


def _short_path(p):
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home + os.sep) else p


def update_hint(upd, interactive=False):
    """有新版本时给一行提示；没有返回 None。装好的副本以暂存区为准，git checkout 以上游版本号为准。"""
    if _from_git_checkout():
        latest = (upd or {}).get("latest")
        if not _newer(latest):
            return None
        return f"🆕 上游有新版本 v{latest}（当前 v{__version__}），git pull 一下就能用上"
    staged = staged_update()[0]
    if not staged:
        return None
    how = "按 u 更新" if interactive else f"运行 python3 {_short_path(str(Path(__file__).resolve()))} --update 更新"
    return f"🆕 新版本 v{staged} 已下载好（当前 v{__version__}），{how}"


def apply_update():
    """把暂存的新版本装到当前脚本的位置，旧版留一份 .bak。返回 (成功与否, 说明)。"""
    if _from_git_checkout():
        return False, "这是 git checkout，用 git pull 更新"
    v, text = staged_update()
    if not v:
        cur = _current_version()
        if cur != __version__:   # 别的终端已经装好了，这个进程还是旧的
            return False, f"已经装好 v{cur}，重启一下就能用上"
        return False, f"已经是最新版 v{cur}"
    target = Path(__file__).resolve()
    if not _newer(v):   # 刚才这段时间里别的终端已经装了同样新或更新的版本
        return False, f"已经装好 v{_current_version()}，重启一下就能用上"
    try:
        old = target.read_text(encoding="utf-8")   # .bak 用原子写：已有同名符号链接时换掉链接本身，不顺着写到别处
        _write_atomic(target.with_name(target.name + ".bak"), old, mode=target.stat().st_mode & 0o7777)
        _write_atomic(target, text, mode=target.stat().st_mode & 0o7777)
    except (OSError, UnicodeError) as e:
        return False, f"更新失败：{e}"
    try:
        UPDATE_STAGED.unlink()
    except OSError:
        pass
    return True, f"已更新到 v{v}（旧版备份在 {_short_path(str(target))}.bak）"


# ── 会话提醒 ──────────────────────────────────────────────────────────────────
#
# Claude Code / Grok 的会话在等你确认权限、或问了你问题在等回复时，宠物弹出来提醒你回去处理。
# 信号来自一个只记录的 hook（--install-hooks 装进 ~/.claude/settings.json；Grok 默认也读这个文件）：
# 它把事件追加到 ~/.see-balance/attention.jsonl，只记会话 id、目录、事件和一小段说明；
# 决策框（PermissionRequest）另记要你决定的内容：问题和选项、命令、文件路径或计划标题，都截短。
# 宠物只提醒，不替你批准任何东西：回原来的终端作答。

ATTENTION_LOG     = STATE_FILE.parent / "attention.jsonl"
ATTENTION_HOOK    = STATE_FILE.parent / "hook.py"
ATTENTION_MAX_AGE = 12 * 3600          # 超过这么久还没动静的提醒（终端关了等）不再显示
ATTENTION_ROTATE  = 1024 * 1024        # 日志超过这么大就换新文件（旧的留一份 .old）
# 外部提醒：别的本机程序写 JSON，宠物才看得到。不建这些文件就跟以前一样。
NOTICES_DIR         = STATE_FILE.parent / "notices"       # 一个文件一条；删掉文件这条就消失
SLACK_SENT_DIR      = STATE_FILE.parent / "slack-sent"    # 每条提醒一个文件：写了 sent 才算发过，空的是在发或没发成
SLACK_DELAY_DEFAULT = 60                                  # 等这么多秒还没处理才发
SLACK_RETRY         = 300                                 # 发送失败后多久再试（秒）
ACTIONS_FILE        = STATE_FILE.parent / "actions.json"  # 只有这里登记过的命令能被按键跑
NOTICE_MAX_BYTES    = 16 * 1024     # 再大的提醒文件不读
NOTICE_STATUS_MAX   = 3             # 底部状态最多同时几条
NOTICE_STATUS_LINES = 4             # 每条状态最多几行正文
ACTION_TIMEOUT      = 60            # actions.json 没写 timeout 时用这个
_ACTION_KEY_TAKEN   = set("quknrtps")  # q u k n r t p s；再绑这些键就只能按回车。数字、Tab、回车不是小写字母
CLAUDE_SETTINGS   = Path(os.environ.get("SEE_BALANCE_CLAUDE_SETTINGS")
                         or (Path.home() / ".claude" / "settings.json"))
GROK_HOOK_FILE    = Path(os.environ.get("SEE_BALANCE_GROK_HOOKS")
                         or (Path.home() / ".grok" / "hooks" / "see-balance.json"))
HOOK_EVENTS       = ("Notification", "PermissionRequest", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")
# Grok 里拒绝 / 取消权限框时发的是 StopCancelled（代替 Stop）。Claude Code 不认识这个事件，
# 所以不放进共享的 ~/.claude/settings.json，单独写一份只有 Grok 读的 ~/.grok/hooks/see-balance.json
GROK_ONLY_EVENTS  = ("StopCancelled",)
SOURCE_NAMES      = {"claude": "Claude", "grok": "Grok"}
# Notification 的类型 → 等你做什么；没列出的（auth_success、agent_completed 等）不算等你
WAIT_KINDS        = {"permission_prompt": "确认", "elicitation_dialog": "填写",
                     "elicitation_url_dialog": "填写", "agent_needs_input": "回复"}
# 决策框弹出的是哪个工具 → 等你做什么；其余工具都是权限确认
DECIDE_KINDS      = {"AskUserQuestion": "选择", "ExitPlanMode": "批准计划"}
ATTENTION_ROTATE_SEC = 4               # 好几个会话在等时，宠物气泡每隔几秒换下一个
PET_WORK_SCAN_SEC    = 5               # 用量页宠物开着时，隔这么久扫一遍进行中的会话，气泡轮播它们在干什么
# 在宠物上做决定：--watch 每秒摸一下 PET_ALIVE；hook 看它新鲜，就在决策框弹出时等宠物上的决定（DECISION_DIR 里一框一个文件），
# 等到了交回会话。终端里的对话框照常显示，哪边先答算哪边。
PET_ALIVE         = STATE_FILE.parent / "pet.alive"
DECISION_DIR      = STATE_FILE.parent / "decisions"
DECIDE_WAIT       = 590                # hook 最多等这么久，之后交回终端（hook 的超时设成 DECIDE_WAIT + 10）

HOOK_SCRIPT = r'''#!/usr/bin/env python3
# see-balance 会话提醒 hook（由 provider_balance.py --install-hooks 生成，--uninstall-hooks 删除）。
# 往日志追加一行：哪个会话、在哪个目录、发生了什么。只有一种情况会输出：宠物（see-balance --watch）开着时，
# Claude Code 的决策框会等你在宠物上做决定，再把决定交回会话；你先在终端答了、宠物关了或等太久，就什么都不输出地退出。
import json, os, re, sys, time
raw = sys.stdin.readline()
try:
    e = json.loads(raw)
except ValueError:              # 不是单行 JSON：读完剩下的再试一次
    try:
        e = json.loads(raw + sys.stdin.read())
    except ValueError:
        sys.exit(0)
if not isinstance(e, dict):
    sys.exit(0)
def pet_alive(sec):
    try:
        return time.time() - os.stat(__ALIVE__).st_mtime < sec
    except OSError:
        return False
ev  = e.get("hook_event_name") or ""
sub = bool(e.get("agent_id") or e.get("subagentType"))
# 子 agent 的输入 / 会话结束不代表主会话；它弹的权限框、它这一轮的结束（只清它自己的提醒）还是要记
if sub and ev not in ("Notification", "PermissionRequest", "Stop", "StopCancelled", "StopFailure", "SessionEnd"):
    sys.exit(0)
rec = {"ts": time.time(), "src": "grok" if "hookEventName" in e else "claude", "ev": ev,
       "sid": e.get("session_id") or e.get("sessionId") or "", "cwd": e.get("cwd") or "",
       "pid": e.get("prompt_id") or e.get("promptId") or ""}
if sub:
    rec["sub"] = True
if ev == "Notification":
    rec["type"] = e.get("notification_type") or e.get("notificationType") or ""
    rec["msg"]  = str(e.get("message") or "")[:120]
    rec["transcript"] = e.get("transcript_path") or ""
elif ev == "PermissionRequest":  # 决策框刚弹出来：记下要你决定的是什么（都截短，不记整段输入）
    tool = str(e.get("tool_name") or e.get("toolName") or "")
    ti   = e.get("tool_input") or e.get("toolInput") or {}
    ti   = ti if isinstance(ti, dict) else {}
    rec["tool"], rec["transcript"] = tool[:60], e.get("transcript_path") or ""
    try:   # 从这里往后，会话记录里出现 user 条目（工具结果、打断、你的新输入）就说明这一框处理完了
        rec["tsize"] = os.stat(rec["transcript"]).st_size
    except OSError:
        pass
    # 宠物上看不全的（超长的命令 / 问题 / 选项，或者计划——只记了标题）记成 cut：只显示，不让在宠物上决定
    if tool == "AskUserQuestion":
        qs = [q for q in (ti.get("questions") or []) if isinstance(q, dict)]
        rec["qs"] = [{"q": str(q.get("question") or "")[:500], "multi": bool(q.get("multiSelect")),
                      "opts": [str(o.get("label") or "")[:100] for o in (q.get("options") or []) if isinstance(o, dict)][:9]}
                     for q in qs][:4]
        rec["cut"] = len(qs) > 4 or any(len(str(q.get("question") or "")) > 500 or len(q.get("options") or []) > 9
                                        or any(len(str(o.get("label") or "")) > 100 for o in (q.get("options") or [])
                                               if isinstance(o, dict)) for q in qs)
    elif tool == "ExitPlanMode":   # 只记计划的第一行（一般是标题）
        rec["what"] = next((l.strip().lstrip("#").strip() for l in str(ti.get("plan") or "").splitlines() if l.strip()), "")[:200]
        rec["cut"] = True
    else:                          # 命令 / 文件 / 网址；认不出的工具记一段输入
        v = next((ti[k] for k in ("command", "file_path", "notebook_path", "url", "path", "pattern", "query")
                  if isinstance(ti.get(k), str) and ti[k]), None)
        full = v if v is not None else json.dumps(ti, ensure_ascii=False)
        rec["what"], rec["cut"] = full[:1000], len(full) > 1000
    if rec["src"] == "claude" and not rec["cut"] and pet_alive(5):   # 宠物开着、看得全：这一框可以在宠物上决定
        rec["rid"] = f"{time.time_ns()}-{os.getpid()}"
elif ev == "Stop":               # 最后一句是不是在问你：之后的“空闲等待”才算在等你回复
    tail = str(e.get("last_assistant_message") or "")[-400:].rstrip().rstrip("\"'”’)）】*_` ")[-300:]   # 先截短：长文本上正则会很慢
    m    = re.search(r"[^\n。！!?？]*[?？]$", tail)    # 只认以问号结尾的最后一句，正文里的 URL?q= 不算
    rec["ask"] = m.group(0).strip()[-120:] if m else ""
try:
    fd = os.open(__LOG__, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    os.write(fd, (json.dumps(rec, ensure_ascii=False) + "\n").encode())
    os.close(fd)
except OSError:
    pass
if rec.get("rid"):   # 等宠物上的决定：picks 是每道题选中的选项序号，按原题补全成 answers
    path, start = os.path.join(__DECIDE__, rec["rid"] + ".json"), time.time()
    def answered():   # 只认 user 条目：Claude Code 还会在后台补写标题等，那些不算
        try:
            with open(rec["transcript"], "rb") as fh:
                fh.seek(rec.get("tsize", 0))
                return b'"type":"user"' in fh.read(4 << 20)
        except OSError:
            return False
    while time.time() - start < __WAIT__:
        try:
            with open(path, encoding="utf-8") as fh:
                d = json.load(fh)
        except (OSError, ValueError):
            d = None
        if isinstance(d, dict):
            try:
                os.unlink(path)
            except OSError:
                pass
            dec = {"behavior": "deny", "message": "用户在 see-balance 宠物上拒绝了这一步"}
            if d.get("allow"):
                dec = {"behavior": "allow"}
                if tool == "AskUserQuestion" and isinstance(d.get("picks"), list):
                    qs, answers = [q for q in (ti.get("questions") or []) if isinstance(q, dict)], {}
                    for q, picked in zip(qs, d["picks"]):
                        opts = [o for o in (q.get("options") or []) if isinstance(o, dict)]
                        labels = [str(opts[i].get("label") or "") for i in picked if isinstance(i, int) and 0 <= i < len(opts)]
                        if labels:
                            answers[str(q.get("question") or "")] = ", ".join(labels)
                    dec["updatedInput"] = dict(ti, answers=answers)
            sys.stdout.write(json.dumps({"hookSpecificOutput": {"hookEventName": "PermissionRequest", "decision": dec}},
                                        ensure_ascii=False))
            break
        if answered() or not pet_alive(30):   # 已经在终端里答了，或者宠物关了：交回终端
            break
        time.sleep(0.3)
'''


def _hook_script():
    return (HOOK_SCRIPT.replace("__LOG__", repr(str(ATTENTION_LOG))).replace("__ALIVE__", repr(str(PET_ALIVE)))
            .replace("__DECIDE__", repr(str(DECISION_DIR))).replace("__WAIT__", str(DECIDE_WAIT)))


def _hook_command():
    """永远退出 0、不输出：hook 脚本被删了也不能拦下你的输入（Claude Code / Grok 把退出码 2 当成阻断）。"""
    return f"python3 -S {shlex.quote(str(ATTENTION_HOOK))} >/dev/null 2>&1 || true"


def _decide_command():
    """决策框用的：同样永远退出 0，但留着 stdout，宠物上做的决定从这里交回会话。"""
    return f"python3 -S {shlex.quote(str(ATTENTION_HOOK))} 2>/dev/null || true"


_OUR_HOOK_CMD = re.compile(r"python3 -S (\S+/hook\.py|'[^']*/hook\.py') (>/dev/null 2>&1|2>/dev/null) \|\| true")


def _is_our_command(command):
    """只认 install_hooks 生成的那几种命令（换过 SEE_BALANCE_STATE 装的也认），别的不碰。"""
    return command in (_hook_command(), _decide_command()) or bool(_OUR_HOOK_CMD.fullmatch(command))


def _wanted_hooks(events):
    """每个事件挂什么命令、超时多少秒。"""
    return {ev: (_decide_command(), DECIDE_WAIT + 10) if ev == "PermissionRequest" else (_hook_command(), 5)
            for ev in events}


def _handlers(groups):
    """一个事件下所有 matcher 组里的 handler；结构不对的组跳过。"""
    for g in groups:
        hs = g.get("hooks") if isinstance(g, dict) else None
        if isinstance(hs, list):
            yield from hs


def _load_settings(path):
    """读 hook 配置文件；格式不对返回 (None, 原因)，不往里改。"""
    try:
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as e:
        return None, str(e)
    if not isinstance(data, dict) or not isinstance(data.get("hooks", {}), dict):
        return None, "格式不是预期的 JSON 对象"
    if any(not isinstance(v, list) for v in data.get("hooks", {}).values()):
        return None, "hooks 里有不是列表的条目"
    return data, None


def _save_settings(path, data):
    """改之前留一份 .see-balance.bak；符号链接（dotfiles）写到链接目标，保留原来的权限。"""
    real = path.resolve()
    real.parent.mkdir(parents=True, exist_ok=True)
    mode = None
    if real.exists():
        mode = real.stat().st_mode & 0o7777
        shutil.copy2(real, real.with_name(real.name + ".see-balance.bak"))
    _write_atomic(real, json.dumps(data, indent=2, ensure_ascii=False) + "\n", mode=mode)


def _add_hooks(path, wanted):
    """按 wanted（事件 → (命令, 超时)）挂到 path 上：已挂好的跳过，旧版本挂的换成现在的命令和超时。
    返回 (新挂或改过的事件, 错误)。"""
    data, err = _load_settings(path)
    if data is None:
        return [], err
    hooks, added = data.setdefault("hooks", {}), []
    for ev, (cmd, timeout) in wanted.items():
        groups = hooks.setdefault(ev, [])
        ours = [h for h in _handlers(groups) if isinstance(h, dict) and _is_our_command(str(h.get("command") or ""))]
        if any(h.get("command") == cmd and h.get("timeout") == timeout for h in ours):
            continue
        if ours:
            ours[0].update(command=cmd, timeout=timeout)
        else:
            groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": timeout}]})
        added.append(ev)
    if added:
        _save_settings(path, data)
    return added, None


def _remove_hooks(path, is_ours):
    """摘掉 is_ours(command) 为真的条目，别的不动。返回 (摘掉的事件, 错误)。"""
    if not path.exists():
        return [], None
    data, err = _load_settings(path)
    if data is None:
        return [], err
    hooks, removed = data.get("hooks") or {}, []
    for ev in list(hooks):
        groups, touched = [], False
        for g in hooks[ev]:
            hs = g.get("hooks") if isinstance(g, dict) else None
            if not isinstance(hs, list):       # 结构看不懂的组原样留着
                groups.append(g)
                continue
            kept = [h for h in hs if not (isinstance(h, dict) and is_ours(str(h.get("command") or "")))]
            if len(kept) == len(hs):
                groups.append(g)
                continue
            touched = True
            if kept:
                groups.append(dict(g, hooks=kept))
        if not touched:
            continue
        removed.append(ev)
        if groups:
            hooks[ev] = groups
        else:                                  # 只删被我们摘空的事件；原本就空的不动
            del hooks[ev]
    if removed:
        _save_settings(path, data)
    return list(dict.fromkeys(removed)), None


def install_hooks():
    """写 hook 脚本，并把它挂到 ~/.claude/settings.json 的几个事件上（已挂过的不重复挂）。返回说明行。"""
    ATTENTION_HOOK.parent.mkdir(parents=True, exist_ok=True)
    _write_atomic(ATTENTION_HOOK, _hook_script(), mode=0o700)
    out = [f"✓ hook 脚本：{_short_path(str(ATTENTION_HOOK))}"]
    added, err = _add_hooks(CLAUDE_SETTINGS, _wanted_hooks(HOOK_EVENTS))
    if err:
        return out + [f"⚠ 读不了 {_short_path(str(CLAUDE_SETTINGS))}：{err}，没有改动"]
    out.append(f"✓ 已挂到 {_short_path(str(CLAUDE_SETTINGS))}：{'、'.join(added)}（原文件备份为 .see-balance.bak）"
               if added else f"✓ {_short_path(str(CLAUDE_SETTINGS))} 里已经挂好了，没有改动")
    if GROK_HOOK_FILE.parent.parent.exists():   # 装了 Grok CLI
        added, err = _add_hooks(GROK_HOOK_FILE, _wanted_hooks(GROK_ONLY_EVENTS))
        if err:
            out.append(f"⚠ 读不了 {_short_path(str(GROK_HOOK_FILE))}：{err}，Grok 拒绝权限后的提醒可能要等下一句话才消失")
        elif added:
            out.append(f"✓ 已挂到 {_short_path(str(GROK_HOOK_FILE))}：{'、'.join(added)}（只有 Grok 读）")
    out.append("  Grok CLI 默认也读 ~/.claude/settings.json；新开的 Claude Code / Grok 会话开始生效。")
    return out


def uninstall_hooks():
    """把 install_hooks 挂上的条目摘掉（别的 hook 不动），删掉 hook 脚本。返回说明行。"""
    out = []
    for path in (CLAUDE_SETTINGS, GROK_HOOK_FILE):
        removed, err = _remove_hooks(path, _is_our_command)
        if err:
            out.append(f"⚠ 读不了 {_short_path(str(path))}：{err}，没有改动")
        elif removed:
            out.append(f"✓ 已从 {_short_path(str(path))} 摘掉：{'、'.join(removed)}（原文件备份为 .see-balance.bak）")
    data, _ = _load_settings(GROK_HOOK_FILE)
    if GROK_HOOK_FILE.exists() and data == {"hooks": {}}:   # 这份是 install 时新建的，摘空了就删掉
        for p in (GROK_HOOK_FILE, GROK_HOOK_FILE.with_name(GROK_HOOK_FILE.name + ".see-balance.bak")):
            try:
                p.unlink()
            except OSError:
                pass
    if not out:
        out.append("✓ 没有找到 see-balance 的 hook")
    try:
        ATTENTION_HOOK.unlink()
        out.append(f"✓ 已删除 {_short_path(str(ATTENTION_HOOK))}")
    except OSError:
        pass
    return out


def hooks_installed():
    data, _ = _load_settings(CLAUDE_SETTINGS)
    cmd = _hook_command()
    return bool(data) and ATTENTION_HOOK.exists() and any(
        isinstance(h, dict) and h.get("command") == cmd
        for h in _handlers((data.get("hooks") or {}).get("Notification", [])))


def refresh_hooks():
    """已经开了会话提醒、但 hook 是旧版本装的（自动更新不会重跑 --install-hooks）：
    把 hook 脚本换成新的，补挂新加的事件、改掉旧的命令和超时。别的 hook 不动；读不了配置就什么都不改。"""
    if not hooks_installed():
        return
    script = _hook_script()
    try:
        if ATTENTION_HOOK.read_text(encoding="utf-8") != script:
            _write_atomic(ATTENTION_HOOK, script, mode=0o700)
        _add_hooks(CLAUDE_SETTINGS, _wanted_hooks(HOOK_EVENTS))
    except OSError:
        pass


def _read_lines(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.readlines()
    except OSError:
        return []


def _attention_lines(now=None):
    """读日志（连同轮换下来的 .old）。太大就轮换：先把当前日志改名挪开（hook 之后的追加落到新文件），
    再把 12 小时内的记录并进 .old —— 还在等的提醒都在这个范围里，轮换几次都不会丢。"""
    now  = now or time.time()
    old  = ATTENTION_LOG.with_name(ATTENTION_LOG.name + ".old")
    try:
        big = ATTENTION_LOG.stat().st_size > ATTENTION_ROTATE
    except OSError:
        big = False
    leftovers = sorted(ATTENTION_LOG.parent.glob(ATTENTION_LOG.name + ".*.rotating"))   # 上次轮换半路失败留下的
    if big or leftovers:
        moving = ATTENTION_LOG.with_name(f"{ATTENTION_LOG.name}.{os.getpid()}-{time.time_ns()}.rotating")
        try:
            if big:
                os.replace(ATTENTION_LOG, moving)
            parts = sorted(set(leftovers) | ({moving} if big else set()))
            keep = []
            for line in _read_lines(old) + [l for p in parts for l in _read_lines(p)]:
                try:
                    if now - float(json.loads(line).get("ts") or 0) <= ATTENTION_MAX_AGE:
                        keep.append(line if line.endswith("\n") else line + "\n")
                except (ValueError, AttributeError):
                    pass
            _write_atomic(old, "".join(keep))
            for p in parts:
                os.unlink(p)
        except OSError:
            pass
    leftovers = sorted(ATTENTION_LOG.parent.glob(ATTENTION_LOG.name + ".*.rotating"))   # 没并成功的照样读，提醒不丢
    return _read_lines(old) + [l for p in leftovers for l in _read_lines(p)] + _read_lines(ATTENTION_LOG)


def _answered_since(transcript, offset):
    """会话记录从 offset 往后有没有 user 条目。Claude Code 会在后台补写标题等，只看 user 条目才不误判。"""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(offset)
            return b'"type":"user"' in fh.read(4 << 20)
    except (OSError, TypeError, ValueError):
        return False


def attention_items(now=None):
    """现在还在等你的会话，按等待时间从早到晚：[{src, sid, cwd, kind, ts, msg}]。

    等你：决策框（PermissionRequest：权限确认、选择题、批准计划，带上具体内容）、表单（Notification 的对应类型），
    或者最后一句在问你、之后一直空闲（idle_prompt）。
    不再等：你回了话（UserPromptSubmit）、这一轮结束（Stop）、会话关了（SessionEnd），
    或者会话记录在提醒之后又有了新内容（你在终端里已经处理了）。
    """
    now, sessions = now or time.time(), {}
    for line in _attention_lines(now):
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if not isinstance(r, dict) or not r.get("sid"):
            continue
        s, ev = sessions.setdefault((r.get("src"), r["sid"]), {"wait": None, "ask": "", "pid": ""}), r.get("ev")
        w, pid = s["wait"], r.get("pid") or ""
        if ev == "UserPromptSubmit" and pid:
            s["pid"] = pid            # 记住这个会话最新的一轮
        if ev in ("Stop", "StopCancelled", "StopFailure", "SessionEnd") and r.get("sub"):
            if w and w.get("sub"):    # 子 agent 这一轮结束 / 子会话关了：只清它自己弹的提醒
                s["wait"] = None
            continue
        if ev in ("Stop", "StopCancelled", "StopFailure"):
            latest = s["pid"] or (w or {}).get("pid") or ""
            if pid and latest and pid != latest:
                continue              # 上一轮迟到的回合结束（Grok 会排队晚到），别清掉这一轮的提醒或问话
        if ev == "PermissionRequest":
            tool = r.get("tool") or ""
            s["wait"] = {"src": r.get("src"), "sid": r["sid"], "cwd": r.get("cwd") or "",
                         "kind": DECIDE_KINDS.get(tool, "确认"), "ts": float(r.get("ts") or 0), "msg": "",
                         "tool": tool, "what": r.get("what") or "", "qs": r.get("qs") or [], "rid": r.get("rid") or "",
                         "tsize": r.get("tsize"), "cut": bool(r.get("cut")),
                         "transcript": r.get("transcript") or "", "pid": r.get("pid") or "", "sub": bool(r.get("sub"))}
        elif ev == "Notification":
            typ  = r.get("type") or ""
            kind = WAIT_KINDS.get(typ) or ("回复" if typ == "idle_prompt" and s["ask"] else None)
            if (typ == "permission_prompt" and w and w.get("tool") and w["sub"] == bool(r.get("sub"))
                    and float(r.get("ts") or 0) - w["ts"] < 30):
                pass                  # 同一个决策框几秒后的提醒：前面已经记下了具体内容
            elif kind:
                msg = s["ask"] if typ == "idle_prompt" else (r.get("msg") or "")
                s["wait"] = {"src": r.get("src"), "sid": r["sid"], "cwd": r.get("cwd") or "", "kind": kind,
                             "ts": float(r.get("ts") or 0), "msg": msg, "transcript": r.get("transcript") or "",
                             "pid": r.get("pid") or "", "sub": bool(r.get("sub"))}
        elif ev == "Stop":
            s["wait"], s["ask"] = None, r.get("ask") or ""
        elif ev in ("StopCancelled", "StopFailure"):   # 拒绝 / 取消了权限框，或出错，这一轮就此结束
            s["wait"], s["ask"] = None, ""
        elif ev in ("UserPromptSubmit", "SessionEnd"):
            s["wait"], s["ask"] = None, ""
    items = []
    for s in sessions.values():
        w = s["wait"]
        if not w or now - w["ts"] > ATTENTION_MAX_AGE:
            continue
        if isinstance(w.get("tsize"), int):   # 决策框：记录里出现了 user 条目（工具结果、打断、新输入）才算处理完
            if _answered_since(w["transcript"], w["tsize"]):
                continue
        else:
            try:   # 会话记录在提醒之后又写了东西：你已经在终端里处理过了
                if w["transcript"] and os.stat(w["transcript"]).st_mtime > w["ts"] + 1:
                    continue
            except OSError:
                pass
        w["title"] = session_title(w["transcript"]) if w["src"] == "claude" else ""
        items.append(w)
    return sorted(items, key=lambda w: w["ts"])


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean(text):
    """日志里的文字来自会话（模型输出、MCP 表单、目录名），显示前去掉控制字符，别让它改终端。"""
    return _CONTROL_CHARS.sub(" ", str(text or "")).strip()


def _project(w):
    return _clean(os.path.basename(w["cwd"].rstrip("/"))) or "~"


_TITLE_CACHE = {}


def session_title(transcript):
    """会话标题：/rename 起的名字优先，其次 Claude Code 自动起的（终端标签页上显示的那个）。读不到返回 ""。
    同一个项目开了好几个会话时，靠它分清是哪个在等你。按文件大小缓存，没变就不重读。"""
    try:
        size = os.stat(transcript).st_size
    except (OSError, TypeError, ValueError):
        return ""
    hit = _TITLE_CACHE.get(transcript)
    if hit and hit[0] == size:
        return hit[1]
    title = ""
    try:
        with open(transcript, "rb") as fh:
            fh.seek(max(0, size - 512 * 1024))      # 标题会反复写进记录，看最后一段就够
            tail = fh.read().decode("utf-8", "replace")
    except OSError:
        tail = ""
    for key in ("customTitle", "aiTitle"):
        found = re.findall(r'"%s":\s*("(?:[^"\\]|\\.)*")' % key, tail)
        if found:
            try:
                title = str(json.loads(found[-1]))
            except ValueError:
                pass
            break
    _TITLE_CACHE[transcript] = (size, title)
    return title


def _session_label(w):
    title = _clean(w.get("title"))
    return _project(w) + (f"「{_clip(title, 30)}」" if title else "")


def _attention_name(w):
    title = _clean(w.get("title"))
    return (f"{_project(w)}" + (f"「{_clip(title, 30)}」" if title else " ")
            + f"的 {SOURCE_NAMES.get(w['src'], _clean(w['src']))}")


def attention_detail(w):
    """决策框的具体内容，一项一行：问题和选项 / 计划标题 / 工具和命令。不是决策框的返回 []。"""
    if w.get("qs"):
        return [f"❓ {_clean(q.get('q'))}（{' / '.join(_clean(o) for o in q.get('opts') or [])}"
                f"{'，可多选' if q.get('multi') else ''}）" for q in w["qs"]]
    if w.get("tool") == "ExitPlanMode":
        return [f"📋 计划：{_clean(w.get('what')) or '（没有标题）'}"]
    if w.get("tool"):
        return [f"{_clean(w['tool'])}：{_clean(w.get('what'))}"]
    return []


def decidable(w, now=None):
    """这一框能不能在宠物上决定：hook 正在等宠物（有 rid），也还没等到超时。外部提醒不能。"""
    if w.get("notice"):
        return False
    return bool(w.get("rid")) and (now or time.time()) - w["ts"] < DECIDE_WAIT


def attention_bubble(items, now=None, pick=None):
    """宠物气泡（两行）：好几个在等时，每隔 ATTENTION_ROTATE_SEC 秒轮到下一个；pick 指定就固定显示那一个。
    外部 alert 的两行是 title、text，有好几条时同样标 i/n。"""
    i = pick if pick is not None else int((now or time.time()) // ATTENTION_ROTATE_SEC) % len(items)
    w, n = items[i], len(items)
    if w.get("notice"):
        mark = f" {i + 1}/{n}" if n > 1 else ""
        room = max(1, PET_MSG_MAX - display_width(mark))
        return f"{_clip(_clean(w.get('title')), room)}{mark}\n{_clip(_clean(w.get('text')), PET_MSG_MAX)}"
    title = _clean(w.get("title"))
    head = (f"等你{w['kind']}" + (f" {i + 1}/{n}" if n > 1 else "")
            + f"：{_project(w)}" + (f"·{title}" if title else ""))
    detail = [_clean(w["qs"][0].get("q"))] if w.get("qs") else attention_detail(w)
    return f"{head}\n{detail[0] if detail else '快去看看 ♥'}"


def work_sessions(sessions):
    """从进行中的会话里挑出正在干活的（运行、⚙ 命令、思考、等子 agent），等你和空闲不要，按紧急程度稳定排序。Grok 只有挂着 ⚙ 命令时才算。"""
    return sorted([s for s in sessions if 1 <= _session_rank(s) <= 3], key=_session_rank)


def _work_doing(s):
    """气泡第二行：先用会话头的在做什么；是「—」或空就找第一个正在跑的命令或工具，找不到就写思考中。"""
    doing = _session_group(s)[0]["doing"]
    if doing == "—" or not doing:
        doing = "思考中…"
        for node in _walk_nodes(s.get("children")):
            kind = _node_kind(node)
            if kind in ("运行", "命令", "外部"):
                if kind in ("命令", "外部"):
                    doing = "⚙ " + _row_name(node)
                else:
                    doing = f"{_row_name(node)}：{_doing_text(node)}"
                break
    return doing


def work_bubble(sessions, now=None):
    """宠物气泡（两行）：有会话在干活时，每隔 ATTENTION_ROTATE_SEC 秒轮到下一个，写着项目、标题和在做什么。"""
    i = int((now or time.time()) // ATTENTION_ROTATE_SEC) % len(sessions)
    s, n = sessions[i], len(sessions)
    head = "工作中" + (f" {i + 1}/{n}" if n > 1 else "") + f"：{_row_name(s)}"
    doing = _work_doing(s)
    return f"{head}\n{doing}"


def decision_lines(w, n, qi=0, picks=None):
    """选中的那一框：列出能按的选项和提示。"""
    picks, out = picks or {}, []
    if w.get("qs"):
        multi = False
        for j, q in enumerate(w["qs"]):
            mark  = "▸ " if len(w["qs"]) > 1 and j == qi else ""
            multi = multi or (j == qi and q.get("multi"))
            text  = _clean(q.get("q"))
            out.append(f"{mark}❓ {text}{'（可多选）' if q.get('multi') and '多选' not in text else ''}")
            out.append("   " + "   ".join(f"{'✓' if k in picks.get(j, []) else ''}{k + 1} {_clean(o)}"
                                          for k, o in enumerate(q.get("opts") or [])))
        keys = "按数字选择" + ("，选好按回车" if multi else "")
    else:
        out += attention_detail(w)
        ok = "批准" if w.get("tool") == "ExitPlanMode" else "允许"
        out.append(f"   1 {ok}   2 拒绝")
        keys = "按 1 / 2 决定"
    out.append(f"   {keys}{' · Tab 换下一个会话' if n > 1 else ''}"
               f" · 有别的想法就回 {_session_label(w)} 那个会话里自己写，那边的选项还在")
    return out


def attention_lines(items, now=None, focus=None, qi=0, picks=None):
    """底部提醒行：每个等你的会话一行，带上等了多久和那句话；决策框再逐行列出要你决定的内容。
    focus 是选中的那一框：能在宠物上决定的话，列出按键。外部 alert 排在会话后面。"""
    now = now or time.time()
    sessions = [w for w in items if not w.get("notice")]
    out = []
    for w in sessions[:5]:
        ago    = fmt_countdown(now - w["ts"])
        detail = attention_detail(w)
        what   = f"：{_clean(w['msg'])}" if w["msg"] and not detail else ""
        mine   = w is focus and decidable(w, now)
        out.append(f"{'👉' if mine else '🔔'} {_attention_name(w)} 在等你{w['kind']}（{ago} 前）{what}")
        if mine:   # 要在宠物上决定的：内容一个字都不截，看清楚再按
            out += [f"     {d}" for d in decision_lines(w, len(items), qi, picks)]
            continue
        if decidable(w, now) and len(items) > 1:
            detail = detail + ["（按 Tab 切过来，就能在这里决定）"]
        elif w.get("cut"):
            detail = detail + [f"（{'计划全文' if w.get('tool') == 'ExitPlanMode' else '内容太长'}在宠物上看不全，"
                               f"请回 {_session_label(w)} 那个会话里看完再决定）"]
        out += [f"     {_clip(d, 200)}" for d in detail]
    if len(sessions) > 5:
        out.append(f"🔔 还有 {len(sessions) - 5} 个会话在等你")
    width = _watch_width()
    for w in items:
        if w.get("notice"):
            out += _alert_notice_lines(w, now, w is focus, width)
    return out


# ── 外部提醒 ──────────────────────────────────────────────────────────────────
#
# 别的本机程序往 ~/.see-balance/notices/*.json 丢一条，宠物就能叫你（alert），
# 或只在底部留一块状态（status，不改情绪）。能按键跑的命令只来自 actions.json，
# 提醒文件里只写动作的名字，不会拿里面的内容去执行。两个路径都没有时，这里什么都不做。


def _watch_width():
    """和会话页底部提醒同一列宽：最右一列留给清行，避免擦掉最后一个字。"""
    return max(1, shutil.get_terminal_size().columns - 1)


def _epoch(value):
    """秒级时间戳。布尔和别的类型不算；转不成有限数字的也不算。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        result = float(value)
    except (OverflowError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _as_text(value):
    """只收字符串。别的类型（数字、对象）不当成要显示的文字。"""
    return _clean(value) if isinstance(value, str) else ""


def _output_line(text):
    """命令输出的第一行。没有就空串。"""
    if not text:
        return ""
    lines = str(text).splitlines()
    return lines[0] if lines else ""


def _action_message(label, ok, detail):
    """动作跑完后底部那句。detail 是 stdout / stderr 的第一行，或 timeout。"""
    label, detail = _clean(label), _clean(detail)
    return f"✓ {label}：{detail}" if ok else f"⚠ {label}失败：{detail}"


def _read_notice(path, now):
    """一个提醒文件。不能用就返回 None：不是自己的、太大、太旧、过期、读不了、格式不对。"""
    try:
        if not _owned_by_me(path):
            return None
        st = os.lstat(path)
        if not stat.S_ISREG(st.st_mode) or st.st_size > NOTICE_MAX_BYTES:
            return None
        if now - st.st_mtime > ATTENTION_MAX_AGE:
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return None
    try:
        if not isinstance(data, dict):
            return None
        level = data.get("level")
        if level not in ("alert", "status"):
            return None
        if "expires" in data and data.get("expires") is not None:
            exp = _epoch(data.get("expires"))
            if exp is None or exp <= now:   # 过期，或写了却不是时间戳：别留着一条清不掉的提醒
                return None
        ts = _epoch(data.get("ts"))
        if ts is None:
            return None
        if level == "alert":
            action = data.get("action")
            return "alert", {"notice": True, "src": "notice", "sid": path.name, "kind": "", "ts": ts,
                             "title": _as_text(data.get("title")), "text": _as_text(data.get("text")),
                             "action": action if isinstance(action, str) else ""}
        lines = data.get("lines")
        body = [_clean(x) for x in lines if isinstance(x, str)] if isinstance(lines, list) else []
        return "status", {"title": _as_text(data.get("title")), "lines": body, "ts": ts}
    except Exception:
        return None   # 这一份解析后出了意外：只丢掉这一份，别让整目录的提醒一起消失


def external_notices(now=None):
    """NOTICES_DIR 里的 *.json → (alerts, statuses)。按文件名排。任何错误都跳过，绝不抛出。"""
    try:
        now = now or time.time()
        try:
            names = sorted(name for name in os.listdir(NOTICES_DIR)
                           if name.endswith(".json") and not name.startswith("."))
        except OSError:
            return [], []
        alerts, statuses = [], []
        for name in names:
            item = _read_notice(NOTICES_DIR / name, now)
            if not item:
                continue
            if item[0] == "alert":
                alerts.append(item[1])
            elif len(statuses) < NOTICE_STATUS_MAX:
                statuses.append(item[1])
        return alerts, statuses
    except Exception:
        return [], []


def _action_timeout(value):
    """没写、或不是正数时，用 ACTION_TIMEOUT。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        return ACTION_TIMEOUT
    return value


def _one_action(name, spec, used):
    """一条动作。cmd 必须是非空的字符串列表，且第一个是绝对路径的可执行文件。不合就返回 None。"""
    if not isinstance(name, str) or not name or not isinstance(spec, dict):
        return None
    cmd = spec.get("cmd")
    if not isinstance(cmd, list) or not cmd or not all(isinstance(x, str) for x in cmd):
        return None
    exe = cmd[0]
    if not os.path.isabs(exe) or not os.path.isfile(exe) or not os.access(exe, os.X_OK):
        return None
    label = _as_text(spec.get("label")) or _clean(name)
    key = spec.get("key")
    if (not isinstance(key, str) or len(key) != 1 or key not in "abcdefghijklmnopqrstuvwxyz"
            or key in _ACTION_KEY_TAKEN or key in used):
        key = ""   # 撞上已有的键、或不是单个小写字母：动作还在，只能按回车
    return {"label": label, "key": key, "cmd": list(cmd), "timeout": _action_timeout(spec.get("timeout"))}


def load_actions():
    """读 ACTIONS_FILE。不是自己的、或群组/其他人写得进，整份不用。出错返回 {}。"""
    try:
        if not _owned_by_me(ACTIONS_FILE, private=True):
            return {}
        st = os.lstat(ACTIONS_FILE)
        if not stat.S_ISREG(st.st_mode):
            return {}
        data = json.loads(ACTIONS_FILE.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {}
    except Exception:
        return {}
    if not isinstance(data, dict):
        return {}
    out, used = {}, set()
    for name, spec in data.items():
        try:
            act = _one_action(name, spec, used)
        except Exception:
            continue
        if not act:
            continue
        out[name] = act
        if act["key"]:
            used.add(act["key"])
    return out


def annotate_notice_actions(alerts, actions):
    """把已登记动作的 label / key 抄到 alert 上，供底部那行用。不抄 cmd。"""
    actions = actions or {}
    for w in alerts:
        act = actions.get(w.get("action") or "")
        w["action_label"] = act["label"] if act else ""
        w["action_key"] = act["key"] if act else ""
    return alerts


def _alert_notice_lines(w, now, focused, width):
    """一条 alert 的底部行。选中且动作已登记时，下一行写出按键。"""
    ago = fmt_countdown(now - float(w.get("ts") or 0))
    line = _clip(f"🔔 {_clean(w.get('title'))}：{_clean(w.get('text'))}（{ago} 前）", width)
    out = [line]
    if focused and w.get("action_label"):
        key = w.get("action_key") or ""
        extra = f" / {key}" if key else ""
        out.append(_clip(f"   按 Enter{extra} {_clean(w.get('action_label'))}", width))
    return out


def status_notice_lines(items, width):
    """底部状态块。最多 3 条、每条 4 行，每行裁到 width 列，不换行。"""
    width = max(1, int(width or 1))
    out = []
    for item in (items or [])[:NOTICE_STATUS_MAX]:
        out.append(_clip(f"📋 {_as_text(item.get('title'))}", width))
        shown = 0
        for line in item.get("lines") or []:
            if shown >= NOTICE_STATUS_LINES:
                break
            if not isinstance(line, str):
                continue
            out.append(_clip("   " + _clean(line), width))
            shown += 1
    return out


# ── Slack 提醒 ────────────────────────────────────────────────────────────────
#
# --watch 看到有事在等你、而且已经等够久，才用 Incoming Webhook 发一条。
# 只写谁在等、等的是哪一类。问题、选项、命令、路径、计划标题、提醒正文都不出门。
# marker 在 SLACK_SENT_DIR：空文件是在发或没发成（mtime 是上次尝试），写了 sent 才算发过。
# 几个 watch 靠这个文件认，不会发重。失败或发到一半退出，过 SLACK_RETRY 由先到的再试。

_SLACK_PRUNE_AT = 0.0  # 上次清理 marker 的时间；一小时最多一次


def _slack_escape(text):
    """Slack 把 & < > 当标记。先换成实体。"""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def slack_text(w, now):
    """会话：谁的哪一类。外部提醒：只有标题。已等时长用 fmt_countdown。"""
    host = socket.gethostname().split(".")[0]
    ago  = fmt_countdown(now - w["ts"])
    if w.get("notice"):
        title = _clean(w.get("title")) or "外部提醒"
        text  = f"🔔 [{host}] {title}（已等 {ago}）"
    else:
        tool  = w.get("tool") or ""
        extra = f"（{tool}）" if tool and tool not in DECIDE_KINDS else ""
        text  = f"🔔 [{host}] {_attention_name(w)} 在等你{w['kind']}{extra}（已等 {ago}）"
    return _slack_escape(_clean(text))


def _slack_key(w):
    """同一条等待的稳定 id。src / sid / ts / rid 没变就是同一条。"""
    blob = json.dumps([w.get("src"), w.get("sid"), w.get("ts"), w.get("rid")])
    return hashlib.sha1(blob.encode()).hexdigest()[:24]


def slack_post(url, text):
    """发一条。Slack 成功回纯文本 ok，所以 http_post 是 (200, None)。返回 (成了没, 失败原因)。"""
    status, body = http_post(url, headers={"Content-Type": "application/json"}, body={"text": text})
    if status == 200:
        return True, ""
    err = str(body.get("_transport_error") or "") if isinstance(body, dict) else ""
    return False, err or f"HTTP {status}"


def notify_slack(items, url, delay, now=None):
    """等够 delay 秒的每条最多发一次；HTTP 放进守护线程，不挡住 watch。任何错都不抛出。"""
    global _SLACK_PRUNE_AT
    try:
        if not url:
            return
        now = time.time() if now is None else now
        for w in items or []:
            try:
                if now - float(w["ts"]) < delay:
                    continue
                key = _slack_key(w)
                text = slack_text(w, now)
                SLACK_SENT_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = SLACK_SENT_DIR / key
                try:
                    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    os.close(fd)
                except FileExistsError:
                    try:
                        st = path.stat()
                    except OSError:
                        continue
                    if st.st_size > 0:   # 已经发出去了
                        if now - st.st_mtime > 3600:   # 还活着就续上 mtime，免得清掉后又发
                            try:
                                os.utime(path)
                            except OSError:
                                pass
                        continue
                    if now - st.st_mtime < SLACK_RETRY:
                        continue   # 正在发，或失败后还在冷却。SLACK_RETRY 远大于 HTTP_TIMEOUT
                    # 空着且过了冷却：上次没成，或发到一半进程没了。按这一代 mtime 抢 O_EXCL 锁。
                    try:
                        fd = os.open(SLACK_SENT_DIR / f"{key}.{st.st_mtime_ns}.claim",
                                     os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                    except (FileExistsError, OSError):
                        continue   # 同一代只有一个赢家；claim 留着，清理按 mtime 删
                    os.close(fd)
                    try:
                        path.unlink()
                    except FileNotFoundError:
                        pass
                    try:
                        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                        os.close(fd)
                    except FileExistsError:
                        continue

                def send(url=url, text=text, path=path):
                    try:
                        ok, _err = slack_post(url, text)
                    except Exception:
                        ok = False
                    if ok:
                        try:
                            _write_atomic(path, "sent", mode=0o600)
                        except OSError:
                            pass
                        return
                    try:
                        os.utime(path)   # 留着空文件；冷却从现在起，别的进程和重启都认
                    except OSError:
                        pass

                threading.Thread(target=send, daemon=True).start()
            except Exception:
                continue
        if now - _SLACK_PRUNE_AT >= 3600:   # 比提醒寿命还久一小时的 marker 和 *.claim 可以丢。
            # 放在上面那轮之后：还活着的提醒先续上 mtime，watch 停了很久再开也不会被清掉重发
            _SLACK_PRUNE_AT = now
            try:
                cutoff = now - (ATTENTION_MAX_AGE + 3600)
                for name in os.listdir(SLACK_SENT_DIR):
                    path = SLACK_SENT_DIR / name
                    try:
                        if path.stat().st_mtime < cutoff:
                            path.unlink()
                    except OSError:
                        pass
            except OSError:
                pass
    except Exception:
        return


# ── 会话页（进行中的会话和它派出的子 agent）──────────────────────────────────
#
# 只读本机：Claude Code 的会话记录和子 agent meta、~/.claude/sessions/<pid>.json 注册表、
# 一次 ps 进程树，加上 Grok 的 active_sessions.json。不挂 hook、不联网。
# 注册表目录不存在或读不了时，Claude 的判定和以前一样（只看 transcript mtime）。
# --watch 里按 t 切到这一页，画成进程表；--sessions 画一帧就退出。
# SEE_BALANCE_SESSIONS 按 cwd 子串过滤，没设就全部显示。用量页右下角的宠物不在这里画。

SESSIONS_SCAN_SEC    = 2             # 会话页隔这么久才重新扫一遍磁盘；每次扫描最多一次 ps
SESSIONS_SESSION_SEC = 15 * 60       # 会话记录这么久没写过、又不在等你、注册表也对不上，就不算进行中
SESSIONS_SUB_SEC     = 10 * 60       # 子 agent 的记录超过这么久没写，算它停了
SESSIONS_DONE_SEC    = 2 * 60        # 做完的子 agent 再多留这么久，显示 ✓ 完成
SESSIONS_TAIL        = 64 * 1024     # 只看记录末尾这一段，判断正在干什么
SESSIONS_TAIL_BIG    = 1024 * 1024   # 最后一条比 64KB 还长、没有完整记录时，放宽再读一次
SESSIONS_PS_TIMEOUT  = 2             # ps 超过这么久，或失败，就跳过进程树这一截增强
SESSIONS_START_SLACK = 5             # procStart 和 etime 反推的启动时间，差几秒仍算同一个进程
SESSIONS_BASH_KEEP   = 20            # 末尾窗口里留多少条 Bash（会话自己和子 agent 都用），最近的在前；再多不留，避免把整段记录都搁在内存里
SESSIONS_BASH_CHUNK    = 4 << 20     # 对不上时从末尾往前翻，一块读 4MB
SESSIONS_BASH_OVERLAP  = 64 << 10    # 相邻两块重叠 64KB，避免一条记录被切开
SESSIONS_BASH_CAP      = 256 << 20   # 每个文件最多往回翻 256MB
SESSIONS_BASH_BUDGET   = 32 << 20    # --watch 每一轮最多读 32MB
SESSIONS_BASH_ONCE     = 1 << 30     # --sessions 一次性最多读 1GB
SESSIONS_BASH_SKEW     = 120         # 记录早于 shell 启动 120 秒就停
SESSIONS_BASH_SCAN_SEC = 2           # --watch 每一轮最多翻 2 秒；出错保持「后台命令」

_ACTIVITY_CACHE = {}
_BASH_DESC_CACHE = {}  # (owner, shell pid, 启动秒) → {fi, pos, desc, head}；shell 不在了就删


def _claude_projects_dir():
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "projects")


def _claude_sessions_dir():
    """注册表目录。基目录和 _claude_projects_dir 一样，尊重 CLAUDE_CONFIG_DIR。"""
    base = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(base, "sessions")


def _parse_time(value):
    """ISO 时间或 unix 秒，解析不了返回 0。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not value:
        return 0.0
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError, OSError):
        return 0.0


def _pid_alive(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


ONE_LINE_MAX = 200   # 工具参数收成一行后按字符硬截到这么长；省略号不在这里加


def _one_line(text):
    """连续空白并成一个空格，再硬截到 ONE_LINE_MAX 个字符。不加「…」。

    截断可以正好落在两个词之间的那个空格上。省略号留到画表格的时候，
    按那一列剩下的宽度只加一次。
    """
    out, gap = [], False
    for ch in _clean(text):
        if ch.isspace():
            gap = bool(out)
            continue
        if gap:
            out.append(" ")
            gap = False
            if len(out) >= ONE_LINE_MAX:
                break
        out.append(ch)
        if len(out) >= ONE_LINE_MAX:
            break
    return "".join(out)


def _tool_summary(name, inp):
    """工具名加一小段参数：Bash: git push…、Read provider_balance.py、Grep <pattern>、Agent: 派出 …。"""
    name = _clean(name) or "工具"
    inp = inp if isinstance(inp, dict) else {}

    def field(*keys):
        for key in keys:
            val = inp.get(key)
            if isinstance(val, str) and val.strip():
                return val
        return ""

    if name == "Bash":
        cmd = _one_line(field("command", "description"))
        return f"Bash: {cmd}" if cmd else "Bash"
    if name in ("Read", "Write", "Edit", "NotebookEdit", "MultiEdit"):
        base = os.path.basename(field("file_path", "notebook_path", "path").rstrip("/"))
        return f"{name} {base}" if base else name
    if name == "Grep":
        pat = _one_line(field("pattern"))
        return f"Grep {pat}" if pat else "Grep"
    if name in ("Agent", "Task"):
        desc = _one_line(field("description", "prompt"))
        return f"Agent: 派出 {desc}" if desc else "Agent: 派出"
    if name == "Glob":
        pat = _one_line(field("pattern"))
        return f"Glob {pat}" if pat else "Glob"
    if name in ("WebSearch", "WebFetch"):
        extra = _one_line(field("query", "url"))
        return f"{name} {extra}" if extra else name
    extra = _one_line(field("command", "file_path", "pattern", "query", "url", "description", "path"))
    return f"{name}: {extra}" if extra else name


def _record_bashes(rec):
    """这条记录里的 Bash tool_use，按出现顺序。每条是 (命令, description)。

    description 没有、或不是字符串时，第二项是 ""。没有 Bash 返回 []。
    """
    if not isinstance(rec, dict):
        return []
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return []
    content = msg.get("content")
    if not isinstance(content, list):
        return []
    found = []
    for item in content:
        if not isinstance(item, dict) or item.get("type") != "tool_use":
            continue
        if _clean(item.get("name")) != "Bash":
            continue
        inp = item.get("input")
        if not isinstance(inp, dict):
            continue
        cmd = inp.get("command")
        if isinstance(cmd, str) and cmd.strip():
            desc = inp.get("description")
            found.append((cmd, desc if isinstance(desc, str) else ""))
    return found


def _activity_from_record(rec):
    """一条 user / assistant 记录在干什么。不是这两种、或是空的，返回 None（接着往前看）。"""
    if not isinstance(rec, dict) or rec.get("type") not in ("assistant", "user"):
        return None
    msg = rec.get("message")
    if not isinstance(msg, dict):
        return None
    content, tool = msg.get("content"), None
    has_text = has_result = has_thinking = False
    if isinstance(content, str) and content.strip():
        has_text = True
    elif isinstance(content, list):
        for item in content:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if kind == "tool_use":
                tool = item
            elif kind == "tool_result":
                has_result = True
            elif kind == "text" and str(item.get("text") or "").strip():
                has_text = True
            elif kind == "thinking" and str(item.get("thinking") or item.get("text") or "").strip():
                has_thinking = True
    elif content:
        return None
    if not (has_text or has_result or has_thinking or tool or rec.get("type") == "user"):
        return None
    ts = _parse_time(rec.get("timestamp") or rec.get("ts"))
    if rec.get("type") == "user" or has_result:
        return {"state": "工作中", "doing": "思考中", "ts": ts}
    if tool:
        return {"state": "工作中", "doing": _tool_summary(tool.get("name"), tool.get("input")), "ts": ts}
    if has_text:
        return {"state": "空闲", "doing": "空闲", "ts": ts}
    if has_thinking:
        return {"state": "工作中", "doing": "思考中", "ts": ts}
    return None


_CWD_RE = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def _tail_activity(path):
    """jsonl 末尾最后一条有效记录在干什么。返回 {state, doing, ts, cwd, bash}。

    bash 是这段末尾窗口里的 Bash tool_use，最近的在前，最多 SESSIONS_BASH_KEEP 条。
    每条是 (命令, description)；没有 description 时第二项是 ""。
    子 agent 常常先起一条长命令，再继续发别的 Bash；只留最后一条，进程树上那个 shell 就挂不上。
    会话自己的窗口也留这份，⚙ 行的名字从这里取 description。
    按 (路径, 大小) 缓存，文件没变就不重读。最后一条比 64KB 还长时，放宽到 1MB 再试一次。"""
    try:
        size = os.stat(path).st_size
    except OSError:
        return {"state": "空闲", "doing": "空闲", "ts": 0.0, "cwd": "", "bash": []}
    hit = _ACTIVITY_CACHE.get(path)
    if hit and hit[0] == size:
        return hit[1]
    prev_cwd = (hit[1].get("cwd") or "") if hit else ""
    text, activity, cwd, bashes = "", None, "", []
    for window in (SESSIONS_TAIL, SESSIONS_TAIL_BIG):
        try:
            with open(path, "rb") as fh:
                fh.seek(max(0, size - window))
                raw = fh.read()
        except OSError:
            raw = b""
        text = raw.decode("utf-8", "replace")
        body = text
        if size > window:   # 窗口从半截记录切开，丢掉第一行残段
            nl = body.find("\n")
            body = body[nl + 1:] if nl >= 0 else ""
        activity, cwd, parsed, bashes = None, "", False, []
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict):
                continue
            parsed = True
            if isinstance(rec.get("cwd"), str) and rec["cwd"]:
                cwd = rec["cwd"]
            cmds = _record_bashes(rec)
            if cmds:
                bashes.extend(cmds)
                extra = len(bashes) - SESSIONS_BASH_KEEP
                if extra > 0:   # 只留窗口里最近的若干条，前面的丢掉
                    del bashes[:extra]
            got = _activity_from_record(rec)
            if got:
                activity = got
        if parsed or size <= window:
            break
    if not activity:
        # 放宽到 1MB 仍没有一条完整记录：最后一条还在写，别显示成空闲
        busy = not parsed and size > SESSIONS_TAIL
        activity = {"state": "工作中" if busy else "空闲",
                    "doing": "思考中" if busy else "空闲", "ts": 0.0}
    if not cwd:
        found = _CWD_RE.findall(text)
        if found:
            try:   # 分组拿到的是 JSON 字符串内容，引号和反斜杠要还原
                got = json.loads('"' + found[-1] + '"')
            except ValueError:
                got = found[-1]
            if isinstance(got, str) and got:
                cwd = got
        cwd = cwd or prev_cwd
    activity["cwd"] = cwd
    activity["bash"] = list(reversed(bashes))   # 最近的在前；每条是 (命令, description)
    if not activity.get("ts"):
        try:
            activity["ts"] = os.stat(path).st_mtime
        except OSError:
            activity["ts"] = 0.0
    _ACTIVITY_CACHE[path] = (size, activity)
    return activity


def _safe_scandir(path):
    try:
        return list(os.scandir(path))
    except OSError:
        return []


def _subagent_tree(session_dir, now):
    """一个会话目录下还在的子 agent，按 parentAgentId 挂成树。没有目录返回 []。"""
    if not session_dir:
        return []
    subdir = os.path.join(session_dir, "subagents")
    fresh = {}
    for ent in _safe_scandir(subdir):
        name = ent.name
        if not name.startswith("agent-") or not name.endswith(".jsonl") or name.endswith(".meta.json"):
            continue
        try:
            st = ent.stat()
        except OSError:
            continue
        if now - st.st_mtime > SESSIONS_SUB_SEC + SESSIONS_DONE_SEC:   # 活着的和刚做完的都更近
            continue
        fresh[name[len("agent-"):-len(".jsonl")]] = (ent.path, st)
    nodes = {}
    for aid, (path, st) in fresh.items():
        meta_path = os.path.join(subdir, f"agent-{aid}.meta.json")
        meta, meta_mtime = {}, 0.0
        try:
            meta_mtime = os.stat(meta_path).st_mtime
            with open(meta_path, encoding="utf-8") as fh:
                loaded = json.load(fh)
            if isinstance(loaded, dict):
                meta = loaded
        except (OSError, ValueError):
            meta = {}
        bash = ""
        if meta.get("finished"):
            done_at = max(meta_mtime, st.st_mtime)
            if now - done_at > SESSIONS_DONE_SEC:
                continue
            state, doing, ts = "完成", "✓ 完成", done_at
        else:
            if now - st.st_mtime > SESSIONS_SUB_SEC:
                continue
            act = _tail_activity(path)
            state, doing, ts = act["state"], act["doing"], act["ts"] or st.st_mtime
            bash = act.get("bash") or ""
        atype = _clean(meta.get("agentType")) or "agent"
        desc = _clean(meta.get("description"))
        if len(desc) > 80:   # 省略号留给表格那一列，这里只硬截
            desc = desc[:80]
        parent = str(meta.get("parentAgentId") or "")
        if parent.startswith("agent-"):
            parent = parent[len("agent-"):]
        nodes[aid] = {"aid": aid, "name": f"{atype}「{desc}」" if desc else atype,
                      "state": state, "doing": doing, "ts": ts, "bash": bash,
                      "children": [], "_parent": parent}
    parent_of = {aid: node.get("_parent") or "" for aid, node in nodes.items()}

    def _ancestors(aid):
        seen, cur = set(), parent_of.get(aid) or ""
        while cur and cur not in seen:
            seen.add(cur)
            cur = parent_of.get(cur) or ""
        return seen

    roots = []
    for aid, node in nodes.items():
        parent = node.pop("_parent", "")
        host = nodes.get(parent)
        if host is not None and host is not node and aid not in _ancestors(parent):
            host["children"].append(node)
        else:
            roots.append(node)

    def _sort_nodes(level):
        level.sort(key=lambda n: (-float(n.get("ts") or 0), n.get("aid") or ""))
        for node in level:
            _sort_nodes(node["children"])

    _sort_nodes(roots)
    return roots


def _ended_sids(now):
    """按日志顺序：SessionEnd 是该 (src, sid) 的最后一条才算结束，返回 {key: end_ts}。
    resume / --continue 会复用 sid，之后任何事件（含 sub）都让它复活。
    子 agent 自己的 SessionEnd 不算这个会话结束。"""
    ended = {}
    for line in _attention_lines(now):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or not rec.get("sid"):
            continue
        key = (rec.get("src") or "claude", rec["sid"])
        if rec.get("ev") == "SessionEnd" and not rec.get("sub"):
            try:
                ended[key] = float(rec.get("ts") or 0)
            except (TypeError, ValueError):
                ended[key] = 0.0
        else:
            ended.pop(key, None)   # 后面又有事件：会话还活着
    return ended


def _wait_doing(w):
    """等你<kind>，再接上决策内容的第一行（和底部提醒同一份）。"""
    kind = _clean(w.get("kind"))
    head = f"等你{kind}" if kind else "等你"
    detail = attention_detail(w)
    if detail:
        return f"{head}：{_clean(detail[0])}"
    if w.get("msg"):
        return f"{head}：{_clean(w['msg'])}"
    return head


def _stamp_wait(node, w):
    node["state"] = "等你"
    node["doing"] = _wait_doing(w)
    if w.get("ts"):
        node["ts"] = float(w["ts"])


def _walk_nodes(nodes):
    for node in nodes or []:
        yield node
        yield from _walk_nodes(node.get("children"))


def _find_wait_node(nodes, w):
    """子 agent 弹的决策框：transcript 文件名对得上就挂到那个子 agent 上。"""
    base = os.path.basename(w.get("transcript") or "")
    if not (base.startswith("agent-") and base.endswith(".jsonl")):
        return None
    aid = base[len("agent-"):-len(".jsonl")]
    return next((node for node in _walk_nodes(nodes) if node.get("aid") == aid), None)


def _apply_wait(session, w):
    if not w:
        return
    if w.get("sub"):
        hit = _find_wait_node(session.get("children"), w)
        if hit:
            _stamp_wait(hit, w)
    _stamp_wait(session, w)   # 子 agent 在等你时，这一会话也标成等你，排序才排到前面


# 注册表文件名是 <pid>.json。同目录的 <pid>.<hash>.key 不是会话，跳过。
_REG_FILE_RE = re.compile(r"^(\d+)\.json$")
# Bash 工具的 shell：source …/shell-snapshots/… && eval '<命令>'
_TOOL_SHELL_RE = re.compile(r"\bsource\b.*shell-snapshots/.*\beval\s+'", re.DOTALL)
# ps 把不可见字符印成 \NNN（三位八进制）。换行是 \012，制表是 \011。
_PS_OCTAL_RE = re.compile(r"\\([0-7]{3})")
# 命令本身是 grok / codex（可带路径、sudo、前面的 VAR=value）。不要匹配 codex-review 这种词。
# 换行也算一条新命令的开头：ps 的 \012 还原之后，命令会跨行。
_EXT_CMD_RE = re.compile(
    r"(?:^|&&|\|\||[;|&`(\n])\s*"
    r"(?:[A-Za-z_][A-Za-z0-9_]*=\S*\s+)*"
    r"(?:sudo\s+)?"
    r"(?:\S*/)?"
    r"(grok|codex)"
    r"(?=$|[\s;|&<>)`])",
    re.IGNORECASE,
)
_PROC_START_RE = re.compile(
    r"^(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)\s+"
    r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+"
    r"(\d{1,2})\s+(\d{2}):(\d{2}):(\d{2})\s+(\d{4})$"
)
_MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
def _epoch_ms(value):
    """unix 秒或毫秒。解析不了返回 0。注册表的 startedAt 是毫秒。"""
    if isinstance(value, bool):
        return 0.0
    try:
        n = float(value)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    if n > 1e12:
        n /= 1000.0
    return n


def _parse_proc_start(text):
    """注册表 procStart，例如 'Fri Oct 16 06:06:48 2026'。这是 UTC，失败返回 None。

    本机注册表里它和 startedAt 的 UTC 时钟一致；ps 的 etime 反推的也是真实启动时刻。
    按本地时区解析会整段对不上，活会话会被当成 pid 复用。
    不用 strptime 的 %a / %b：那两个跟 locale 走，中文环境下对不上英文缩写。
    """
    if not text:
        return None
    matched = _PROC_START_RE.match(str(text).strip())
    if not matched:
        return None
    try:
        return datetime(int(matched.group(6)), _MONTHS[matched.group(1)], int(matched.group(2)),
                        int(matched.group(3)), int(matched.group(4)), int(matched.group(5)),
                        tzinfo=timezone.utc).timestamp()
    except (ValueError, OSError, OverflowError):
        return None


def _parse_etime(text):
    """ps 的 etime（[[dd-]hh:]mm:ss）换成秒。解析不了返回 None。"""
    text = str(text or "").strip()
    if not text:
        return None
    days = 0
    if "-" in text:
        day_s, _, rest = text.partition("-")
        if not day_s.isdigit() or not rest:
            return None
        days = int(day_s)
        text = rest
    parts = text.split(":")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        return None
    nums = [int(part) for part in parts]
    if len(nums) == 2:
        hours, mins, secs = 0, nums[0], nums[1]
    else:
        hours, mins, secs = nums
        if mins > 59:
            return None
    if secs > 59:
        return None
    return days * 86400 + hours * 3600 + mins * 60 + secs


def _parse_ps(text):
    """ps -axww -o pid=,ppid=,etime=,command= 的输出。pid → {pid, ppid, etime, cmd, kids}。"""
    by_pid = {}
    for line in str(text or "").splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        try:
            pid = int(parts[0])
            ppid = int(parts[1])
        except ValueError:
            continue
        if pid <= 0:
            continue
        by_pid[pid] = {"pid": pid, "ppid": ppid, "etime": _parse_etime(parts[2]),
                       "cmd": parts[3], "kids": []}
    for proc in by_pid.values():
        parent = by_pid.get(proc["ppid"])
        if parent is not None and parent is not proc:
            parent["kids"].append(proc)
    return by_pid


def _ps_snapshot():
    """一次 ps。失败返回 (None, None)，调用方跳过进程树增强，启动时间改为只看 pid。

    live_sessions 每次扫描只调这一次。-ww 让长命令不被截断。
    """
    try:
        proc = subprocess.run(
            ["ps", "-axww", "-o", "pid=,ppid=,etime=,command="],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=SESSIONS_PS_TIMEOUT,
        )
        taken = time.time()
    except (OSError, subprocess.TimeoutExpired):
        return None, None
    if proc.returncode != 0:
        return None, None
    text = (proc.stdout or b"").decode("utf-8", "replace")
    return _parse_ps(text), taken


def _walk_procs(proc, seen=None):
    """proc 的子孙。环（pid 互相指）到此为止，不把 proc 自己算进去。"""
    if seen is None:
        seen = set()
        root = proc.get("pid")
        if root is not None:
            seen.add(root)
    for kid in proc.get("kids") or []:
        pid = kid.get("pid")
        if pid in seen:
            continue
        seen.add(pid)
        yield kid
        yield from _walk_procs(kid, seen)


def _is_zsh_bash(cmd):
    tok = (cmd or "").strip().split(None, 1)
    if not tok:
        return False
    base = os.path.basename(tok[0].lstrip("-")).lower()
    return base in ("zsh", "bash")


def _is_tool_shell(cmd):
    """Bash 工具的 shell：zsh/bash，并且是 source shell-snapshots + eval '…'。

    MCP server、常驻辅助进程、没有这层包装的 shell 都不是。
    """
    if not _is_zsh_bash(cmd):
        return False
    return _TOOL_SHELL_RE.search(cmd or "") is not None


def _unescape_ps_octal(text):
    """把 ps 印出来的 \\NNN 还原成字符。没有这种转义时原样返回。

    换行、制表留在文本里，规整和显示时再当成空白。不在这里收成空格，
    这样取出来的命令还能和 transcript 里带换行的 Bash 对上。
    """
    if not text or "\\" not in text:
        return text or ""
    return _PS_OCTAL_RE.sub(lambda matched: chr(int(matched.group(1), 8)), text)


def _extract_eval_command(command):
    """从 `source …/shell-snapshots/… && eval '<命令>'` 里取出命令。

    单引号有两种转义，都还原成一个单引号：'"'"'（Claude Code 现在的写法）和 '\\''。
    ps 把换行印成 \\012，这里还原成真正的换行。不是这种写法返回 ""。
    ps 把行截断、收尾引号丢了时，已经读到的那段仍然返回。
    """
    if not command or "shell-snapshots" not in command:
        return ""
    mark = "eval '"
    pos = command.find(mark)
    if pos < 0:
        return ""
    head = command[:pos]
    if "source" not in head or "shell-snapshots" not in head:
        return ""
    i = pos + len(mark)
    out, n = [], len(command)
    # '"'"' 是 5 个字符，'\'' 是 4 个。都从这一个单引号开始，要先认长的那种。
    quote_dq = "'\"'\"'"
    quote_bs = "'\\''"
    while i < n:
        if command.startswith(quote_dq, i):
            out.append("'")
            i += 5
            continue
        if command.startswith(quote_bs, i):
            out.append("'")
            i += 4
            continue
        if command[i] == "'":
            break
        out.append(command[i])
        i += 1
    return _unescape_ps_octal("".join(out)).strip()


def _norm_cmd(text):
    """空白收成单空格，便于前缀匹配。换行、制表也算空白。"""
    return " ".join(str(text or "").split())


def _cmd_prefix_match(shell_cmd, tool_cmd):
    """空白规整后，较短的那边是较长那边的前缀（边界是结尾或空格）才算同一条命令。"""
    a, b = _norm_cmd(shell_cmd), _norm_cmd(tool_cmd)
    if not a or not b:
        return False
    short, long = (a, b) if len(a) <= len(b) else (b, a)
    if not long.startswith(short):
        return False
    return len(short) == len(long) or long[len(short)] == " "


def _text_external(cmd):
    """命令文本里第一个像 grok / codex 可执行文件的词。没有返回 ""。"""
    matched = _EXT_CMD_RE.search(cmd or "")
    if not matched:
        return ""
    return "Grok" if matched.group(1).lower() == "grok" else "Codex"


def _proc_external(cmd):
    """进程的 argv0 是 grok 或 codex 就返回名字，否则 ""。"""
    tok = (cmd or "").strip().split(None, 1)
    if not tok:
        return ""
    base = os.path.basename(tok[0].lstrip("-")).strip("'\"").lower()
    if base == "grok":
        return "Grok"
    if base == "codex":
        return "Codex"
    return ""


def _external_kind(shell, raw):
    """eval 出来的命令，或这个 shell 的任一子孙进程，是 grok / codex 就返回 Grok / Codex。"""
    kind = _text_external(raw)
    if kind:
        return kind
    for proc in _walk_procs(shell):
        kind = _proc_external(proc.get("cmd") or "")
        if kind:
            return kind
    return ""


def _shell_row(shell, now, owner=0):
    """一条 Bash 工具 shell → 表格子行。取不出 eval 命令就返回 None（包装噪音不单列）。

    名字先写「后台命令」，或「外部 Grok」/「外部 Codex」。
    对得上 transcript 里的 description 时，_place_command_rows 再改这一列。
    「在做什么」仍是命令本身。
    """
    raw = _extract_eval_command(shell.get("cmd") or "")
    if not raw.strip():
        return None
    summary = _one_line(raw)
    if not summary:
        return None
    ext = _external_kind(shell, raw)
    if ext:
        name, state = f"外部 {ext}", "外部"
    else:
        name, state = "后台命令", "命令"
    etime = shell.get("etime")
    if isinstance(etime, (int, float)) and not isinstance(etime, bool) and etime >= 0:
        ts = now - float(etime)
    else:
        ts = now
    row = {"name": name, "state": state, "doing": summary, "ts": ts, "children": [], "_raw": raw}
    try:
        shell_pid = int(shell.get("pid"))
        owner_pid = int(owner)
        row["_shell_pid"] = shell_pid
        row["_owner"] = owner_pid
    except (TypeError, ValueError):
        pass
    return row


def _tool_shells(procs, pid, direct_only):
    """pid 下面的 Bash 工具 shell。

    Claude 只要直接的 zsh/bash 子进程。Grok 放宽到全部子孙，嵌套的只留最外层，
    避免用户命令自己再套一层同样的包装时列两次。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return []
    root = procs.get(pid)
    if not root:
        return []
    if direct_only:
        return [p for p in (root.get("kids") or []) if _is_tool_shell(p.get("cmd") or "")]
    matched = [p for p in _walk_procs(root) if _is_tool_shell(p.get("cmd") or "")]
    return _outermost_shells(matched, procs)


def _outermost_shells(shells, procs):
    keep = {p["pid"] for p in shells}
    out = []
    for proc in shells:
        parent = proc.get("ppid")
        seen, nested = set(), False
        while parent and parent not in seen:
            seen.add(parent)
            if parent in keep:
                nested = True
                break
            up = procs.get(parent)
            parent = up.get("ppid") if up else 0
        if not nested:
            out.append(proc)
    return out


def _command_rows(procs, pids, now, direct_only):
    """这些 pid 上的 ⚙ 行，跑得久的排在前面。procs 为空就没有。"""
    if not procs:
        return []
    rows, seen = [], set()
    for pid in pids:
        try:
            pid = int(pid)
        except (TypeError, ValueError):
            continue
        if pid in seen:
            continue
        seen.add(pid)
        for shell in _tool_shells(procs, pid, direct_only):
            row = _shell_row(shell, now, pid)
            if row:
                rows.append(row)
    rows.sort(key=lambda row: (float(row.get("ts") or 0), row.get("name") or ""))
    return rows


def _node_bash_entries(node):
    """末尾窗口里的 Bash：(命令, description)，最近的在前。

    旧数据是一条字符串，或命令字符串列表，那时没有 description。
    新数据是 (命令, description) 的列表。挂 shell 时两种都认。
    """
    raw = node.get("bash") if isinstance(node, dict) else None
    if isinstance(raw, str):
        return [(raw, "")] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, str):
            if item.strip():
                out.append((item, ""))
            continue
        if isinstance(item, (tuple, list)) and item:
            cmd = item[0]
            desc = item[1] if len(item) > 1 else ""
            if isinstance(cmd, str) and cmd.strip():
                out.append((cmd, desc if isinstance(desc, str) else ""))
    return out


def _node_bash_cmds(node):
    """这个子 agent 末尾窗口里的 Bash 命令。最近的在前。旧数据是一条字符串时也认。"""
    return [cmd for cmd, _desc in _node_bash_entries(node)]


def _bash_desc_on(node, shell_cmd):
    """这个节点里，和 shell 命令对得上、而且有 description 的最具体一条。

    一样长留先看到的：列表是最近的在前。没有返回 ("", -1)。
    description 先 _clean 再 _one_line，空的不算。
    """
    best, best_len = "", -1
    for cmd, desc in _node_bash_entries(node):
        text = _one_line(_clean(desc))
        if not text or not _cmd_prefix_match(shell_cmd, cmd):
            continue
        n = len(_norm_cmd(cmd))
        if n > best_len:
            best, best_len = text, n
    return best, best_len


def _shell_desc(session, shell_cmd):
    """会话自己的末尾窗口，和进行中的子 agent，里面对得上的 description。

    多个都像时留命令更长的那条；一样长就留树上更靠后的子 agent
    （和 _match_bash_host 同一条规矩）。没有返回 ""。
    """
    best, best_len = _bash_desc_on(session, shell_cmd)
    for node in _walk_nodes(session.get("children")):
        if not node.get("aid") or _node_kind(node) == "完成":
            continue
        text, n = _bash_desc_on(node, shell_cmd)
        if n > 0 and n >= best_len:
            best, best_len = text, n
    return best


def _name_shell(row, desc):
    """外部写成「外部 X · 说明」，其余直接用说明。"""
    if row.get("state") == "外部":
        name = row.get("name") or "外部"
        row["name"] = f"{name} · {desc}"
    else:
        row["name"] = desc


def _apply_shell_desc(session, row, raw):
    """对得上就用 description 当名字。外部行写成「外部 Grok · 说明」。对不上保持原名。"""
    desc = _shell_desc(session, raw)
    if desc:
        _name_shell(row, desc)


def _match_bash_host(nodes, shell_cmd):
    """进行中的子 agent 里，末尾窗口的某条 Bash 和 shell 命令对得上的那个。

    每个子 agent 只看留住的那几条（最近的在前），任一匹配就行。
    多个都像时，留对得上的命令里更长的（更具体）；一样长就留树上更靠后的。做完的不算。
    """
    best, best_len = None, -1
    for node in _walk_nodes(nodes):
        if not node.get("aid") or _node_kind(node) == "完成":
            continue
        matched = -1
        for tool in _node_bash_cmds(node):
            if not _cmd_prefix_match(shell_cmd, tool):
                continue
            n = len(_norm_cmd(tool))
            if n > matched:
                matched = n
        if matched > 0 and matched >= best_len:
            best, best_len = node, matched
    return best


def _place_command_rows(session, rows):
    """对得上子 agent 的挂到它下面，其余挂在会话下面。

    名字在挂上去之前定：对得上的 Bash 有 description 就用它，否则 ⚙ 命令行保持「后台命令」。
    """
    if not rows:
        return
    children = session.setdefault("children", [])
    for row in rows:
        raw = row.get("_raw") or ""
        _apply_shell_desc(session, row, raw)
        host = _match_bash_host(children, raw)
        if host is not None:
            host.setdefault("children", []).append(row)
        else:
            children.append(row)


def _attach_command_rows(session, pid, procs, now, direct_only):
    if not procs:
        return
    _place_command_rows(session, _command_rows(procs, [pid], now, direct_only))


def _load_claude_registry():
    """读 ~/.claude/sessions/<pid>.json。

    目录不存在或列不出来返回 None：调用方完全按 transcript mtime 的旧规则来。
    忽略 <pid>.<hash>.key。单个文件读坏就跳过。目录在、只是没有会话时返回 []。
    """
    root = _claude_sessions_dir()
    try:
        entries = list(os.scandir(root))
    except OSError:
        return None
    recs = []
    for ent in entries:
        matched = _REG_FILE_RE.match(ent.name)
        if not matched:
            continue
        try:
            if not ent.is_file():
                continue
            with open(ent.path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError, UnicodeError):
            continue
        if not isinstance(data, dict):
            continue
        sid = str(data.get("sessionId") or "").strip()
        if not sid:
            continue
        try:
            pid = int(data.get("pid"))
        except (TypeError, ValueError):
            try:
                pid = int(matched.group(1))
            except ValueError:
                continue
        if pid <= 0:
            continue
        recs.append({
            "pid": pid,
            "sessionId": sid,
            "cwd": data.get("cwd") if isinstance(data.get("cwd"), str) else "",
            "procStart": data.get("procStart") if isinstance(data.get("procStart"), str) else "",
            "status": data.get("status") if isinstance(data.get("status"), str) else "",
            "name": data.get("name") if isinstance(data.get("name"), str) else "",
            "startedAt": data.get("startedAt"),
            "statusUpdatedAt": data.get("statusUpdatedAt") or 0,
        })
    return recs


def _start_aligned(rec, procs, ps_at):
    """procStart 和 etime 反推的启动时间。

    True 对得上，False 明确不是同一个进程（pid 被复用），None 比不了。
    """
    if not procs or ps_at is None:
        return None
    try:
        pid = int(rec.get("pid"))
    except (TypeError, ValueError):
        return None
    proc = procs.get(pid)
    if not proc or proc.get("etime") is None:
        return None
    started = _parse_proc_start(rec.get("procStart"))
    if started is None:
        return None
    if abs((ps_at - proc["etime"]) - started) <= SESSIONS_START_SLACK:
        return True
    return False


def _registry_live(rec, procs, ps_at):
    """这条注册表还指着正在跑的那个进程吗。pid 不在了就不算；启动时间比不了就只看 pid。"""
    if not _pid_alive(rec.get("pid")):
        return False
    aligned = _start_aligned(rec, procs, ps_at)
    if aligned is None:
        return True
    return aligned


def _live_registry(recs, procs, ps_at):
    """sessionId → {rec, pids}。只留进程还对得上的。

    同一 sid 多条都活着时，状态用 statusUpdatedAt 更新的那条，pid 都留着，命令行一起算。
    """
    grouped = {}
    for rec in recs or []:
        if not _registry_live(rec, procs, ps_at):
            continue
        sid = rec["sessionId"]
        slot = grouped.get(sid)
        if slot is None:
            grouped[sid] = {"rec": rec, "pids": [rec["pid"]]}
            continue
        if rec["pid"] not in slot["pids"]:
            slot["pids"].append(rec["pid"])
        if _updated_at(rec) >= _updated_at(slot["rec"]):
            slot["rec"] = rec
    return grouped


def _updated_at(rec):
    try:
        return float(rec.get("statusUpdatedAt") or 0)
    except (TypeError, ValueError):
        return 0.0


def _apply_registry_status(session, slot, procs, now):
    """用注册表 status 盖住会话自己这一行，并把该 pid 的 Bash shell 挂成 ⚙ 行。

    busy → 运行，在做什么仍用 transcript 末尾的细节（有的话）。
    shell → 运行。有 ⚙ 行时在做什么写成「跑着 N 条命令」，N 是这个会话直接的那些行
    （还没分给子 agent 之前数好的）；一条都没有就写成「后台命令」。
    idle → 空闲（等你、等 N 个子 agent 之后仍会盖上来）。
    不认识的 status 不动 transcript 推出的状态，命令行还是照挂。
    """
    reg = slot["rec"]
    pids = slot.get("pids") or [reg.get("pid")]
    rows = _command_rows(procs, pids, now, direct_only=True) if procs else []
    status = str(reg.get("status") or "").strip()
    if status == "busy":
        session["reg"] = "busy"
        session["state"] = "工作中"
        if (session.get("doing") or "") in ("", "空闲"):
            session["doing"] = ""
    elif status == "shell":
        session["reg"] = "shell"
        session["state"] = "工作中"
        session["doing"] = f"跑着 {len(rows)} 条命令" if rows else "后台命令"
    elif status == "idle":
        session["reg"] = "idle"
        session["state"] = "空闲"
        session["doing"] = "空闲"
    _place_command_rows(session, rows)


def _make_claude_session(path, sid, session_dir, mtime, waiting, now, slot=None, procs=None):
    act = _tail_activity(path)
    reg = (slot or {}).get("rec") if slot else None
    cwd = act.get("cwd") or (waiting or {}).get("cwd") or ""
    if not cwd and reg:
        cwd = reg.get("cwd") or ""
    session = {"src": "claude", "sid": sid, "cwd": cwd,
               "title": session_title(path) or (waiting or {}).get("title") or "",
               "state": act["state"], "doing": act["doing"], "ts": act["ts"] or mtime,
               "bash": act.get("bash") or [],
               "children": _subagent_tree(session_dir, now), "_path": path or "", "_dir": session_dir or ""}
    if slot is not None:
        _apply_registry_status(session, slot, procs, now)
    _apply_wait(session, waiting)
    return session


def _session_from_wait(w, now):
    return {"src": w.get("src") or "claude", "sid": w["sid"], "cwd": w.get("cwd") or "",
            "title": w.get("title") or "", "state": "等你", "doing": _wait_doing(w),
            "ts": float(w.get("ts") or now), "children": []}


def _session_from_registry(slot, procs, waiting, now):
    """注册表说进程还在，但项目目录里没有这份 transcript。用注册表的 cwd / 名字顶上。"""
    reg = slot["rec"]
    ts = _parse_proc_start(reg.get("procStart")) or _epoch_ms(reg.get("startedAt")) or now
    session = {"src": "claude", "sid": reg["sessionId"], "cwd": reg.get("cwd") or (waiting or {}).get("cwd") or "",
               "title": (waiting or {}).get("title") or _clean(reg.get("name")) or "",
               "state": "空闲", "doing": "空闲", "ts": ts, "children": []}
    _apply_registry_status(session, slot, procs, now)
    _apply_wait(session, waiting)
    return session


def _claude_live(now, ended, waits, procs=None, ps_at=None, registry=None):
    """进行中的 Claude 会话。

    registry 是 None（目录读不了）时，判断和以前完全一样：transcript 15 分钟内写过，或正在等你。
    registry 是列表时，进程还对得上的会话不管 mtime 多旧都留着；对不上的退回 mtime 规则。
    """
    live = None if registry is None else _live_registry(registry, procs, ps_at)
    found = {}
    root = _claude_projects_dir()
    for proj in _safe_scandir(root):
        try:
            if not proj.is_dir():
                continue
        except OSError:
            continue
        entries = _safe_scandir(proj.path)
        dirs = {}
        for ent in entries:
            try:
                if ent.is_dir():
                    dirs[ent.name] = ent.path
            except OSError:
                continue
        for ent in entries:
            name = ent.name
            try:
                if not ent.is_file() or not name.endswith(".jsonl"):
                    continue
                mtime = ent.stat().st_mtime
            except OSError:
                continue
            sid = name[:-len(".jsonl")]
            end_ts = ended.get(("claude", sid))
            slot = live.get(sid) if live else None
            # 记录比 SessionEnd 晚 1 秒以上：resume / --continue 写过了，会话复活。
            # 注册表确认还是同一个进程时，不因为旧的 SessionEnd / 旧 mtime 把它丢掉。
            if not sid or (end_ts is not None and mtime <= end_ts + 1 and not slot):
                continue
            waiting = waits.get(("claude", sid))
            if slot is None and now - mtime > SESSIONS_SESSION_SEC and not waiting:
                continue
            found[sid] = _make_claude_session(ent.path, sid, dirs.get(sid), mtime, waiting, now,
                                              slot=slot, procs=procs)
    for key, w in waits.items():
        if key[0] != "claude" or key[1] in found:
            continue
        path = w.get("transcript") or ""
        end_ts = ended.get(key)
        slot = live.get(key[1]) if live else None
        if path and os.path.isfile(path):
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                mtime = now
            if end_ts is not None and mtime <= end_ts + 1 and not slot:
                continue
            parent = os.path.dirname(path)
            session_dir = os.path.join(parent, key[1])
            found[key[1]] = _make_claude_session(path, key[1], session_dir if os.path.isdir(session_dir) else None,
                                                mtime, w, now, slot=slot, procs=procs)
        elif end_ts is None or slot is not None:
            if slot is not None:
                found[key[1]] = _session_from_registry(slot, procs, w, now)
            else:
                found[key[1]] = _session_from_wait(w, now)
    if live:
        for sid, slot in live.items():
            if sid not in found:
                found[sid] = _session_from_registry(slot, procs, waits.get(("claude", sid)), now)
    return list(found.values())


def _grok_sessions_path():
    return os.path.expanduser("~/.grok/active_sessions.json")


def _grok_live(now, ended, waits, procs=None):
    path = _grok_sessions_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return []
    if not isinstance(data, list):
        return []
    out, seen = [], set()
    for item in data:
        if not isinstance(item, dict):
            continue
        sid = str(item.get("session_id") or "")
        if not sid or sid in seen or ("grok", sid) in ended or not _pid_alive(item.get("pid")):
            continue
        seen.add(sid)
        w = waits.get(("grok", sid))
        opened = _parse_time(item.get("opened_at"))
        session = {"src": "grok", "sid": sid, "cwd": item.get("cwd") or "", "title": (w or {}).get("title") or "",
                   "state": "空闲", "doing": "空闲", "ts": opened or now, "children": []}
        _apply_wait(session, w)
        # 和 Claude 一样尽力挂 ⚙ 行；Grok 的 shell 不一定是直接子进程，所以看整棵子孙
        _attach_command_rows(session, item.get("pid"), procs, now, direct_only=False)
        out.append(session)
    return out


def _bash_needle(cmd):
    """命令第一行（_norm_cmd 后）前 60 字的 JSON 转义。换行不并进去。"""
    first_line = str(cmd or "").split("\n", 1)[0]
    first = _norm_cmd(first_line)[:60]
    if not first:
        return b""
    try:
        escaped = json.dumps(first, ensure_ascii=False)[1:-1]
        return escaped.encode()
    except (TypeError, ValueError, UnicodeError):
        return b""


def _bash_on_line(line, cmd, not_before):
    """(说明, 过旧)。整行才解析；timestamp 早于 not_before 则过旧。"""
    try:
        rec = json.loads(line)
    except (ValueError, UnicodeError):
        return "", False
    if not isinstance(rec, dict):
        return "", False
    ts = _parse_time(rec.get("timestamp"))
    if ts and not_before and ts < not_before:
        return "", True
    for command, desc in _record_bashes(rec):
        text = _one_line(_clean(desc))
        if text and _cmd_prefix_match(cmd, command):
            return text, False
    return "", False


def _find_bash_desc(path, cmd, not_before, budget):
    """从末尾按块往前找。找到、翻完或块首记录过旧时 pos=0。整行在块内才解析，游标只往回。"""
    needle = _bash_needle(cmd)
    end = None
    if not needle:
        budget["pos"] = 0
        return ""
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            pos = budget.get("pos")
            if not isinstance(pos, int) or pos <= 0 or pos > size:
                end = size
            else:
                end = pos
            floor = max(0, size - SESSIONS_BASH_CAP)
            while end > floor and budget["left"] > 0 and time.monotonic() < budget["deadline"]:
                start = max(floor, end - SESSIONS_BASH_CHUNK)
                if end >= size:
                    read_end = end
                else:
                    read_end = min(size, end + SESSIONS_BASH_OVERLAP)
                if start >= end:
                    break
                want = read_end - start
                if want <= 0 or want > budget["left"]:
                    break
                fh.seek(start)
                buf = fh.read(want)
                budget["left"] -= len(buf)
                if len(buf) != want:
                    break
                limit = len(buf)
                while limit > 0:
                    hit = buf.rfind(needle, 0, limit)
                    if hit < 0 or hit >= limit:
                        break
                    line_start = buf.rfind(b"\n", 0, hit) + 1
                    line_end = buf.find(b"\n", hit)
                    limit = hit
                    # 这一行没完整落在块里：开头被截，或还没读到行尾
                    if (line_start == 0 and start) or (line_end < 0 and read_end < size):
                        continue
                    if line_end < 0:
                        line = buf[line_start:]
                    else:
                        line = buf[line_start:line_end]
                    desc, old = _bash_on_line(line, cmd, not_before)
                    if old or desc:
                        budget["pos"] = 0
                        return desc
                if start == 0:
                    off = 0
                else:
                    off = buf.find(b"\n") + 1
                if start == 0 or off:
                    newline = buf.find(b"\n", off)
                else:
                    newline = -1
                try:
                    if not_before and newline > off:
                        rec = json.loads(buf[off:newline])
                    else:
                        rec = None
                except (ValueError, UnicodeError):
                    rec = None
                if isinstance(rec, dict):
                    ts = _parse_time(rec.get("timestamp"))
                    if ts and ts < not_before:
                        budget["pos"] = 0
                        return ""
                end = start
            budget["pos"] = 0 if end <= floor else end
            return ""
    except (OSError, ValueError, UnicodeError):
        budget["pos"] = 0 if end is None else end
        return ""


def _bash_files(session, started):
    """mtime ≥ 启动−2s 的子 agent jsonl（含已完成，新的在前），然后是会话记录。"""
    root = session.get("_dir") or ""
    cut = float(started) - 2
    rows = []
    if root:
        entries = _safe_scandir(os.path.join(root, "subagents"))
    else:
        entries = ()
    for ent in entries:
        try:
            is_log = (
                ent.name.startswith("agent-")
                and ent.name.endswith(".jsonl")
                and ent.is_file()
            )
            mt = ent.stat().st_mtime if is_log else None
        except OSError:
            continue
        if mt is not None and mt >= cut:
            rows.append((-mt, ent.name, ent.path))
    paths = [path for _mtime, _name, path in sorted(rows)]
    own = [session["_path"]] if session.get("_path") else []
    return paths + own


def _shell_key(row):
    """(owner, shell pid, 启动秒)。差不超过 1 秒就沿用已有键。"""
    try:
        owner = int(row["_owner"])
        pid = int(row["_shell_pid"])
        started = int(float(row["ts"]))
    except (KeyError, TypeError, ValueError):
        return None
    if min(owner, pid, started) <= 0:
        return None
    for key in _BASH_DESC_CACHE:
        if key[:2] == (owner, pid) and abs(key[2] - started) <= 1:
            return key
    return (owner, pid, started)


def _backfill_shell_descs(sessions, limit):
    """过滤之后补「后台命令」和没说明的「外部 X」。Grok 不翻。出错保持原名。"""
    now = time.monotonic()
    if limit > SESSIONS_BASH_BUDGET:
        window = 1e9
    else:
        window = SESSIONS_BASH_SCAN_SEC
    deadline = now + window
    left = max(0, int(limit))
    seen = set()
    for session in sessions or []:
        try:
            if session.get("src") == "grok":
                continue
            for row in _walk_nodes(session.get("children")):
                key = _shell_key(row)
                raw = row.get("_raw") or ""
                name = row.get("name") or ""
                state = row.get("state")
                needs = state in ("命令", "外部") and (
                    name == "后台命令" or (state == "外部" and " · " not in name)
                )
                if not key or not needs or not raw:
                    continue
                seen.add(key)
                hit = _BASH_DESC_CACHE.get(key)
                if hit and hit.get("desc"):
                    _name_shell(row, hit["desc"])
                    continue
                if left <= 0 or time.monotonic() >= deadline:
                    continue
                started = float(row.get("ts") or key[2])
                files = _bash_files(session, started)
                head = files[0] if files else ""
                same = bool(hit) and hit.get("head") == head
                if same:
                    file_index = int(hit.get("fi") or 0)
                    cursor = hit.get("pos")
                    fi = file_index
                    pos = cursor
                    desc = ""
                else:
                    fi = 0
                    pos = None
                    desc = ""
                while fi < len(files) and left > 0 and time.monotonic() < deadline:
                    box = {"left": left, "deadline": deadline, "pos": pos}
                    file_path = files[fi]
                    not_before = started - SESSIONS_BASH_SKEW
                    desc = _find_bash_desc(file_path, raw, not_before, box)
                    left = box["left"]
                    if desc or box["pos"]:
                        pos = box["pos"]
                        break
                    fi = fi + 1
                    pos = None
                if desc:
                    _name_shell(row, desc)
                cached_pos = 0 if desc or fi >= len(files) else pos
                _BASH_DESC_CACHE[key] = {
                    "fi": fi,
                    "pos": cached_pos,
                    "desc": desc,
                    "head": head,
                }
        except Exception:
            pass
        finally:
            for obj in (session, *_walk_nodes(session.get("children"))):
                for field in ("_path", "_dir", "_raw", "_shell_pid", "_owner"):
                    obj.pop(field, None)
    stale = [key for key in _BASH_DESC_CACHE if key not in seen]
    for key in stale:
        _BASH_DESC_CACHE.pop(key, None)


def live_sessions(now=None, decided=None, bash_limit=None):
    """进行中的会话：[{src, sid, cwd, title, state, doing, ts, children}]。

    children 是子 agent：{aid, name, state, doing, ts, children}，parentAgentId 挂到对应的子 agent 下，
    没有就挂在会话下。⚙ 命令 / ⚙ 外部也在 children 里，没有 aid，不算子 agent。
    注册表在的 Claude 会话，status 决定自己这一行；不在注册表里的仍看 transcript。
    Grok 没有子 agent，状态只有空闲或等你；它的 shell 一样尽量挂成 ⚙ 行。
    decided 是刚在宠物上决定过的 rid，这些先不算在等你。
    SEE_BALANCE_SESSIONS 设了的话，cwd 不含其中任一子串的会话不返回（--watch 和 --sessions 都走这里）。
    """
    now = now or time.time()
    items = attention_items(now)
    if decided:   # 宠物上刚决定的，别等会话记录更新还显示「等你」
        items = [w for w in items if w.get("rid") not in decided]
    waits = {(w.get("src"), w["sid"]): w for w in items if w.get("sid")}
    ended = _ended_sids(now)
    registry = _load_claude_registry()   # None：目录读不了，Claude 走原来的 mtime 规则，这次也不为它调 ps
    # 有活着的注册表进程，或有 Grok 会话文件，才值得调那一次 ps。都没有就和以前一样不调。
    want_ps = os.path.isfile(_grok_sessions_path())
    if not want_ps and registry:
        want_ps = any(_pid_alive(rec.get("pid")) for rec in registry)
    procs, ps_at = _ps_snapshot() if want_ps else (None, None)
    sessions = (_claude_live(now, ended, waits, procs, ps_at, registry)
                + _grok_live(now, ended, waits, procs))
    parts = load_env_sessions()
    if parts:
        sessions = [s for s in sessions if _cwd_ok(s, parts)]
    if bash_limit is None:
        limit = SESSIONS_BASH_BUDGET
    else:
        limit = bash_limit
    _backfill_shell_descs(sessions, limit)
    return sessions


def show_sessions():
    """--sessions：按当前终端大小画一帧会话表，然后退出。不查余额。"""
    size = shutil.get_terminal_size(fallback=(80, 24))
    rows = max(1, size.lines - 1) if sys.stdout.isatty() else max(size.lines, 200)
    label = ",".join(load_env_sessions())
    lines = session_lines(
        live_sessions(bash_limit=SESSIONS_BASH_ONCE),
        max(1, size.columns),
        rows,
        filt=label or None,
    )
    print("\n".join(lines))


# ── Main ──────────────────────────────────────────────────────────────────────

def collect(update=True):
    data = {
        "ts":       int(time.time()),
        "deepseek": fetch_deepseek(),
        "codex":    fetch_codex(),
        "claude":   fetch_claude(),
        "agy":      fetch_agy(),
        "grok":     fetch_grok(),
        "opencode": fetch_opencode(),
    }
    if update:
        data["update"] = check_update(load_state().get("update"))
    return data

def main():
    args        = sys.argv[1:]
    watch_mode  = "--watch" in args
    json_mode   = "--json" in args
    compact     = "--compact" in args

    interval_min = 30
    if watch_mode:
        try:
            idx = args.index("--watch")
            if idx + 1 < len(args) and args[idx + 1].replace(".", "").isdigit():
                interval_min = int(float(args[idx + 1]))
        except: pass

    if "--pet-demo" in args:
        pet_demo()
        return
    if "--sessions" in args:
        show_sessions()
        return
    if "--install-hooks" in args or "--uninstall-hooks" in args:
        print("\n".join(install_hooks() if "--install-hooks" in args else uninstall_hooks()))
        return
    if "--version" in args:
        print(f"see-balance v{__version__}")
        return
    if "--slack-test" in args:
        url = load_slack_webhook()
        if not url:
            print("把 SLACK_WEBHOOK_URL=https://hooks.slack.com/... 写进 ~/.see-balance.env")
            sys.exit(1)
        host = socket.gethostname().split(".")[0]
        ok, err = slack_post(url, _slack_escape(_clean(f"🔔 [{host}] see-balance：Slack 提醒测试")))
        print("✓ 已发到 Slack" if ok else f"⚠ 发送失败：{err}")
        sys.exit(0 if ok else 1)
    if "--update" in args:
        upd  = check_update(load_state().get("update"), force=True)
        full = load_state()   # 查更新要几秒，期间别的终端可能写过 state：重新读，只改 update 段
        if full or not STATE_FILE.exists():   # 读不出来就别写回，免得把 history 等整份冲掉
            full["update"] = upd
            save_state(full)
        if upd.get("error"):
            print(f"⚠ 检查更新失败：{upd['error']}")
        if _from_git_checkout():
            print(update_hint(upd) or f"当前 v{__version__}，没发现更新的版本")
            sys.exit(1 if upd.get("error") else 0)
        ok, msg = apply_update()   # 这次没查成，之前暂存好的新版本照样能装
        if not ok and upd.get("error") and msg.startswith("已经是最新版"):
            msg = f"这次没查成，暂时没有可装的新版本（当前 v{_current_version()}）"
        print(("✓ " if ok else "") + msg)
        failed = msg.startswith("更新失败") or (not ok and upd.get("error"))
        sys.exit(1 if failed else 0)
    # --json / --compact 给脚本用：不联网查更新，输出也不多出 update 字段
    update_mode = ("--no-update" not in args and os.environ.get("SEE_BALANCE_UPDATE", "1").strip() != "0"
                   and not json_mode and not compact)

    # 宠物只跟完整视图走：--compact / --json / 管道输出保持原样，方便脚本解析
    pet_mode    = ("--no-pet" not in args and os.environ.get("SEE_BALANCE_PET", "1").strip() != "0"
                   and not compact and not json_mode)
    # 交互 watch：原地重绘 + 宠物动画 + 快捷键（p 宠物 / t 切换会话页 / r 刷新 / q 退出）
    # 只在前台进程启用：`--watch &` 这种后台任务改终端模式会被 SIGTTOU 挂起
    interactive = (watch_mode and not json_mode and termios is not None
                   and sys.stdout.isatty() and sys.stdin.isatty()
                   and os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp())

    if not json_mode and not compact:
        refresh_hooks()
    full0 = load_state()
    state = {"full": full0, "pet": PetView(species=(full0.get("pet") or {}).get("species", "cat")),
             "pet_visible": True, "size": None, "place": None,
             "attention_on": not json_mode and not compact and hooks_installed(),
             "focus": None, "qi": 0, "picks": {}, "pick_for": None, "decided": set(), "touched": 0.0,
             "tab": "usage", "paint_tab": None, "sessions": [], "sessions_at": 0.0, "sessions_filt": "",
             "work": [], "work_at": 0.0,
             "slack": ((load_slack_webhook(), load_slack_delay())
                       if watch_mode and not json_mode and not compact else ("", 0))}

    def update_prompt():
        """要不要弹升级选项：有下载好的新版本，这次没跳过，这个版本也没说过“不要提醒”。"""
        v = state.get("update_ver")
        return bool(v) and not state.get("update_skip") and v != state.get("update_muted")

    def refresh_attention():
        """重读“哪些会话在等你”，以及外部提醒。有变化返回 True。没装 hook 就不读会话日志。"""
        items = attention_items() if state["attention_on"] else []
        items = [w for w in items if w.get("rid") not in state["decided"]]   # 刚在宠物上决定的，别等会话记录更新再消失
        try:
            alerts, statuses = external_notices()
            actions = load_actions()
        except Exception:          # 外部提醒出任何错都不能影响 watch
            alerts, statuses, actions = [], [], {}
        state["actions"] = actions   # 命令变了也要换上，哪怕底部文字没变
        annotate_notice_actions(alerts, actions)
        items.extend(alerts)          # 会话在前（等得最久的排最前），然后才是 alert（按文件名）
        key = [(w.get("src"), w.get("sid"), w.get("kind"), w.get("ts"), w.get("what"), str(w.get("qs")),
                w.get("rid"), w.get("title"), w.get("text"), w.get("action"), w.get("action_label"),
                w.get("action_key")) for w in items]
        status_key = [(w.get("title"), tuple(w.get("lines") or ()), w.get("ts")) for w in statuses]
        if key == state.get("attention_key") and status_key == state.get("status_key"):
            return False
        state["attention"], state["attention_key"] = items, key
        state["statuses"], state["status_key"] = statuses, status_key
        return True

    def set_pet_mood():
        """优先级：会话在等你 > 升级选项 > 用量情绪（轮到说时）> 在干活的会话 > 其余。"""
        if state.get("attention"):   # 刚按过键：固定显示选中的那个会话，不轮播
            # 有能在宠物上决定的框：气泡固定显示 👉 选中的那个，数字键按的就是气泡上这个；都不能决定时才轮播
            w    = focused()
            pin  = time.time() - state["touched"] < 30 or any(decidable(x) for x in state["attention"])
            pick = state["attention"].index(w) if pin else None
            state["pet"].set_mood("call", attention_bubble(state["attention"], pick=pick))
            return
        if update_prompt():
            state["pet"].set_mood("update", f"新版本 v{state['update_ver']}！u 立即升级\nk 跳过本次 · n 不要提醒")
            return
        mood, now = state.get("usage_mood") or ("idle", None), time.time()
        if mood != state.get("usage_shown"):     # 用量情绪变了：从头开始算，先说一会儿
            state["usage_shown"], state["usage_since"] = mood, now
        age = now - state["usage_since"]
        if mood[0] != "idle" and age >= USAGE_MOOD_FIRST and (age - USAGE_MOOD_FIRST) % USAGE_MOOD_EVERY >= USAGE_MOOD_SHOW:
            mood = ("idle", None)                # 没轮到就做日常小动作
        if mood[0] == "idle" and state.get("work"):   # 用量情绪没轮到说时，轮播在干活的会话
            state["pet"].set_mood("work", work_bubble(state["work"]))
            return
        if mood[0] == "idle" and state.get("update_hint") and not state.get("can_update"):
            mood = ("encourage", "有新版本啦，看看底下～")
        state["pet"].set_mood(*mood)

    def reload_sessions():
        """重新扫一遍进行中的会话，并记下过滤词（标题用）。用量页不调用。"""
        state["sessions"] = live_sessions(decided=state["decided"])
        state["sessions_at"] = time.time()
        state["sessions_filt"] = ",".join(load_env_sessions())

    def paint():
        """整屏重画：正文 + footer，再把宠物画在右下角（放不下就在 footer 下提示）。
        会话页的正文是进程表，不再往角落叠一只。用量页每次清全屏；
        会话页连续重画不清屏，切页或终端变了大小才清一次。文本没变就不写。"""
        size     = shutil.get_terminal_size()
        tab      = state.get("tab") or "usage"
        tab_name = "会话" if tab == "sessions" else "用量"
        keys     = " · p 宠物 · s 换宠物" if pet_mode and tab != "sessions" else ""
        if state.get("update_msg"):
            note = state["update_msg"]
        elif state.get("can_update"):   # 宠物藏起来或放不下时，底下也能看到选项
            note = (f"🆕 新版本 v{state['update_ver']} 已下载好（当前 v{__version__}）："
                    "u 立即升级 · k 跳过本次 · n 不要提醒") if update_prompt() else None
        else:
            note = state.get("update_hint")
        sync_focus()
        width = max(1, size.columns - 1)
        calls = "\n".join(attention_lines(state.get("attention") or [], focus=focused(),
                                          qi=state["qi"], picks=state["picks"]))
        msg   = state.get("decide_msg")
        if msg and (time.time() - msg[1] < 15 or state.get("action_running")):
            calls = (calls + "\n" if calls else "") + msg[0]
        status = "\n".join(status_notice_lines(state.get("statuses") or [], width))
        if status:
            calls = (calls + "\n" if calls else "") + status
        state["attention_min"] = int(time.time() // 60)
        footer = f"🔄 每 {interval_min} 分钟刷新 · {tab_name} · t 切换{keys} · r 刷新 · q 退出"
        if tab == "sessions":
            # 标题行右侧已经有页签，不再另画 footer。末尾换行让光标停在下一行。
            extra = 1
            if calls:
                extra += calls.count("\n") + 2
            if note:
                extra += note.count("\n") + 2
            if state.get("busy"):
                extra += 1
            # 会话页不清屏重画，每行后面跟 \033[K。一行写满最后一列时，光标停在待换行，
            # \033[K 会把最后一个字擦掉。宽度只在上面定一次：表格交给 session_lines
            # （它保证每行 ≤ width，单元格只裁一次），底部的提醒、更新提示、正在刷新另收。
            body = "\n".join(session_lines(state.get("sessions") or [], width,
                                            max(1, size.lines - extra),
                                            filt=state.get("sessions_filt") or None))
            text = body
            if calls:
                text += "\n\n" + _clip_lines(calls, width)
            if note:
                text += "\n\n" + _clip_lines(note, width)
            text += "\n"
        else:
            body = state["body"]
            text = (body + (f"\n\n{calls}" if calls else "") + (f"\n\n{note}" if note else "")
                    + f"\n\n{footer}\n")
        place = None
        if tab != "sessions" and pet_mode and state["pet_visible"]:
            rows    = screen_rows(text, size.columns)   # 末尾空行就是光标所在行
            visible = rows[-size.lines:] + [0] * max(0, size.lines - len(rows))
            place   = pet_place(visible, size.columns, size.lines - PET_H - 1)   # 底下留 1 行给光标
            if not place:
                need  = max(visible) + 2 + PET_W + 1
                text += f"   （终端太窄，宠物先躲起来了：加宽到约 {need} 列可显示；p 隐藏宠物）\n"
        if state.get("busy") and not place:   # 宠物不在屏幕上，就用一行字提示正在刷新
            busy = "⏳ 正在刷新，稍等片刻…"
            if tab == "sessions":
                busy = _clip_lines(busy, width)
            text += busy + "\n"
        clear = tab != "sessions" or state.get("paint_tab") != "sessions" or state.get("size") != size
        # 没有帧动画：和上一帧一字不差就不写终端。时长跳了、扫描结果变了，文本自然不同
        if tab == "sessions" and not clear and text == state.get("frame_text"):
            return
        sys.stdout.write(_watch_frame(text, clear))
        sys.stdout.flush()
        state["size"], state["place"], state["paint_tab"] = size, place, tab
        state["frame_text"] = text if tab == "sessions" else None
        if place:
            pet_draw(state["pet"].frame(), *place)

    def show_update(upd):
        """按最新的检查结果设好升级提示（底部那行、u/k/n 选项）。提示变了返回 True。"""
        before = (state.get("update_hint"), state.get("update_ver"))
        hint = update_hint(upd, interactive=interactive) if update_mode else None
        state["update_hint"] = hint
        state["can_update"]  = bool(hint) and interactive and not _from_git_checkout()
        state["update_ver"]  = staged_update()[0] if state["can_update"] else None
        changed = (hint, state["update_ver"]) != before
        if changed:
            state["update_msg"] = None   # 提示换了，旧的“⚠ 更新失败”之类的话也就不用留着了
        return changed

    def poll_update(now):
        """watch 期间每分钟在后台问一次 check_update（到没到 10 分钟由它看 state 决定），不等余额刷新。"""
        box = state.get("update_box")
        if box is not None and "upd" in box:      # 后台查完了：存进 state，有新版本就弹出来
            state["update_box"] = None
            upd = box["upd"]
            if upd != box["before"]:              # 没到点、没真查的话什么都不写
                full = load_state()
                theirs = (full.get("update") or {}).get("checked_at") or 0
                # 读不出来就别写回（免得冲掉 history 等）；别的终端刚写了更新的检查结果也别盖掉
                if (full or not STATE_FILE.exists()) and theirs <= (upd.get("checked_at") or 0):
                    full["update"] = upd
                    save_state(full)
                    state["full"] = full
            if show_update(upd) and not state.get("busy"):
                set_pet_mood()
                paint()
            return
        if box is None and not state.get("busy") and now >= state.get("update_poll_at", 0):
            state["update_poll_at"] = now + 60
            box = state["update_box"] = {"before": load_state().get("update") or {}}
            def work():
                try:
                    box["upd"] = check_update(box["before"])
                except Exception:          # 查更新出任何错都不能影响 watch
                    box["upd"] = load_state().get("update") or {}
            threading.Thread(target=work, daemon=True).start()

    def focused():
        """选中的那一框：按过 Tab 选的那个会话，还在等的话；不然就是等得最久的那个。"""
        items = state.get("attention") or []
        return next((w for w in items if (w["src"], w["sid"]) == state["focus"]), items[0] if items else None)

    def sync_focus():
        """选中的换了一框（切了会话，或者同一个会话弹了新的框）：之前选到一半的选项作废。"""
        w   = focused()
        key = w and (w["src"], w["sid"], w["ts"])
        if key != state["pick_for"]:
            state["pick_for"], state["qi"], state["picks"] = key, 0, {}

    def submit(w, decision):
        """把决定写给正在等的 hook（它读到就交回会话），顺手清掉没人取的旧决定。"""
        try:
            DECISION_DIR.mkdir(parents=True, exist_ok=True)
            _write_atomic(DECISION_DIR / f"{w['rid']}.json", json.dumps(decision))
            state["decided"].add(w["rid"])
            msg = f"✓ 已把你的决定交给 {_session_label(w)} 那个会话"
        except OSError as e:
            msg = f"⚠ 没能交出决定（{e}），请回 {_session_label(w)} 那个会话里处理"
        try:
            for f in DECISION_DIR.glob("*.json"):
                if time.time() - f.stat().st_mtime > DECIDE_WAIT + 60:
                    f.unlink()
        except OSError:
            pass
        state["decide_msg"], state["pick_for"] = (msg, time.time()), None
        state["cooldown"] = time.time() + 1.5   # 交完别让连按 / 按住的数字落到下一个会话上
        refresh_attention()
        set_pet_mood()
        paint()

    def _notice_action_for_key(ch):
        """当前显示的 alert 里，绑了这个键的动作名。选中的那条优先。没有返回 ""。"""
        if not ch:
            return ""
        actions, found, current = state.get("actions") or {}, "", focused()
        for w in state.get("attention") or []:
            if not w.get("notice"):
                continue
            name = w.get("action") or ""
            act = actions.get(name)
            if not act or act.get("key") != ch:
                continue
            if w is current:
                return name
            if not found:
                found = name
        return found

    def _finish_notice_action():
        """后台动作跑完了：换上结果那句，并马上再读一遍提醒。"""
        if "action_result" not in state:
            return
        text = state.pop("action_result")
        state["action_running"] = None
        state["decide_msg"] = (_clip(_clean(text), _watch_width()), time.time())
        refresh_attention()
        if not state.get("busy"):   # 刷新余额时宠物停在“正在刷新”，别被这里改掉
            set_pet_mood()
        paint()

    def start_notice_action(name):
        """跑一个已登记的动作。没挂在正在显示的 alert 上、或上一个还没跑完，就什么都不做。"""
        if not name or state.get("action_running") or "action_result" in state:
            return False
        act = (state.get("actions") or {}).get(name)
        if not act:
            return False
        if not any(w.get("notice") and w.get("action") == name for w in (state.get("attention") or [])):
            return False
        state["action_running"] = name
        label, cmd, timeout = act["label"], list(act["cmd"]), act["timeout"]
        state["decide_msg"] = (_clip(f"⏳ 正在{label}…", _watch_width()), time.time())

        def work():
            try:
                proc = subprocess.run(cmd, timeout=timeout, stdin=subprocess.DEVNULL,
                                      capture_output=True, text=True, errors="replace")
            except subprocess.TimeoutExpired:
                text = _action_message(label, False, "timeout")
            except Exception as e:
                text = _action_message(label, False, _clip(_clean(str(e)), 200))
            else:
                if proc.returncode == 0:
                    text = _action_message(label, True, _clip(_clean(_output_line(proc.stdout)), 200))
                else:
                    detail = _output_line(proc.stderr) or _output_line(proc.stdout)
                    text = _action_message(label, False, _clip(_clean(detail), 200))
            state["action_result"] = text

        threading.Thread(target=work, daemon=True).start()
        paint()
        return True

    def decide_key(ch):
        """数字选选项 / 允许拒绝，回车提交多选，Tab 换下一个会话。交出了决定返回 True。
        外部 alert 不能用数字决定；选中它时回车触发它登记的动作。"""
        items = state.get("attention") or []
        if not items:
            return False
        if ch != "\t" and time.time() < state.get("cooldown", 0):
            # 冷却挡住决定键和回车，免得连按落到下一个会话，或误触发外部提醒
            return False
        sync_focus()
        w = focused()
        state["touched"] = time.time()
        if ch == "\t":
            nxt = items[(items.index(w) + 1) % len(items)]
            state["focus"] = (nxt["src"], nxt["sid"])
            set_pet_mood()
            paint()
            return False
        if w.get("notice"):
            if ch in ("\r", "\n"):
                start_notice_action(w.get("action"))
            return False   # 数字键对外部提醒不起作用
        if not decidable(w):
            return False
        if not w.get("qs"):
            if ch in ("1", "2"):
                submit(w, {"allow": ch == "1"})
                return True
            return False
        qi, q = state["qi"], w["qs"][state["qi"]]
        picks = state["picks"].setdefault(qi, [])
        if ch.isdigit():
            k = int(ch) - 1
            if k >= len(q.get("opts") or []):
                return False
            if q.get("multi"):   # 多选：按一下选上，再按一下取消，选好按回车
                picks.remove(k) if k in picks else picks.append(k)
                paint()
                return False
            picks[:] = [k]
        elif not (q.get("multi") and picks):
            return False
        if qi + 1 < len(w["qs"]):   # 还有下一道题
            state["qi"] = qi + 1
            paint()
            return False
        submit(w, {"allow": True, "picks": [sorted(state["picks"].get(j, [])) for j in range(len(w["qs"]))]})
        return True

    def pump(timeout):
        """推进一帧宠物动画，再等按键最多 timeout 秒。返回 "quit" / "refresh" / None。"""
        now = time.time()
        if "action_result" in state:   # 动作刚跑完：马上换结果，并重读提醒
            _finish_notice_action()
            now = time.time()
        if update_mode:
            poll_update(now)
        if state["attention_on"] and now - state.get("alive_at", 0) >= 1:
            state["alive_at"] = now   # 告诉 hook 宠物开着，决策框可以等宠物上的决定（刷新余额时也照常摸）
            try:
                PET_ALIVE.touch()
            except OSError:
                pass
        msg = state.get("decide_msg")
        if (msg and now - msg[1] >= 15 and not state.get("busy")
                and not state.get("action_running")):   # 动作还在跑时，“正在…”一直留着
            state["decide_msg"] = None
            paint()
        if now - state.get("attention_at", 0) >= 1 and not state.get("busy"):
            state["attention_at"] = now   # 会话提醒和外部提醒都在这一拍读，不等下一次刷新
            if refresh_attention():
                set_pet_mood()
                paint()
            elif state.get("attention") and int(now // 60) != state.get("attention_min"):
                paint()               # 底部“等了多久”按分钟走
            elif len(state.get("attention") or []) > 1:
                set_pet_mood()        # 气泡轮到下一个（没到点就不变）
            if state["slack"][0]:    # 刚在宠物上决定的已经从 attention 里拿掉了
                notify_slack(state.get("attention") or [], *state["slack"])
        if not state.get("attention") and not state.get("busy") and now - state.get("mood_at", 0) >= 1:
            state["mood_at"] = now    # 用量提醒该说或该停了（没到点就不变）
            set_pet_mood()
        if (state.get("tab") or "usage") == "sessions":
            if now - state.get("sessions_at", 0) >= SESSIONS_SCAN_SEC:   # 扫描放这里：用量页不碰这些文件
                reload_sessions()
            paint()          # 文本没变时 paint 自己会跳过写入
        elif pet_mode and state["pet_visible"]:
            if not state.get("busy") and now - state.get("work_at", 0) >= PET_WORK_SCAN_SEC:
                state["work_at"] = now   # 用量页也扫会话，宠物才知道谁在干活；会话页自己有 reload_sessions
                # bash_limit=0：不往回翻 transcript 找命令说明（只用缓存），免得卡住动画
                try:
                    state["work"] = work_sessions(live_sessions(decided=state["decided"], bash_limit=0))
                except Exception:     # 扫会话出任何错都不能影响 watch
                    state["work"] = []
                if not state.get("attention"):
                    set_pet_mood()
            if shutil.get_terminal_size() != state["size"]:
                paint()      # 终端尺寸变了：整屏重排
            elif state["place"]:
                pet_draw(state["pet"].frame(), *state["place"])
        fd = sys.stdin.fileno()
        ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
        if not ready:
            return None
        keys = os.read(fd, 64).decode(errors="ignore")
        if "\033" in keys:   # 方向键 / F 键等转义序列（F1 = ESC O P）整段忽略，免得误触 p
            return None
        for ch in keys.lower():
            if ch in "123456789\t\r\n":
                if state.get("attention") and decide_key(ch):
                    break             # 一次只交一个决定：同一批按键里剩下的不再往下个会话落
                continue
            if ch == "q":
                return "quit"
            if ch == "u" and state.get("can_update"):
                ok, msg = apply_update()
                if ok:   # 退出 watch、恢复终端后用新版本重启
                    state["restart"] = msg
                    return "quit"
                state["update_msg"] = f"⚠ {msg}"
                paint()
            if ch in ("k", "n") and update_prompt():
                if ch == "k":   # 跳过本次：这次刷新结束前不再提，下次刷新再提醒
                    state["update_skip"] = True
                    state["update_skip_until"] = state.get("refresh_no", 0) + (2 if state.get("busy") else 1)
                else:           # 不要提醒：这个版本不再提，出了更新的版本或重启后再提
                    state["update_muted"] = state["update_ver"]
                if not state.get("busy"):   # 刷新中宠物在显示“正在刷新”，刷完会按最新状态重设
                    set_pet_mood()
                paint()
            if ch == "r" and not state.get("busy"):
                return "refresh"
            if ch == "t":
                state["tab"] = "usage" if state.get("tab") == "sessions" else "sessions"
                if state["tab"] == "sessions":
                    reload_sessions()
                paint()
            if ch == "p" and pet_mode:
                state["pet_visible"] = not state["pet_visible"]
                paint()
            if ch == "s" and pet_mode and state["pet_visible"]:
                # 换下一种宠物，并记进 state.json，下次启动沿用
                full = state["full"]
                full["pet"] = dict(full.get("pet") or {}, species=state["pet"].next_species())
                save_state(full)
                if state["place"]:
                    pet_draw(state["pet"].frame(), *state["place"])
            if "a" <= ch <= "z":   # 占用键在 load_actions 里已经摘掉，这里不会撞上 q/u/k/n/r/t/p/s
                start_notice_action(_notice_action_for_key(ch))
        return None

    def wait_interactive(seconds):
        """等到下次刷新，期间推进宠物动画、处理按键。返回 False 表示要退出。"""
        deadline = time.time() + seconds
        while True:
            left = deadline - time.time()
            if left <= 0:
                return True
            act = pump(min(PET_TICK_SEC, left))
            if act:
                return act == "refresh"

    def refresh_interactive():
        """后台线程拉数据，主线程照常跑动画、响应按键；拉完再渲染。返回 False 表示要退出。"""
        box = {}
        def work():
            try:
                box["data"] = collect(update_mode)
            except BaseException as e:   # 交给主线程抛出，和原来同步取数时一样
                box["err"] = e
        worker = threading.Thread(target=work, daemon=True)   # 中途按 q 直接退出，不等它
        worker.start()
        state["busy"] = True
        state["pet"].set_mood("busy", "稍等片刻，正在刷新…")
        paint()
        try:
            while worker.is_alive():
                if pump(PET_TICK_SEC) == "quit":
                    return False
        finally:
            state["busy"] = False
        if "err" in box:
            raise box["err"]
        state["full"] = load_state()
        do_query(box["data"])
        return True

    def do_query(data=None):
        full      = state["full"]
        prev_data = {k: full[k] for k in ("deepseek", "codex", "claude", "agy", "grok", "opencode") if k in full}
        data      = data if data is not None else collect(update_mode)

        # ── History（实测消耗速率的数据源）──────────────────────────────────
        hist = full.get("history") or []
        hist.append(history_sample(data))
        cutoff = time.time() - HISTORY_MAX_SEC
        hist = [h for h in hist if float(h.get("ts") or 0) >= cutoff][-HISTORY_MAX_ROWS:]

        # ── Daily baselines ───────────────────────────────────────────────────
        today     = datetime.now().strftime("%Y-%m-%d")
        baselines = full.get("daily_baselines", {})
        if today not in baselines:
            entry = {}
            for prov in ("codex", "claude", "agy", "grok"):
                for win in ("five_hour", "weekly", "daily", "main_weekly", "bot_weekly"):
                    w   = data.get(prov, {}).get(win) or {}
                    pct = w.get("pct")
                    if pct is None:
                        continue
                    entry[f"{prov}_{win}"] = float(pct)
                    if win in ("weekly", "main_weekly", "bot_weekly"):
                        reset_at = w.get("reset_at")
                        if reset_at:
                            rd = max(0.0, (reset_at - time.time()) / 86400)
                            if rd > 0.05:
                                dt = max(0.0, 100.0 - float(pct)) / rd
                                entry[f"{prov}_{win}_daily_target"] = round(dt, 2)
                    elif win == "daily":
                        entry[f"{prov}_daily_used"] = w.get("used", 0)
            baselines[today] = entry
            for old in sorted(baselines)[:-7]:   # keep 7 days
                del baselines[old]

        # ── Pet：记住 Claude 7d 窗口位置，用来发现“一周没用完就重置了” ──────
        pet_state = pet_track_reset(data.get("claude"), full.get("pet"))

        to_save = dict(data)
        to_save["daily_baselines"] = baselines
        to_save["history"] = hist
        to_save["pet"] = pet_state
        if "update" not in to_save and full.get("update"):   # --no-update 这次没查，别把上次的检查记录丢了
            to_save["update"] = full["update"]
        save_state(to_save)
        state["full"] = to_save

        today_bl = baselines.get(today, {})
        state["usage_mood"] = claude_pet_mood(data.get("claude"), hist, pet_state, data=data)

        # 新版本：git checkout 里只提示 git pull；装好的副本下载好了，watch 里在宠物上弹升级选项
        # 这次刷新取到的、和后台定时检查写进 state 的，用更新的那份
        upd  = max((u for u in (data.get("update"), full.get("update")) if u),
                   key=lambda u: u.get("checked_at") or 0, default=None)
        show_update(upd)
        state["update_msg"] = None
        hint = state["update_hint"]
        state["update_poll_at"] = time.time() + 60   # 刚查过，后台定时检查从现在起算
        state["refresh_no"]  = state.get("refresh_no", 0) + 1
        state["update_skip"] = state["refresh_no"] < state.get("update_skip_until", 0)   # “跳过本次”只管到下次刷新
        refresh_attention()
        set_pet_mood()

        # watch 模式在终端里原地重绘：数据取完再清屏，避免抓取期间白屏
        redraw = watch_mode and not json_mode and sys.stdout.isatty()

        if json_mode:
            print(json.dumps({**data, "history_summary": history_summary(hist)}, indent=2, ensure_ascii=False))
        else:
            if compact:
                buf = io.StringIO()
                with redirect_stdout(buf):
                    render_compact(data, prev_data, today_bl, hist)
                body = buf.getvalue().rstrip("\n")
            else:
                body = render_all(data["deepseek"], data["codex"], data["claude"], data["agy"], data["grok"],
                                  prev_data, today_bl, hist, oc=data.get("opencode"))
            if interactive:
                state["body"] = body
                paint()
                return
            if redraw:
                sys.stdout.write("\033[H\033[2J")
            if pet_mode and sys.stdout.isatty():
                body = pet_merge_static(body, state["pet"].frame(), shutil.get_terminal_size().columns)
            print(body)
            block = list(attention_lines(state.get("attention") or []))
            block += status_notice_lines(state.get("statuses") or [], _watch_width())
            if block:
                print("\n" + "\n".join(block))
            if hint and not compact:
                print("\n" + hint)
        if watch_mode and (redraw or not state.get("footer_shown")):
            print(f"\n🔄 Refreshing every {interval_min} min. Ctrl+C to stop.\n", flush=True)
            state["footer_shown"] = True

    if interactive:
        fd  = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        for sig in (signal.SIGTERM, signal.SIGHUP):   # 被 kill 时也走 finally，别把终端留在无回显状态
            signal.signal(sig, lambda *_: sys.exit(0))
        tty.setcbreak(fd)                    # 按键不用回车、不回显；Ctrl+C 照常生效
        sys.stdout.write("\033[?25l")        # 动画期间隐藏光标

        state["body"] = "⏳ 正在获取数据，稍等片刻…"   # 第一次取数前先占个位，宠物照常在右下角动
        try:
            ok = refresh_interactive()
            while ok and wait_interactive(interval_min * 60):
                ok = refresh_interactive()
        except KeyboardInterrupt:
            pass
        finally:
            try:
                if state["place"] and state["pet_visible"]:   # 擦掉宠物，别留在 shell 里
                    pet_draw([" " * PET_W] * PET_H, *state["place"])
            except OSError:
                pass
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
                sys.stdout.write("\033[?25h")
                sys.stdout.flush()
            except OSError:      # SIGHUP：终端已经没了
                state["restart"] = None   # 终端没恢复好就别重启，免得新进程把无回显当成原始状态
        if state.get("restart"):
            print(f"\n✓ {state['restart']}，正在重启…", flush=True)
            os.execv(sys.executable, [sys.executable, str(Path(__file__).resolve())] + args)
        print("\n👋 Stopped.")
        return

    do_query()

    if watch_mode:
        try:
            while True:
                if state["slack"][0]:
                    # 没开交互终端时也盯着“在等你”。按小步睡，发失败也不能把循环弄断
                    deadline = time.time() + interval_min * 60
                    while True:
                        left = deadline - time.time()
                        if left <= 0:
                            break
                        time.sleep(min(5, left))
                        try:
                            notify_slack((attention_items() if state["attention_on"] else [])
                                         + external_notices()[0], *state["slack"])
                        except Exception:
                            pass
                else:
                    time.sleep(interval_min * 60)
                state["full"] = load_state()
                do_query()
        except KeyboardInterrupt:
            print("\n👋 Stopped.")

if __name__ == "__main__":
    main()
