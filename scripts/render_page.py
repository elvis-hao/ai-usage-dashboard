#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯渲染层（自包含，不 import 数据层）：接收 ctx 字典，产出整页 HTML。
ctx = {periods_data, quotas, rl, zinfo, cstats, pol, bm, gen, local_css, now}
所有时段/状态/样式逻辑在此；数据获取在 usage_dashboard.py。"""
from datetime import datetime

BANDS = [("night", "夜间"), ("peak", "高峰"), ("offpeak", "非高峰"),
         ("campaign", "节假日（限时）"), ("daily", "日常")]
PROVIDER_COLS = ["GLM", "DeepSeek", "阿里百炼", "Kimi", "MiniMax", "StepFun"]
WINDOW_BANDS = {
    ("GLM (9.22)", "5小时"): "night", ("GLM (9.22)", "周"): "offpeak",
    ("GLM (9.22)", "MCP 每月额度"): "daily",
    ("GLM 官方 (BigModel Coding Max)", "5小时"): "night",
    ("GLM 官方 (BigModel Coding Max)", "周"): "offpeak",
    ("GLM 官方 (BigModel Coding Max)", "MCP 每月额度"): "daily",
    ("阿里 Coding Plan", "5小时"): "night", ("阿里 Coding Plan", "周"): "offpeak",
    ("阿里 Coding Plan", "月"): "daily", ("阿里 Token Plan", "月"): "daily",
    ("Kimi", "5小时"): "daily", ("Kimi", "7天"): "daily",
    ("Kimi", "总使用量"): "daily",
    ("MiniMax", "5小时"): "daily", ("MiniMax", "周"): "daily",
}


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_epoch(v):
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


def _countdown(epoch_s, now):
    if not epoch_s:
        return ""
    delta = datetime.fromtimestamp(epoch_s).astimezone() - now
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


def _window_remaining(w):
    if w.get("remaining_percent") is not None:
        return float(w["remaining_percent"])
    if w.get("used_percent") is not None:
        return 100.0 - float(w["used_percent"])
    if w.get("remaining") is not None and w.get("quota"):
        return float(w["remaining"]) / float(w["quota"]) * 100
    return None


def fmt_tokens(n):
    if n is None:
        return "—"
    if n >= 1_000_000_000:
        return f"{n/1_000_000_000:.2f}B"
    if n >= 1_000_000:
        return f"{n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}K"
    return str(n)


def _rule_active(rule, now):
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


def _active_bands(pol, now):
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


def _band_providers(pol, band):
    provs = []
    for it in pol.get("items") or []:
        if (it.get("band") or "daily") == band and it.get("provider") not in provs:
            provs.append(it.get("provider"))
    return "、".join(provs)


def _next_transition_detail(now):
    hm = now.strftime("%H:%M")
    cands = []
    for h in ("09:00", "12:00", "14:00", "18:00", "23:00"):
        if h > hm:
            cands.append(now.replace(hour=int(h[:2]), minute=int(h[3:]),
                                     second=0, microsecond=0))
    from datetime import timedelta
    nxt = cands[0] if cands else (now + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0)
    pol = _NEXT_POL[0]
    cur = set(_active_bands(pol, now))
    after = set(_active_bands(pol, nxt + timedelta(minutes=1)))
    lbl = dict(BANDS)
    order = [k for k, _ in BANDS]
    parts = []
    for b in sorted(after - cur, key=order.index):
        provs = _band_providers(pol, b)
        parts.append(f"进入{lbl[b]}档" + (f"（{provs}）" if provs else ""))
    for b in sorted(cur - after, key=order.index):
        parts.append(f"{lbl[b]}档结束")
    desc = "；".join(parts) if parts else "时段组合不变"
    secs = int((nxt - now).total_seconds() // 60)
    dh, dm = divmod(secs, 60)
    dd, dh = divmod(dh, 24)
    cd = f"{dd}天{dh}小时" if dd else (f"{dh}小时{dm}分" if dh else f"{dm}分钟")
    return nxt.strftime("%H:%M"), cd, desc


_NEXT_POL = [{}]  # 由 render_html 注入，供 _next_transition_detail 使用


# ---------------------------------------------------------------- CSS / JS
OVERRIDE_CSS = """
.fz-12{font-size:.78rem}
.card-dim{opacity:.7}
.progressbg .progress-bar.bar-fill-subtle{background:rgba(244,244,245,.16)}
/* 进度条样式切换：progressbg=行背景式(默认)；其余=隐藏背景条、显示独立条 */
.qbar-alt{display:none}
html:not([data-pstyle="progressbg"]) .qbar-alt{display:block}
html:not([data-pstyle="progressbg"]) .progressbg-progress{display:none}
table.table td.num,table.table th.num{text-align:right;font-variant-numeric:tabular-nums}
.tab-pane{display:none}.tab-pane.active{display:block}
.panel{display:none}.panel.active{display:block}
.banner-now{font-size:1.15rem; font-weight:700; color:var(--tblr-primary, #e5e5e5)}
.text-green{color:var(--tblr-green)!important}
.text-yellow{color:var(--tblr-yellow)!important}
.text-red{color:var(--tblr-red)!important}
.js-drag-handle{cursor:grab;user-select:none;opacity:.4}
.js-drag-handle:hover{opacity:.9}
.js-drag-handle:active{cursor:grabbing}
.sortable-ghost{opacity:.4}
.settings-panel{position:fixed;top:0;left:0;bottom:0;width:17rem;background:var(--tblr-bg-surface,#141414);
  border-right:1px solid var(--tblr-border-color,#333);z-index:1040;transform:translateX(-100%);
  transition:transform .2s ease;overflow-y:auto;padding:1rem}
.settings-panel.open{transform:translateX(0)}
.settings-panel .form-label{font-size:.8rem;margin-bottom:.25rem}
.settings-backdrop{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:1035;display:none}
.settings-backdrop.show{display:block}
"""

JS = """
function showTab(name){
  document.querySelectorAll('.tab-pane').forEach(function(e){e.classList.remove('active')});
  document.getElementById('pane-'+name).classList.add('active');
  document.querySelectorAll('#tabs .nav-link').forEach(function(t){
    t.classList.toggle('active', t.getAttribute('onclick').indexOf("'"+name+"'") >= 0)});
}
function showPeriod(p){
  document.querySelectorAll('.panel').forEach(function(e){e.classList.remove('active')});
  document.getElementById('panel-'+p).classList.add('active');
  document.querySelectorAll('#periods .nav-link').forEach(function(t){
    t.classList.toggle('active', t.getAttribute('onclick').indexOf("'"+p+"'") >= 0)});
}
"""

SETTINGS_JS = """
(function(){
  var LS = function(k,v){ if(v===undefined) return localStorage.getItem(k); localStorage.setItem(k,v); };
  var ALLOW = {
    base:['slate','gray','zinc','neutral','stone'],
    primary:['inverted','azure','blue','cyan','green','indigo','lime','orange','pink','purple','red','teal','yellow'],
    radius:['0','0.5','1','1.5','2'],
    font:['sans-serif','serif','monospace','comic']
  };
  var DEF = {base:'neutral', primary:'inverted', radius:'1', font:'sans-serif',
             pstyle:'progressbg', statuscolor:'off'};
  function get(k){ var v=LS('aud-'+k); return (v!==null&&v!==undefined)?v:DEF[k]; }
  function applyTheme(){
    ['base','primary','radius','font'].forEach(function(k){
      var v=get(k); if(ALLOW[k].indexOf(v)<0) v=DEF[k];
      document.documentElement.setAttribute('data-bs-theme-'+k, v);
    });
  }
  function statusColor(rem){ if(rem>60) return 'green'; if(rem<20) return 'red'; return 'yellow'; }
  function applyStatusColor(){
    var on = get('statuscolor')==='on';
    document.querySelectorAll('[data-rem]').forEach(function(el){
      var rem=parseFloat(el.getAttribute('data-rem')); if(isNaN(rem)) return;
      var c=statusColor(rem);
      el.classList.remove('bg-green','bg-yellow','bg-red','text-green','text-yellow','text-red','bar-fill-subtle');
      if(on){
        if(el.classList.contains('js-bar')) el.classList.add('bg-'+c);
        if(el.classList.contains('js-val')) el.classList.add('text-'+c);
      } else if(el.classList.contains('js-bar')) el.classList.add('bar-fill-subtle');
    });
  }
  function applyProgressStyle(){
    var s=get('pstyle');
    document.documentElement.setAttribute('data-pstyle', s);
    document.querySelectorAll('.qbar-alt').forEach(function(p){
      p.classList.remove('progress-sm','progress-lg','progress-xl');
      var bar=p.querySelector('.progress-bar'); if(!bar) return;
      bar.classList.remove('progress-bar-striped','progress-bar-animated');
      if(s==='sm') p.classList.add('progress-sm');
      if(s==='lg') p.classList.add('progress-lg');
      if(s==='xl') p.classList.add('progress-xl');
      if(s==='striped'||s==='animated') bar.classList.add('progress-bar-striped');
      if(s==='animated') bar.classList.add('progress-bar-animated');
    });
    applyStatusColor();
  }
  function applyOrder(key, containerSel){
    var raw=LS('aud-order-'+key); if(!raw) return;
    var order; try{ order=JSON.parse(raw);}catch(e){return;}
    var c=document.querySelector(containerSel); if(!c) return;
    order.forEach(function(id){ var el=c.querySelector('[data-card-id="'+id+'"]'); if(el) c.appendChild(el); });
  }
  function saveOrder(key, containerSel){
    var c=document.querySelector(containerSel); if(!c) return;
    var ids=[].map.call(c.querySelectorAll('[data-card-id]'), function(e){return e.getAttribute('data-card-id');});
    LS('aud-order-'+key, JSON.stringify(ids));
  }
  function initDrag(){
    if(typeof Sortable==='undefined') return;
    [['quota','#quotaGrid'],['links','#linksGrid']].forEach(function(pair){
      var el=document.querySelector(pair[1]); if(!el) return;
      Sortable.create(el, {handle:'.js-drag-handle', animation:150, ghostClass:'sortable-ghost',
        onEnd:function(){ saveOrder(pair[0], pair[1]); }});
    });
  }
  function initBookmarkFilter(){
    var cats=document.querySelectorAll('[data-bookmark-category]');
    cats.forEach(function(a){
      a.addEventListener('click', function(){
        cats.forEach(function(x){x.classList.remove('active');});
        a.classList.add('active');
        var sel=a.getAttribute('data-bookmark-category');
        document.querySelectorAll('#linksGrid [data-category]').forEach(function(card){
          card.style.display = (sel==='__all__' || card.getAttribute('data-category')===sel) ? '' : 'none';
        });
      });
    });
  }
  function bindPanel(){
    var panel=document.getElementById('settingsPanel');
    var bd=document.getElementById('settingsBackdrop');
    var open=function(o){ panel.classList.toggle('open',o); bd.classList.toggle('show',o); };
    var btn=document.getElementById('settingsBtn'); if(btn) btn.onclick=function(){open(true);};
    if(bd) bd.onclick=function(){open(false);};
    var close=document.getElementById('settingsClose'); if(close) close.onclick=function(){open(false);};
    panel.querySelectorAll('input[type=radio]').forEach(function(r){
      r.checked = (get(r.name)===r.value);
      r.addEventListener('change', function(){
        LS('aud-'+r.name, r.value);
        if(['base','primary','radius','font'].indexOf(r.name)>=0) applyTheme();
        if(r.name==='pstyle') applyProgressStyle();
        if(r.name==='statuscolor') applyStatusColor();
      });
    });
    var reset=document.getElementById('settingsReset');
    if(reset) reset.onclick=function(){
      Object.keys(DEF).forEach(function(k){ localStorage.removeItem('aud-'+k); });
      ['base','primary','radius','font'].forEach(function(k){ localStorage.removeItem('tabler-'+k); });
      location.reload();
    };
  }
  window.addEventListener('DOMContentLoaded', function(){
    showTab('overview'); showPeriod('today');
    applyTheme(); applyProgressStyle(); bindPanel(); initDrag(); initBookmarkFilter();
    applyOrder('quota','#quotaGrid'); applyOrder('links','#linksGrid');
  });
})();
"""


# ---------------------------------------------------------------- 组件
def _radio_group(name, label, options, checked):
    items = "".join(
        f'<label class="me-2 mb-1 d-inline-block fz-12" style="cursor:pointer">'
        f'<input type="radio" name="{name}" value="{v}" class="me-1"'
        f'{" checked" if v == checked else ""}>{t}</label>'
        for v, t in options)
    return (f'<div class="mb-3"><div class="fz-12 fw-medium mb-1 text-secondary">{label}</div>'
            f'<div>{items}</div></div>')


def _settings_html():
    return f"""
<div class="settings-backdrop" id="settingsBackdrop"></div>
<aside class="settings-panel" id="settingsPanel" aria-label="基础设置">
 <div class="d-flex align-items-center mb-3">
   <h3 class="card-title mb-0">基础设置</h3>
   <button class="btn btn-ghost-secondary btn-icon btn-sm ms-auto" id="settingsClose">✕</button>
 </div>
 {_radio_group("base", "主题基调", [("slate","slate"),("gray","gray"),("zinc","zinc"),("neutral","neutral(默认)"),("stone","stone")], "neutral")}
 {_radio_group("primary", "主色", [("inverted","反转(默认)"),("blue","blue"),("green","green"),("red","red"),("yellow","yellow"),("purple","purple")], "inverted")}
 {_radio_group("radius", "圆角", [("0","0"),("0.5","0.5"),("1","1(默认)"),("1.5","1.5"),("2","2")], "1")}
 {_radio_group("font", "字体", [("sans-serif","无衬线(默认)"),("serif","衬线"),("monospace","等宽"),("comic","comic")], "sans-serif")}
 <hr class="my-3">
 {_radio_group("pstyle", "进度条样式", [("progressbg","背景式(当前)"),("sm","细条"),("lg","粗条"),("xl","特粗"),("striped","条纹"),("animated","条纹动画")], "progressbg")}
 {_radio_group("statuscolor", "状态色(剩余>60绿/20-60黄/<20红)", [("on","开"),("off","关(纯灰阶)")], "off")}
 <div class="mt-3"><button class="btn btn-sm btn-outline-secondary" id="settingsReset">重置为默认</button></div>
 <div class="fz-12 text-secondary mt-2">设置与拖拽顺序存浏览器 localStorage；换浏览器重置。</div>
</aside>"""


def _quota_cards_html(quotas, rl, pol, now):
    cards = []

    def wrap(name, head_value, body="", dim=False):
        cls = " card-dim" if dim else ""
        head = (f'<span class="ms-auto h3 mb-0">{head_value}</span>' if head_value else "")
        cid = name.replace(" ", "-").replace("(", "").replace(")", "")
        return (f'<div class="col-sm-6 col-xl-4 col-xxl-3" data-card-id="{cid}">'
                f'<div class="card{cls}"><div class="card-body py-3">'
                f'<div class="d-flex align-items-baseline">'
                f'<button class="btn btn-ghost-secondary btn-icon btn-sm js-drag-handle me-1" '
                f'title="拖动排序" style="cursor:grab">⋮⋮</button>'
                f'<span class="fw-medium">{name}</span>{head}</div>'
                f'{body}</div></div></div>')

    def win_block(w):
        rem = _window_remaining(w)
        lab = w.get("label") or "窗口"
        cd = _countdown(_to_epoch(w.get("reset_at")), now)
        left = f'{lab}' + (f' · {cd}' if cd else '')
        bold = ' fw-bold' if (rem is not None and rem < 15) else ''
        val = f'{rem:g}%' if rem is not None else '—'
        width = f'{max(0.0, min(100.0, rem)):.1f}' if rem is not None else '0'
        remattr = f' data-rem="{rem:.1f}"' if rem is not None else ''
        return (f'<div class="progressbg mt-1">'
                f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle js-bar" '
                f'style="width:{width}%"{remattr} role="progressbar" aria-valuenow="{width}" '
                f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                f'<div class="progressbg-text fz-12">{left}</div>'
                f'<div class="progressbg-value{bold} js-val"{remattr}>{val}</div></div>'
                f'<div class="progress qbar-alt mt-1"><div class="progress-bar js-bar" '
                f'style="width:{width}%"{remattr} role="progressbar" aria-valuenow="{width}" '
                f'aria-valuemin="0" aria-valuemax="100"></div></div>')

    if rl and rl.get("used_percent") is not None:
        rem = 100.0 - float(rl["used_percent"])
        bold = ' fw-bold' if rem < 15 else ''
        width = f'{max(0.0, min(100.0, rem)):.1f}'
        body = (f'<div class="progressbg mt-1">'
                f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle js-bar" '
                f'style="width:{width}%" data-rem="{rem:.1f}" role="progressbar" aria-valuenow="{width}" '
                f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                f'<div class="progressbg-text fz-12">周额度 · '
                f'{_countdown(rl.get("resets_at"), now)}</div></div>'
                f'<div class="progress qbar-alt mt-1"><div class="progress-bar js-bar" '
                f'style="width:{width}%" data-rem="{rem:.1f}" role="progressbar" '
                f'aria-valuenow="{width}" aria-valuemin="0" aria-valuemax="100"></div></div>')
        cards.append(wrap("Codex", f'<span class="{bold.strip()} js-val" data-rem="{rem:.1f}">{rem:g}%</span>', body))

    order = ["GLM (9.22)", "GLM 官方 (BigModel Coding Max)", "阿里 Coding Plan",
             "阿里 Token Plan", "Kimi", "MiniMax", "DeepSeek", "StepFun (阶跃星辰)",
             "智谱钱包"]
    order += [n for n in quotas if n not in order]
    for name in order:
        v = quotas.get(name)
        if v is None:
            continue
        note_html = (f'<div class="fz-12 text-secondary mt-1">{v["note"]}</div>'
                     if v.get("note") else "")
        ts_html = (f'<div class="fz-12 text-secondary mt-1">数据 {v["fetched_at"]}</div>'
                   if v.get("fetched_at") else "")
        if not v.get("ok"):
            cards.append(wrap(name, '<span class="text-secondary fz-12">不可获得</span>',
                              f'<div class="fz-12 text-secondary mt-1" '
                              f'style="white-space:normal">{v.get("error", "")}</div>', dim=True))
            continue
        if v.get("kind") == "quota":
            wins = v.get("windows", [])
            if len(wins) == 1:
                w = wins[0]
                rem = _window_remaining(w)
                bold = ' fw-bold' if (rem is not None and rem < 15) else ''
                cd = _countdown(_to_epoch(w.get("reset_at")), now)
                width = f'{max(0.0, min(100.0, rem)):.1f}' if rem is not None else '0'
                body = (f'<div class="progressbg mt-1">'
                        f'<div class="progress progressbg-progress"><div class="progress-bar bar-fill-subtle js-bar" '
                        f'style="width:{width}%" data-rem="{rem:.1f}" role="progressbar" aria-valuenow="{width}" '
                        f'aria-valuemin="0" aria-valuemax="100"></div></div>'
                        f'<div class="progressbg-text fz-12">{w.get("label") or "窗口"}'
                        + (f' · {cd}' if cd else '') + '</div></div>'
                        f'<div class="progress qbar-alt mt-1"><div class="progress-bar js-bar" '
                        f'style="width:{width}%" data-rem="{rem:.1f}" role="progressbar" '
                        f'aria-valuenow="{width}" aria-valuemin="0" aria-valuemax="100"></div></div>') + note_html
                cards.append(wrap(name,
                                  f'<span class="{bold.strip()} js-val" data-rem="{rem:.1f}">{rem:g}%</span>'
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
                    lines.append(f'key 剩余配额 ${v.get("remaining_usd"):g}')
            elif v.get("unlimited"):
                lines.append("key 剩余配额：不限量")
            if v.get("month_spend_usd") is not None:
                lines.append(f'key 本月消费 ${v.get("month_spend_usd"):g}')
            body = "".join(f'<div class="fz-12 text-secondary mt-1">{x}</div>' for x in lines)
            cards.append(wrap(name, head, body + note_html + ts_html))
        else:
            cards.append(wrap(name, f'¥{v.get("available"):g}', note_html + ts_html))
    return "".join(cards)


def _policies_html(pol, now):
    items = pol.get("items") or []
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


def _bookmarks_html(bm):
    cats = bm.get("categories") or []
    items = bm.get("items") or []
    counts = {}
    for it in items:
        counts[it.get("category")] = counts.get(it.get("category"), 0) + 1
    cat_links = [f'<a class="list-group-item list-group-item-action d-flex align-items-center active" '
                 f'data-bookmark-category="__all__" style="cursor:pointer">'
                 f'<span class="text-truncate">全部收藏</span>'
                 f'<span class="badge bg-secondary-lt ms-auto">{len(items)}</span></a>']
    for c in cats:
        cat_links.append(
            f'<a class="list-group-item list-group-item-action d-flex align-items-center" '
            f'data-bookmark-category="{c["id"]}" style="cursor:pointer">'
            f'<span class="text-truncate">{c["name"]}</span>'
            f'<span class="badge bg-secondary-lt ms-auto">{counts.get(c["id"], 0)}</span></a>')
    cards = []
    for it in items:
        tags = "".join(f'<span class="badge bg-secondary-lt me-1 mb-1">{t}</span>'
                       for t in (it.get("tags") or []))
        note = (f'<p class="text-secondary mb-2 fz-12">{it["note"]}</p>' if it.get("note") else "")
        cid = (it.get("title") or "").replace(" ", "-").replace("/", "-")
        cards.append(
            f'<article class="col-12 col-sm-6 col-xl-4" data-card-id="{cid}" '
            f'data-category="{it.get("category")}">'
            f'<div class="card card-sm h-100"><div class="card-body p-3">'
            f'<div class="d-flex align-items-start gap-2 mb-2">'
            f'<h3 class="card-title fs-3 mb-0 text-truncate flex-fill">'
            f'<a class="text-reset" href="{it.get("url")}" target="_blank" '
            f'rel="noopener noreferrer">{it.get("title")}</a></h3>'
            f'<button class="btn btn-ghost-secondary btn-icon btn-sm js-drag-handle" '
            f'title="拖动排序" style="cursor:grab">⋮⋮</button></div>'
            f'{note}<div>{tags}</div></div></div></article>')
    return f'''<div class="row g-3">
 <div class="col-12 col-lg-3"><div class="card"><div class="card-header">
   <h3 class="card-title">分类</h3></div>
   <div class="list-group list-group-flush" id="bookmarkCategories">{"".join(cat_links)}</div>
 </div></div>
 <div class="col-12 col-lg-9">
   <div class="row g-3" id="linksGrid">{"".join(cards)}</div>
 </div>
</div>'''


def _panel_html(p, pd, period_labels):
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


def render_html(ctx):
    periods_data = ctx["periods_data"]
    quotas = ctx["quotas"]
    rl = ctx["rl"]
    zinfo = ctx["zinfo"]
    pol = ctx["pol"]
    bm = ctx["bm"]
    gen = ctx["gen"]
    local_css = ctx["local_css"]
    now = ctx["now"]
    period_labels = ctx["period_labels"]
    periods = ctx["periods"]
    q_ok = sum(1 for v in quotas.values() if isinstance(v, dict) and v.get("ok"))
    q_all = len(quotas)
    _NEXT_POL[0] = pol

    if local_css:
        css_links = ('<link rel="stylesheet" href="css/tabler.css">\n'
                     '<link rel="stylesheet" href="css/tabler-themes.css">\n'
                     '<link rel="stylesheet" href="css/base.css">\n'
                     '<link rel="stylesheet" href="css/design-theme.css?v=20260925">')
    else:
        css_links = ('<link rel="stylesheet" '
                     'href="https://cdn.jsdelivr.net/npm/@tabler/css@1.5.0/dist/tabler.min.css">')

    section_tabs = "".join(
        f'<li class="nav-item"><a class="nav-link{" active" if t == "overview" else ""}" '
        f'onclick="showTab(\'{t}\'); return false;" href="#">{l}</a></li>'
        for t, l in [("overview", "概览"), ("policy", "政策"), ("links", "收藏夹"), ("details", "明细")])
    section_tabs = f'<ul class="nav nav-tabs mb-4">{section_tabs}</ul>'

    period_tabs = "".join(
        f'<li class="nav-item"><a class="nav-link{" active" if p == "today" else ""}" '
        f'onclick="showPeriod(\'{p}\'); return false;" href="#">{period_labels[p]}</a></li>'
        for p in periods)
    period_tabs = f'<ul class="nav nav-pills mb-3">{period_tabs}</ul>'
    panels = "".join(f'<div class="panel" id="panel-{p}">{_panel_html(p, periods_data[p], period_labels)}</div>'
                     for p in periods)

    active_bands = _active_bands(pol, now)
    active_details = []
    for band in active_bands:
        band_label = dict(BANDS).get(band, band)
        affected = _band_providers(pol, band)
        if affected:
            active_details.append(f'{band_label}（{affected}）')
    banner_detail = "；".join(active_details) if active_details else "无时段性政策"
    nt_time, nt_cd, nt_desc = _next_transition_detail(now)

    cost_note = next((pd["cost_note"] for pd in periods_data.values() if pd.get("cost_note")), "")
    partial = "、".join(zinfo.get("partial_days", []))
    partial_note = (f"⚠ {partial} 数据不完整：ZCode 源库为约 1 万行滚动窗口，早期数据已被源头修剪且不可恢复；"
                    f"自 2026-09-25 起看板按天留存聚合，此后不再丢失。" if partial else "")

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-bs-theme="dark" data-bs-theme-base="neutral"
      data-bs-theme-primary="inverted" data-bs-theme-font="sans-serif"
      data-bs-theme-radius="1" data-pstyle="progressbg" data-figo-ready="true">
<head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI 用量中心</title>
{css_links}
<style>{OVERRIDE_CSS}</style></head>
<body>
<div class="page"><div class="page-wrapper">
<div class="page-body"><div class="container-xl">
<div class="page-header">
 <div class="row align-items-center w-100">
  <div class="col">
   <h2 class="page-title mb-1">AI 用量中心</h2>
   <div class="fz-12 text-secondary gen">生成 {gen} ｜ 抓取 {q_ok}/{q_all} 成功 ｜ 重新生成即刷新</div>
  </div>
  <div class="col-auto">
   <button class="btn btn-outline-secondary btn-sm" id="settingsBtn"> 设置</button>
  </div>
 </div>
</div>
{section_tabs}
<div class="tab-pane active" id="pane-overview">
 <div class="banner-now">现在 {now.strftime("%H:%M")} {['周一','周二','周三','周四','周五','周六','周日'][now.weekday()]} ｜ {nt_cd}后（{nt_time}）{nt_desc}</div>
 <div class="fz-12 text-secondary mt-1">当前生效：{banner_detail}</div>
 <h3 class="mt-4 mb-2" style="font-size:1rem">额度 / 余额
  <span class="text-secondary fw-normal fz-12">（每卡脚"数据 HH:MM"=该源取数时刻；不可获得卡显示原因）</span></h3>
 <div class="row row-cards g-2" id="quotaGrid">{_quota_cards_html(quotas, rl, pol, now)}</div>
</div>
<div class="tab-pane" id="pane-policy">
 {_policies_html(pol, now)}
</div>
<div class="tab-pane" id="pane-links">
 {_bookmarks_html(bm)}
</div>
<div class="tab-pane" id="pane-details">
 {period_tabs}
 {panels}
 <div class="fz-12 text-secondary mt-4" style="line-height:1.8">
  <div>{cost_note}</div>
  <div>{partial_note}</div>
  <div>不可获得：阿里百炼额度（需控制台登录态）｜ Gemini、二狗API（无公开额度接口）。</div>
 </div>
</div>
</div></div>
</div></div>
{_settings_html()}
<script src="js/Sortable.min.js"></script>
<script>{JS}</script>
<script>{SETTINGS_JS}</script>
</body></html>"""
