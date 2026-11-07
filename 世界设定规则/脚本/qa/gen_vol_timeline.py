# -*- coding: utf-8 -*-
"""按《卷N补写分槽细案》生成 正文/第N卷_xxx/0、总览/卷N补写章时间线.md（取代 gen_v2_timeline.py）。

    python 世界设定规则/脚本/qa/gen_vol_timeline.py 2

字段全部取自细案表 `槽｜子卷｜幕｜型｜日子｜要办的事｜须回看`，不另加剧情判断。
幕界（每幕的新章区间）从《卷N…幕表…》里自动解析；解析不到就跳过幕界校验，不影响出表。
只读细案/幕表/正文，写入点只有一个：本卷 `0、总览` 下的时间线文件。
"""
import argparse
import glob
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))
CN = '一二三四五六七八九十百零两'
UNI = {'零': 0, '一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
       '六': 6, '七': 7, '八': 8, '九': 9}


def cn2int(s):
    """「第十五」「第八十六」「幕三」→ 整数；转不了返回 None。"""
    m = re.search(r'第([%s]+)日?' % CN, str(s))
    if not m:
        return None
    t, val, buf = m.group(1), 0, 0
    if t == '十':
        return 10
    for ch in t:
        if ch in UNI:
            buf = UNI[ch]
        elif ch == '十':
            val += (buf or 1) * 10
            buf = 0
        elif ch == '百':
            val += (buf or 1) * 100
            buf = 0
    return val + buf


def int2cn(n):
    if n < 10:
        return '零一二三四五六七八九'[n]
    tens, ones = divmod(n, 10)
    s = ('' if tens == 1 else '零一二三四五六七八九'[tens]) + '十'
    return s + ('零一二三四五六七八九'[ones] if ones else '')


def vol_dir(n):
    hits = sorted(glob.glob(os.path.join(ROOT, '正文', '第%d卷_*' % n)))
    if not hits:
        sys.exit('找不到 正文/第%d卷_* 目录' % n)
    return hits[0]


def book_dirs(vd):
    out = {}
    for b in glob.glob(os.path.join(vd, '[123]、*')):
        out[{'1': '上', '2': '中', '3': '下'}[os.path.basename(b)[0]]] = b
    return out


def act_ranges(n):
    """从幕表里抓 `| 幕一 | 名 | 001–072 |` 形式的新章区间。"""
    p = plan_path(n, '*幕表*.md')
    if p:
        found = {}
        for ln in io.open(p, encoding='utf-8'):
            cs = [c.strip() for c in ln.strip().strip('|').split('|')]
            if len(cs) < 3 or not cs[0].startswith('幕'):
                continue
            rng = next((c for c in cs[2:] if re.fullmatch(r'\d{3}[—\-–]\d{3}', c)), '')
            if rng:
                lo, hi = re.split(r'[—\-–]', rng)
                found[cs[0]] = (lo, hi, cs[1])
        if found:
            return found, os.path.basename(p)
    return {}, ''


def plan_path(n, kind):
    """细案/幕表在 00_大纲 里用中文数字命名（卷二…），阿拉伯数字作兜底。"""
    base = os.path.join(ROOT, '世界设定规则', '设定', '00_大纲')
    for tag in (int2cn(n) if n < 100 else '', str(n)):
        if not tag:
            continue
        for p in glob.glob(os.path.join(base, '卷%s%s' % (tag, kind))):
            return p
    return ''


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('vol', type=int)
    a = ap.parse_args()

    vd = vol_dir(a.vol)
    bd = book_dirs(vd)
    plan = plan_path(a.vol, '补写分槽细案.md')
    if not plan:
        sys.exit('找不到 卷%d 的补写分槽细案（00_大纲 下）' % a.vol)

    rs = []
    for ln in io.open(plan, encoding='utf-8'):
        if not ln.startswith('|'):
            continue
        cs = [c.strip() for c in ln.strip().strip('|').split('|')]
        if len(cs) != 7 or not re.fullmatch(r'\d{3}', cs[0]):
            continue
        rs.append(cs)

    names = {}
    for k, d in bd.items():
        for f in os.listdir(d):
            m = re.match(r'^第(\d{3})章_(.+)\.md$', f)
            if m:
                names[m.group(1)] = m.group(2)

    acts, act_src = act_ranges(a.vol)
    hit = noday = 0
    noloss = []
    for cs in rs:
        if cs[0] not in names or cs[1] not in bd:
            noloss.append(cs[0])
            continue
        p = os.path.join(bd[cs[1]], '第%s章_%s.md' % (cs[0], names[cs[0]]))
        txt = io.open(p, encoding='utf-8').read()
        day, nn = cs[4], cn2int(cs[4])
        ok = day in txt
        if not ok and nn is not None and nn < 100:
            c = int2cn(nn)
            ok = ('第%s日' % c) in txt or ('第%s天' % c) in txt
        hit += 1 if ok else 0
        noday += 0 if ok else 1

    segs = {}
    for cs in rs:
        segs.setdefault(cs[2], []).append(cs)
    act_order = sorted(segs, key=lambda x: cn2int(x) or 99)

    L = []
    tag = int2cn(a.vol) if a.vol < 100 else str(a.vol)
    L.append('# 卷%s补写章时间线（%d 章，依《卷%s补写分槽细案》生成）' % (tag, len(rs), tag))
    L.append('')
    L.append('> 本文件由 `世界设定规则/脚本/qa/gen_vol_timeline.py %d` 生成，字段全部取自细案表 `槽｜子卷｜幕｜型｜日子｜要办的事｜须回看`，不另加剧情判断。' % a.vol)
    L.append('> 节点锚章不在本表内（本卷在册 %d 章 = 锚章 + 补写章）。章名取磁盘实存。' % len(names))
    L.append('> 日子按细案的「离山第N日」记；正文里另出现月日或干支的，以正文为准。')
    L.append('')
    L.append('## 一、按幕分日')
    stray = []
    for act in act_order:
        grp = sorted(segs[act], key=lambda c: (cn2int(c[4]) if cn2int(c[4]) is not None else 9999, c[0]))
        if act in acts:
            lo, hi, name = acts[act]
            stray += [(act, c[0]) for c in grp if not lo <= c[0] <= hi]
        else:
            name = ''
        days = {}
        for cs in grp:
            nn = cn2int(cs[4])
            days.setdefault(nn if nn is not None else cs[4], []).append(cs)
        L.append('')
        L.append('### %s%s（%d 槽）' % (act, '　' + name if name else '', len(grp)))
        L.append('')
        L.append('| 日子 | 章 | 册 | 型 | 要办的事 | 须回看 |')
        L.append('|------|----|----|----|----------|--------|')
        for d in sorted(days, key=lambda x: (isinstance(x, str), x if not isinstance(x, str) else 0)):
            for cs in sorted(days[d], key=lambda c: c[0]):
                L.append('| %s | 第%s章_%s | %s | %s | %s | %s |' % (
                    cs[4], cs[0], names.get(cs[0], '（缺稿）'), cs[1], cs[3], cs[5], cs[6]))

    dset = sorted(n for n in (cn2int(c[4]) for c in rs) if n is not None)
    have = set(dset)
    gap = [d for d in range(dset[0], dset[-1] + 1) if d not in have] if dset else []
    L.append('')
    L.append('## 二、统计')
    L.append('')
    L.append('- 细案槽数：%d；按幕分：%s' % (len(rs), '、'.join('%s %d' % (x, len(segs[x])) for x in act_order)))
    L.append('- 按册分：%s' % '、'.join('%s卷 %d' % (k, sum(1 for c in rs if c[1] == k)) for k in ('上', '中', '下')))
    L.append('- 型别分：%s' % '、'.join('%s %d' % (k, sum(1 for c in rs if c[3] == k))
                                    for k in sorted(set(c[3] for c in rs))))
    if dset:
        L.append('- 日子跨度：第 %d 日 至 第 %d 日，共 %d 个不同日子（同日多槽＝同一时间不同地点，各章人物不许照面）'
                 % (dset[0], dset[-1], len(have)))
        L.append('- 细案未排槽的日子：%d 个（%s）——这些天只有节点锚章在场，锚章的日子不在细案里，不作判定'
                 % (len(gap), '、'.join(str(x) for x in gap)))
    L.append('- 正文里字面出现该日子的章：%d；未字面出现（多数是就地叙事不落日期，日子以本表为准）：%d；细案有槽而磁盘无稿：%d%s'
             % (hit, noday, len(noloss), '（' + ' '.join(noloss) + '）' if noloss else ''))
    if not act_src:
        act_msg = '未找到含新章区间的幕表，跳过'
    elif stray:
        act_msg = '不符 %d 处：%s（幕界取自《%s》）' % (
            len(stray), '、'.join('%s 第%s章' % (x, y) for x, y in stray), act_src)
    else:
        act_msg = '全部落在《%s》该幕新章区间内' % act_src
    L.append('- 幕界校验：%s' % act_msg)
    book_bad = [c[0] for c in rs if not (
        (c[1] == '上' and c[0] <= '100') or (c[1] == '中' and '101' <= c[0] <= '200') or (c[1] == '下' and c[0] >= '201'))]
    L.append('- 子卷校验（上 001—100／中 101—200／下 201—300）：%s' % ('全部对上' if not book_bad else '不符：' + ' '.join(book_bad)))

    L.append('')
    L.append('## 三、同日多槽清单（并行排布检查用）')
    L.append('')
    byday = {}
    for cs in rs:
        byday.setdefault(cs[4], []).append(cs)
    multi = {d: v for d, v in byday.items() if len(v) > 1}
    L.append('| 日子 | 章数 | 章号 |')
    L.append('|------|------|------|')
    for d in sorted(multi, key=lambda x: (cn2int(x) if cn2int(x) is not None else 9999)):
        cs = sorted(multi[d], key=lambda c: c[0])
        L.append('| %s | %d | %s |' % (d, len(cs), ' '.join(c[0] for c in cs)))

    out = os.path.join(vd, '0、总览', '卷%s补写章时间线.md' % tag)
    io.open(out, 'w', encoding='utf-8', newline='\n').write('\n'.join(L) + '\n')
    print('槽 %d｜同日多槽日子 %d｜字面命中 %d｜幕界来源 %s' % (len(rs), len(multi), hit, act_src or '无'))
    print('写出', os.path.relpath(out, ROOT).replace('\\', '/'))


if __name__ == '__main__':
    main()
