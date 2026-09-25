#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
菲戈 AI 用量看板 v1 — 单文件 · 纯 Python 标准库 · 零 pip 依赖

用法:
  py usage_dashboard.py --scan [period]   CLI 文本输出（验证数据用，period 默认 today）
  py usage_dashboard.py                   启动本地看板 http://127.0.0.1:8787

原则:
  - ZCode 库只读连接 (mode=ro)；.codex 目录只读；不写任何原始数据源
  - API key 仅运行时内存中使用（读自 ZCode 自有配置），绝不写入本项目任何文件/日志
  - 无中间数据库：每次从原始日志全量重算，天然幂等
"""
from __future__ import annotations

import glob
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

# ---------------------------------------------------------------- 路径与常量
ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
HOME = Path.home()
# 可用环境变量覆盖：ZCODE_HOME / CODEX_HOME（默认 ~/.zcode 与 ~/.codex）
ZCODE_HOME = Path(os.environ.get("ZCODE_HOME", HOME / ".zcode"))
CODEX_HOME = Path(os.environ.get("CODEX_HOME", HOME / ".codex"))
ZCODE_DB = ZCODE_HOME / "cli" / "db" / "db.sqlite"
ZCODE_PROVIDER_CONFIG = ZCODE_HOME / "v2" / "provider_config.json"
CODEX_CACHE = DATA_DIR / "codex_cache.json"
ZCODE_DAILY = DATA_DIR / "zcode_daily.json"
QUOTA_CACHE = DATA_DIR / "quota_cache.json"
PRICES_FILE = DATA_DIR / "prices.json"
EXTRA_KEYS = DATA_DIR / "extra_keys.json"
ALI_QUOTA_JSON = DATA_DIR / "ali_quota.json"   # scripts/ali_quota.py --fetch 的产出
ALI_PROFILE = DATA_DIR / "ali_profile"         # Playwright 持久会话目录（含登录态）

# 本机时区偏移（Asia/Shanghai=+8，无夏令时）。ZCode 源库按 ~1 万行滚动修剪
# model_usage（实测 2026-09-25 一小时内从 22,847 行剪到 9,611 行），
# 因此看板必须自己留存"按天聚合"，否则老数据会被源头永久删除。
def tz_offset_hours() -> int:
    off = now_local().utcoffset()
    return int(off.total_seconds() // 3600) if off else 8

PERIODS = ["today", "yesterday", "7d", "30d", "month", "all"]
PERIOD_LABELS = {
    "today": "今天", "yesterday": "昨天", "7d": "近7天",
    "30d": "近30天", "month": "本月", "all": "全部",
}


def now_local() -> datetime:
    return datetime.now().astimezone()


def period_bounds(key: str):
    """返回 (start_ms, end_ms)，本地时区(Asia/Shanghai)日界；end 为 None 表示到现在。"""
    n = now_local()
    today0 = n.replace(hour=0, minute=0, second=0, microsecond=0)

    def ms(dt: datetime) -> int:
        return int(dt.timestamp() * 1000)

    if key == "today":
        return ms(today0), None
    if key == "yesterday":
        return ms(today0 - timedelta(days=1)), ms(today0)
    if key == "7d":
        return ms(today0 - timedelta(days=6)), None
    if key == "30d":
        return ms(today0 - timedelta(days=29)), None
    if key == "month":
        return ms(today0.replace(day=1)), None
    if key == "all":
        return 0, None
    raise ValueError(f"unknown period: {key}")


# ---------------------------------------------------------------- ZCode 扫描
def load_provider_names() -> dict:
    """provider_id -> 显示名（读 ZCode 自有配置，只读，不记录任何凭据）。"""
    names = {}
    try:
        cfg = json.loads(ZCODE_PROVIDER_CONFIG.read_text(encoding="utf-8"))
        rules = cfg.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
        for r in rules:
            pid = r.get("providerId")
            if pid:
                names[pid] = r.get("providerName") or pid
    except Exception:
        pass
    return names


def load_provider_keys() -> dict:
    """provider_id -> {key, baseUrl, templateId}。仅内存中使用，禁止写入任何文件/日志。"""
    keys = {}
    try:
        cfg = json.loads(ZCODE_PROVIDER_CONFIG.read_text(encoding="utf-8"))
        rules = cfg.get("config", {}).get("providerConfigRules", {}).get("providerRules", [])
        for r in rules:
            pid = r.get("providerId")
            key = (r.get("config", {}).get("access", {}) or {}).get("apiKey")
            base = (r.get("config", {}).get("api", {}) or {}).get("baseUrl", "")
            tmpl = r.get("templateId") or ""
            if pid and key:
                keys[pid] = {"key": key, "baseUrl": base, "templateId": tmpl}
    except Exception:
        pass
    return keys


def _zcode_db_daily():
    """从源库按 本地日×provider×model 直接聚合（只读）。
    返回 (daily: {day: {"pid|model": metrics}}, db_min_day, db_row_count)。"""
    if not ZCODE_DB.exists():
        return {}, None, 0
    off = tz_offset_hours()
    con = sqlite3.connect(f"file:{ZCODE_DB.as_posix()}?mode=ro", uri=True)
    try:
        sql = f"""
            SELECT date(started_at/1000,'unixepoch','{off:+d} hours') AS day,
                   provider_id, model_id,
                   COUNT(*),
                   COALESCE(SUM(input_tokens),0),
                   COALESCE(SUM(cache_read_input_tokens),0),
                   COALESCE(SUM(output_tokens),0),
                   COALESCE(SUM(reasoning_tokens),0),
                   COALESCE(SUM(computed_total_tokens),0),
                   COALESCE(SUM(CASE WHEN status!='completed' THEN 1 ELSE 0 END),0)
            FROM model_usage
            GROUP BY 1,2,3
        """
        rows = con.execute(sql).fetchall()
        min_ts, cnt = con.execute(
            "SELECT MIN(started_at), COUNT(*) FROM model_usage").fetchone()
    finally:
        con.close()
    daily = {}
    for r in rows:
        daily.setdefault(r[0], {})[f"{r[1]}|{r[2]}"] = {
            "provider_id": r[1], "model": r[2],
            "calls": r[3], "input": r[4], "cached": r[5], "output": r[6],
            "reasoning": r[7], "total": r[8], "errors": r[9],
        }
    db_min_day = None
    if min_ts:
        db_min_day = datetime.fromtimestamp(min_ts / 1000).astimezone().strftime("%Y-%m-%d")
    return daily, db_min_day, cnt


def zcode_daily_refresh(use_store: bool = True, save: bool = True):
    """刷新"按天聚合留存"（data/zcode_daily.json）。
    规则：源库完整覆盖的天 → 直接覆盖留存；源库最早那天（可能已被修剪掉一部分）
    → 若留存里已有更早抓到的值则保留旧值并标 partial；更老的天只存在于留存 → 原样保留。
    返回 (daily_map, info)。"""
    db_daily, db_min_day, db_rows = _zcode_db_daily()
    if not use_store:
        return db_daily, {"db_min_day": db_min_day, "db_rows": db_rows,
                          "partial_days": [db_min_day] if db_min_day else [],
                          "stored_days": sorted(db_daily.keys()), "store_used": False}

    store = {"days": {}, "partial_days": []}
    if ZCODE_DAILY.exists():
        try:
            store = json.loads(ZCODE_DAILY.read_text(encoding="utf-8"))
        except Exception:
            pass
    days = dict(store.get("days", {}))
    partial = set(store.get("partial_days", []))
    today_str = now_local().strftime("%Y-%m-%d")

    for day, entries in db_daily.items():
        if day == db_min_day and day != today_str:
            # 源库最早一天可能已被修剪：已有留存值时保留更早（更全）的抓取
            partial.add(day)
            if day in days:
                continue
        else:
            partial.discard(day)
        days[day] = entries

    if save:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"days": days, "partial_days": sorted(partial),
                       "updated_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}
            tmp = ZCODE_DAILY.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, ZCODE_DAILY)
        except Exception:
            pass
    info = {"db_min_day": db_min_day, "db_rows": db_rows,
            "partial_days": sorted(partial), "stored_days": sorted(days.keys()),
            "store_used": True}
    return days, info


def period_day_range(key: str):
    """时间段 → (start_day, end_day)，含头不含尾的 'YYYY-MM-DD' 字符串；None 表示不限。"""
    n = now_local()
    today0 = n.replace(hour=0, minute=0, second=0, microsecond=0)
    if key == "today":
        start, end = today0, None
    elif key == "yesterday":
        start, end = today0 - timedelta(days=1), today0
    elif key == "7d":
        start, end = today0 - timedelta(days=6), None
    elif key == "30d":
        start, end = today0 - timedelta(days=29), None
    elif key == "month":
        start, end = today0.replace(day=1), None
    elif key == "all":
        start, end = None, None
    else:
        raise ValueError(f"unknown period: {key}")
    return (start.strftime("%Y-%m-%d") if start else None,
            end.strftime("%Y-%m-%d") if end else None)


def zcode_period_rows(daily, start_day, end_day):
    """从按天聚合中取指定时间段的 provider×model 行。"""
    acc = {}
    for day, entries in daily.items():
        if start_day and day < start_day:
            continue
        if end_day and day >= end_day:
            continue
        for k, m in entries.items():
            a = acc.get(k)
            if a is None:
                a = acc[k] = {"provider_id": m["provider_id"], "model": m["model"],
                              "calls": 0, "input": 0, "cached": 0, "output": 0,
                              "reasoning": 0, "total": 0, "errors": 0}
            for f in ("calls", "input", "cached", "output", "reasoning", "total", "errors"):
                a[f] += m.get(f, 0)
    return list(acc.values())


# ---------------------------------------------------------------- Codex 扫描
def _parse_iso_ms(s: str):
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return None


def _codex_files():
    files = []
    for pat in ("sessions/**/*.jsonl", "archived_sessions/*.jsonl"):
        files.extend(glob.glob(str(CODEX_HOME / pat), recursive=True))
    return sorted(set(files))


def _parse_codex_file(path: str):
    """解析单个 rollout-*.jsonl。
    返回 (records, rate_limit_latest)：
      records 行: [ts_ms, model, input, cached_in, cache_write, output, reasoning, total, response_id]
      rate_limit_latest: 该文件内时间最新的 rate_limits 快照（无则 None）
    模型归属: 文件内时序上最近的前一个 turn_context.model。
    """
    records = []
    rl = None
    current_model = None
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if ('"token_usage_record"' not in line and '"token_count"' not in line
                    and '"turn_context"' not in line):
                continue
            try:
                obj = json.loads(line)
            except Exception:
                continue
            t = obj.get("type")
            payload = obj.get("payload") or {}
            ts = _parse_iso_ms(obj.get("timestamp", ""))
            if t == "turn_context":
                m = payload.get("model") or (
                    (payload.get("collaboration_mode") or {}).get("settings", {}) or {}
                ).get("model")
                if m:
                    current_model = m
            elif t == "token_usage_record":
                u = payload.get("usage") or {}
                records.append([
                    ts, current_model or "unknown",
                    u.get("input_tokens", 0) or 0,
                    u.get("cached_input_tokens", 0) or 0,
                    u.get("cache_write_input_tokens", 0) or 0,
                    u.get("output_tokens", 0) or 0,
                    u.get("reasoning_output_tokens", 0) or 0,
                    u.get("total_tokens", 0) or 0,
                    payload.get("response_id") or "",
                ])
            elif payload.get("type") == "token_count":
                r = payload.get("rate_limits")
                if r and ts:
                    prim = r.get("primary") or {}
                    # 实测存在 primary 为 null 的 token_count 事件（会话开头等），
                    # 这类快照没有额度信息，必须跳过，否则会顶掉有效快照。
                    if prim.get("used_percent") is None and prim.get("resets_at") is None:
                        continue
                    snap = {
                        "ts_ms": ts,
                        "used_percent": prim.get("used_percent"),
                        "window_minutes": prim.get("window_minutes"),
                        "resets_at": prim.get("resets_at"),
                        "plan_type": r.get("plan_type"),
                        "credits_balance": (r.get("credits") or {}).get("balance"),
                        "file": os.path.basename(path),
                    }
                    if rl is None or ts > rl["ts_ms"]:
                        rl = snap
    return records, rl


def scan_codex_raw(use_cache: bool = True):
    """扫描全部 Codex 会话文件（带按文件 mtime/size 的缓存，缓存仅提速，删掉不影响正确性）。
    返回 (records, rate_limit_latest, stats)。
    """
    files_cache = {}
    if use_cache and CODEX_CACHE.exists():
        try:
            raw = json.loads(CODEX_CACHE.read_text(encoding="utf-8"))
            if raw.get("version") == 2:  # v1 缓存含无效 rate_limits 快照，直接作废
                files_cache = raw.get("files", {})
        except Exception:
            files_cache = {}

    new_cache = {}
    all_records = []
    rl_latest = None
    parsed = reused = orphan = 0
    for path in _codex_files():
        try:
            st = os.stat(path)
        except OSError:
            continue
        key = path.replace("\\", "/")
        ent = files_cache.get(key)
        if ent and ent.get("size") == st.st_size and abs(ent.get("mtime", 0) - st.st_mtime) < 1e-6:
            records, rl = ent.get("records", []), ent.get("rate_limit")
            reused += 1
        else:
            records, rl = _parse_codex_file(path)
            parsed += 1
        new_cache[key] = {"size": st.st_size, "mtime": st.st_mtime,
                          "records": records, "rate_limit": rl}
        all_records.extend(records)
        if rl and (rl_latest is None or rl["ts_ms"] > rl_latest["ts_ms"]):
            rl_latest = rl

    # 源文件消失时保留其缓存条目（防 Codex 端清理导致历史丢失）；
    # 若文件只是被移动（sessions→archived），新旧两份由 response_id 去重兜底。
    live_keys = {p.replace("\\", "/") for p in _codex_files()}
    for key, ent in files_cache.items():
        if key in live_keys or key in new_cache:
            continue
        if ent.get("records") or ent.get("rate_limit"):
            ent = dict(ent)
            ent["orphan"] = True
            new_cache[key] = ent
            all_records.extend(ent.get("records", []))
            orphan += 1
            rl = ent.get("rate_limit")
            if rl and (rl_latest is None or rl["ts_ms"] > rl_latest["ts_ms"]):
                rl_latest = rl

    if use_cache:
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CODEX_CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 2, "files": new_cache}, ensure_ascii=False),
                           encoding="utf-8")
            os.replace(tmp, CODEX_CACHE)
        except Exception:
            pass
    stats = {"files": parsed + reused, "parsed": parsed, "reused": reused,
             "orphan_files": orphan, "records": len(all_records)}
    return all_records, rl_latest, stats


def aggregate_codex(records, start_ms: int, end_ms):
    """按 response_id 去重后按模型聚合（防 sessions/archived 重叠与孤儿缓存重复）。"""
    seen = set()
    agg = {}
    dup = 0
    for r in records:
        ts, model, rid = r[0], r[1], r[8]
        key = rid if rid else f"{r[0]}|{r[1]}|{r[2]}|{r[5]}|{r[7]}"
        if key in seen:
            dup += 1
            continue
        seen.add(key)
        if ts is None or ts < start_ms:
            continue
        if end_ms is not None and ts >= end_ms:
            continue
        a = agg.setdefault(model, {"calls": 0, "input": 0, "cached": 0, "cache_write": 0,
                                   "output": 0, "reasoning": 0, "total": 0})
        a["calls"] += 1
        a["input"] += r[2]
        a["cached"] += r[3]
        a["cache_write"] += r[4]
        a["output"] += r[5]
        a["reasoning"] += r[6]
        a["total"] += r[7]
    agg["_dups"] = dup
    return agg


# ---------------------------------------------------------------- 统一汇总
def build_summary(period: str, use_cache: bool = True):
    start_ms, end_ms = period_bounds(period)
    start_day, end_day = period_day_range(period)
    pnames = load_provider_names()
    rows = []
    daily, zinfo = zcode_daily_refresh(use_store=use_cache, save=use_cache)
    for r in zcode_period_rows(daily, start_day, end_day):
        rows.append({
            "source": "ZCode",
            "provider": pnames.get(r["provider_id"], r["provider_id"]),
            "provider_id": r["provider_id"],
            "model": r["model"], "calls": r["calls"], "input": r["input"],
            "cached": r["cached"], "output": r["output"], "reasoning": r["reasoning"],
            "total": r["total"], "errors": r["errors"],
        })
    records, rl, stats = scan_codex_raw(use_cache=use_cache)
    agg = aggregate_codex(records, start_ms, end_ms)
    dups = agg.pop("_dups", 0)
    for model, a in sorted(agg.items(), key=lambda kv: -kv[1]["calls"]):
        rows.append({
            "source": "Codex", "provider": "OpenAI 订阅 (Codex)", "provider_id": "codex",
            "model": model, "calls": a["calls"], "input": a["input"],
            "cached": a["cached"], "output": a["output"], "reasoning": a["reasoning"],
            "total": a["total"], "errors": 0,
        })
    meta = {"period": period, "generated_at": now_local().strftime("%Y-%m-%d %H:%M:%S"),
            "codex_stats": stats, "codex_dedup_dropped": dups, "codex_rate_limit": rl,
            "zcode_info": zinfo}
    return rows, meta


# ---------------------------------------------------------------- 费用估算
def load_prices() -> dict:
    """prices.json 结构：
    {"usd_cny": 7.1, "models": {"模型名": {"input": USD/百万, "cached": ..., "output": ...}}}
    官方价目为 USD；¥ 显示 = USD × usd_cny（汇率可编辑，页面标注估算口径）。"""
    if PRICES_FILE.exists():
        try:
            return json.loads(PRICES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def estimate_cost(rows, prices: dict):
    """按价目表估算费用，仅对 prices.models 中存在的模型计算，其余不猜测。
    返回 ({(source, model): ¥}, 口径说明 or None)。
    口径: input 含 cached（ZCode/Codex 均如此）；cached 部分按缓存命中价。
    注意: DeepSeek 官方高峰时段(工作日 UTC 01-04/06-10)价格为 2 倍，估算统一按非高峰价。"""
    models = (prices or {}).get("models", {})
    usd_cny = (prices or {}).get("usd_cny") or 7.1
    lower = {k.lower(): v for k, v in models.items()}
    out = {}
    hit = False
    for r in rows:
        p = lower.get(r["model"].lower())
        if not p:
            continue
        hit = True
        fresh_in = max(r["input"] - r["cached"], 0)
        usd = (fresh_in * p.get("input", 0) + r["cached"] * p.get("cached", p.get("input", 0))
               + r["output"] * p.get("output", 0)) / 1_000_000
        key = (r["source"], r["model"])
        out[key] = out.get(key, 0.0) + usd * usd_cny
    note = None
    if hit:
        note = (f"估算口径：官方 USD 价目（非高峰）× 汇率 {usd_cny}（prices.json 可编辑）；"
                f"高峰时段实际最高 2 倍；真实扣费以服务商账单为准")
    return out, note


# ---------------------------------------------------------------- 额度/余额适配器
# 原则：key 运行时读自 ZCode provider_config.json，仅内存中使用；
# 缓存文件只存归一化后的数字与时间戳，绝不存 key/原始响应头。
import urllib.request
import urllib.error
from concurrent.futures import ThreadPoolExecutor, as_completed

QUOTA_TTL_SECONDS = 15 * 60  # 页面加载时缓存新鲜度；手动刷新忽略 TTL


def _http_json(url: str, headers: dict, timeout: float = 8.0, method: str = "GET", body=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _err(e: Exception) -> str:
    """异常 → 安全短文本（不携带任何凭据；HTTPError 只取状态码）。"""
    if isinstance(e, urllib.error.HTTPError):
        try:
            detail = json.loads(e.read().decode("utf-8", errors="replace"))
            msg = str(detail.get("msg") or detail.get("message") or detail.get("error") or "")[:60]
        except Exception:
            msg = ""
        return f"HTTP {e.code}" + (f"：{msg}" if msg else "")
    return type(e).__name__


def _to_epoch(v):
    """任意时间表示 → epoch 秒；无法识别返回 None。
    实测各家格式：GLM/Codex=epoch(秒或毫秒)、MiniMax=epoch毫秒、Kimi=ISO字符串。"""
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


def _num(v):
    """各家数字口径不一（Kimi 返回字符串数字），统一转 float；失败返回 None。"""
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _fail(reason: str):
    return {"ok": False, "error": reason,
            "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}


def _glm_quota(key: str, host: str, label: str):
    """智谱系 coding plan 额度。实测(2026-09-25)：裸 key 即可，HTTP 200 + code=200。
    TOKENS_LIMIT unit=3 → 5小时、unit=6 → 周；TIME_LIMIT unit=5 → 工具调用
    （usage=总额, currentValue=已用, remaining=剩余，usageDetails 为工具明细）。
    已知坑：坏 key 返回 HTTP 200 + body code!=0，判错必须看 body。"""
    url = f"{host}/api/monitor/usage/quota/limit"
    last = None
    for auth in (key, f"Bearer {key}"):
        try:
            j = _http_json(url, {"Authorization": auth})
        except Exception as e:
            last = _err(e)
            continue
        code = j.get("code", j.get("status"))
        if code not in (0, 200, None):
            last = f"业务码 {code}：{str(j.get('msg') or j.get('message') or '')[:60]}"
            continue
        data = j.get("data") or {}
        raw_limits = data.get("limits") or []
        if not raw_limits:
            last = "响应中无 limits 条目"
            continue
        windows = []
        tokens = [x for x in raw_limits if x.get("type") == "TOKENS_LIMIT"]
        srt = sorted(tokens, key=lambda x: x.get("nextResetTime") or 0)
        for x in tokens:
            u = x.get("unit")
            if u == 3:
                lab = "5小时"
            elif u == 6:
                lab = "周"
            elif len(srt) >= 2 and x is srt[0]:
                lab = "5小时"
            elif len(srt) >= 2 and x is srt[-1]:
                lab = "周"
            else:
                lab = f"窗口(unit={u})"
            windows.append({"label": lab, "used_percent": _num(x.get("percentage")),
                            "used": None, "quota": None,
                            "remaining": _num(x.get("remaining")),
                            "reset_at": _to_epoch(x.get("nextResetTime"))})
        for x in raw_limits:
            if x.get("type") == "TIME_LIMIT":
                windows.append({"label": "MCP 每月额度", "used_percent": _num(x.get("percentage")),
                                "used": _num(x.get("currentValue")), "quota": _num(x.get("usage")),
                                "remaining": _num(x.get("remaining")), "unit": "次",
                                "reset_at": _to_epoch(x.get("nextResetTime"))})
        if not windows:
            last = "响应中无可用额度条目"
            continue
        return {"ok": True, "kind": "quota", "plan": data.get("level"),
                "windows": windows,
                "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}
    return _fail(last or "未知错误")


def _bigmodel_report(key: str):
    """智谱开放平台账户报告。实测(2026-09-25)：/api/paas/v4/balance 已 404；
    /api/biz/account/query-customer-account-report + 裸 key 可用。"""
    url = "https://open.bigmodel.cn/api/biz/account/query-customer-account-report"
    try:
        j = _http_json(url, {"Authorization": key})
    except Exception as e:
        return _fail(_err(e))
    if j.get("code") not in (0, 200, None) or not j.get("data"):
        return _fail(f"业务码 {j.get('code')}：{str(j.get('msg') or '')[:60]}")
    d = j["data"]
    return {"ok": True, "kind": "balance", "currency": "CNY",
            "available": _num(d.get("availableBalance")),
            "detail": {"累计充值": _num(d.get("rechargeAmount")),
                       "累计消费": _num(d.get("totalSpendAmount")),
                       "赠送余额": _num(d.get("giveAmount"))},
            "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}


def _balance_generic(key: str, url: str, extract, name: str):
    try:
        j = _http_json(url, {"Authorization": f"Bearer {key}"})
    except Exception as e:
        return _fail(_err(e))
    try:
        out = extract(j)
    except Exception as e:
        return _fail(f"响应结构异常：{type(e).__name__}")
    if out is None:
        return _fail("响应中未找到余额字段")
    out.update({"ok": True, "kind": "balance",
                "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")})
    return out


def _ali_coding_plan(key: str, cookie: str = None):
    """阿里百炼 Coding/Token Plan 额度（控制台 RPC，社区双源验证）。
    实测(2026-09-25)：API key（含 token-plan 型 sk-sp-*）一律 ConsoleNeedLogin；
    仅控制台 Cookie 会话可用 → Cookie 为可选启用项（data/extra_keys.json）。"""
    url = ("https://bailian.console.aliyun.com/data/api.json"
           "?action=zeldaEasy.broadscope-bailian.codingPlan.queryCodingPlanInstanceInfoV2"
           "&product=broadscope-bailian&api=queryCodingPlanInstanceInfoV2&currentRegionId=cn-beijing")
    if cookie:
        headers = {"Cookie": cookie, "Content-Type": "application/json"}
    else:
        headers = {"Authorization": f"Bearer {key}", "x-api-key": key,
                   "X-DashScope-API-Key": key, "Content-Type": "application/json"}
    body = {"queryCodingPlanInstanceInfoRequest": {"commodityCode": "sfm_codingplan_public_cn"}}
    try:
        j = _http_json(url, headers, method="POST", body=body)
    except Exception as e:
        return _fail(_err(e))
    s = json.dumps(j, ensure_ascii=False)
    if "ConsoleNeedLogin" in s:
        return _fail("不可获得：需百炼控制台登录态；将控制台 Cookie 填入 "
                     "data\\extra_keys.json 的 ali_cookie 可启用（Cookie 会过期）")
    try:
        infos = (j.get("data") or j).get("codingPlanInstanceInfos") or []
        q = (infos[0] or {}).get("codingPlanQuotaInfo") or {}
        windows = []
        for pre, lab in (("per5Hour", "5小时"), ("perWeek", "周"), ("perBillMonth", "月")):
            tot, used = q.get(pre + "TotalQuota"), q.get(pre + "UsedQuota")
            reset = q.get(pre + "QuotaNextRefreshTime")
            if tot is None and used is None and reset is None:
                continue
            pct = round(used / tot * 100, 1) if (tot and used is not None) else None
            windows.append({"label": lab, "used_percent": pct, "used": used, "quota": tot,
                            "remaining": (tot - used) if (tot is not None and used is not None) else None,
                            "reset_at": (reset / 1000 if reset and reset > 10**12 else reset),
                            "unit": "次"})
        if not windows:
            return _fail("响应中无套餐额度字段")
        return {"ok": True, "kind": "quota", "plan": (infos[0] or {}).get("planName"),
                "windows": windows,
                "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return _fail(f"响应结构异常：{type(e).__name__}")


def _kimi_coding(key: str):
    """Kimi For Coding。实测(2026-09-25)：官方三指标中 key 接口只有两个——
    usages.limit_5h / usages.limit_7d（used_ratio 0-1，与控制台 56.28% 精确吻合）；
    "总使用量(Kimi+Code 合计)"为网页会话聚合，coding API 无端点（已穷举 404）。"""
    try:
        j = _http_json("https://api.kimi.com/coding/v1/usages",
                       {"Authorization": f"Bearer {key}"})
    except Exception as e:
        return _fail(_err(e))
    try:
        us = j.get("usages") or {}
        windows = []
        for lab, kk in (("本 key · 5小时", "limit_5h"), ("本 key · 7天", "limit_7d")):
            u = us.get(kk) or {}
            ratio, rt = u.get("used_ratio"), u.get("reset_time")
            if ratio is None and rt is None:
                continue
            pct = ratio * 100 if (ratio is not None and ratio <= 1) else ratio
            windows.append({"label": lab,
                            "used_percent": round(pct, 2) if pct is not None else None,
                            "used": None, "quota": None, "remaining": None,
                            "reset_at": _to_epoch(rt), "unit": ""})
        if not windows:
            return _fail("响应中无 usages.limit_5h/7d 字段")
        return {"ok": True, "kind": "quota", "plan": "Kimi For Coding",
                "windows": windows,
                "note": "总使用量(Kimi+Code 合计)：官方无 key 接口",
                "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}
    except Exception as e:
        return _fail(f"响应结构异常：{type(e).__name__}")


def _minimax_remains(key: str):
    """MiniMax 编程套餐。实测(2026-09-25)：api.minimax.cn 两个路径均可用，
    数字为百分比剩余（remaining_percent），时间为 epoch 毫秒。"""
    hosts = ["https://api.minimax.cn", "https://api.minimaxi.com"]
    paths = ["/v1/api/openplatform/coding_plan/remains", "/v1/token_plan/remains"]
    last = None
    for h in hosts:
        for p in paths:
            try:
                j = _http_json(h + p, {"Authorization": f"Bearer {key}"})
            except Exception as e:
                last = _err(e)
                continue
            br = j.get("base_resp") or {}
            if br.get("status_code") not in (0, None):
                last = f"业务码 {br.get('status_code')}：{str(br.get('status_msg') or '')[:60]}"
                continue
            remains = j.get("model_remains") or []
            gen = next((x for x in remains if x.get("model_name") == "general"),
                       remains[0] if remains else {})
            windows = []
            ip = _num(gen.get("current_interval_remaining_percent"))
            wp = _num(gen.get("current_weekly_remaining_percent"))
            if ip is not None:
                windows.append({"label": "5小时", "used_percent": round(100 - ip, 1),
                                "remaining_percent": ip,
                                "reset_at": _to_epoch(gen.get("end_time"))})
            if wp is not None and gen.get("current_weekly_status") == 1:
                windows.append({"label": "周", "used_percent": round(100 - wp, 1),
                                "remaining_percent": wp,
                                "reset_at": _to_epoch(gen.get("weekly_end_time"))})
            if not windows:
                last = "响应中无剩余百分比字段"
                continue
            return {"ok": True, "kind": "quota", "plan": "MiniMax 编程套餐",
                    "windows": windows,
                    "fetched_at": now_local().strftime("%Y-%m-%d %H:%M:%S")}
    return _fail(last or "未知错误")


def load_extra_keys() -> dict:
    """可选启用项 data/extra_keys.json（用户自愿粘贴的明文凭据，仅内存使用）：
    {"glm_individual": "<智谱个人套餐明文API Key>", "ali_cookie": "<百炼控制台Cookie>"}"""
    if EXTRA_KEYS.exists():
        try:
            return json.loads(EXTRA_KEYS.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------- 通用中继适配器
# 未来加一个 new-api 系中转 = 在 data/custom_providers.json 加一段 JSON，零代码。
CUSTOM_PROVIDERS = DATA_DIR / "custom_providers.json"
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def load_custom_providers() -> list:
    if CUSTOM_PROVIDERS.exists():
        try:
            return json.loads(CUSTOM_PROVIDERS.read_text(encoding="utf-8")) or []
        except Exception:
            return []
    return []


def _resolve_key(ks):
    """key_source → 明文 key（仅内存）。三种来源：zcode_provider / extra_keys / sqlite_cell。"""
    if not isinstance(ks, dict):
        return None
    t = ks.get("type")
    try:
        if t == "zcode_provider":
            return (load_provider_keys().get(ks.get("id")) or {}).get("key")
        if t == "extra_keys":
            return load_extra_keys().get(ks.get("field"))
        if t == "sqlite_cell":
            con = sqlite3.connect(f"file:{ks.get('path')}?mode=ro", uri=True)
            try:
                row = con.execute(ks.get("sql", "")).fetchone()
            finally:
                con.close()
            return row[0] if row else None
    except Exception:
        return None
    return None


def extra_keys_cookie(pid):
    """extra_keys.json 里粘贴的站点 Cookie 请求头原文（<id>_cookie 字段）。"""
    return load_extra_keys().get(f"{pid}_cookie") or None


def _newapi_self_by_cookie(base: str, cookie: str):
    """用日常浏览器的 Cookie 头直调 /api/user/self → 钱包余额 $（quota/500000）。"""
    try:
        j = _http_json(base + "/api/user/self",
                       {"Cookie": cookie, "User-Agent": BROWSER_UA})
    except Exception:
        return None
    d = j.get("data") or {}
    u = d.get("user") or d
    q = _num(u.get("quota"))
    return None if q is None else round(q / 500000, 2)


def _newapi_site_balance(base: str, user: str, pw: str):
    """new-api 系站点钱包余额（账户维度）：登录 → /api/user/self → quota/500000=$。
    失败返回 None（如实降级为 key 维度）。"""
    import http.cookiejar
    import urllib.request
    cj = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    try:
        req = urllib.request.Request(
            base + "/api/user/login",
            data=json.dumps({"username": user, "password": pw}).encode(),
            headers={"Content-Type": "application/json", "User-Agent": BROWSER_UA},
            method="POST")
        j = json.loads(op.open(req, timeout=10).read().decode("utf-8", errors="replace"))
        if not (j.get("success") or str(j.get("message") or "") in ("", "success")):
            return None
        req2 = urllib.request.Request(base + "/api/user/self",
                                      headers={"User-Agent": BROWSER_UA})
        j2 = json.loads(op.open(req2, timeout=10).read().decode("utf-8", errors="replace"))
        d = j2.get("data") or {}
        u = d.get("user") or d
        q = _num(u.get("quota"))
        return None if q is None else q / 500000
    except Exception:
        return None


def _newapi_billing(entry: dict):
    """new-api 系中转：subscription 给 key 剩余配额(USD)，usage 给本月消费(美分→USD)；
    若 extra_keys 配了站点账号，则加钱包余额（账户维度）。"""
    key = _resolve_key(entry.get("key_source"))
    if not key:
        return _fail(f"取不到 key（key_source={entry.get('key_source')}）")
    h = {"Authorization": f"Bearer {key}"}
    if entry.get("needs_browser_ua"):
        h["User-Agent"] = BROWSER_UA
    base = (entry.get("base_url") or "").rstrip("/")
    try:
        sub = _http_json(base + "/v1/dashboard/billing/subscription", h)
    except Exception as e:
        return _fail(_err(e))
    hard = _num(sub.get("hard_limit_usd"))
    n = now_local()
    start = n.replace(day=1)
    spend = None
    try:
        use = _http_json(base + "/v1/dashboard/billing/usage"
                         f"?start_date={start:%Y-%m-%d}&end_date={n:%Y-%m-%d}", h)
        spend = round((_num(use.get("total_usage")) or 0) / 100, 2)
    except Exception:
        pass
    unlimited = hard is not None and hard >= 1e8
    wallet = None
    # 1) 站点会话钱包（console_quota.py --login <id> 导出会话后 --fetch 写入）
    qf = DATA_DIR / f'{entry.get("id")}_quota.json'
    if qf.exists():
        try:
            k = json.loads(qf.read_text(encoding="utf-8"))
            if k.get("kind") == "relay-site" and k.get("wallet_usd") is not None:
                ft = datetime.strptime(k["fetched_at"], "%Y-%m-%d %H:%M:%S")
                if (now_local().replace(tzinfo=None) - ft).total_seconds() < 12 * 3600:
                    wallet = k["wallet_usd"]
        except Exception:
            pass
    # 2) extra_keys 里粘贴的站点 Cookie 头（日常浏览器过 CF 的会话，httpOnly 也能带）
    if wallet is None:
        ck = extra_keys_cookie(entry.get("id"))
        if ck:
            wallet = _newapi_self_by_cookie(base, ck)
    # 3) 兜底：extra_keys 站点账号密码登录
    if wallet is None:
        ex = load_extra_keys()
        su, sp = ex.get(f'{entry.get("id")}_user'), ex.get(f'{entry.get("id")}_pass')
        if su and sp:
            wallet = _newapi_site_balance(base, su, sp)
    return {"ok": True, "kind": "relay", "unlimited": unlimited,
            "remaining_usd": None if unlimited else hard,
            "month_spend_usd": spend, "wallet_usd": wallet,
            "fetched_at": n.strftime("%Y-%m-%d %H:%M:%S")}


def _quota_job_registry():
    """返回 {卡片名: 无参 callable} —— 凭据运行时取，仅内存使用。"""
    pkeys = load_provider_keys()
    extra = load_extra_keys()

    def _match_provider(m):
        """按 templateId / baseUrl 子串 / providerId 匹配 ZCode 里的 provider（跨用户可移植）。"""
        for pid, ent in pkeys.items():
            if pid in m.get("providerIds", []):
                return ent
            if ent.get("templateId") in m.get("templateIds", []):
                return ent
            if any(s in (ent.get("baseUrl") or "") for s in m.get("baseUrlHas", [])):
                return ent
        return None

    def via(m, fn, label=""):
        def job():
            ent = _match_provider(m)
            if not ent:
                return _fail(f"未在 ZCode provider 配置中匹配到 {label}")
            return fn(ent["key"])
        return job

    def console_file(fname, section=None, target="ali"):
        """读 console_quota.py 的会话结果（12h 内有效），可选取其中一段。"""
        def job():
            f = DATA_DIR / fname
            if not f.exists():
                return _fail(f"无会话结果：运行 py scripts/console_quota.py --login {target}")
            try:
                j = json.loads(f.read_text(encoding="utf-8"))
            except Exception:
                return _fail("会话结果文件损坏")
            ft = j.get("fetched_at")
            if ft:
                try:
                    age = (now_local().replace(tzinfo=None) -
                           datetime.strptime(ft, "%Y-%m-%d %H:%M:%S")).total_seconds()
                    if age > 12 * 3600:
                        return _fail(f"会话结果超 12h：运行 py scripts/console_quota.py --fetch {target}")
                except Exception:
                    pass
            if section:
                s = j.get(section)
                return s if s else _fail(f"会话结果中无 {section} 段")
            return j if j.get("ok") else _fail("会话结果无效")
        return job

    def kimi_job():
        ent = _match_provider({"templateIds": ["moonshot-kimi"],
                               "baseUrlHas": ["api.kimi.com", "api.moonshot.cn"]})
        r = _kimi_coding(ent["key"]) if ent else _fail("未在 ZCode provider 配置中匹配到 Kimi")

        def _sb(x):
            if isinstance(x, dict):
                if isinstance(x.get("subscriptionBalance"), dict):
                    return x["subscriptionBalance"]
                for v in x.values():
                    g = _sb(v)
                    if g:
                        return g
            elif isinstance(x, list):
                for v in x:
                    g = _sb(v)
                    if g:
                        return g
            return None

        def _append_total(sb):
            ratio = sb.get("amountUsedRatio")
            if ratio is None:
                return False
            pct = ratio * 100 if ratio <= 1 else ratio
            r["windows"].append({"label": "总(Kimi+Code)", "used_percent": round(pct, 2),
                                 "used": None, "quota": None, "remaining": None,
                                 "reset_at": _to_epoch(sb.get("expireTime")), "unit": ""})
            r["note"] = None
            return True

        # 通道1：extra_keys.kimi_web_token（kimi.com/code/console LocalStorage 的 access_token）
        tok = extra.get("kimi_web_token")
        if tok and r.get("ok"):
            try:
                j = _http_json("https://www.kimi.com/apiv2/kimi.gateway.membership.v2."
                               "MembershipService/GetSubscriptionStats",
                               {"Authorization": f"Bearer {tok}",
                                "Content-Type": "application/json"},
                               method="POST", body={})
                sb = _sb(j)
                if sb and _append_total(sb):
                    return r
            except Exception:
                pass
        # 通道2：console_quota.py --login kimi 的会话捕获结果
        kj = DATA_DIR / "kimi_quota.json"
        if r.get("ok") and kj.exists():
            try:
                k = json.loads(kj.read_text(encoding="utf-8"))
                ft = k.get("fetched_at")
                fresh = True
                if ft:
                    fresh = (now_local().replace(tzinfo=None) -
                             datetime.strptime(ft, "%Y-%m-%d %H:%M:%S")).total_seconds() < 12 * 3600
                if k.get("ok") and k.get("window") and fresh:
                    cap = (k.get("fetched_at") or "")[-5:]  # 会话捕获时刻 HH:MM
                    w = dict(k["window"])
                    w["label"] = f'订阅总量 · 会话{cap}' if cap else "订阅总量(Kimi+Code)"
                    r["windows"].append(w)
                    r["note"] = None
            except Exception:
                pass
        return r

    jobs = {
        "GLM (9.22)": via({"providerIds": ["glm-v2max"], "baseUrlHas": ["api.z.ai"]},
                          lambda k: _glm_quota(k, "https://api.z.ai", "z.ai"), "GLM coding (api.z.ai)"),
        "GLM 官方 (BigModel Coding Max)": console_file("glm_quota.json", target="glm"),
        "阿里 Coding Plan": console_file("ali_quota.json", "coding", "ali"),
        "阿里 Token Plan": console_file("ali_quota.json", "token", "ali"),
        "Kimi": kimi_job,
        "MiniMax": via({"templateIds": ["minimax"], "baseUrlHas": ["api.minimax"]},
                       _minimax_remains, "MiniMax"),
        "DeepSeek": via({"templateIds": ["deepseek"], "baseUrlHas": ["api.deepseek.com"]},
                        lambda k: _balance_generic(
                            k, "https://api.deepseek.com/user/balance",
                            lambda j: (lambda b: {"currency": b.get("currency", "CNY"),
                                                  "available": float(b.get("total_balance", 0)),
                                                  "detail": {"充值余额": b.get("topped_up_balance"),
                                                             "赠送余额": b.get("granted_balance")}})(
                                next((x for x in (j.get("balance_infos") or []) if x.get("currency") == "CNY"),
                                     (j.get("balance_infos") or [None])[0])) if j.get("balance_infos") else None,
                            "deepseek"), "DeepSeek"),
        "StepFun (阶跃星辰)": via({"templateIds": ["stepfun"], "baseUrlHas": ["stepfun.com"]},
                                 lambda k: _balance_generic(
                                     k, "https://api.stepfun.com/v1/accounts",
                                     lambda j: {"currency": "CNY", "available": float(j.get("balance", 0)),
                                                "detail": {"累计赠送": j.get("total_voucher_balance")}},
                                     "stepfun"), "StepFun"),
        "智谱钱包": via({"providerIds": ["bigmodel-open"], "baseUrlHas": ["open.bigmodel.cn"]},
                       _bigmodel_report, "BigModel 按量账户"),
    }
    for entry in load_custom_providers():
        if entry.get("type") == "newapi_billing":
            label = entry.get("label") or entry.get("id")
            jobs[label] = (lambda e: (lambda: _newapi_billing(e)))(entry)
    return jobs


# 本地/静态说明（页脚用）
STATIC_QUOTA_NOTES = {
    "Codex 周额度": "读自本地会话文件 rate_limits 快照（无需 API）",
    "Gemini (AI Studio)": "额度不可获得：无公开用量/余额 API",
}


def fetch_quotas(refresh: bool = False):
    """并发查询全部额度卡片；结果缓存 data/quota_cache.json（只存归一化数字+时间）。"""
    cache = {}
    if QUOTA_CACHE.exists():
        try:
            cache = json.loads(QUOTA_CACHE.read_text(encoding="utf-8"))
        except Exception:
            cache = {}
    jobs = _quota_job_registry()
    ALWAYS_FRESH = {"阿里 Coding Plan", "阿里 Token Plan",
                    "GLM 官方 (BigModel Coding Max)"}  # 只读本地会话结果，不吃缓存
    # 剪掉注册表已不存在的旧卡片名缓存（防止改名后旧卡残留在页面）
    cache = {k: v for k, v in cache.items() if k in jobs}

    def apply_fresh(results):
        for n in ALWAYS_FRESH:
            if n in jobs:
                try:
                    results[n] = jobs[n]()
                except Exception as e:
                    results[n] = _fail(f"适配器异常：{type(e).__name__}")
        return results

    if not refresh and cache:
        newest = max((v.get("fetched_at", "") for v in cache.values() if isinstance(v, dict)),
                     default="")
        try:
            age = (now_local() - datetime.strptime(newest, "%Y-%m-%d %H:%M:%S")
                   .replace(tzinfo=now_local().tzinfo)).total_seconds()
        except Exception:
            age = 10**9
        if age < QUOTA_TTL_SECONDS and set(jobs) <= set(cache):
            return apply_fresh(dict(cache))

    results = dict(cache)  # 保留旧值作底，逐项覆盖

    def run(name, fn):
        try:
            return name, fn()
        except Exception as e:  # 适配器未兜底的异常也如实显示，不静默吞掉
            return name, _fail(f"适配器异常：{type(e).__name__}")

    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = [ex.submit(run, n, fn) for n, fn in jobs.items() if n not in ALWAYS_FRESH]
        for fu in as_completed(futs):
            try:
                name, res = fu.result()
                results[name] = res
            except Exception:
                pass
    apply_fresh(results)

    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = QUOTA_CACHE.with_suffix(".tmp")
        tmp.write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, QUOTA_CACHE)
    except Exception:
        pass
    return results


# ---------------------------------------------------------------- 页面渲染
FALLBACK_PROVIDER_NAMES = {
    "account:bigmodel-individual-coding-plan": "GLM 编程套餐(个人)",
    "account:bigmodel-start-plan": "GLM 入门套餐",
    "account:bigmodel-offpeak-idle-plan": "GLM  offpeak 套餐",
    "account:zai-start-plan": "Z.ai 入门套餐",
    "builtin:zai-start-plan": "Z.ai 入门套餐(内置)",
    "new-provider-2": "new-provider-2(配置已移除)",
    "codex": "OpenAI 订阅 (Codex)",
}


def provider_display(pid: str, pnames: dict) -> str:
    return pnames.get(pid) or FALLBACK_PROVIDER_NAMES.get(pid) or pid


def collect_all(refresh_quotas: bool = False):
    """一次性采集全部数据：ZCode 留存刷新 + Codex 扫描 + 六个时段聚合 + 额度查询。"""
    daily, zinfo = zcode_daily_refresh(use_store=True, save=True)
    records, rl, cstats = scan_codex_raw(use_cache=True)
    pnames = load_provider_names()
    prices = load_prices()
    periods_data = {}
    for p in PERIODS:
        s_ms, e_ms = period_bounds(p)
        s_d, e_d = period_day_range(p)
        rows = []
        for r in zcode_period_rows(daily, s_d, e_d):
            rows.append({"source": "ZCode",
                         "provider": provider_display(r["provider_id"], pnames),
                         "model": r["model"], **{k: r[k] for k in
                         ("calls", "input", "cached", "output", "reasoning", "total", "errors")}})
        agg = aggregate_codex(records, s_ms, e_ms)
        agg.pop("_dups", None)
        for model, a in agg.items():
            rows.append({"source": "Codex", "provider": FALLBACK_PROVIDER_NAMES["codex"],
                         "model": model, "calls": a["calls"], "input": a["input"],
                         "cached": a["cached"], "output": a["output"],
                         "reasoning": a["reasoning"], "total": a["total"], "errors": 0})
        rows.sort(key=lambda x: -x["calls"])
        costs, note = estimate_cost(rows, prices)
        periods_data[p] = {
            "rows": rows,
            "costs": {f"{k[0]}|{k[1]}": v for k, v in costs.items()},
            "cost_note": note,
            "totals": {k: sum(r[k] for r in rows)
                       for k in ("calls", "input", "cached", "output", "reasoning", "total", "errors")},
        }
    quotas = fetch_quotas(refresh=refresh_quotas)
    return periods_data, quotas, rl, zinfo, cstats


def _countdown(epoch_s) -> str:
    """只给倒计时（菲戈 2026-09-25 要求），不给绝对时间。"""
    if not epoch_s:
        return ""
    delta = datetime.fromtimestamp(epoch_s).astimezone() - now_local()
    secs = int(delta.total_seconds())
    if secs <= 0:
        return "待重置"
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return f"{d}天{h}小时"
    if h:
        return f"{h}小时{m}分"
    return f"{m}分钟"


def _window_remaining(w) -> float:
    """窗口 → 剩余百分比（统一口径）。"""
    if w.get("remaining_percent") is not None:
        return float(w["remaining_percent"])
    if w.get("used_percent") is not None:
        return 100.0 - float(w["used_percent"])
    if w.get("remaining") is not None and w.get("quota"):
        return float(w["remaining"]) / float(w["quota"]) * 100
    return None


# 窗口 → band 映射（provider×窗口标签），用于卡片高亮与分组
WINDOW_BANDS = {
    ("GLM (9.22)", "5小时"): "night",
    ("GLM (9.22)", "周"): "offpeak",
    ("GLM (9.22)", "MCP 每月额度"): "daily",
    ("GLM 官方 (BigModel Coding Max)", "5小时"): "night",
    ("GLM 官方 (BigModel Coding Max)", "周"): "offpeak",
    ("GLM 官方 (BigModel Coding Max)", "MCP 每月额度"): "daily",
    ("阿里 Coding Plan", "5小时"): "night",
    ("阿里 Coding Plan", "周"): "offpeak",
    ("阿里 Coding Plan", "月"): "daily",
    ("阿里 Token Plan", "月"): "daily",
    ("Kimi", "本 key · 5小时"): "daily",
    ("Kimi", "本 key · 7天"): "daily",
    ("Kimi", "订阅总量"): "daily",
    ("MiniMax", "5小时"): "daily",
    ("MiniMax", "周"): "daily",
}


def _active_bands(pol: dict, now=None) -> list:
    """返回当前生效的 band 名列表（去重保序）。"""
    now = now or now_local()
    bands = []
    for band, _ in BANDS:
        for it in pol.get("items") or []:
            if (it.get("band") or "daily") != band:
                continue
            if _rule_active(it.get("active_rule"), now) is True:
                if band not in bands:
                    bands.append(band)
                break
    return bands


def _next_transition(now=None) -> str:
    """下一时段切换点文案（本地时间，含倒计时）。"""
    now = now or now_local()
    wd, hm = now.weekday(), now.strftime("%H:%M")
    transitions = [
        ("09:00", "peak" if wd < 5 else None), ("12:00", "offpeak" if wd < 5 else None),
        ("14:00", "peak" if wd < 5 else None), ("18:00", "offpeak" if wd < 5 else None),
        ("23:00", "night"), ("09:00", None),
    ]
    # 当天内找下一次；否则取明天 09:00
    for h, _ in transitions:
        if h > hm:
            tdelta_h = int(h.split(":")[0]) - int(hm.split(":")[0])
            tdelta_m = int(h.split(":")[1]) - int(hm.split(":")[1])
            if tdelta_m < 0:
                tdelta_h -= 1
                tdelta_m += 60
            return f"{h}（{tdelta_h}小时{tdelta_m}分后）"
    return "明天 09:00"


def _quota_band_label(window_label: str) -> str:
    """把窗口标签映射到简短 band 徽标文本；没有映射返回空串。"""
    m = {"night": "夜间", "peak": "高峰", "offpeak": "非高峰", "campaign": "限时", "daily": "日常"}
    return m.get(window_label, "")


def _quota_cards_html(quotas: dict, rl, pol: dict) -> str:
    """额度卡片网格（v5 恢复 v3 确认样式）：
    每家一张小卡，卡头=渠道名+主剩余值并排，多窗口逐行紧凑排列。
    只加数据时间脚注，不做活跃分组/高亮/band 徽标（那些在顶部横幅统一展示）。"""
    cards = []

    def wrap(name: str, head_value: str, body: str = "", dim: bool = False) -> str:
        cls = " card-dim" if dim else ""
        head = (f'<span class="ms-auto h3 mb-0">{head_value}</span>' if head_value else "")
        return (f'<div class="col-sm-6 col-xl-4 col-xxl-3"><div class="card{cls}">'
                f'<div class="card-body py-3">'
                f'<div class="d-flex align-items-baseline">'
                f'<span class="fw-medium">{name}</span>{head}</div>'
                f'{body}</div></div></div>')

    def win_block(w) -> str:
        rem = _window_remaining(w)
        lab = w.get("label") or "窗口"
        cd = _countdown(_to_epoch(w.get("reset_at")))
        left = f'{lab}' + (f' · {cd}' if cd else '')
        bold = ' fw-bold' if (rem is not None and rem < 15) else ''
        val = f'{rem:g}%' if rem is not None else '—'
        width = f'{max(0.0, min(100.0, rem)):.1f}' if rem is not None else '0'
        return (f'<div class="progressbg mt-1">'
                f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle" '
                f'style="width:{width}%" role="progressbar" aria-valuenow="{width}" '
                f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                f'<div class="progressbg-text fz-12">{left}</div>'
                f'<div class="progressbg-value{bold}">{val}</div></div>')

    if rl and rl.get("used_percent") is not None:
        rem = 100.0 - float(rl["used_percent"])
        bold = ' fw-bold' if rem < 15 else ''
        width = f'{max(0.0, min(100.0, rem)):.1f}'
        body = (f'<div class="progressbg mt-1">'
                f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle" '
                f'style="width:{width}%" role="progressbar" aria-valuenow="{width}" '
                f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                f'<div class="progressbg-text fz-12">周额度 · '
                f'{_countdown(rl.get("resets_at"))}</div></div>')
        cards.append(wrap("Codex", f'<span class="{bold.strip()}">{rem:g}%</span>', body))

    order = ["GLM (9.22)", "GLM 官方 (BigModel Coding Max)", "阿里 Coding Plan",
             "阿里 Token Plan", "Kimi", "MiniMax", "DeepSeek", "StepFun (阶跃星辰)",
             "智谱钱包"]
    order += [n for n in quotas if n not in order]  # 自定义中继等追加在后
    for name in order:
        v = quotas.get(name)
        if v is None:
            continue
        note_html = (f'<div class="fz-12 text-secondary mt-1">{v["note"]}</div>'
                     if v.get("note") else "")
        ts_html = (f'<div class="fz-12 text-secondary mt-1">数据 {v["fetched_at"]}</div>'
                   if v.get("fetched_at") else "")
        if not v.get("ok"):
            cards.append(wrap(name,
                              '<span class="text-secondary fz-12">不可获得</span>',
                              f'<div class="fz-12 text-secondary mt-1" '
                              f'style="white-space:normal">{v.get("error", "")}</div>',
                              dim=True))
            continue
        if v.get("kind") == "quota":
            wins = v.get("windows", [])
            if len(wins) == 1:
                w = wins[0]
                rem = _window_remaining(w)
                bold = ' fw-bold' if (rem is not None and rem < 15) else ''
                cd = _countdown(_to_epoch(w.get("reset_at")))
                width = f'{max(0.0, min(100.0, rem)):.1f}' if rem is not None else '0'
                body = (f'<div class="progressbg mt-1">'
                        f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle" '
                        f'style="width:{width}%" role="progressbar" aria-valuenow="{width}" '
                        f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                        f'<div class="progressbg-text fz-12">{w.get("label") or "窗口"}'
                        + (f' · {cd}' if cd else '') + '</div></div>') + note_html
                cards.append(wrap(name,
                                  f'<span class="{bold.strip()}">{rem:g}%</span>'
                                  if rem is not None else "—", body + ts_html))
            else:
                cards.append(wrap(name, "",
                                  "".join(win_block(w) for w in wins) + note_html + ts_html))
        elif v.get("kind") == "relay":
            wallet = v.get("wallet_usd")
            head = (f'${wallet:g}' if wallet is not None
                    else ("不限量" if v.get("unlimited") else f'${v.get("remaining_usd"):g}'))
            lines = []
            if wallet is not None:
                lines.append("钱包余额（账户维度）")
                if v.get("remaining_usd") is not None:
                    lines.append(f'本 key 剩余配额 ${v.get("remaining_usd"):g}')
            elif v.get("unlimited"):
                lines.append("本 key 剩余配额：不限量")
            if v.get("month_spend_usd") is not None:
                lines.append(f'本 key 本月消费 ${v.get("month_spend_usd"):g}')
            body = "".join(f'<div class="fz-12 text-secondary mt-1">{x}</div>'
                           for x in lines)
            cards.append(wrap(name, head, body + note_html + ts_html))
        else:
            cards.append(wrap(name, f'¥{v.get("available"):g}', note_html + ts_html))

    return "".join(cards)


def load_policies() -> dict:
    pf = DATA_DIR / "policies.json"
    if pf.exists():
        try:
            return json.loads(pf.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


BANDS = [("night", "夜间"), ("peak", "高峰"), ("offpeak", "非高峰"),
         ("campaign", "节假日（限时）"), ("daily", "日常")]
PROVIDER_COLS = ["GLM", "DeepSeek", "阿里百炼", "Kimi", "MiniMax", "StepFun"]


def _rule_active(rule, now):
    """policy active_rule → True/False/None(不判定)。本地时区。"""
    if not isinstance(rule, dict):
        return None
    t = rule.get("type")
    if t in (None, "none", "unknown"):
        return None
    hm = now.strftime("%H:%M")
    wd = now.weekday()
    today = now.strftime("%Y-%m-%d")
    if t == "daily":
        if rule.get("until") and today > rule["until"]:
            return False
        s, e = rule.get("start", "00:00"), rule.get("end", "23:59")
        return (hm >= s or hm < e) if s > e else (s <= hm < e)
    if t == "daterange":
        return rule.get("from", "0000-00-00") <= today <= rule.get("to", "9999-99-99")
    inside = (wd in rule.get("days", [])) and \
        any(s <= hm < e for s, e in rule.get("windows", []))
    return (not inside) if t == "outside" else inside


def _policies_html(pol: dict) -> str:
    """政策情报＝一张矩阵表：第一列纵向时段（日常/高峰/非高峰/夜间/节假日），
    横向厂商，交叉格=该时段该厂政策；当前生效的行首列打徽章+行内●。
    来源以角标[n]引用，表脚统一列原文链接+核实日期。"""
    items = pol.get("items") or []
    now = now_local()

    sources = []

    def src_ref(s):
        if not s:
            return ""
        if s not in sources:
            sources.append(s)
        return f'<sup class="text-secondary">[{sources.index(s) + 1}]</sup>'

    rows = []
    active_bands = []
    for band, label in BANDS:
        its = [it for it in items if (it.get("band") or "daily") == band]
        states = {_rule_active(it.get("active_rule"), now) for it in its}
        if band == "daily":
            badge = '<span class="badge bg-secondary-lt ms-1">常时</span>'
        elif True in states:
            badge = '<span class="badge bg-secondary-lt ms-1 fw-bold">当前生效</span>'
            active_bands.append(label)
        elif None in states:
            badge = '<span class="badge bg-secondary-lt ms-1">含未公开时段</span>'
        else:
            badge = '<span class="text-secondary fz-12 ms-1">不在时段内</span>'
        cells = []
        for prov in PROVIDER_COLS:
            pits = [x for x in its if x.get("provider") == prov]
            if not pits:
                cells.append('<td class="text-secondary">—</td>')
                continue
            parts = []
            for it in pits:
                st = _rule_active(it.get("active_rule"), now)
                dot = '<span class="fw-bold">●</span> ' if st is True else ''
                parts.append(
                    f'<div class="fz-12">{dot}<span class="fw-medium">{it.get("effect") or ""}</span>'
                    f'{src_ref(it.get("source"))}</div>'
                    f'<div class="fz-12 text-secondary">{it.get("models") or ""}</div>'
                    f'<div class="fz-12 text-secondary">{it.get("title") or ""}'
                    f'{" · " + it.get("window") if it.get("window") else ""}</div>')
            cells.append(f'<td>{" ".join(parts)}</td>')
        rows.append(f'<tr><th class="text-nowrap" style="min-width:7rem">{label}{badge}</th>'
                    f'{"".join(cells)}</tr>')

    foot = "".join(
        f'<div>[{i + 1}] <a href="{s}" target="_blank" style="text-decoration:none">{s}</a>'
        f'（核实 {pol.get("updated_at", "—")}）</div>' if s.startswith("http")
        else f'<div>[{i + 1}] {s}（核实 {pol.get("updated_at", "—")}）</div>'
        for i, s in enumerate(sources))
    now_line = (f'<div class="fz-12 text-secondary mb-2">现在 {now.strftime("%H:%M")} ｜ '
                f'当前生效：{"、".join(active_bands) if active_bands else "无时段性政策"} ｜ '
                f'已核实快照不自动抓取；政策变动后核实更新 data\\policies.json</div>')
    return (now_line +
            '<div class="card"><div class="table-responsive"><table class="table card-table">'
            f'<thead><tr><th>时段</th>{"".join(f"<th>{p}</th>" for p in PROVIDER_COLS)}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div></div>'
            f'<div class="fz-12 text-secondary mt-2" style="line-height:1.7">{foot}</div>')


def load_links() -> list:
    lf = DATA_DIR / "links.json"
    if lf.exists():
        try:
            return json.loads(lf.read_text(encoding="utf-8")) or []
        except Exception:
            return []
    return []


def _links_html(items: list) -> str:
    """收藏夹：各家控制台/订阅/钱包直达（data/links.json 随时增删）。"""
    if not items:
        return ""
    parts = []
    cur = None
    for it in items:
        g = it.get("group") or ""
        if g != cur:
            if parts:
                parts.append('</span>')
            parts.append(f'<span class="d-inline-flex align-items-center me-2 mt-1">'
                         f'<span class="fz-12 text-secondary me-1">{g}</span>')
            cur = g
        parts.append(f'<a class="btn btn-sm btn-outline-secondary me-1" target="_blank" '
                     f'href="{it.get("url")}" style="text-decoration:none">'
                     f'{it.get("label") or it.get("url")}</a>')
    parts.append("</span>")
    return ('<div class="mt-2 mb-1 d-flex flex-wrap align-items-center">'
            f'{"".join(parts)}</div>')


def _panel_html(p: str, pd: dict) -> str:
    t = pd["totals"]
    costs = pd["costs"]
    cost_sum = sum(costs.values())
    cost_html = (f'¥{cost_sum:.2f}<span class="badge bg-secondary-lt ms-1">估算</span>'
                 if costs else '<span class="text-secondary">—</span>')

    def bigcard(label, value, sub):
        return (f'<div class="col-sm-6 col-xl-3"><div class="card"><div class="card-body">'
                f'<div class="fz-12 text-secondary">{label}</div>'
                f'<div class="h1 mt-1 mb-0">{value}</div>'
                f'<div class="fz-12 text-secondary mt-1">{sub}</div></div></div></div>')

    cards = ('<div class="row row-cards g-2">'
             + bigcard("调用次数", f'{t["calls"]:,}', '')
             + bigcard("总 Token", fmt_tokens(t["total"]),
                       f'输入 {fmt_tokens(t["input"])}（缓存读 {fmt_tokens(t["cached"])}）')
             + bigcard("输出 Token", fmt_tokens(t["output"]), f'含推理 {fmt_tokens(t["reasoning"])}')
             + bigcard("费用", cost_html, "仅 DeepSeek 有价目 · 估算")
             + '</div>')

    trs = []
    for r in pd["rows"]:
        trs.append(f'<tr><td>{r["source"]}</td><td class="text-secondary">{r["provider"]}</td>'
                   f'<td class="fw-medium">{r["model"]}</td><td class="num">{r["calls"]:,}</td>'
                   f'<td class="num">{fmt_tokens(r["input"])}</td>'
                   f'<td class="num">{fmt_tokens(r["output"])}</td>'
                   f'<td class="num fw-medium">{fmt_tokens(r["total"])}</td></tr>')
    table = f'''<div class="card mt-3"><div class="table-responsive">
<table class="table card-table table-vcenter text-nowrap">
<thead><tr><th>来源</th><th>渠道</th><th>模型</th><th class="num">调用</th><th class="num">输入</th>
<th class="num">输出</th><th class="num">合计</th></tr></thead>
<tbody>{"".join(trs)}</tbody>
<tfoot><tr><td colspan="3">合计</td><td class="num">{t["calls"]:,}</td>
 <td class="num">{fmt_tokens(t["input"])}</td>
 <td class="num">{fmt_tokens(t["output"])}</td>
 <td class="num fw-medium">{fmt_tokens(t["total"])}</td></tr></tfoot>
</table></div></div>'''
    return cards + table


OVERRIDE_CSS = """
.fz-12{font-size:.78rem}
.card-dim{opacity:.7}
.progressbg .progress-bar.bar-fill-subtle{background:rgba(244,244,245,.16)}
table.table td.num,table.table th.num{text-align:right;font-variant-numeric:tabular-nums}
.nav-tabs .nav-link{color:var(--tblr-secondary-color, #b3b3b3); border:none; border-bottom:2px solid transparent}
.nav-tabs .nav-link.active{color:var(--tblr-primary, #e5e5e5); background:transparent; border-bottom-color:var(--tblr-primary, #e5e5e5); font-weight:700}
.nav-tabs .nav-link:hover{color:var(--tblr-primary, #e5e5e5)}
.tab-pane{display:none}.tab-pane.active{display:block}
.banner-now{font-size:1.15rem; font-weight:700; color:var(--tblr-primary, #e5e5e5)}
"""

JS = """
function showTab(name){
  document.querySelectorAll('.tab-pane').forEach(function(e){e.classList.remove('active')});
  document.getElementById('pane-'+name).classList.add('active');
  document.querySelectorAll('#tabs .nav-link').forEach(function(t){
    t.classList.toggle('active', t.dataset.tab===name)});
}
function showPeriod(p){
  document.querySelectorAll('.panel').forEach(function(e){e.classList.remove('active')});
  document.getElementById('panel-'+p).classList.add('active');
  document.querySelectorAll('#periods .nav-link').forEach(function(t){
    t.classList.toggle('active', t.dataset.p===p)});
}
window.addEventListener('DOMContentLoaded', function(){ showTab('overview'); showPeriod('today'); });
"""


def render_html() -> str:
    periods_data, quotas, rl, zinfo, cstats = _RENDER_CTX
    q_ok = sum(1 for v in quotas.values() if isinstance(v, dict) and v.get("ok"))
    q_all = len(quotas)
    pol = load_policies()
    gen = now_local().strftime("%Y-%m-%d %H:%M:%S")
    local_css = (ROOT / "dashboard" / "css" / "tabler.css").exists()
    if local_css:
        css_links = ('<link rel="stylesheet" href="css/tabler.css">\n'
                     '<link rel="stylesheet" href="css/tabler-themes.css">\n'
                     '<link rel="stylesheet" href="css/base.css">\n'
                     '<link rel="stylesheet" href="css/design-theme.css?v=20260925">')
    else:
        css_links = ('<link rel="stylesheet" '
                     'href="https://cdn.jsdelivr.net/npm/@tabler/css@1.5.0/dist/tabler.min.css">')

    # 4 Tab nav
    section_tabs = "".join(
        f'<li class="nav-item"><a class="nav-link" data-tab="{t}" onclick="showTab(\'{t}\')">{l}</a></li>'
        for t, l in [("overview", "概览"), ("policy", "政策"), ("links", "收藏夹"), ("details", "明细")])

    # Period tabs inside details pane
    period_tabs = "".join(
        f'<li class="nav-item"><a class="nav-link" data-p="{p}" onclick="showPeriod(\'{p}\')">{PERIOD_LABELS[p]}</a></li>'
        for p in PERIODS)
    panels = "".join(f'<div class="panel" id="panel-{p}">{_panel_html(p, periods_data[p])}</div>'
                     for p in PERIODS)

    # Banner: current time + active bands + affected providers
    active_bands = _active_bands(pol)
    banner_bands = ", ".join(dict(BANDS).get(b, b) for b in active_bands) if active_bands else "无时段性政策"
    # Show which providers are affected by active bands
    active_details = []
    for band in active_bands:
        band_label = dict(BANDS).get(band, band)
        affected = [it.get("provider") for it in (pol.get("items") or [])
                    if (it.get("band") or "daily") == band]
        if affected:
            active_details.append(f'{band_label}（{"、".join(dict.fromkeys(affected))}）')
    banner_detail = "；".join(active_details) if active_details else banner_bands
    next_trans = _next_transition()

    cost_note = next((pd["cost_note"] for pd in periods_data.values() if pd.get("cost_note")), "")
    partial = "、".join(zinfo.get("partial_days", []))
    partial_note = (f"⚠ {partial} 数据不完整：ZCode 源库为约 1 万行滚动窗口，早期数据已被源头修剪且不可恢复；"
                    f"自 2026-09-25 起看板按天留存聚合，此后不再丢失。"
                    if partial else "")

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-bs-theme="dark" data-bs-theme-base="neutral"
      data-bs-theme-primary="inverted" data-bs-theme-font="sans-serif"
      data-bs-theme-radius="1" data-figo-ready="true">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 用量中心</title>
{css_links}
<style>{OVERRIDE_CSS}</style></head>
<body>
<div class="page"><div class="page-wrapper">
<div class="page-body"><div class="container-xl">
<div class="page-header">
 <div class="w-100">
  <h2 class="page-title mb-1">AI 用量中心</h2>
  <div class="fz-12 text-secondary gen">生成 {gen} ｜ 抓取 {q_ok}/{q_all} 成功 ｜ 重新生成即刷新</div>
 </div>
</div>

<ul class="nav nav-tabs mt-3 mb-3" id="tabs">{section_tabs}</ul>

<div class="tab-pane active" id="pane-overview">
 <div class="banner-now">现在 {now_local().strftime("%H:%M")} {['周一','周二','周三','周四','周五','周六','周日'][now_local().weekday()]}，下一切换 {next_trans}</div>
 <div class="fz-12 text-secondary mt-1">当前生效：{banner_detail}</div>
 <h3 class="mt-4 mb-2" style="font-size:1rem">额度 / 余额
  <span class="text-secondary fw-normal fz-12">（每卡脚"数据 HH:MM"=该源取数时刻；不可获得卡显示原因）</span></h3>
 <div class="row row-cards g-2">{_quota_cards_html(quotas, rl, pol)}</div>
</div>

<div class="tab-pane" id="pane-policy">
 {_policies_html(pol)}
</div>

<div class="tab-pane" id="pane-links">
 {_links_html(load_links())}
</div>

<div class="tab-pane" id="pane-details">
 <ul class="nav nav-pills mt-2 mb-3" id="periods">{period_tabs}</ul>
 {panels}
 <div class="fz-12 text-secondary mt-4" style="line-height:1.8">
  <div>{cost_note}</div>
  <div>{partial_note}</div>
  <div>不可获得：阿里百炼额度（需控制台登录态）｜ Gemini、二狗API（无公开额度接口）。</div>
 </div>
</div>

</div></div>
</div></div>
<script>{JS}</script>
</body></html>"""


_RENDER_CTX = None


def generate_page(refresh_quotas: bool = False, out: Path = None):
    global _RENDER_CTX
    _RENDER_CTX = collect_all(refresh_quotas=refresh_quotas)
    html = render_html()
    out = out or (ROOT / "dashboard" / "index.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    return out


# ---------------------------------------------------------------- CLI 输出
def fmt_tokens(n) -> str:
    if n is None:
        return "—"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def cli_scan(period: str, use_cache: bool = True):
    rows, meta = build_summary(period, use_cache=use_cache)
    prices = load_prices()
    costs, cost_note = estimate_cost(rows, prices)

    label = PERIOD_LABELS.get(period, period)
    print(f"== 菲戈 AI 用量看板 · {label} · 生成于 {meta['generated_at']} ==")
    cs = meta["codex_stats"]
    print(f"[Codex 扫描] 文件 {cs['files']}（新解析 {cs['parsed']} / 缓存 {cs['reused']}"
          f" / 孤儿 {cs.get('orphan_files', 0)}），记录 {cs['records']}，"
          f"去重丢弃 {meta['codex_dedup_dropped']}")
    zi = meta.get("zcode_info", {})
    print(f"[ZCode 留存] 源库现存 {zi.get('db_rows', '?')} 行（最早 {zi.get('db_min_day', '?')}），"
          f"留存天数 {len(zi.get('stored_days', []))}"
          f"（{zi.get('stored_days', ['?'])[0] if zi.get('stored_days') else '?'} ~ "
          f"{zi.get('stored_days', ['?'])[-1] if zi.get('stored_days') else '?'}）")
    if zi.get("partial_days"):
        print(f"  ⚠ 不完整天（源库已修剪，无法恢复）：{', '.join(zi['partial_days'])}")

    tot = {k: sum(r[k] for r in rows) for k in ("calls", "input", "cached", "output", "reasoning", "total", "errors")}
    print(f"\n[总计] 调用 {tot['calls']} 次 | input {fmt_tokens(tot['input'])}"
          f"（其中缓存读 {fmt_tokens(tot['cached'])}）| output {fmt_tokens(tot['output'])}"
          f" | reasoning {fmt_tokens(tot['reasoning'])} | total {fmt_tokens(tot['total'])}"
          f" | 非成功 {tot['errors']}")

    print(f"\n{'来源':<6} {'渠道':<22} {'模型':<24} {'调用':>5} {'input':>8} {'cached':>8} "
          f"{'output':>8} {'reason':>7} {'total':>8} {'估算¥':>8}")
    for r in sorted(rows, key=lambda x: (x["source"], -x["calls"])):
        c = costs.get((r["source"], r["model"]))
        cstr = f"{c:.2f}" if c is not None else "—"
        print(f"{r['source']:<6} {r['provider'][:20]:<22} {r['model'][:22]:<24} "
              f"{r['calls']:>5} {fmt_tokens(r['input']):>8} {fmt_tokens(r['cached']):>8} "
              f"{fmt_tokens(r['output']):>8} {fmt_tokens(r['reasoning']):>7} "
              f"{fmt_tokens(r['total']):>8} {cstr:>8}")

    rl = meta.get("codex_rate_limit")
    print("\n[Codex 周额度（本地最新快照）]")
    if rl:
        ts = datetime.fromtimestamp(rl["ts_ms"] / 1000).astimezone()
        reset = ""
        if rl.get("resets_at"):
            rt = datetime.fromtimestamp(rl["resets_at"]).astimezone()
            delta = rt - now_local()
            days, hours = delta.days, delta.seconds // 3600
            reset = f"，重置于 {rt.strftime('%Y-%m-%d %H:%M')}（{days}天{hours}小时后）"
        print(f"  已用 {rl.get('used_percent')}% ｜ 窗口 {rl.get('window_minutes')} 分钟"
              f" ｜ 套餐 {rl.get('plan_type')} ｜ 快照时间 {ts.strftime('%Y-%m-%d %H:%M')}{reset}")
    else:
        print("  本地未找到 rate_limits 快照")
    if cost_note:
        print(f"\n[费用口径] {cost_note}")


def cli_quota(refresh: bool = False):
    res = fetch_quotas(refresh=refresh)
    print(f"== 额度/余额查询 · {now_local().strftime('%Y-%m-%d %H:%M:%S')} ==")
    for name, v in res.items():
        if not isinstance(v, dict):
            continue
        if not v.get("ok"):
            print(f"\n● {name}: ✗ {v.get('error', '未知错误')}（{v.get('fetched_at', '')}）")
            continue
        if v.get("kind") == "quota":
            print(f"\n● {name}: 套餐 {v.get('plan') or '—'}（{v.get('fetched_at')}）")
            for w in v.get("windows", []):
                reset = ""
                re_ = _to_epoch(w.get("reset_at"))
                if re_:
                    rt = datetime.fromtimestamp(re_).astimezone()
                    reset = f"，重置 {rt.strftime('%m-%d %H:%M')}"
                unit = w.get("unit", "")
                parts = []
                if w.get("used_percent") is not None:
                    parts.append(f"已用 {w['used_percent']}%")
                if w.get("used") is not None and w.get("quota"):
                    parts.append(f"{w['used']:g}/{w['quota']:g}{unit}")
                elif w.get("remaining_percent") is not None:
                    parts.append(f"剩余 {w['remaining_percent']:g}%")
                body = "，".join(parts) if parts else "无数据"
                print(f"    {w.get('label')}: {body}{reset}")
        elif v.get("kind") == "balance":
            det = "，".join(f"{k} {v2}" for k, v2 in (v.get("detail") or {}).items()
                            if v2 is not None)
            print(f"\n● {name}: {v.get('available')} {v.get('currency', '')}"
                  + (f"（{det}）" if det else "") + f"（{v.get('fetched_at')}）")
    print("\n[静态说明]")
    for k, v in STATIC_QUOTA_NOTES.items():
        print(f"  {k}: {v}")


def refresh_store():
    """仅刷新本地留存（ZCode 按天聚合 + Codex 文件缓存），供每日计划任务调用。
    源库按 ~1 万行滚动修剪，隔几天不刷新就可能永久丢失最老的天。"""
    t0 = time.time()
    daily, zinfo = zcode_daily_refresh(use_store=True, save=True)
    records, rl, stats = scan_codex_raw(use_cache=True)
    print(f"留存刷新完成（{time.time()-t0:.1f}s）：ZCode {zinfo.get('db_rows', 0)} 行→"
          f"{len(zinfo.get('stored_days', []))} 天留存；Codex 解析 {stats['parsed']} 新文件/"
          f"缓存 {stats['reused']}，记录 {stats['records']} 条")
    if zinfo.get("partial_days"):
        print(f"不完整天：{', '.join(zinfo['partial_days'])}")


TASK_NAME = "FeigeUsageDashboardRefreshStore"


def install_task(remove: bool = False):
    """每日 23:50 跑 --refresh-store 的 Windows 计划任务（可选，需菲戈明确启用）。
    作用：ZCode 源库滚动修剪老数据，隔几天不刷新会永久丢失超出窗口的天。"""
    import subprocess
    if remove:
        subprocess.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"], check=False)
        print(f"已删除计划任务 {TASK_NAME}（若存在）")
        return
    script = Path(__file__).resolve()
    cmd = (f'schtasks /Create /TN {TASK_NAME} /SC DAILY /ST 23:50 /RL LIMITED '
           f'/TR "py \\"{script}\\" --refresh-store" /F')
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True)
    print(r.stdout.strip() or r.stderr.strip())
    print(f"计划任务 {TASK_NAME} 注册请求已执行（每日 23:50 静默刷新留存）")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--scan":
        period = args[1] if len(args) > 1 and args[1] in PERIODS else "today"
        use_cache = "--no-cache" not in args
        cli_scan(period, use_cache=use_cache)
        return
    if args and args[0] == "--refresh-store":
        refresh_store()
        return
    if args and args[0] == "--quota":
        cli_quota(refresh="--refresh" in args)
        return
    if args and args[0] == "--install-task":
        install_task(remove=False)
        return
    if args and args[0] == "--remove-task":
        install_task(remove=True)
        return
    # 默认：生成正式页面并打开浏览器（--no-open 只生成；--refresh 强制重查额度）
    out = generate_page(refresh_quotas="--refresh" in args)
    print(f"看板已生成: {out}")
    if "--no-open" not in args:
        try:
            os.startfile(str(out))  # Windows
        except Exception:
            pass


if __name__ == "__main__":
    main()
