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

Config:
  ~/.see-balance.env   — put DEEPSEEK_API_KEY, XAI_API_KEY, and proxy settings here
  State cached at:     ~/.see-balance/state.json

No secrets ever leave this machine.
"""

import base64, json, os, socket, ssl, subprocess, sys, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

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
        try:    return json.loads(STATE_FILE.read_text())
        except: pass
    return {}

def save_state(snapshot):
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False) + "\n")

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


def burn_advice(w, rate, alt_hint=None):
    """返回 (实测消耗行, 进展建议行) 二元组（已是完整行，含缩进）；没有则 None。

    建议就是把速率翻译成“能不能撑到重置”—— 这是唯一有用的结论。
    """
    if not w or w.get("pct") is None:
        return None, None
    pct  = float(w["pct"])
    left = max(0.0, 100.0 - pct)
    reset_in_h = (max(0.0, w["reset_at"] - time.time()) / 3600.0) if w.get("reset_at") else None

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

def pace_line(w: dict, total_sec: int) -> str:
    """Return a pace-assessment line for a rate-limited usage window.

    Compares actual usage against how much should have been used given elapsed
    time, then projects what the final usage will be at the current rate.
    """
    if not w or w.get("pct") is None or not w.get("reset_at"):
        return ""
    pct      = float(w["pct"])
    remaining = max(0.0, w["reset_at"] - time.time())
    elapsed   = max(0.0, total_sec - remaining)
    if elapsed < total_sec * 0.05:   # < 5% elapsed — too early to judge
        return ""
    if remaining < total_sec * 0.03:  # 窗口快重置了，此刻的节奏判断没意义
        return ""
    frac      = elapsed / total_sec
    projected = pct / frac           # estimated usage at end of window

    if projected <= 75:
        icon, verdict = "🔴", f"偏慢可加速"
    elif projected <= 100:
        icon, verdict = "🟢", f"节奏正常"
    elif projected <= 120:
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
        g_left  = 100.0 - float((agy.get("gemini_weekly") or {}).get("pct") or 0.0)
        p3_left = 100.0 - float((agy.get("3p_weekly")     or {}).get("pct") or 0.0)
        alt_hint = None
        if g_left < 30.0 and p3_left >= 30.0:
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

# ── Main ──────────────────────────────────────────────────────────────────────

def collect():
    return {
        "ts":       int(time.time()),
        "deepseek": fetch_deepseek(),
        "codex":    fetch_codex(),
        "claude":   fetch_claude(),
        "agy":      fetch_agy(),
        "grok":     fetch_grok(),
    }

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

    state = {"full": load_state()}

    def do_query():
        full      = state["full"]
        prev_data = {k: full[k] for k in ("deepseek", "codex", "claude", "agy", "grok") if k in full}
        data      = collect()

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

        to_save = dict(data)
        to_save["daily_baselines"] = baselines
        to_save["history"] = hist
        save_state(to_save)
        state["full"] = to_save

        today_bl = baselines.get(today, {})

        # watch 模式在终端里原地重绘：数据取完再清屏，避免抓取期间白屏
        redraw = watch_mode and not json_mode and sys.stdout.isatty()
        if redraw:
            sys.stdout.write("\033[H\033[2J")

        if json_mode:
            print(json.dumps({**data, "history_summary": history_summary(hist)}, indent=2, ensure_ascii=False))
        elif compact:
            render_compact(data, prev_data, today_bl, hist)
        else:
            print(render_all(data["deepseek"], data["codex"], data["claude"], data["agy"], data["grok"],
                             prev_data, today_bl, hist))
        if watch_mode and (redraw or not state.get("footer_shown")):
            print(f"\n🔄 Refreshing every {interval_min} min. Ctrl+C to stop.\n", flush=True)
            state["footer_shown"] = True

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
