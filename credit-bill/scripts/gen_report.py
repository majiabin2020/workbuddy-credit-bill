# -*- coding: utf-8 -*-
"""WorkBuddy 积分账单（credit-bill）— 积分花在哪了：按模型/任务/每一步的账单生成器

扫描 ~/.workbuddy/projects/<工作空间>/*.jsonl（含 subagents/ 子代理日志），
把每个会话还原成「用户提问 → 思考 → 调用工具/Skill/MCP → 拿到结果 → 再思考」
的完整流程，并逐步标注真实 token 构成与官方积分，生成单文件自包含 HTML。

跨平台：Windows / macOS / Linux 通用，仅依赖 Python 标准库（3.8+），无需安装任何依赖。

用法：
    python gen_report.py                      # 输出到当前目录 credit-bill.html
    python gen_report.py --out 看板.html       # 自定义输出路径
    python gen_report.py --projects <目录>     # 自定义会话日志目录
    python gen_report.py --days 90            # 完整回放嵌入最近 N 天的会话（默认 90，0=全部）
    python gen_report.py --full               # 思考/工具返回不截断（HTML 体积会变大）
"""
import argparse, json, glob, os, sys, datetime, re

# Windows 控制台 GBK 环境下防止中文 print 报错
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

_ap = argparse.ArgumentParser(description="WorkBuddy 积分账单生成器")
_ap.add_argument("--projects", default=os.path.join(os.path.expanduser("~"), ".workbuddy", "projects"),
                 help="WorkBuddy 会话日志目录（默认 ~/.workbuddy/projects）")
_ap.add_argument("--out", default="credit-bill.html", help="输出 HTML 路径（默认当前目录 credit-bill.html）")
_ap.add_argument("--days", type=int, default=30, help="完整回放嵌入最近 N 天的会话（默认 30，0=全部）")
_ap.add_argument("--full", action="store_true", help="思考/工具返回不截断")
_args = _ap.parse_args()

PROJECTS = _args.projects
OUT = _args.out
REPLAY_DAYS = _args.days
FULL = _args.full
T_THINK = 100000 if FULL else 350      # 思考原文保留长度
T_ARGS = 100000 if FULL else 250       # 工具参数保留长度
T_RES = 100000 if FULL else 250        # 工具返回保留长度
T_MSG = 100000 if FULL else 250        # 用户消息/回复保留长度
MAX_EVENTS = 100000 if FULL else 600   # 单会话回放事件上限（超出保留首 300 + 尾 300）


def parse_ts(v):
    if isinstance(v, (int, float)):
        if v > 1e12: return v / 1000.0
        if v > 1e9: return float(v)
    if isinstance(v, str):
        s = v.strip()
        for fmt_ in ('%Y-%m-%dT%H:%M:%S.%fZ', '%Y-%m-%dT%H:%M:%SZ',
                     '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f'):
            try:
                import calendar
                return calendar.timegm(datetime.datetime.strptime(s, fmt_).timetuple())
            except Exception:
                pass
        try: return float(s) / 1000.0
        except Exception: return None
    return None


def clip(s, n):
    if not s: return ''
    s = str(s)
    return s if len(s) <= n else s[:n] + ' …'


def grab_usage(o):
    """从一条记录里抽请求级 usage（token 构成 + 官方积分）。返回 None 表示该记录不带 usage。"""
    pd = o.get('providerData')
    if not isinstance(pd, dict): return None
    ru = pd.get('rawUsage') or {}
    if not ru.get('total_tokens'): return None
    ptd = ru.get('prompt_tokens_details') or {}
    ch = ru.get('prompt_cache_hit_tokens')
    if ch is None: ch = ptd.get('cached_tokens') or 0
    ctd = ru.get('completion_tokens_details') or {}
    model = pd.get('model') or pd.get('requestModelId') or pd.get('requestModelName') or 'unknown'
    return dict(model=model,
                it=int(ru.get('prompt_tokens') or 0),
                ot=int(ru.get('completion_tokens') or 0),
                ch=int(ch or 0),
                think=int(ctd.get('reasoning_tokens') or 0),
                credit=(float(ru['credit']) if ru.get('credit') is not None else None))


def session_title(o):
    """从用户消息提取标题候选（前 42 字）：剥 system-reminder 包装 → 标签取 summary → 剥首层标签取内文。"""
    if o.get('role') != 'user': return None
    c = o.get('content')
    texts = []
    if isinstance(c, str): texts = [c]
    elif isinstance(c, list):
        for seg in c:
            if isinstance(seg, dict) and seg.get('type') in ('input_text', 'text'):
                texts.append(seg.get('text') or '')
    for t in texts:
        if not t.strip(): continue
        t = re.sub(r'<system-reminder[\s\S]*?</system-reminder>', '', t).strip()
        if not t: continue
        if t.startswith('<'):
            m = re.match(r'<(\w+)([^>]*)>', t)
            if not m: continue
            sm = re.search(r'summary="([^"]+)"', m.group(2))
            if sm: return sm.group(1).strip()[:42]
            inner = re.sub(r'^<[^>]*>', '', t).strip()
            if inner and not inner.startswith('<'):
                t = ' '.join(inner.split())
                if t: return t[:42]
            continue
        t = ' '.join(t.split())
        if t: return t[:42]
    return None


def _text_of(o):
    c = o.get('content')
    if isinstance(c, str): return c
    if isinstance(c, list):
        buf = []
        for seg in c:
            if isinstance(seg, dict) and seg.get('type') in ('input_text', 'text', 'output_text'):
                buf.append(seg.get('text') or '')
        return '\n'.join(buf)
    return o.get('message') or ''


def short_ws(name):
    i = name.rfind('-WorkBuddy-')
    if i >= 0 and name[i + 11:]: return name[i + 11:]
    i = name.rfind('-workbuddy-')
    if i >= 0 and name[i + 11:]: return name[i + 11:]
    return name


def ws_display(folder, cwd):
    base = os.path.basename((cwd or '').rstrip('/')) if cwd else ''
    if base:
        m = re.match(r'(automation-)?(\d{4})-(\d{2})-(\d{2})-(\d{2})-(\d{2})-(\d{2})$', base)
        if m:
            return ('自动化 ' if m.group(1) else '临时空间 ') + '%s-%s %s:%s' % (m.group(3), m.group(4), m.group(5), m.group(6))
        return base
    return short_ws(folder)


# ---------- 模型积分倍率（官方文档实时抓取 → 本地缓存 → 内置表） ----------
MULT_FALLBACK = {'glm-5.3': 0.79, 'glm-5.3-flash': 0.06, 'glm-5.2': 0.79, 'glm-5.1': 0.79,
                 'glm-5v-turbo': 0.95, 'kimi-k3': 1.62, 'kimi-k2.8': 0.77, 'kimi-k2.7': 0.57,
                 'kimi-k2.6': 0.52, 'minimax-m3': 0.25, 'hy4': 0.29, 'hy3': 0.0,
                 'deepseek-v4.1-flash': 0.03, 'deepseek-v4-flash': 0.06, 'deepseek-v4-pro': 0.16}
MULT_SRC = '内置表（未联网）'


def fetch_multipliers():
    global MULT_SRC
    cache = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'multipliers.json')
    try:
        from urllib.request import urlopen, Request
        hdr = {'User-Agent': 'Mozilla/5.0'}
        base = 'https://www.workbuddy.cn'
        page = urlopen(Request(base + '/docs/workbuddymini/features/Select-Model', headers=hdr), timeout=8).read().decode('utf-8', 'ignore')
        m = re.search(r'(workbuddymini_features_Select-Model\.md\.[A-Za-z0-9_-]+)((?:\.lean)?\.js)', page)
        if m:
            stem, suffix = m.group(1), m.group(2)
            mult = {}
            for path in (stem + suffix.replace('.lean', ''), stem + suffix):
                try:
                    cj = urlopen(Request(base + '/docs/static/' + path, headers=hdr), timeout=8).read().decode('utf-8', 'ignore')
                except Exception:
                    continue
                pairs = re.findall(r'<strong>([^<]+)</strong></td><td>[^<]*</td><td>(x[0-9.]+|限时免费)</td>', cj)
                for name, v in pairs:
                    mult[name.lower()] = 0.0 if '免费' in v else float(v.lstrip('x'))
                if len(mult) >= 3: break
            if len(mult) >= 3:
                for _k, _v in MULT_FALLBACK.items(): mult.setdefault(_k, _v)
                mult['_fetched_at'] = datetime.datetime.now().strftime('%Y-%m-%d %H:%M')
                try: open(cache, 'w', encoding='utf-8').write(json.dumps(mult, ensure_ascii=False))
                except Exception: pass
                MULT_SRC = '官方文档实时抓取 ' + mult['_fetched_at']
                return mult
    except Exception:
        pass
    if os.path.exists(cache):
        try:
            mult = json.load(open(cache, encoding='utf-8'))
            MULT_SRC = '本地缓存 ' + str(mult.get('_fetched_at', '(时间未知)'))
            return mult
        except Exception:
            pass
    return dict(MULT_FALLBACK)


MULT = fetch_multipliers()


def nominal_mult(model):
    ml = (model or '').lower()
    for k in sorted(MULT.keys(), key=len, reverse=True):
        if not k.startswith('_') and ml.startswith(k): return MULT[k]
    return None


# ---------- 扫描全部会话日志 ----------
now = datetime.datetime.now()
gen_ms = int(now.timestamp() * 1000)
replay_cutoff = gen_ms - REPLAY_DAYS * 86400000 if REPLAY_DAYS > 0 else 0

ws_names, ws_idx, cwd_by_folder = {}, {}, {}
sessions = {}
files = glob.glob(os.path.join(PROJECTS, '**', '*.jsonl'), recursive=True)
if not files:
    print('错误：在 %s 下没有找到任何 .jsonl 会话日志。' % PROJECTS)
    print('如果本机没有用过 WorkBuddy，或日志在别的目录，请用 --projects 指定。')
    sys.exit(1)

for fp in files:
    rel = os.path.relpath(fp, PROJECTS)
    parts = rel.split(os.sep)
    folder = parts[0]
    # 二级文件 = 会话文件；三级及更深（subagents/agent-*.jsonl）归属父会话
    sid = os.path.splitext(parts[1])[0] if len(parts) == 2 else parts[1]
    is_sub = len(parts) > 2
    if folder not in ws_idx:
        ws_names[folder] = short_ws(folder)
        ws_idx[folder] = len(ws_idx)
    title = None; title_ai = None
    events = []          # 回放时间线（有序）
    pending_calls = {}   # callId -> 工具事件（等 result 合入）
    with open(fp, encoding='utf-8', errors='ignore') as fh:
        for line in fh:
            try: o = json.loads(line)
            except Exception: continue
            if not isinstance(o, dict): continue
            if not is_sub and o.get('sessionId'): sid = o['sessionId']
            cwdo = o.get('cwd')
            if cwdo:
                cc = cwd_by_folder.setdefault(folder, {})
                cc[cwdo] = cc.get(cwdo, 0) + 1
            ts = parse_ts(o.get('timestamp')) or parse_ts((o.get('_meta') or {}).get('timestamp'))
            t = o.get('type')
            if t == 'ai-title' and o.get('aiTitle'):
                title_ai = str(o['aiTitle']).strip() or title_ai
            if title is None:
                tt = session_title(o)
                if tt: title = tt
            u = grab_usage(o)
            meta = (o.get('_meta') or {})
            pd = o.get('providerData') or meta.get('providerData') or {}
            model = u['model'] if u else (pd.get('model') or pd.get('requestModelId') or '')
            if t == 'message' and o.get('role') == 'user':
                # 剥掉 system-reminder 等包装块，只留用户真正打的内容
                utxt = _text_of(o)
                utxt = re.sub(r'<system-reminder[\s\S]*?</system-reminder>', '', utxt).strip()
                utxt = re.sub(r'<[^>]+>', '', utxt).strip() or _text_of(o)
                events.append(dict(k='user', ts=ts, txt=clip(' '.join(utxt.split()), T_MSG)))
            elif t == 'reasoning':
                events.append(dict(k='think', ts=ts, txt=clip(o.get('content') or o.get('rawContent') or '', T_THINK), model=model, u=u))
            elif t == 'message' and o.get('role') == 'assistant':
                events.append(dict(k='reply', ts=ts, txt=clip(_text_of(o), T_MSG), model=model, u=u))
            elif t == 'function_call':
                ev = dict(k='tool', ts=ts, name=o.get('name') or '?', args=clip(o.get('arguments') or '', T_ARGS),
                          model=model, u=u, sub=is_sub, ok=None, res='')
                cid = o.get('callId') or o.get('id')
                if cid: pending_calls[cid] = ev
                events.append(ev)
            elif t == 'function_call_result':
                cid = o.get('callId')
                ev = pending_calls.get(cid)
                out = o.get('output')
                if isinstance(out, (dict, list)): out = json.dumps(out, ensure_ascii=False)
                if ev is not None:
                    ev['ok'] = not (isinstance(out, str) and out.strip().startswith('Error'))
                    ev['res'] = clip(out or '', T_RES)
    # 收尾：把一直没等到 result 的工具调用补上
    for ev in events:
        if ev['k'] == 'tool' and ev['ok'] is None:
            ev['ok'] = None
    if not events: continue
    events.sort(key=lambda e: e['ts'] or 0)
    # 汇总（只统计带 usage 的步骤，token/积分以官方逐请求真实值为准）——必须在事件截断之前算，保证总数不丢
    tot = dict(it=0, ot=0, ch=0, think=0, credit=0.0, steps=0)
    models_used, tools_used = {}, {}
    for e in events:
        u = e.get('u')
        if e['k'] == 'tool':
            tools_used[e['name']] = tools_used.get(e['name'], 0) + 1
        if u:
            tot['it'] += u['it']; tot['ot'] += u['ot']; tot['ch'] += u['ch']
            tot['think'] += u['think']; tot['credit'] += u['credit'] or 0; tot['steps'] += 1
            models_used[u['model']] = models_used.get(u['model'], 0) + 1
    # 单会话事件数超限：保留首 300 + 尾 300（只影响回放展示，不影响上面的汇总）
    if len(events) > MAX_EVENTS:
        truncated = len(events) - 600
        events = events[:300] + [dict(k='gap', ts=None, txt='中间省略 %d 个事件（生成时加 --full 可见完整流程）' % truncated)] + events[-300:]
    evs_compact = []
    for e in events:
        if e['k'] == 'gap':
            evs_compact.append(dict(k='gap', txt=e['txt'])); continue
        u = e.get('u')
        item = dict(k=e['k'], ts=e['ts'], txt=e.get('txt', ''))
        if e.get('model'): item['m'] = e['model']
        if e['k'] == 'tool':
            item['n'] = e['name']; item['a'] = e.get('args', ''); item['r'] = e.get('res', ''); item['ok'] = e.get('ok')
            if e.get('sub'): item['s'] = 1
        if u:
            item['u'] = dict(it=u['it'], ot=u['ot'], ch=u['ch'], th=u['think'], c=round(u['credit'] or 0, 2))
        evs_compact.append(item)
    first_ts = next((e['ts'] for e in events if e['ts']), None)
    d = sessions.setdefault(sid, dict(id=sid, folder=folder, title=None, title_ai=None, evs=[], tot=dict(it=0, ot=0, ch=0, think=0, credit=0.0, steps=0), models={}, tools={}, st=None))
    if title and not d['title']: d['title'] = title
    if title_ai and not d['title_ai']: d['title_ai'] = title_ai
    if first_ts and not d['st']: d['st'] = first_ts
    d['evs'].extend(evs_compact)
    for k in ('it', 'ot', 'ch', 'think', 'credit', 'steps'): d['tot'][k] += tot[k]
    for k, v in models_used.items(): d['models'][k] = d['models'].get(k, 0) + v
    for k, v in tools_used.items(): d['tools'][k] = d['tools'].get(k, 0) + v

for folder, cc in cwd_by_folder.items():
    if folder in ws_names and cc:
        ws_names[folder] = ws_display(folder, max(cc, key=cc.get))


# ---------- 全局与按模型聚合 ----------
g_tot = dict(it=0, ot=0, ch=0, think=0, credit=0.0, steps=0)
g_models = {}
g_tools = {}
for d in sessions.values():
    for k in ('it', 'ot', 'ch', 'think', 'credit', 'steps'): g_tot[k] += d['tot'][k]
    for m, n in d['models'].items(): g_models[m] = g_models.get(m, 0) + n
    for tn, n in d['tools'].items(): g_tools[tn] = g_tools.get(tn, 0) + n

model_rows = []
for m, steps in sorted(g_models.items(), key=lambda x: -x[1]):
    it = ot = ch = th = cr = 0.0
    for d in sessions.values():
        for e in d['evs']:
            u = e.get('u')
            if u and e.get('m') == m:
                it += u['it']; ot += u['ot']; ch += u['ch']; th += u['th']; cr += u['c']
    tt = it + ot
    model_rows.append(dict(m=m, steps=steps, it=it, ot=ot, ch=ch, think=th, credit=round(cr, 1),
                           rate=(cr / tt * 1e6 if tt else 0), mult=nominal_mult(m)))
model_rows.sort(key=lambda r: -r['credit'])

sess_list = []
for sid, d in sessions.items():
    if d['tot']['steps'] == 0: continue
    tot_credit = d['tot']['credit']
    tot_tok = d['tot']['it'] + d['tot']['ot']
    sess_list.append(dict(id=d['id'][:8], full_id=d['id'],
                          t=d['title_ai'] or d['title'] or '(无标题会话)',
                          w=ws_names.get(d['folder'], short_ws(d['folder'])),
                          st=d['st'] or 0, steps=d['tot']['steps'], credit=round(tot_credit, 1),
                          nc=1 if (tot_credit <= 0 and tot_tok > 0) else 0,
                          it=d['tot']['it'], ot=d['tot']['ot'], ch=d['tot']['ch'], think=d['tot']['think'],
                          models=sorted(d['models'], key=lambda m: -d['models'][m]),
                          tools=dict(sorted(d['tools'].items(), key=lambda x: -x[1])[:6]),
                          replay=bool(d['st'] and d['st'] * 1000 >= replay_cutoff),
                          evs=d['evs'] if (d['st'] and d['st'] * 1000 >= replay_cutoff) else []))
sess_list.sort(key=lambda s: -s['st'])

# ---------- HTML ----------
def esc(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;').replace("'", '&#39;'))


def fmt_tokens(n):
    if n >= 1e8: return '%.2f 亿' % (n / 1e8)
    if n >= 1e4: return '%.1f 万' % (n / 1e4)
    return '{:,}'.format(int(n))


def fmt_dt(ts):
    if not ts: return '—'
    return datetime.datetime.fromtimestamp(ts).strftime('%m-%d %H:%M')


def svg_model_bars():
    """模型积分对比横向条形图（SVG）"""
    rows = [r for r in model_rows if r['credit'] > 0][:12]
    if not rows: return '<p class="muted">暂无积分数据</p>'
    mx = max(r['credit'] for r in rows)
    RH, GAP, X0, WMAX = 26, 10, 150.0, 200.0
    h = len(rows) * (RH + GAP) + 4
    out = ['<svg viewBox="0 0 640 %d" class="chart" role="img" aria-label="模型积分对比">' % h]
    for i, r in enumerate(rows):
        y = i * (RH + GAP) + 2
        w = max(2.0, r['credit'] / mx * WMAX)
        mult_s = ('%.2fx' % r['mult']) if r['mult'] is not None else '—'
        out.append(
            '<text x="0" y="%d" class="sv-lbl">%s</text>'
            '<rect x="%.0f" y="%d" width="%.1f" height="%d" rx="4" fill="var(--accent)" opacity="%.2f"/>'
            '<text x="%.1f" y="%d" class="sv-val">%s 积分｜实际 %.1f/百万token｜标称 %s</text>'
            % (y + RH - 8, esc(r['m']), X0, y + 3, w, RH - 6, 0.9 - i * 0.05, X0 + w + 8, y + RH - 8,
               '{:,.0f}'.format(r['credit']), r['rate'], mult_s))
    out.append('</svg>')
    free_rows = [r for r in model_rows if r['credit'] <= 0 and (r['it'] + r['ot']) > 0]
    if free_rows:
        names = '、'.join('%s（%s token）' % (esc(r['m']), fmt_tokens(r['it'] + r['ot']))
                          for r in sorted(free_rows, key=lambda x: -(x['it'] + x['ot']))[:6])
        out.append('<p class="muted" style="margin-top:10px">上图只画了计费模型。另有几个模型的日志积分一直是 0，'
                   '它们走的是免费或预览通道，不扣积分：%s。token 是真实消耗的，只是平台不扣积分，'
                   '所以这类会话在列表里显示「不计积分」。</p>' % names)
    return ''.join(out)


def svg_story():
    """「一笔积分账怎么花出去的」图解：流程框 + 滚雪球柱 + 公式卡，替代纯文字解释"""
    T, M, B = 'var(--text)', 'var(--muted)', 'var(--border)'
    A, W = 'var(--accent)', 'var(--credit)'
    o = ['<svg viewBox="0 0 680 316" class="chart" role="img" aria-label="积分账单形成过程图解">']

    # —— 第一行：一次请求的生命周期（4 个框 + 箭头 + 回环）——
    boxes = ['你提问', 'agent 思考', '调用工具/技能', '结果塞回上下文']
    for i, txt in enumerate(boxes):
        x0 = 10 + i * 168
        o.append('<rect x="%d" y="10" width="148" height="46" rx="8" fill="var(--card)" stroke="%s" stroke-width="1.2"/>'
                 '<text x="%d" y="38" text-anchor="middle" fill="%s" font-size="13" font-weight="600">%s</text>'
                 % (x0, B, x0 + 74, T, esc(txt)))
        if i < 3:
            ax = x0 + 150
            o.append('<line x1="%d" y1="33" x2="%d" y2="33" stroke="%s" stroke-width="1.4"/><polygon points="%d,29 %d,33 %d,29" fill="%s"/>'
                     % (ax, ax + 14, M, ax + 10, ax + 16, ax + 10, M))
    # 回环箭头：第 4 框 → 第 2 框（下一轮把全部历史再发一遍，循环）
    o.append('<path d="M 594 58 L 594 76 L 264 76 L 264 62" fill="none" stroke="%s" stroke-width="1.3" stroke-dasharray="4 3"/>'
             '<polygon points="260,66 264,58 268,66" fill="%s"/>'
             '<text x="429" y="72" text-anchor="middle" fill="%s" font-size="11.5">下一轮带着全部历史再来一遍，循环 N 轮</text>' % (M, M, M))

    # —— 第二行左：上下文滚雪球柱 ——
    snow = [('第1轮', 3), ('第2轮', 5.5), ('第3轮', 8), ('第4轮', 11), ('第N轮', 15)]
    base, bw, gap, x0 = 244, 40, 18, 46
    mx = max(v for _, v in snow)
    o.append('<text x="10" y="104" fill="%s" font-size="12.5" font-weight="600">上下文滚雪球：每轮请求都把历史再发一遍</text>' % T)
    for i, (lbl, v) in enumerate(snow):
        h = v / mx * 108
        x = x0 + i * (bw + gap)
        o.append('<rect x="%d" y="%.1f" width="%d" height="%.1f" rx="4" fill="var(--it)" opacity="%.2f"/>'
                 '<text x="%d" y="%.1f" text-anchor="middle" fill="%s" font-size="10.5">%.0f万</text>'
                 '<text x="%d" y="258" text-anchor="middle" fill="%s" font-size="11">%s</text>'
                 % (x, base - h, bw, h, 0.45 + i * 0.12, x + bw / 2, base - h - 5, M, v,
                    x + bw / 2, T, esc(lbl)))
    o.append('<text x="46" y="276" fill="%s" font-size="11">柱子 = 每轮请求发送的 token（示意），最后一轮的积分由全部轮次加总</text>' % M)

    # —— 第二行右：两张卡片 ——
    rx = 386
    o.append('<rect x="%d" y="112" width="284" height="72" rx="8" fill="var(--card)" stroke="%s" stroke-width="1.2"/>'
             '<text x="%d" y="136" fill="%s" font-size="12" font-weight="600">积分怎么算</text>'
             '<text x="%d" y="158" fill="%s" font-size="12.5">积分 = 每轮 token 加总 × 模型单价</text>'
             '<text x="%d" y="176" fill="%s" font-size="10.5">单价≈标称倍率，缓存/思考/输出计价各不同</text>'
             % (rx, B, rx + 14, T, rx + 14, W, rx + 14, M))
    # 动态例证卡
    cand = [r for r in model_rows if r['credit'] > 0 and r['steps'] >= 20]
    if cand:
        top = max(cand, key=lambda r: r['credit'])
        thr = min(cand, key=lambda r: r['rate'])
        o.append('<rect x="%d" y="194" width="284" height="72" rx="8" fill="var(--card)" stroke="%s" stroke-width="1.2"/>'
                 '<text x="%d" y="218" fill="%s" font-size="12" font-weight="600">本机真实账单</text>'
                 '<text x="%d" y="240" fill="%s" font-size="12">花最多：%s，实际 %.1f 积分/百万token</text>'
                 '<text x="%d" y="258" fill="%s" font-size="12">最省：%s，实际 %.1f 积分/百万token</text>'
                 % (rx, B, rx + 14, T, rx + 14, T, esc(top['m']), top['rate'],
                    rx + 14, M, esc(thr['m']), thr['rate']))

    # —— 结论条 ——
    o.append('<rect x="10" y="282" width="660" height="30" rx="8" fill="var(--accent)" fill-opacity="0.08"/>'
             '<text x="340" y="301" text-anchor="middle" fill="%s" font-size="12.5" font-weight="600">'
             '倍率只是单价，任务决定烧多少 token。任务越复杂、轮次越多，倍率再低也能烧出高积分。</text>' % T)
    o.append('</svg>')
    return ''.join(o)


def build_story():
    """故事卡 = 图解 + 一行补充说明"""
    s = [svg_story()]
    s.append('<p class="muted" style="margin:12px 2px 0">看懂这张图，就看懂了积分账单：倍率决定的是「每单位 token 多少钱」，'
             '而账单 = 单价 × 实际烧掉的 token。agent 每一轮思考、每一次读文件、每一次调用工具或技能，'
             '返回的结果都会塞回上下文，跟着下一轮请求原样再发一遍——任务越复杂、轮次越多、工具回填越厚，'
             'token 总量就越大。下面每个会话的「上下文滚雪球」图，说的就是这件事在你电脑上的真实发生过程。</p>')
    return ''.join(s)


def build_session_rows():
    out = []
    for s in sess_list[:200]:
        tools = '、'.join('%s×%d' % (esc(k), v) for k, v in list(s['tools'].items())[:3]) or '—'
        badge = '' if s['replay'] else '<span class="tag tag-warn">超出回放范围</span>'
        credit_s = ('<span class="muted" title="本会话模型走免费/预览通道，平台日志回传积分为 0，不扣积分">不计积分</span>'
                    if s.get('nc') else '{:,.0f}'.format(s['credit']))
        out.append(
            '<tr class="sess-row%s" data-id="%s">'
            '<td class="nowrap">%s</td>'
            '<td class="s-title" title="%s">%s %s</td>'
            '<td class="nowrap muted">%s</td>'
            '<td class="num">%s</td>'
            '<td class="num strong">%s</td>'
            '<td class="nowrap muted">%s</td>'
            '<td class="muted tools-cell">%s</td>'
            '</tr>'
            % ('' if s['replay'] else ' no-replay', esc(s['id']), fmt_dt(s['st']),
               esc(s['full_id']), esc(s['t']), badge, esc(s['w']),
               '{:,}'.format(s['steps']), credit_s,
               esc('、'.join(s['models'][:2])), tools))
    return ''.join(out)


DATA = dict(
    gen=datetime.datetime.now().strftime('%Y-%m-%d %H:%M'),
    mult_src=MULT_SRC,
    projects=PROJECTS,
    replay_days=REPLAY_DAYS,
    total=dict(sessions=len(sess_list), steps=g_tot['steps'], it=g_tot['it'], ot=g_tot['ot'],
               ch=g_tot['ch'], think=g_tot['think'], credit=round(g_tot['credit'], 1)),
    models=model_rows,
    tools=[dict(n=k, c=v) for k, v in sorted(g_tools.items(), key=lambda x: -x[1])[:15]],
    sessions=[dict(id=s['id'], t=s['t'], w=s['w'], st=s['st'], steps=s['steps'], credit=s['credit'],
                   nc=s.get('nc', 0),
                   it=s['it'], ot=s['ot'], ch=s['ch'], think=s['think'], models=s['models'],
                   tools=s['tools'], replay=s['replay'], evs=s['evs']) for s in sess_list[:200]],
)
DATA_JSON = json.dumps(DATA, ensure_ascii=False, separators=(',', ':')).replace('</', '<\\/')

tool_tags = ''.join('<span class="tool-tag">%s ×%s</span>' % (esc(t['n']), t['c']) for t in DATA['tools'][:10])
story_html = build_story()
cache_hit_pct = (g_tot['ch'] / g_tot['it'] * 100) if g_tot['it'] else 0

HTML = r'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkBuddy 积分账单 · 积分花在哪，一笔一笔看清楚</title>
<style>
:root{
  --bg:#f5f6f8; --card:#ffffff; --text:#1f2329; --muted:#6b7280; --border:#e5e6eb;
  --accent:#5b5bd6; --accent-2:#7c5cd6; --ok:#0e9f6e; --warn:#d97706; --bad:#dc2626;
  --it:#3b82f6; --ch:#bfdbfe; --think:#8b5cf6; --ot:#0e9f6e; --credit:#f59e0b;
  --code-bg:#f3f4f6;
}
html[data-theme="dark"]{
  --bg:#14161b; --card:#1e2128; --text:#e5e6eb; --muted:#9ca3af; --border:#333844;
  --accent:#8b8bf0; --accent-2:#a78bfa; --it:#60a5fa; --ch:#1e3a5f; --think:#a78bfa;
  --ot:#34d399; --credit:#fbbf24; --code-bg:#262a33;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.65 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB","Microsoft YaHei",sans-serif;}
.wrap{max-width:1080px;margin:0 auto;padding:24px 16px 80px}
header.top{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;flex-wrap:wrap;margin-bottom:8px}
h1{font-size:22px;margin:0;letter-spacing:.5px}
.sub{color:var(--muted);font-size:13px;margin-top:4px}
.btn{background:var(--card);border:1px solid var(--border);color:var(--text);border-radius:8px;padding:6px 14px;cursor:pointer;font-size:13px}
.btn:hover{border-color:var(--accent)}
.btn.primary{background:var(--accent);border-color:var(--accent);color:#fff}
.btn.act{background:var(--accent);border-color:var(--accent);color:#fff}
.card{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:18px 20px;margin-top:16px}
h2{font-size:16px;margin:0 0 12px;display:flex;align-items:center;gap:8px}
h2 .dot{width:8px;height:8px;border-radius:50%;background:var(--accent);display:inline-block}
.muted{color:var(--muted)}
.nowrap{white-space:nowrap}
.num{text-align:right;font-variant-numeric:tabular-nums}
.strong{font-weight:600}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--border);border-radius:12px;padding:14px 16px}
.kpi .v{font-size:22px;font-weight:700;font-variant-numeric:tabular-nums}
.kpi .l{font-size:12px;color:var(--muted);margin-top:2px}
.kpi .v em{font-style:normal;font-size:12px;color:var(--muted);font-weight:400;margin-left:2px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;padding:8px 10px;border-bottom:1px solid var(--border);white-space:nowrap}
td{padding:9px 10px;border-bottom:1px solid var(--border);vertical-align:top}
tr:last-child td{border-bottom:none}
.sess-row{cursor:pointer}
.sess-row:hover{background:rgba(91,91,214,.06)}
.sess-row.sel{background:rgba(91,91,214,.10)}
.sess-row.no-replay{opacity:.62}
.s-title{max-width:340px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.tools-cell{max-width:200px;font-size:12px}
.tag{display:inline-block;font-size:11px;padding:1px 7px;border-radius:20px;background:rgba(91,91,214,.12);color:var(--accent);margin-left:6px;vertical-align:1px}
.tag-warn{background:rgba(217,119,6,.14);color:var(--warn)}
.tool-tag{display:inline-block;background:var(--code-bg);border:1px solid var(--border);border-radius:6px;padding:2px 8px;margin:0 6px 6px 0;font-size:12px;color:var(--muted)}
.chart{width:100%;height:auto;display:block}
.sv-lbl{font-size:11.5px;fill:var(--text)}
.sv-val{font-size:11px;fill:var(--muted)}
.story{background:linear-gradient(135deg,rgba(91,91,214,.07),rgba(124,92,214,.07));border:1px solid rgba(91,91,214,.25);border-radius:12px;padding:16px 18px;line-height:1.9}
.story b{color:var(--accent)}
.filters{display:flex;gap:8px;margin:4px 0 12px;flex-wrap:wrap}
.replay-head{display:flex;justify-content:space-between;align-items:baseline;flex-wrap:wrap;gap:8px}
.replay-stats{display:flex;gap:18px;flex-wrap:wrap;font-size:12.5px;color:var(--muted);margin:8px 0 4px}
.replay-stats b{color:var(--text);font-variant-numeric:tabular-nums}
.charts2{display:grid;grid-template-columns:1fr 1fr;gap:14px}
@media(max-width:760px){.charts2{grid-template-columns:1fr}}
.turn{margin-top:18px}
.turn-q{background:var(--code-bg);border-left:3px solid var(--accent);border-radius:0 8px 8px 0;padding:8px 12px;font-size:13px;margin-bottom:8px}
.turn-q .qn{color:var(--muted);font-size:11px;margin-right:8px}
.step{display:flex;gap:10px;padding:7px 4px;border-bottom:1px dashed var(--border);align-items:flex-start}
.step:last-child{border-bottom:none}
.step .ix{flex:0 0 26px;color:var(--muted);font-size:11px;text-align:right;padding-top:3px;font-variant-numeric:tabular-nums}
.badge{flex:0 0 auto;font-size:11px;padding:2px 8px;border-radius:6px;white-space:nowrap;margin-top:1px}
.b-think{background:rgba(139,92,246,.14);color:var(--think)}
.b-reply{background:rgba(14,159,110,.13);color:var(--ok)}
.b-tool{background:rgba(59,130,246,.13);color:var(--it)}
.b-user{background:rgba(91,91,214,.14);color:var(--accent)}
.b-sub{background:rgba(217,119,6,.14);color:var(--warn);margin-left:4px}
.step .body{flex:1;min-width:0}
.step .txt{word-break:break-all}
.step .txt.fold{color:var(--muted)}
.expand{font-size:11px;color:var(--accent);cursor:pointer;margin-left:8px;user-select:none;white-space:nowrap}
.expand:hover{text-decoration:underline}
.detail{display:none;margin-top:6px;background:var(--code-bg);border-radius:8px;padding:10px 12px;font-size:12px;white-space:pre-wrap;word-break:break-all;max-height:320px;overflow:auto}
.detail.open{display:block}
.tok{display:inline-flex;height:8px;border-radius:4px;overflow:hidden;vertical-align:1px;margin-right:8px;min-width:60px}
.tok i{display:block;height:100%}
.tok .t-it{background:var(--it)} .tok .t-ch{background:var(--ch)} .tok .t-th{background:var(--think)} .tok .t-ot{background:var(--ot)}
.step .meta{flex:0 0 auto;text-align:right;font-size:11px;color:var(--muted);font-variant-numeric:tabular-nums;white-space:nowrap;padding-top:2px}
.step .meta .cr{color:var(--credit);font-weight:600}
.ok-y{color:var(--ok)} .ok-n{color:var(--bad)}
.gap-note{text-align:center;color:var(--warn);font-size:12px;padding:10px;background:rgba(217,119,6,.08);border-radius:8px;margin:8px 0}
footer{margin-top:28px;color:var(--muted);font-size:12px;line-height:1.9;border-top:1px solid var(--border);padding-top:14px}
.legend{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--muted);margin:6px 0 0}
.legend i{display:inline-block;width:10px;height:10px;border-radius:2px;margin-right:5px;vertical-align:-1px}
.empty{padding:40px;text-align:center;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
<header class="top">
  <div>
    <h1>WorkBuddy 积分账单</h1>
    <div class="sub">积分花在哪了 · 哪些模型哪些任务花得多 · 每一笔都有明细 · 生成于 __GEN__</div>
  </div>
  <button class="btn" id="themeBtn">切换深浅色</button>
</header>

<div class="kpis">
  <div class="kpi"><div class="v" style="color:var(--credit)">__N_CREDIT__<em>积分</em></div><div class="l">本期积分总支出</div></div>
  <div class="kpi"><div class="v">__N_SESS__<em>笔</em></div><div class="l">消费笔数（会话）</div></div>
  <div class="kpi"><div class="v">__N_STEP__<em>步</em></div><div class="l">模型请求步骤</div></div>
  <div class="kpi"><div class="v">__N_TOK__</div><div class="l">Token 合计（输入+输出）</div></div>
  <div class="kpi"><div class="v">__N_THINK__</div><div class="l">其中思考 token</div></div>
  <div class="kpi"><div class="v">__N_CACHE__<em>%</em></div><div class="l">输入缓存命中率</div></div>
</div>

<div class="card">
  <h2><span class="dot"></span>为什么倍率低的模型，反而更耗积分？</h2>
  <div class="story">__STORY__</div>
</div>

<div class="card">
  <h2><span class="dot"></span>钱都花在哪些模型上了 <span class="muted" style="font-weight:400;font-size:12px">（标称倍率只是名义参考，实际每百万 token 积分才是真实单价）</span></h2>
  __MODEL_BARS__
  <div class="legend">
    <span><i style="background:var(--accent)"></i>该模型累计积分（条形长度）</span>
    <span>倍率来源：__MULT_SRC__</span>
  </div>
</div>

<div class="card">
  <h2><span class="dot"></span>积分都经了哪些工具的手（调用 TOP10）</h2>
  <div>__TOOL_TAGS__</div>
  <div class="legend">任务越复杂，工具/Skill/MCP 调用越密集，回填进上下文的内容就越多——这是 token 涨得快的主因之一。</div>
</div>

<div class="card">
  <h2><span class="dot"></span>哪些任务花得多？消费明细清单 <span class="muted" style="font-weight:400;font-size:12px">（点任意一行，看这一笔的完整明细）</span></h2>
  <div class="filters" id="filters">
    <button class="btn act" data-d="7">近 7 天</button>
    <button class="btn" data-d="30">近 30 天</button>
    <button class="btn" data-d="90">近 90 天</button>
    <button class="btn" data-d="0">全部</button>
    <span style="width:14px"></span>
    <button class="btn act" data-s="time">按时间</button>
    <button class="btn" data-s="credit">按积分（多→少）</button>
  </div>
  <div style="overflow-x:auto">
  <table>
    <thead><tr><th>时间</th><th>会话标题</th><th>工作空间</th><th class="num">请求步骤</th><th class="num">积分</th><th>模型</th><th>主要工具</th></tr></thead>
    <tbody id="sessBody">__SESS_ROWS__</tbody>
  </table>
  </div>
</div>

<div class="card" id="replayCard" style="display:none">
  <div id="replayBox"></div>
</div>

<footer>
  <div>数据说明：本看板全部来自本机 WorkBuddy 会话日志（__PROJECTS__），token 与积分为平台逐请求回传的真实值（与 App 账单同源），全程在本机处理，不上传任何数据。</div>
  <div>回放范围：近 __REPLAY_DAYS__ 天的会话嵌入了完整流程，更早的会话只显示汇总；生成时加 --days 0 可嵌入全部，加 --full 可看完整的思考与工具返回原文。</div>
  <div>「步骤」= 一次模型请求；「输入」含缓存命中部分；思考 token 已含在输出内单列展示。</div>
</footer>
</div>

<script>
var DATA = __DATA_JSON__;
var $ = function(id){ return document.getElementById(id); };
var curNC = false;  // 当前回放会话是否为免积分（免费/预览）模型
var FREE = {};      // 日志积分恒为 0 的模型（免费/预览通道）
DATA.models.forEach(function(r){ if(r.credit<=0 && (r.it+r.ot)>0) FREE[r.m]=1; });
function fmtTok(n){ if(n>=1e8) return (n/1e8).toFixed(2)+' 亿'; if(n>=1e4) return (n/1e4).toFixed(1)+' 万'; return n.toLocaleString(); }
function fmtDt(ts){ if(!ts) return '—'; var d=new Date(ts*1000); function p(x){return (x<10?'0':'')+x;} return p(d.getMonth()+1)+'-'+p(d.getDate())+' '+p(d.getHours())+':'+p(d.getMinutes()); }
function esc(s){ return String(s==null?'':s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;'); }

// 主题（localStorage 在某些内嵌预览的沙箱里会禁用，必须兜底，否则整页脚本崩掉）
var theme = 'light';
try { theme = localStorage.getItem('tx-theme') || 'light'; } catch(e) {}
document.documentElement.setAttribute('data-theme', theme);
$('themeBtn').onclick = function(){ theme = theme==='light'?'dark':'light'; document.documentElement.setAttribute('data-theme', theme); try { localStorage.setItem('tx-theme', theme); } catch(e) {} };

// 窗口过滤 + 排序
var curDays = 7;
var curSort = 'time';
function renderList(){
  var cut = curDays>0 ? (Date.now()/1000 - curDays*86400) : 0;
  var rows = [];
  DATA.sessions.forEach(function(s){ if(!(cut && s.st < cut)) rows.push(s); });
  if(curSort === 'credit'){
    // 免积分会话排最后，其余按积分从多到少
    rows.sort(function(a,b){ var ca = a.nc ? -1 : a.credit, cb = b.nc ? -1 : b.credit; return cb !== ca ? cb - ca : (b.st||0)-(a.st||0); });
  } else {
    rows.sort(function(a,b){ return (b.st||0)-(a.st||0); });
  }
  var html = '';
  rows.forEach(function(s){
    var tools = Object.keys(s.tools||{}).slice(0,3).map(function(k){return esc(k)+'×'+s.tools[k];}).join('、') || '—';
    html += '<tr class="sess-row'+(s.replay?'':' no-replay')+'" data-id="'+esc(s.id)+'">'
      + '<td class="nowrap">'+fmtDt(s.st)+'</td>'
      + '<td class="s-title" title="'+esc(s.id)+'">'+esc(s.t)+(s.replay?'':' <span class="tag tag-warn">超出回放范围</span>')+'</td>'
      + '<td class="nowrap muted">'+esc(s.w)+'</td>'
      + '<td class="num">'+s.steps.toLocaleString()+'</td>'
      + '<td class="num strong">'+(s.nc?'<span class="muted" title="本会话模型走免费/预览通道，平台日志回传积分为 0，不扣积分">不计积分</span>':s.credit.toLocaleString())+'</td>'
      + '<td class="nowrap muted">'+esc((s.models||[]).slice(0,2).join('、'))+'</td>'
      + '<td class="muted tools-cell">'+tools+'</td></tr>';
  });
  $('sessBody').innerHTML = html || '<tr><td colspan="7" class="empty">该时间范围内没有会话</td></tr>';
}
Array.prototype.forEach.call(document.querySelectorAll('#filters .btn[data-d]'), function(b){
  b.onclick = function(){
    Array.prototype.forEach.call(document.querySelectorAll('#filters .btn[data-d]'), function(x){x.classList.remove('act');});
    b.classList.add('act'); curDays = +b.dataset.d; renderList();
  };
});
Array.prototype.forEach.call(document.querySelectorAll('#filters .btn[data-s]'), function(b){
  b.onclick = function(){
    Array.prototype.forEach.call(document.querySelectorAll('#filters .btn[data-s]'), function(x){x.classList.remove('act');});
    b.classList.add('act'); curSort = b.dataset.s; renderList();
  };
});
renderList();

// 会话行点击 → 回放
function tokBar(u){
  if(!u) return '';
  var tot = u.it + u.ot;
  if(!tot) return '';
  var p = function(v){ return (v/tot*100).toFixed(1)+'%'; };
  return '<span class="tok" title="输入(未命中) '+u.it.toLocaleString()+' · 缓存命中 '+u.ch.toLocaleString()+' · 思考 '+u.th.toLocaleString()+' · 输出 '+u.ot.toLocaleString()+'">'
    + '<i class="t-ch" style="width:'+p(u.ch)+'"></i><i class="t-it" style="width:'+p(Math.max(0,u.it-u.ch))+'"></i>'
    + '<i class="t-th" style="width:'+p(u.th)+'"></i><i class="t-ot" style="width:'+p(u.ot)+'"></i></span>';
}
function trendChart(evs){
  var steps = evs.filter(function(e){return e.u;}).slice(-300);  // 只画最近 300 步，防止超长会话柱子糊成一片
  if(!steps.length) return '';
  var W=640, H=120, pad=2;
  var mx = 0; steps.forEach(function(e){ mx = Math.max(mx, e.u.it); });
  if(!mx) return '';
  var bw = Math.max(1.5, (W - pad*(steps.length-1)) / steps.length);
  var bars = '', credits = '';
  steps.forEach(function(e, i){
    var x = i*(bw+pad);
    var hCh = e.u.ch/mx*(H-14), hIt = Math.max(0,(e.u.it-e.u.ch))/mx*(H-14);
    bars += '<rect x="'+x.toFixed(1)+'" y="'+(H-14-hIt).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+hIt.toFixed(1)+'" fill="var(--it)"><title>第'+(i+1)+'步 未命中输入 '+e.u.it.toLocaleString()+'</title></rect>'
          + '<rect x="'+x.toFixed(1)+'" y="'+(H-14-hIt-hCh).toFixed(1)+'" width="'+bw.toFixed(1)+'" height="'+hCh.toFixed(1)+'" fill="var(--ch)"><title>第'+(i+1)+'步 缓存命中 '+e.u.ch.toLocaleString()+'</title></rect>';
  });
  // 累计积分折线（右轴）
  var cum = 0, pts = [];
  steps.forEach(function(e, i){ cum += e.u.c; pts.push([i*(bw+pad)+bw/2, cum]); });
  var cmx = cum || 1;
  var d = pts.map(function(p, i){ return (i?'L':'M')+p[0].toFixed(1)+','+(H-2-(p[1]/cmx*(H-20))).toFixed(1); }).join(' ');
  return '<svg viewBox="0 0 '+W+' '+H+'" class="chart"><g>'+bars+'</g>'
    + '<path d="'+d+'" fill="none" stroke="var(--credit)" stroke-width="1.6" opacity="0.9"/>'
    + '<text x="2" y="10" class="sv-val">每步输入 token（蓝=未命中，浅蓝=缓存命中）｜橙线=累计积分</text></svg>';
}
function stepHtml(e, i){
  if(e.k==='gap') return '<div class="gap-note">'+esc(e.txt)+'</div>';
  var badge, body = '', meta = '';
  if(e.k==='user'){ badge = '<span class="badge b-user">提问</span>'; body = '<span class="txt fold">'+esc(e.txt)+'</span>'; }
  else if(e.k==='think'){ badge = '<span class="badge b-think">思考</span>'; body = '<span class="txt fold">'+esc(e.txt)+'</span>'; }
  else if(e.k==='reply'){ badge = '<span class="badge b-reply">回复</span>'; body = '<span class="txt fold">'+esc(e.txt)+'</span>'; }
  else if(e.k==='tool'){
    badge = '<span class="badge b-tool">'+esc(e.n||'工具')+'</span>';
    if(e.s) badge += '<span class="badge b-sub">子代理</span>';
    var ok = e.ok===true?'<span class="ok-y">成功</span>':(e.ok===false?'<span class="ok-n">失败</span>':'');
    body = '<span class="txt fold">'+esc(e.a)+'</span> <span class="muted">'+ok+'</span>'
         + '<span class="expand" onclick="toggleDetail(this)">参数/返回 ▾</span>'
         + '<div class="detail">'+esc(e.a)+'\n\n—— 返回 ——\n'+esc(e.r)+'</div>';
  }
  var u = e.u;
  if(u) meta = tokBar(u) + '<br>' + (u.c>0?'<span class="cr">'+u.c.toFixed(1)+' 积分</span>':((curNC||FREE[e.m])?'<span class="muted">不计积分</span>':'0 积分'))
         + ' · '+fmtTok(u.it+u.ot)+' tok';
  return '<div class="step"><div class="ix">'+(i+1)+'</div>'+badge+'<div class="body">'+body+'</div><div class="meta">'+meta+'</div></div>';
}
function toggleDetail(el){
  var d = el.parentNode.querySelector('.detail');
  if(d) d.classList.toggle('open');
}
function openSession(id){
  var s = null;
  DATA.sessions.forEach(function(x){ if(x.id===id) s=x; });
  if(!s){ return; }
  Array.prototype.forEach.call(document.querySelectorAll('.sess-row'), function(r){ r.classList.toggle('sel', r.dataset.id===id); });
  if(!s.replay){
    // 生成时没嵌入这个会话的完整流程：给出明确提示，不再静默无反应
    $('replayBox').innerHTML = '<div class="replay-head"><h2 style="margin:0">'+esc(s.t)+'</h2><span class="muted">'+fmtDt(s.st)+' · '+esc(s.w)+'</span></div>'
      + '<div style="padding:28px 6px;line-height:2">'
      + '<div style="font-weight:600;margin-bottom:6px">这个会话只有汇总，没有逐步流程</div>'
      + '<div class="muted">生成看板时默认只把近 '+DATA.replay_days+' 天的会话嵌入完整回放，这个会话不在范围内（共 '+s.steps.toLocaleString()+' 步请求、'+(s.nc?'不计积分':s.credit.toLocaleString()+' 积分')+'、'+fmtTok(s.it+s.ot)+' token）。</div>'
      + '<div>想看它的完整流程，在对话里对 AI 说一句「<b>重新生成积分账单，回放全部会话</b>」就行了。</div>'
      + '</div>';
    $('replayCard').style.display = '';
    $('replayCard').scrollIntoView({behavior:'smooth', block:'start'});
    return;
  }
  curNC = !!s.nc;
  var evs = s.evs || [];
  var html = '<div class="replay-head"><h2 style="margin:0">'+esc(s.t)+'</h2><span class="muted">'+fmtDt(s.st)+' · '+esc(s.w)+'</span></div>'
    + '<div class="replay-stats">'
    + '<span>请求步骤 <b>'+s.steps.toLocaleString()+'</b></span>'
    + '<span>积分 <b style="color:var(--credit)">'+(s.nc?'0（免费/预览通道，不扣积分）':s.credit.toLocaleString())+'</b></span>'
    + '<span>Token <b>'+fmtTok(s.it+s.ot)+'</b>（输入 '+fmtTok(s.it)+' / 输出 '+fmtTok(s.ot)+'）</span>'
    + '<span>思考 <b>'+fmtTok(s.think)+'</b></span>'
    + '<span>缓存命中 <b>'+(s.it?Math.round(s.ch/s.it*100):0)+'%</b></span>'
    + '<span>模型 <b>'+esc((s.models||[]).join('、'))+'</b></span>'
    + '</div>'
    + '<div class="charts2"><div>'+trendChart(evs)+'</div><div class="muted" style="font-size:12px;align-self:center">左图就是这个任务的「上下文滚雪球」：柱子越来越高，说明每一轮请求都要把之前所有工具返回重新发一遍——token 主要涨在这里，不是涨在你打的那几个字上。</div></div>'
    + '<div class="legend" style="margin:10px 0 4px"><span><i style="background:var(--it)"></i>未命中输入</span><span><i style="background:var(--ch)"></i>缓存命中输入</span><span><i style="background:var(--think)"></i>思考</span><span><i style="background:var(--ot)"></i>输出</span><span><i style="background:var(--credit)"></i>积分</span></div>'
    + '<div class="turn-box">';
  var ti = 0, tn = 0;
  evs.forEach(function(e){
    if(e.k==='user'){ tn++; ti = 0;
      html += '</div><div class="turn"><div class="turn-q"><span class="qn">第 '+tn+' 次提问</span>'+esc(e.txt)+'</div>'; }
    else { if(tn===0){ html += '<div class="turn">'; tn = 1; } html += stepHtml(e, ti); ti++; }
  });
  html += '</div>';
  $('replayBox').innerHTML = html;
  $('replayCard').style.display = '';
  $('replayCard').scrollIntoView({behavior:'smooth', block:'start'});
}
document.addEventListener('click', function(ev){
  var tr = ev.target.closest ? ev.target.closest('.sess-row') : null;
  if(tr && tr.dataset.id) openSession(tr.dataset.id);
});
</script>
</body>
</html>
'''

html = (HTML
        .replace('__GEN__', esc(DATA['gen']))
        .replace('__PROJECTS__', esc(PROJECTS))
        .replace('__REPLAY_DAYS__', str(REPLAY_DAYS))
        .replace('__N_SESS__', '{:,}'.format(len(sess_list)))
        .replace('__N_STEP__', '{:,}'.format(g_tot['steps']))
        .replace('__N_CREDIT__', '{:,.0f}'.format(g_tot['credit']))
        .replace('__N_TOK__', fmt_tokens(g_tot['it'] + g_tot['ot']))
        .replace('__N_THINK__', fmt_tokens(g_tot['think']))
        .replace('__N_CACHE__', '%.0f' % cache_hit_pct)
        .replace('__STORY__', story_html)
        .replace('__MODEL_BARS__', svg_model_bars())
        .replace('__MULT_SRC__', esc(MULT_SRC))
        .replace('__TOOL_TAGS__', tool_tags)
        .replace('__SESS_ROWS__', build_session_rows())
        .replace('__DATA_JSON__', DATA_JSON))

with open(OUT, 'w', encoding='utf-8') as fh:
    fh.write(html)

print('已生成：%s（%.1f KB）' % (os.path.abspath(OUT), os.path.getsize(OUT) / 1024))
print('会话 %d 个 · 请求步骤 %s · 总积分 %s · 总 token %s（思考 %s，缓存命中 %.0f%%）'
      % (len(sess_list), '{:,}'.format(g_tot['steps']), '{:,.0f}'.format(g_tot['credit']),
         fmt_tokens(g_tot['it'] + g_tot['ot']), fmt_tokens(g_tot['think']), cache_hit_pct))
print('完整回放嵌入：近 %d 天的 %d 个会话' % (REPLAY_DAYS, sum(1 for s in sess_list if s['replay'])))
