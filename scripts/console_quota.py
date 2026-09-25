#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""控制台会话额度（可选组件；核心看板保持纯标准库，本文件独立依赖 playwright）

适用：官方不给 key 可调额度接口、只认控制台登录态的服务（阿里百炼、智谱 BigModel）。
机制（2026-09-25 实测定型）：
  --login <ali|glm>   有头浏览器登录；登录生效瞬间立即导出会话 cookie 到
                      data/<target>_cookies.json（此后任何失败都不需重扫）；随后自动取数。
  --fetch <ali|glm|all>  无头注入 cookie → 打开用量/订阅页 → 捕获页面自身发出的
                      quota 响应（不猜接口格式）→ 写 data/<target>_quota.json。
安全：捕获黑名单（url 含 ApiKeysPlain 等回明文 key 的接口）一律不解析不落盘；
      不再落任何 raw 响应；cookie 文件为 bearer 凭据，仅本地、可删。
"""
import json
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

BLACKLIST = ("ApiKeysPlain", "keysPlain", "listCodingPlanApiKeys")

ALI_LOGIN_PROBE_JS = """
async () => {
  try {
    const r = await fetch('/data/api.json?_fetcher_=notifications__ReadMessageList', {
      method: 'POST', credentials: 'include',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: 'product=notifications&action=ReadMessageList&params=%7B%7D'});
    return await r.json();
  } catch (e) { return {error: String(e)}; }
}
"""

# 智谱控制台登录探针：用量页未登录会被重定向到登录页；用重定向目标判定，
# 不依赖任何需 key 的 API（cookie 会话对 /api/biz/* 返回 401，实测）。
GLM_LOGIN_PROBE_JS = """
async () => {
  try {
    const r = await fetch('/coding-plan/personal/usage', {credentials: 'include'});
    return {ok: r.status === 200 && !/login|signin|auth/i.test(r.url), url: r.url.slice(0, 80)};
  } catch (e) { return {error: String(e)}; }
}
"""

# kimi.com 控制台用 LocalStorage access_token 调 Bearer 接口；探针直接试会员统计接口
KIMI_LOGIN_PROBE_JS = """
async () => {
  try {
    const t = localStorage.getItem('access_token') || '';
    if (!t) return {error: 'no-local-token'};
    const r = await fetch('https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats',
      {method: 'POST', credentials: 'include',
       headers: {'Content-Type': 'application/json', 'Authorization': 'Bearer ' + t},
       body: '{}'});
    return await r.json();
  } catch (e) { return {error: String(e)}; }
}
"""

NEWAPI_PROBE_JS = """
async () => {
  try {
    const r = await fetch('/api/user/self', {credentials: 'include'});
    return await r.json();
  } catch (e) { return {error: String(e)}; }
}
"""

TARGETS = {
    "ali": {
        "login_url": "https://bailian.console.aliyun.com/",
        "app_url": ["https://bailian.console.aliyun.com/cn-beijing/subscription/coding-plan",
                    "https://bailian.console.aliyun.com/cn-beijing/subscription/token-plan/personal"],
        "probe": ALI_LOGIN_PROBE_JS,
        "capture_keys": ("codingPlan", "tokenplan"),
    },
    "glm": {
        "login_url": "https://www.bigmodel.cn/",
        "app_url": "https://www.bigmodel.cn/coding-plan/personal/usage",
        "probe": GLM_LOGIN_PROBE_JS,
        "capture_keys": ("quota", "usage", "limit"),
    },
    "kimi": {
        "login_url": "https://www.kimi.com/code/console",
        "app_url": "https://www.kimi.com/code/console",
        "probe": KIMI_LOGIN_PROBE_JS,
        "capture_keys": ("MembershipService", "subscription", "Subscription"),
    },
    # 示例：new-api 系中转（登录后读 /api/user/self 钱包）。
    # 加自己的站 = 复制一段改 login_url/channel；mode:"self" 走 NEWAPI_PROBE_JS+parse_newapi_self。
    # "my-relay": {
    #     "login_url": "https://relay.example.com/",
    #     "app_url": "https://relay.example.com/",
    #     "probe": NEWAPI_PROBE_JS,
    #     "capture_keys": (),
    #     "mode": "self",
    #     "channel": "chrome",
    # },
}


def _logged_in(target, j):
    if not isinstance(j, dict) or j.get("error"):
        return False
    s = json.dumps(j, ensure_ascii=False)
    if target == "ali":
        return ("ConsoleNeedLogin" not in s) and ("请登录" not in s)
    if target == "kimi":
        return ("subscriptionBalance" in s) or ("ratelimitCode" in s)
    if TARGETS.get(target, {}).get("mode") == "self":
        return bool(j.get("success")) and isinstance(j.get("data"), dict)
    if target == "glm":
        return bool(j.get("ok"))
    return s.startswith("{") and ("401" not in s[:200]) and ("未登录" not in s) and ("code\" : 200" in s or '"code":200' in s or '"code": 200' in s)


def _export(ctx, target):
    keep = [{k: c[k] for k in ("name", "value", "domain", "path", "expires",
                               "httpOnly", "secure", "sameSite")}
            for c in ctx.cookies()]
    (DATA / f"{target}_cookies.json").write_text(
        json.dumps(keep, ensure_ascii=False), encoding="utf-8")


def _capture(page, target, seconds=16):
    """打开 app_url（可为多页），捕获页面自身发出的 quota 类 JSON 响应（黑名单除外）。"""
    caps = []
    keys = TARGETS[target]["capture_keys"]

    def on_resp(resp):
        try:
            u = resp.url
            if resp.status != 200 or any(b in u for b in BLACKLIST):
                return
            if not ("api" in u or "json" in u):
                return
            if not any(k in u for k in keys):
                return
            caps.append({"url": u, "body": resp.json()})
        except Exception:
            pass

    page.on("response", on_resp)
    urls = TARGETS[target]["app_url"]
    urls = urls if isinstance(urls, list) else [urls]
    per = max(4, seconds // len(urls))
    for u in urls:
        try:
            page.goto(u, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            continue
        page.wait_for_timeout(per * 1000)
    return caps


# ---------------------------------------------------------------- 解析
def _win(label, used, total, reset_ms, unit="次"):
    if used is None and total is None and reset_ms is None:
        return None
    return {"label": label,
            "used_percent": round(used / total * 100, 1) if total else None,
            "used": used, "quota": total,
            "remaining": (total - used) if (total is not None and used is not None) else None,
            "reset_at": (reset_ms / 1000 if reset_ms and reset_ms > 10**12 else reset_ms),
            "unit": unit}


def _find_body(caps, *url_parts):
    for c in caps:
        if all(p in c["url"] for p in url_parts):
            return c["body"]
    return None


def _deep_get(j, *path):
    cur = j
    for k in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(k)
    return cur


def parse_ali(caps):
    out = {}
    j = _find_body(caps, "codingPlan", "queryCodingPlanInstanceInfo")
    infos = _deep_get(j, "data", "DataV2", "data", "data", "codingPlanInstanceInfos") or \
        _deep_get(j, "data", "codingPlanInstanceInfos") or []
    if infos:
        info = infos[0] or {}
        q = info.get("codingPlanQuotaInfo") or {}
        wins = [w for w in (
            _win("5小时", q.get("per5HourUsedQuota"), q.get("per5HourTotalQuota"),
                 q.get("per5HourQuotaNextRefreshTime")),
            _win("周", q.get("perWeekUsedQuota"), q.get("perWeekTotalQuota"),
                 q.get("perWeekQuotaNextRefreshTime")),
            _win("月", q.get("perBillMonthUsedQuota"), q.get("perBillMonthTotalQuota"),
                 q.get("perBillMonthQuotaNextRefreshTime")),
        ) if w]
        if wins:
            out["coding"] = {"ok": True, "kind": "quota",
                             "plan": info.get("instanceName") or "Coding Plan",
                             "windows": wins}
    j2 = _find_body(caps, "tokenplan", "/usage")
    d2 = _deep_get(j2, "data", "DataV2", "data", "data") or {}
    pct = d2.get("per1MonthPercentage")
    if pct is not None:
        used_pct = pct * 100 if pct <= 1 else pct
        sub = _deep_get(_find_body(caps, "tokenplan", "subscription") or {},
                        "data", "DataV2", "data", "data") or {}
        out["token"] = {"ok": True, "kind": "quota",
                        "plan": f"Token Plan {(sub.get('specCode') or '').upper()}".strip(),
                        "note": (f"剩余 {sub.get('remainingDays')} 天 · 百分比制（官方不给绝对量）"
                                 if sub.get("remainingDays") is not None else None),
                        "windows": [{"label": "月",
                                     "used_percent": round(used_pct, 1),
                                     "used": None, "quota": None,
                                     "remaining_percent": round(100 - used_pct, 1),
                                     "reset_at": _ms2s(d2.get("per1MonthResetTime")),
                                     "unit": ""}]}
    if not out:
        return None
    return {"ok": True, "fetched_at": _now(), **out}


def _ms2s(v):
    return v / 1000 if v and v > 10**12 else v


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_epoch(v):
    """任意时间表示 → epoch 秒（epoch 秒/毫秒、ISO 字符串）。"""
    if v is None:
        return None
    if isinstance(v, (int, float)):
        return v / 1000 if v > 10**12 else float(v)
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
        except Exception:
            try:
                f = float(v)
                return f / 1000 if f > 10**12 else f
            except Exception:
                return None
    return None


def parse_glm(caps):
    """控制台用量页捕获：优先找含 limits/四块结构的响应；找不到则存捕获供适配。"""
    for c in caps:
        body = c["body"]
        s = json.dumps(body, ensure_ascii=False)
        if "percentage" not in s and "remaining" not in s:
            continue
        limits = None
        if isinstance(body, dict):
            limits = _deep_get(body, "data", "limits") or body.get("limits")
            if limits is None:
                for v in body.values():
                    if isinstance(v, dict) and isinstance(v.get("limits"), list):
                        limits = v["limits"]
                        break
        if isinstance(limits, list) and limits:
            wins = []
            for x in limits:
                u = x.get("unit")
                lab = {3: "5小时", 6: "周", 5: "MCP 每月额度"}.get(u) or \
                    x.get("name") or x.get("type") or f"unit={u}"
                if x.get("type") == "TIME_LIMIT" or x.get("currentValue") is not None:
                    wins.append(_win(lab, x.get("currentValue"), x.get("usage"),
                                     x.get("nextResetTime")))
                else:
                    wins.append({"label": lab, "used_percent": x.get("percentage"),
                                 "used": None, "quota": None,
                                 "remaining": x.get("remaining"),
                                 "reset_at": _ms2s(x.get("nextResetTime")), "unit": ""})
            if wins:
                return {"ok": True, "kind": "quota", "plan": "BigModel Coding Max",
                        "windows": wins, "fetched_at": _now()}
    return None


def parse_kimi(caps):
    """kimi.com 控制台自身响应里找 subscriptionBalance（总使用量=Kimi+Code 合计）。"""
    def find_sb(x):
        if isinstance(x, dict):
            if "subscriptionBalance" in x and isinstance(x["subscriptionBalance"], dict):
                return x["subscriptionBalance"]
            for v in x.values():
                r = find_sb(v)
                if r:
                    return r
        elif isinstance(x, list):
            for v in x:
                r = find_sb(v)
                if r:
                    return r
        return None

    for c in caps:
        sb = find_sb(c["body"])
        if not sb:
            continue
        ratio = sb.get("amountUsedRatio")
        if ratio is None:
            continue
        pct = ratio * 100 if ratio <= 1 else ratio
        return {"ok": True,
                "window": {"label": "总(Kimi+Code)",
                           "used_percent": round(pct, 2),
                           "used": None, "quota": None, "remaining": None,
                           "reset_at": _to_epoch(sb.get("expireTime")), "unit": ""},
                "fetched_at": _now()}
    return None


def parse_newapi_self(j):
    """new-api /api/user/self → 钱包余额（quota 单位 500000 = $1）。"""
    d = j.get("data") or {}
    u = d.get("user") or d
    q = _num(u.get("quota"))
    if q is None:
        return None
    return {"ok": True, "kind": "relay-site",
            "wallet_usd": round(q / 500000, 2),
            "username": u.get("username"),
            "fetched_at": _now()}


def _now():
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S")


PARSERS = {"ali": parse_ali, "glm": parse_glm, "kimi": parse_kimi}


# ---------------------------------------------------------------- 流程
def login(target):
    from playwright.sync_api import sync_playwright
    t = TARGETS[target]
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(DATA / f"{target}_profile"), headless=False,
            channel=t.get("channel"),
            viewport={"width": 980, "height": 720}, locale="zh-CN")
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(t["login_url"], wait_until="domcontentloaded", timeout=60000)
            print(f"已打开 {target} 登录页：请完成登录（扫码/短信）…")
            print("登录一旦生效立即导出会话（之后任何情况都无需重扫），再自动取数。")
            deadline = time.time() + 300
            logged = False
            while time.time() < deadline:
                try:
                    j = page.evaluate(t["probe"])
                except Exception:
                    j = None
                if _logged_in(target, j):
                    logged = True
                    break
                time.sleep(2)
            if not logged:
                print("5 分钟内未检测到登录，未写入任何数据。")
                return None
            _export(ctx, target)
            print(f"会话已导出 data/{target}_cookies.json（无需再扫）。正在取数…")
            if TARGETS[target].get("mode") == "self":
                result = parse_newapi_self(page.evaluate(TARGETS[target]["probe"]))
            else:
                caps = _capture(page, target)
                result = PARSERS[target](caps)
                if result is None and caps:
                    safe = [c for c in caps if not any(b in c["url"] for b in BLACKLIST)]
                    (DATA / f"{target}_captures.json").write_text(
                        json.dumps(safe, ensure_ascii=False, indent=1), encoding="utf-8")
                    print(f"捕获 {len(caps)} 个响应但解析未命中，已存 data/{target}_captures.json 供适配。")
        finally:
            ctx.close()
    return result


def _self_via_urllib(target):
    """self 型目标优先走 urllib：导出 cookie + 浏览器 UA 直调 /api/user/self。
    （实测两站 API 路径不拦 python TLS；仅登录页有人机验证。）"""
    cf = DATA / f"{target}_cookies.json"
    if not cf.exists():
        return None, "无会话凭据文件"
    saved = json.loads(cf.read_text(encoding="utf-8"))
    host = TARGETS[target]["login_url"].split("//", 1)[1].rstrip("/")
    ck = "; ".join(f'{c["name"]}={c["value"]}' for c in saved
                   if c.get("domain") and (host.endswith(c["domain"].lstrip("."))
                                            or c["domain"].lstrip(".").endswith(host)))
    if not ck:
        return None, "会话 cookie 与主机不匹配"
    import urllib.request
    req = urllib.request.Request(TARGETS[target]["login_url"].rstrip("/") + "/api/user/self",
                                 headers={"Cookie": ck, "User-Agent":
                                          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                                          "Chrome/124.0 Safari/537.36"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            j = json.loads(r.read().decode("utf-8", errors="replace"))
    except Exception as e:
        return None, f"urllib 直调失败：{type(e).__name__}"
    res = parse_newapi_self(j)
    if res is None:
        return None, "会话无效或响应结构不符"
    return res, None


def fetch(target):
    t = TARGETS[target]
    cf = DATA / f"{target}_cookies.json"
    if t.get("mode") == "self":
        res, err = _self_via_urllib(target)
        if res:
            return res, None
    elif not cf.exists():
        return None, f"无 {target} 会话凭据：先运行 --login {target}"
    if target == "kimi" and not (DATA / "kimi_profile").exists():
        return None, "无 kimi 会话：先运行 --login kimi"
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = None
        if target == "kimi":
            # web access_token 存于 localStorage，必须复用持久 profile（cookie 注入带不过去）
            ctx = p.chromium.launch_persistent_context(
                str(DATA / "kimi_profile"), headless=True,
                locale="zh-CN", viewport={"width": 980, "height": 720})
        else:
            saved = json.loads(cf.read_text(encoding="utf-8"))
            browser = p.chromium.launch(headless=True, channel=t.get("channel"))
            ctx = browser.new_context(locale="zh-CN",
                                      viewport={"width": 980, "height": 720})
            ctx.add_cookies(saved)
        try:
            page = ctx.pages[0] if ctx.pages else ctx.new_page()
            page.goto(TARGETS[target]["login_url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1200)
            if not _logged_in(target, page.evaluate(TARGETS[target]["probe"])):
                return None, f"{target} 服务器会话已过期：重新运行 --login {target}"
            if TARGETS[target].get("mode") == "self":
                result = parse_newapi_self(page.evaluate(TARGETS[target]["probe"]))
                if result is None:
                    return None, f"{target} 会话有效但 /api/user/self 解析失败"
            else:
                caps = _capture(page, target)
                result = PARSERS[target](caps)
                if result is None and caps:
                    safe = [c for c in caps if not any(b in c["url"] for b in BLACKLIST)]
                    (DATA / f"{target}_captures.json").write_text(
                        json.dumps(safe, ensure_ascii=False, indent=1), encoding="utf-8")
                    return None, f"{target} 会话有效但解析未命中（捕获已存 data/{target}_captures.json）"
        finally:
            ctx.close()
            if browser:
                browser.close()
    if result is None:
        return None, f"{target} 捕获为空（页面未发出 quota 请求？）"
    return result, None


def main():
    args = sys.argv[1:]
    quiet = "--quiet" in args
    targets = []
    for a in args:
        if a in TARGETS:
            targets.append(a)
        if a == "all":
            targets = list(TARGETS)
    if "--login" in args:
        for t in (targets or ["ali"]):
            res = login(t)
            if res:
                (DATA / f"{t}_quota.json").write_text(
                    json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
                if not quiet:
                    print(f"{t} 额度已取")
        return
    if "--fetch" in args:
        for t in (targets or ["ali"]):
            res, err = fetch(t)
            if res:
                (DATA / f"{t}_quota.json").write_text(
                    json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
                if not quiet:
                    print(f"{t} 额度已取")
            elif not quiet:
                print(err)
        return
    print(__doc__)


if __name__ == "__main__":
    main()
