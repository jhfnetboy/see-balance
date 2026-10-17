#!/usr/bin/env python3
"""
see-balance — query usage/balance for AI providers.

  DeepSeek    : GET  api.deepseek.com/user/balance            (API key)
  Codex       : GET  chatgpt.com/backend-api/wham/usage       (~/.codex/auth.json)
  Claude      : GET  api.anthropic.com/api/oauth/usage        (Keychain OAuth)
  Antigravity : POST daily-cloudcode-pa.googleapis.com        (Keychain / Google OAuth)
  Grok (xAI)  : GET  cli-chat-proxy.grok.com/v1/billing       (~/.grok/auth.json or XAI_API_KEY)

Usage:
  python3 provider_balance.py                  # one-shot, human-readable
  python3 provider_balance.py --watch          # refresh every 30 min (default)
  python3 provider_balance.py --watch 15       # refresh every 15 min
  python3 provider_balance.py --compact        # one-line per provider
  python3 provider_balance.py --json           # raw JSON
  python3 provider_balance.py --no-pet         # hide the Claude pet (or SEE_BALANCE_PET=0)
  python3 provider_balance.py --pet-demo       # preview every pet mood, no API calls
  python3 provider_balance.py --update         # install the latest version from upstream main now
  python3 provider_balance.py --version        # print the version
  python3 provider_balance.py --no-update      # skip the daily update check (or SEE_BALANCE_UPDATE=0)
  python3 provider_balance.py --install-hooks  # pet reminds you when a Claude Code / Grok session waits for you
  python3 provider_balance.py --uninstall-hooks

  In --watch on a terminal: p toggles the pet, s switches pet (cat/bunny/bear), r refreshes now,
  u installs a downloaded update and restarts, q quits.

Config:
  ~/.see-balance.env   — put DEEPSEEK_API_KEY, XAI_API_KEY, and proxy settings here
  State cached at:     ~/.see-balance/state.json

No secrets ever leave this machine.
"""

import base64, http.client, io, json, os, random, re, select, shlex, shutil, signal, socket, ssl, stat, subprocess, sys, tempfile, threading, time, unicodedata
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError
try:
    import termios, tty      # watch 模式读快捷键；没有（如 Windows）就退回普通 sleep
except ImportError:
    termios = tty = None

__version__ = "1.2.2"   # 自动更新拿上游 main 上的这个号比新旧：要推给用户的改动记得调大

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


def render_all(ds, cx, cl, agy, gr, prev_snap, today_bl: dict, hist=None):
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
        lines.append(f"     5h used     {window_pct_bar(cl.get('five_hour'))}")
        lines.append(f"     7d used     {window_pct_bar(cl.get('weekly'))}")
        d = today_target_line(cl.get("weekly", {}), today_bl.get("claude_weekly"), today_bl.get("claude_weekly_daily_target"))
        if d: lines.append(d)
        p = pace_line(cl.get("weekly", {}), 7 * 24 * 3600)
        if p: lines.append(p)
        b, a = burn_advice(cl.get("weekly"), advice_rate(cl.get("weekly"), hist, "claude_weekly", 7 * 24 * 3600))
        if b: lines.append(b)
        if a: lines.append(a)
        b, a = burn_advice(cl.get("five_hour"), burn_rate(hist, "claude_5h", 5 * 3600))
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

    advice = dispatch_advice({"codex": cx, "claude": cl, "agy": agy, "grok": gr}, hist=hist)
    if advice:
        lines.append("  🧭 调度建议")
        lines += advice
        lines.append("")
    lines.append("═" * 54)
    return "\n".join(lines)

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
    # 刷新取数中（后台线程在拉数据，动画照常跑）
    "busy":      [_pf(m="o", x1="."), _pf(x1=".."), _pf(m="o", x1="..."), _pf(x1="...")],
}

# 日常小动作：(气泡文字, 帧序列)；没别的情绪时每隔几秒随机换一个
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
    传了 data（全部订阅）时，劝歇的同时指一家有余量的订阅去用。
    """
    now = now or time.time()
    if not cl or "error" in cl:
        return "idle", None
    wk, fh = cl.get("weekly") or {}, cl.get("five_hour") or {}
    proj   = pace_projection(wk, 7 * 24 * 3600)
    fh_pct = fh.get("pct")
    pct    = wk.get("pct")
    stats  = sub_status(data, now, hist) if data else []
    alt    = best_spare(stats, "claude")

    if pct is not None and float(pct) >= PET_HAPPY_PCT:
        return "happy", "Claude 周额度用完啦！好棒！"
    if fh_pct is not None and float(fh_pct) >= EXHAUSTED_PCT:   # 和调度建议同一口径
        if alt:
            return "caring", f"5h 用光啦，试试 {alt['short']} 吧？♥"
        return "caring", "5h 额度用光啦，起来走走吧 ♥"
    if proj and proj[1] > PACE_FAST_PCT:
        if alt:
            return "caring", f"要用到 {proj[1]:.0f}% 啦，试试 {alt['short']}？♥"
        return "caring", f"照这样到期要用 {proj[1]:.0f}%，歇一歇 ♥"
    if fh_pct is not None and fh.get("reset_at"):
        r          = burn_rate(hist, "claude_5h", 5 * 3600)
        reset_in_h = max(0.0, fh["reset_at"] - now) / 3600.0
        if r and r["per_hour"] > 0.005 and reset_in_h >= 0.25:
            exhaust_h = max(0.0, 100.0 - float(fh_pct)) / r["per_hour"]
            if exhaust_h < reset_in_h:
                if alt:
                    return "caring", f"5h {fmt_countdown(exhaust_h * 3600)} 后见底，试试 {alt['short']}？♥"
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


def pet_draw(pet_lines, top, col):
    """在屏幕绝对位置 (0 起算) 画一帧，画完把光标放回原处。"""
    out = ["\0337"]
    for i, line in enumerate(pet_lines):
        out.append(f"\033[{top + i + 1};{col + 1}H{line}")
    out.append("\0338")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def pet_demo():
    """--pet-demo：不查接口，依次演示每种情绪和日常小动作（宠物轮流上场）。"""
    view  = PetView()
    shows = [("caring", "要用到 135% 啦，试试 Grok？♥"), ("happy", "Claude 周额度用完啦！好棒！"),
             ("angry", "上周白白浪费了 35%！哼！"), ("encourage", "剩 45% 要过期啦，试试 Opus？"),
             ("encourage", "还剩 62% 呢，多用用嘛～加油！"),
             ("update", "新版本 v1.3.0！u 立即升级\nk 跳过本次 · n 不要提醒"),
             ("call", "see-balance 的 Claude\n在等你确认，快去看看 ♥")]
    shows += [("idle", i) for i in range(len(PET_IDLE))]
    if not sys.stdout.isatty():
        for i, (mood, arg) in enumerate(shows):
            view.species = PET_SPECIES_ORDER[i % len(PET_SPECIES_ORDER)]
            view.set_idle(arg) if mood == "idle" else view.set_mood(mood, arg)
            print(f"── {PET_SPECIES[view.species][0]} · {mood}\n" + "\n".join(l.rstrip() for l in view.frame()) + "\n")
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
# 它把事件追加到 ~/.see-balance/attention.jsonl，只记会话 id、目录、事件和一小段说明，不记工具输入输出。
# 宠物只提醒，不替你批准任何东西：回原来的终端作答。

ATTENTION_LOG     = STATE_FILE.parent / "attention.jsonl"
ATTENTION_HOOK    = STATE_FILE.parent / "hook.py"
ATTENTION_MAX_AGE = 12 * 3600          # 超过这么久还没动静的提醒（终端关了等）不再显示
ATTENTION_ROTATE  = 1024 * 1024        # 日志超过这么大就换新文件（旧的留一份 .old）
CLAUDE_SETTINGS   = Path(os.environ.get("SEE_BALANCE_CLAUDE_SETTINGS")
                         or (Path.home() / ".claude" / "settings.json"))
GROK_HOOK_FILE    = Path(os.environ.get("SEE_BALANCE_GROK_HOOKS")
                         or (Path.home() / ".grok" / "hooks" / "see-balance.json"))
HOOK_EVENTS       = ("Notification", "UserPromptSubmit", "Stop", "StopFailure", "SessionEnd")
# Grok 里拒绝 / 取消权限框时发的是 StopCancelled（代替 Stop）。Claude Code 不认识这个事件，
# 所以不放进共享的 ~/.claude/settings.json，单独写一份只有 Grok 读的 ~/.grok/hooks/see-balance.json
GROK_ONLY_EVENTS  = ("StopCancelled",)
SOURCE_NAMES      = {"claude": "Claude", "grok": "Grok"}
# Notification 的类型 → 等你做什么；没列出的（auth_success、agent_completed 等）不算等你
WAIT_KINDS        = {"permission_prompt": "确认", "elicitation_dialog": "填写",
                     "elicitation_url_dialog": "填写", "agent_needs_input": "回复"}

HOOK_SCRIPT = r'''#!/usr/bin/env python3
# see-balance 会话提醒 hook（由 provider_balance.py --install-hooks 生成，--uninstall-hooks 删除）。
# 只往日志追加一行：哪个会话、在哪个目录、发生了什么；不输出任何东西，不影响会话本身。
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
ev  = e.get("hook_event_name") or ""
sub = bool(e.get("agent_id") or e.get("subagentType"))
# 子 agent 的输入 / 会话结束不代表主会话；它弹的权限框、它这一轮的结束（只清它自己的提醒）还是要记
if sub and ev not in ("Notification", "Stop", "StopCancelled", "StopFailure", "SessionEnd"):
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
'''


def _hook_command():
    """永远退出 0、不输出：hook 脚本被删了也不能拦下你的输入（Claude Code / Grok 把退出码 2 当成阻断）。"""
    return f"python3 -S {shlex.quote(str(ATTENTION_HOOK))} >/dev/null 2>&1 || true"


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


def _add_hooks(path, events, cmd):
    """把 cmd 挂到 path 的这些事件上（已挂过的跳过）。返回 (新挂的事件, 错误)。"""
    data, err = _load_settings(path)
    if data is None:
        return [], err
    hooks, added = data.setdefault("hooks", {}), []
    for ev in events:
        groups = hooks.setdefault(ev, [])
        if any(isinstance(h, dict) and h.get("command") == cmd for h in _handlers(groups)):
            continue
        groups.append({"hooks": [{"type": "command", "command": cmd, "timeout": 5}]})
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
    _write_atomic(ATTENTION_HOOK, HOOK_SCRIPT.replace("__LOG__", repr(str(ATTENTION_LOG))), mode=0o700)
    cmd, out = _hook_command(), [f"✓ hook 脚本：{_short_path(str(ATTENTION_HOOK))}"]
    added, err = _add_hooks(CLAUDE_SETTINGS, HOOK_EVENTS, cmd)
    if err:
        return out + [f"⚠ 读不了 {_short_path(str(CLAUDE_SETTINGS))}：{err}，没有改动"]
    out.append(f"✓ 已挂到 {_short_path(str(CLAUDE_SETTINGS))}：{'、'.join(added)}（原文件备份为 .see-balance.bak）"
               if added else f"✓ {_short_path(str(CLAUDE_SETTINGS))} 里已经挂好了，没有改动")
    if GROK_HOOK_FILE.parent.parent.exists():   # 装了 Grok CLI
        added, err = _add_hooks(GROK_HOOK_FILE, GROK_ONLY_EVENTS, cmd)
        if err:
            out.append(f"⚠ 读不了 {_short_path(str(GROK_HOOK_FILE))}：{err}，Grok 拒绝权限后的提醒可能要等下一句话才消失")
        elif added:
            out.append(f"✓ 已挂到 {_short_path(str(GROK_HOOK_FILE))}：{'、'.join(added)}（只有 Grok 读）")
    out.append("  Grok CLI 默认也读 ~/.claude/settings.json；新开的 Claude Code / Grok 会话开始生效。")
    return out


def uninstall_hooks():
    """把 install_hooks 挂上的条目摘掉（别的 hook 不动），删掉 hook 脚本。返回说明行。"""
    out = []
    ours = re.compile(r"python3 -S (\S+/hook\.py|'[^']*/hook\.py') >/dev/null 2>&1 \|\| true")
    def is_ours(command):   # 只认 install_hooks 生成的那种命令（换过 SEE_BALANCE_STATE 装的也认），别的不碰
        return command == _hook_command() or bool(ours.fullmatch(command))
    for path in (CLAUDE_SETTINGS, GROK_HOOK_FILE):
        removed, err = _remove_hooks(path, is_ours)
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


def attention_items(now=None):
    """现在还在等你的会话，按等待时间从早到晚：[{src, sid, cwd, kind, ts, msg}]。

    等你：权限确认 / 表单（Notification 的对应类型），或者最后一句在问你、之后一直空闲（idle_prompt）。
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
        if ev == "Notification":
            typ  = r.get("type") or ""
            kind = WAIT_KINDS.get(typ) or ("回复" if typ == "idle_prompt" and s["ask"] else None)
            if kind:
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
        try:   # 会话记录在提醒之后又写了东西：你已经在终端里处理过了
            if w["transcript"] and os.stat(w["transcript"]).st_mtime > w["ts"] + 1:
                continue
        except OSError:
            pass
        items.append(w)
    return sorted(items, key=lambda w: w["ts"])


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]+")


def _clean(text):
    """日志里的文字来自会话（模型输出、MCP 表单、目录名），显示前去掉控制字符，别让它改终端。"""
    return _CONTROL_CHARS.sub(" ", str(text or "")).strip()


def _project(w):
    return _clean(os.path.basename(w["cwd"].rstrip("/"))) or "~"


def _attention_name(w):
    return f"{_project(w)} 的 {SOURCE_NAMES.get(w['src'], _clean(w['src']))}"


def attention_bubble(items):
    """宠物气泡（两行）。"""
    if len(items) == 1:
        w = items[0]
        return f"{_attention_name(w)}\n在等你{w['kind']}，快去看看 ♥"
    names = "、".join(_project(w) for w in items)
    return f"{len(items)} 个会话在等你 ♥\n{names}"


def attention_lines(items, now=None):
    """底部提醒行：每个等你的会话一行，带上等了多久和那句话。"""
    now = now or time.time()
    out = []
    for w in items[:5]:
        ago  = fmt_countdown(now - w["ts"])
        what = f"：{_clean(w['msg'])}" if w["msg"] else ""
        out.append(f"🔔 {_attention_name(w)} 在等你{w['kind']}（{ago} 前）{what}")
    if len(items) > 5:
        out.append(f"🔔 还有 {len(items) - 5} 个会话在等你")
    return out


# ── Main ──────────────────────────────────────────────────────────────────────

def collect(update=True):
    data = {
        "ts":       int(time.time()),
        "deepseek": fetch_deepseek(),
        "codex":    fetch_codex(),
        "claude":   fetch_claude(),
        "agy":      fetch_agy(),
        "grok":     fetch_grok(),
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
    if "--install-hooks" in args or "--uninstall-hooks" in args:
        print("\n".join(install_hooks() if "--install-hooks" in args else uninstall_hooks()))
        return
    if "--version" in args:
        print(f"see-balance v{__version__}")
        return
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
    # 交互 watch：原地重绘 + 宠物动画 + 快捷键（p 宠物 / r 刷新 / q 退出）
    # 只在前台进程启用：`--watch &` 这种后台任务改终端模式会被 SIGTTOU 挂起
    interactive = (watch_mode and not json_mode and termios is not None
                   and sys.stdout.isatty() and sys.stdin.isatty()
                   and os.tcgetpgrp(sys.stdin.fileno()) == os.getpgrp())

    full0 = load_state()
    state = {"full": full0, "pet": PetView(species=(full0.get("pet") or {}).get("species", "cat")),
             "pet_visible": True, "size": None, "place": None,
             "attention_on": not json_mode and not compact and hooks_installed()}

    def update_prompt():
        """要不要弹升级选项：有下载好的新版本，这次没跳过，这个版本也没说过“不要提醒”。"""
        v = state.get("update_ver")
        return bool(v) and not state.get("update_skip") and v != state.get("update_muted")

    def refresh_attention():
        """重读“哪些会话在等你”；有变化返回 True。没装 hook 就什么都不做。"""
        items = attention_items() if state["attention_on"] else []
        key   = [(w["src"], w["sid"], w["kind"], w["ts"]) for w in items]
        if key == state.get("attention_key"):
            return False
        state["attention"], state["attention_key"] = items, key
        return True

    def set_pet_mood():
        """会话在等你最优先，其次升级选项（选完就让开），其余按用量；git checkout 里闲着时提一句底下有更新提示。"""
        if state.get("attention"):
            state["pet"].set_mood("call", attention_bubble(state["attention"]))
            return
        if update_prompt():
            state["pet"].set_mood("update", f"新版本 v{state['update_ver']}！u 立即升级\nk 跳过本次 · n 不要提醒")
            return
        mood = state.get("usage_mood") or ("idle", None)
        if mood[0] == "idle" and state.get("update_hint") and not state.get("can_update"):
            mood = ("encourage", "有新版本啦，看看底下～")
        state["pet"].set_mood(*mood)

    def paint():
        """整屏重画：正文 + footer，再把宠物画在右下角（放不下就在 footer 下提示）。"""
        size  = shutil.get_terminal_size()
        keys  = " · p 宠物 · s 换宠物" if pet_mode else ""
        if state.get("update_msg"):
            note = state["update_msg"]
        elif state.get("can_update"):   # 宠物藏起来或放不下时，底下也能看到选项
            note = (f"🆕 新版本 v{state['update_ver']} 已下载好（当前 v{__version__}）："
                    "u 立即升级 · k 跳过本次 · n 不要提醒") if update_prompt() else None
        else:
            note = state.get("update_hint")
        calls = "\n".join(attention_lines(state.get("attention") or []))
        state["attention_min"] = int(time.time() // 60)
        text  = (state["body"] + (f"\n\n{calls}" if calls else "") + (f"\n\n{note}" if note else "")
                 + f"\n\n🔄 每 {interval_min} 分钟刷新{keys} · r 刷新 · q 退出\n")
        place = None
        if pet_mode and state["pet_visible"]:
            rows    = screen_rows(text, size.columns)   # 末尾空行就是光标所在行
            visible = rows[-size.lines:] + [0] * max(0, size.lines - len(rows))
            place   = pet_place(visible, size.columns, size.lines - PET_H - 1)   # 底下留 1 行给光标
            if not place:
                need  = max(visible) + 2 + PET_W + 1
                text += f"   （终端太窄，宠物先躲起来了：加宽到约 {need} 列可显示；p 隐藏宠物）\n"
        if state.get("busy") and not place:   # 宠物不在屏幕上，就用一行字提示正在刷新
            text += "⏳ 正在刷新，稍等片刻…\n"
        sys.stdout.write("\033[H\033[2J" + text)
        sys.stdout.flush()
        state["size"], state["place"] = size, place
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

    def pump(timeout):
        """推进一帧宠物动画，再等按键最多 timeout 秒。返回 "quit" / "refresh" / None。"""
        now = time.time()
        if update_mode:
            poll_update(now)
        if state["attention_on"] and now - state.get("attention_at", 0) >= 1 and not state.get("busy"):
            state["attention_at"] = now   # 会话提醒要及时：每秒看一眼日志，不等下一次刷新
            if refresh_attention():
                set_pet_mood()
                paint()
            elif state.get("attention") and int(now // 60) != state.get("attention_min"):
                paint()               # 底部“等了多久”按分钟走
        if pet_mode and state["pet_visible"]:
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
        prev_data = {k: full[k] for k in ("deepseek", "codex", "claude", "agy", "grok") if k in full}
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
                                  prev_data, today_bl, hist)
            if interactive:
                state["body"] = body
                paint()
                return
            if redraw:
                sys.stdout.write("\033[H\033[2J")
            if pet_mode and sys.stdout.isatty():
                body = pet_merge_static(body, state["pet"].frame(), shutil.get_terminal_size().columns)
            print(body)
            calls = attention_lines(state.get("attention") or [])
            if calls:
                print("\n" + "\n".join(calls))
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
                time.sleep(interval_min * 60)
                state["full"] = load_state()
                do_query()
        except KeyboardInterrupt:
            print("\n👋 Stopped.")

if __name__ == "__main__":
    main()
