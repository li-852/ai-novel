# -*- coding: utf-8 -*-
"""为十卷各生成 `正文/第N卷_卷名/0、总览/总览.md`：
主线与状态、三册构成、幕表、每章一览（章号/章名/册/汉字/一句话内容）、字数与门禁、
钩子、埋坑与计划回收、待回收伏笔、本卷时间线、文档索引。
所有字段取自磁盘正文与 世界设定规则/设定/章节追踪/（时间线.md、钩子.md、章节追踪.yaml）
以及本卷 0、总览 内的蓝图文档，抽不到的留空或标"待补"，不编造剧情。

    python 世界设定规则/脚本/qa/gen_overview.py            # 全十卷
    python 世界设定规则/脚本/qa/gen_overview.py 1 9        # 只做指定卷
"""
import concurrent.futures as cf
import glob
import io
import os
import re
import sys

sys.path.insert(0, '世界设定规则/脚本')
from chapter_gate import gate

TRACK = '世界设定规则/设定/章节追踪'
SEG_ORDER = {'1': '上', '2': '中', '3': '下'}
CHAP = re.compile(r'^第(\d{3})章_(.+)\.md$')
NUM = re.compile(r'^\s*(\d{1,3})\s*$')


def vols():
    out = []
    for v in sorted(os.listdir('正文'), key=lambda x: (len(x), x)):
        m = re.match(r'^第(\d+)卷_(.+)$', v)
        if m and os.path.isdir(os.path.join('正文', v)):
            out.append((int(m.group(1)), m.group(2), v))
    return out


def chapters(vp):
    """返回 [(册序, 册目录, 章号, 章名, 汉字, 提示)]，按章号排序。"""
    rows = []
    for d in sorted(os.listdir(vp)):
        sp = os.path.join(vp, d)
        if not (os.path.isdir(sp) and re.match(r'^[123]、', d)):
            continue
        for f in sorted(os.listdir(sp)):
            m = CHAP.match(f)
            if m:
                rows.append((d[0], d, int(m.group(1)), m.group(2), os.path.join(sp, f)))
    def work(r):
        fails, info = gate(r[4])
        return r + (info.get('汉字', 0), info.get('字数提示', ''), fails)
    with cf.ThreadPoolExecutor(max_workers=16) as ex:
        return sorted(ex.map(work, rows), key=lambda x: x[2])


def cells(line):
    if not line.strip().startswith('|'):
        return None
    parts = [c.strip() for c in line.strip().strip('|').split('|')]
    if all(set(c) <= set('-: ') for c in parts if c):
        return None
    return parts


NOISE = re.compile(r'^[\d,，、;；.%\s]*(?:PASS|FAIL|节点|补写|重写|—|-)*$')
LINE_NOTE = re.compile(r'^\s*[#>*\-•]?\s*第(\d{1,3})章\s*[｜|\s]?\s*(\S{2,12}?)\s*[：:]\s*(\S.*)$')


def blueprint_notes(paths):
    """从给定蓝图文档的表格里抽 章名→一句话内容 与 幕表标题。"""
    notes, notes_num, acts = {}, {}, []
    for p in paths:
        if os.path.basename(p) == '总览.md' or not p.endswith('.md'):
            continue
        try:
            txt = io.open(p, encoding='utf-8').read()
        except (UnicodeDecodeError, IsADirectoryError):
            continue
        for ln in txt.split('\n'):
            if re.match(r'^#{2,4}\s*(第?[0-9一二三四五六七八九十]+)?幕', ln):
                acts.append((os.path.basename(p), ln.strip('# ').strip()))
            m = LINE_NOTE.match(ln)
            if m:
                num, title, body = int(m.group(1)), m.group(2).strip(), m.group(3).strip()
                if 2 <= len(title) <= 12 and len(body) >= 12:
                    if title not in notes or len(body) > len(notes[title]):
                        notes[title] = body
                    if num not in notes_num or len(body) > len(notes_num[num]):
                        notes_num[num] = body
                continue
            cs = cells(ln)
            if not cs or len(cs) < 3:
                continue
            first = cs[0]
            km = NUM.match(first) or re.match(r'^第(\d{1,3})章$', first)
            if not km:
                continue
            name = cs[1]
            if not name or len(name) > 12:
                continue
            cands = [c for c in cs[2:] if len(c) >= 12 and not NOISE.match(c)]
            if not cands:
                continue
            desc = max(cands, key=len)
            if name not in notes or len(desc) > len(notes[name]):
                notes[name] = desc
            k = int(km.group(1))
            if k not in notes_num or len(desc) > len(notes_num[k]):
                notes_num[k] = desc
    return notes, notes_num, acts


CN = '零一二三四五六七八九十'


def tl_rows(vol_n):
    pat = r'第%d卷|卷%s' % (vol_n, CN[vol_n] if vol_n <= 10 else vol_n)
    txt = io.open(os.path.join(TRACK, '时间线.md'), encoding='utf-8').read()
    main, other = None, []
    for ln in txt.split('\n'):
        cs = cells(ln)
        if not cs:
            continue
        if re.match(r'^第%d卷\s' % vol_n, cs[0]):
            main = cs
        elif re.search(pat, ln) and len(other) < 10:
            other.append(ln.strip())
    return main, other


def hook_rows(vol_n):
    txt = io.open(os.path.join(TRACK, '钩子.md'), encoding='utf-8').read()
    sus, glob_f = [], []
    in_global = False
    for ln in txt.split('\n'):
        if ln.startswith('## 全局伏笔待办'):
            in_global = True
        elif ln.startswith('## ') and in_global:
            in_global = False
        cs = cells(ln)
        if not cs:
            continue
        if in_global and re.match(r'^F\d+[a-z]?$', cs[0]) and ('第%d卷' % vol_n) in ln:
            glob_f.append(cs)
        elif not in_global and re.match(r'^第%d卷\s*第' % vol_n, cs[0]):
            sus.append(cs)
    return sus, glob_f


def yaml_pending(vol_n):
    import yaml
    path = os.path.join(TRACK, '章节追踪.yaml')
    data = yaml.safe_load(io.open(path, encoding='utf-8').read())
    mine, for_me = [], []
    pend = data.get('待回收伏笔') or []
    for e in pend:
        if not isinstance(e, dict):
            continue
        born = str(e.get('埋下', ''))
        st = str(e.get('状态', '')) + str(e.get('呼应', ''))
        row = (str(e.get('内容', '')).strip(), born.strip(), st.strip())
        if re.search(r'第%d卷' % vol_n, born):
            mine.append(row)
        elif re.search(r'第%d卷' % vol_n, st):
            for_me.append(row)
    archived = []
    for g in (data.get('卷归档') or []):
        if isinstance(g, dict) and re.search(r'第%d卷' % vol_n, str(g.get('卷', ''))):
            archived.append(g)
    return mine, for_me, pend, archived, str(data.get('当前卷', ''))


def doc_index(ov_dir):
    out = []
    for f in sorted(os.listdir(ov_dir)):
        p = os.path.join(ov_dir, f)
        if f == '总览.md':
            continue
        if os.path.isdir(p):
            n = len([x for x in os.listdir(p) if x.endswith('.md')])
            out.append((f + '/', '子目录，内含 %d 个 md' % n, ''))
            continue
        head = ''
        try:
            for ln in io.open(p, encoding='utf-8'):
                if ln.startswith('#'):
                    head = ln.strip('# ').strip()
                    break
        except UnicodeDecodeError:
            head = '（非文本）'
        out.append((f, head, '%.1f KB' % (os.path.getsize(p) / 1024.0)))
    return out


def rng(nums):
    if not nums:
        return '空'
    return '%03d–%03d（%d 章）' % (min(nums), max(nums), len(nums))


def build(vol_n, vol_name, vdir, only):
    vp = os.path.join('正文', vdir)
    ov = os.path.join(vp, '0、总览')
    rows = chapters(vp)
    src = sorted(glob.glob(os.path.join(ov, '*.md')))
    src += sorted(glob.glob(os.path.join(TRACK, '卷%d*' % vol_n)))
    src += sorted(glob.glob(os.path.join(TRACK, '第%d卷*' % vol_n)))
    src += sorted(glob.glob('世界设定规则/设定/00_大纲/卷%d*' % vol_n))
    src += sorted(glob.glob('世界设定规则/设定/00_大纲/第%d卷*' % vol_n))
    notes, notes_num, acts = blueprint_notes(src)
    main, tl_other = tl_rows(vol_n)
    sus, glob_f = hook_rows(vol_n)
    mine, for_me, pend, archived, cur_vol = yaml_pending(vol_n)

    segs = {}
    for r in rows:
        segs.setdefault(r[0], []).append(r)
    L = []
    A = L.append
    A('# 第%d卷 %s · 总览' % (vol_n, vol_name))
    A('')
    A('> 本文件由 `世界设定规则/脚本/qa/gen_overview.py` 生成，数据取自磁盘正文与 `%s/`（时间线.md、钩子.md、章节追踪.yaml）及本目录蓝图文档；抽不到的字段留空或标"待补"，不臆造剧情。上中下三册只放正文，其余文档一律在本目录 `0、总览/`。' % TRACK)
    A('')
    A('## 一、主线与进度')
    A('')
    if main:
        A('| 项 | 内容 |')
        A('|----|------|')
        for i, c in enumerate(main):
            A('| %s | %s |' % (('主线' if i == 1 else ('状态' if i == 2 else '卷')), c if i else '第%d卷 %s' % (vol_n, vol_name)))
    else:
        A('- 时间线.md「主线剧情节点」未登记本卷行，待该卷会话补写。')
    nums = [r[2] for r in rows]
    if main and len(main) > 2:
        m = re.search(r'第(\d{1,3})章', main[2])
        if m and nums and int(m.group(1)) < max(nums):
            A('')
            A('- 口径冲突：`时间线.md` 本卷状态仍写「%s」，正文实际已到 %03d 章，该行待校正。' % (
                main[2].strip('* '), max(nums)))
        elif '待写' in main[2] and rows:
            A('')
            A('- 口径冲突：`时间线.md` 本卷状态写「%s」，正文实际已有 %d 章，该行待校正。' % (
                main[2].strip('* '), len(rows)))
    A('')
    total_hz = sum(r[5] for r in rows)
    fails = [r for r in rows if r[7]]
    A('- 正文合计 %d 章、%d 汉字；章号跨度 %s。' % (len(rows), total_hz, rng(nums)))
    for k in ('1', '2', '3'):
        s = segs.get(k, [])
        n = [x[2] for x in s]
        lo, hi = {'1': (1, 100), '2': (101, 200), '3': (201, 300)}[k]
        miss = sorted(set(range(lo, hi + 1)) - set(n))
        A('- %s卷 `%s`：%s；缺号 %d 个%s' % (SEG_ORDER[k],
          s[0][1] if s else '%d、%s卷_%s（空）' % (int(k), SEG_ORDER[k], vol_name),
          rng(n), len(miss),
          ('：' + '、'.join('%03d' % x for x in miss[:12]) + ('…' if len(miss) > 12 else '')) if miss else ''))
    A('')
    A('## 二、三册构成')
    A('')
    A('| 册 | 目录 | 章数 | 章号区间 | 汉字 | 均值 | min | max |')
    A('|----|------|------|----------|------|------|-----|-----|')
    for k in ('1', '2', '3'):
        s = segs.get(k, [])
        if not s:
            continue
        c = [x[5] for x in s]
        A('| %s卷 | `%s` | %d | %s | %d | %d | %d | %d |' % (
            SEG_ORDER[k], s[0][1], len(s), rng([x[2] for x in s]), sum(c),
            sum(c) // len(c), min(c), max(c)))
    A('')
    A('## 三、幕表（本卷蓝图原标题）')
    A('')
    if acts:
        for doc, h in acts[:40]:
            A('- %s ←《%s》' % (h, doc))
        A('')
        A('- 以上为蓝图文档原标题，标出的章号区间未必与正文实际成稿一致；正文的幕划分与逐章状态以本卷台账为准。')
    else:
        A('- 未在本卷文档中识别到幕标题，待补。')
    A('')
    A('## 四、每章一览')
    A('')
    A('| 章 | 章名 | 册 | 汉字 | 一句话内容 |')
    A('|----|------|----|------|------------|')
    hit = 0
    for r in rows:
        note = notes.get(r[3]) or notes_num.get(r[2]) or ''
        if note:
            hit += 1
        A('| %03d | %s | %s | %d | %s |' % (r[2], r[3], SEG_ORDER[r[0]], r[5],
          note if len(note) <= 90 else note[:88] + '…'))
    A('')
    A('- 一句话内容取自本卷分章表，命中 %d/%d 章；未命中的章在蓝图里没有对应行（或章名已改），待该卷会话补。' % (hit, len(rows)))
    A('')
    A('## 五、字数与门禁')
    A('')
    A('- `chapter_gate` 全量：FAIL %d 章。' % len(fails))
    for r in fails[:12]:
        A('  - %03d %s：%s' % (r[2], r[3], '；'.join(r[7])))
    off = [r for r in rows if r[6]]
    low = [r for r in rows if r[5] < 2600]
    high = [r for r in rows if r[5] > 3000]
    A('- 偏离目标档 2600–3000：%d 章（<2600 共 %d，>3000 共 %d）；均值 %d。' % (
        len(off), len(low), len(high), (total_hz // len(rows)) if rows else 0))
    hard = [r for r in rows if r[5] < 2300 or r[5] > 3700]
    A('- 硬区间 2300–3700 之外：%d 章%s。' % (len(hard),
      '' if not hard else '（' + '、'.join('%03d=%d' % (r[2], r[5]) for r in hard[:10]) + '）'))
    A('')
    A('## 六、钩子（章级悬念）')
    A('')
    if sus:
        open_n = sum(1 for s in sus if '待解' in (s[-1] if s else ''))
        A('- `钩子.md` 命中本卷 %d 条，其中状态含"待解" %d 条。' % (len(sus), open_n))
        A('')
        A('| 章节 | 悬念 | 状态 |')
        A('|------|------|------|')
        for s in sus[:60]:
            s = (s + ['', ''])[:3]
            A('| %s | %s | %s |' % (s[0], s[1] if len(s[1]) <= 120 else s[1][:118] + '…', s[2]))
        if len(sus) > 60:
            A('')
            A('- 余 %d 条见 `%s/钩子.md`。' % (len(sus) - 60, TRACK))
    else:
        A('- `钩子.md` 未登记本卷章级悬念，待补。')
    A('')
    A('## 七、埋坑与计划回收（全局伏笔 F 表）')
    A('')
    if glob_f:
        A('| ID | 伏笔内容 | 埋下 | 计划回收 | 状态 |')
        A('|----|----------|------|----------|------|')
        for g in glob_f:
            g = (g + ['', '', '', '', ''])[:5]
            A('| %s | %s | %s | %s | %s |' % (g[0], g[1][:70], g[2], g[3][:60], g[4]))
    else:
        A('- `钩子.md`「全局伏笔待办」里没有涉及本卷的 F 条目。')
    A('')
    A('## 八、待回收伏笔（章节追踪.yaml 摘录）')
    A('')
    A('- 全库待回收共 %d 条；本卷埋下 %d 条、需在本卷回收 %d 条。' % (len(pend), len(mine), len(for_me)))
    if mine:
        A('')
        A('**本卷埋下**')
        A('')
        for c, b, st in mine[:40]:
            A('- %s ｜埋下 %s ｜%s' % (c[:80], b, st[:60]))
    if for_me:
        A('')
        A('**需在本卷回收**')
        A('')
        for c, b, st in for_me[:40]:
            A('- %s ｜埋下 %s ｜%s' % (c[:80], b, st[:60]))
    for g in archived:
        lg = g.get('遗留伏笔') or []
        A('')
        A('**卷归档条目（%s，遗留伏笔 %d 条）**' % (g.get('卷'), len(lg)))
        A('')
        for x in lg[:40]:
            A('- %s' % str(x)[:90])
    A('')
    A('## 九、本卷时间线（时间线.md 摘录）')
    A('')
    if tl_other:
        for ln in tl_other:
            A('- %s' % (ln if len(ln) <= 200 else ln[:198] + '…'))
    else:
        A('- 时间线.md 未出现本卷其它行。')
    A('')
    A('## 十、文档索引（本目录）')
    A('')
    A('| 文档 | 主题 | 大小 |')
    A('|------|------|------|')
    for f, h, sz in doc_index(ov):
        A('| %s | %s | %s |' % (f, h, sz))
    A('')
    A('## 十一、口径与待办指针')
    A('')
    A('- 全库当前卷口径：`章节追踪.yaml` 记 当前卷: %s。' % (cur_vol or '未填'))
    A('- 本卷写作硬口径见 `CLAUDE.md`（字数规则、禁用词）与 `世界设定规则/设定/核心数值表.yaml`。')
    if os.path.exists(os.path.join(TRACK, '卷%d起草进度与问题.md' % vol_n)):
        A('- 本卷起草台账：`%s/卷%d起草进度与问题.md`。' % (TRACK, vol_n))
    ledger = [x for x in os.listdir(TRACK) if ('卷%d' % vol_n) in x and x.endswith('.md')]
    for x in ledger:
        if '起草进度' not in x:
            A('- 相关台账/规范：`%s/%s`。' % (TRACK, x))
    A('- 复查命令（仓库根执行）：`PYTHONIOENCODING=utf-8 .venv/Scripts/python.exe 世界设定规则/脚本/qa/gen_overview.py %s`；全卷体检用 `qa/vol_status.py <卷号>`，跨章复用用 `qa/vol_cross.py "<卷目录>"`。' % (' '.join(str(x) for x in only) if only else '<卷号>'))
    A('')
    out = '\n'.join(L)
    io.open(os.path.join(ov, '总览.md'), 'w', encoding='utf-8', newline='\n').write(out)
    return vdir, len(rows), hit, len(sus), len(glob_f), len(mine)


want = [int(x) for x in sys.argv[1:] if x.isdigit()]
picked = [v for v in vols() if not want or v[0] in want]
print('生成 %d 卷' % len(picked))
print('%-22s %5s %6s %5s %5s %5s' % ('卷', '章', '命中', '钩子', 'F坑', '本卷埋'))
for vol_n, vol_name, vdir in picked:
    r = build(vol_n, vol_name, vdir, want)
    print('%-22s %5d %6d %5d %5d %5d' % r)
