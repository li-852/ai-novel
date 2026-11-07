# -*- coding: utf-8 -*-
"""全仓正文 gate 扫描（线程化）：按卷统计章数与 FAIL，另列低于 2300 / 高于 3700 的硬失败。"""
import concurrent.futures as cf
import os
import re
import sys

sys.path.insert(0, '世界设定规则/脚本')
from chapter_gate import gate

CHAP = re.compile(r'^第\d{3}章_.+\.md$')
SEG = re.compile(r'^([123])、([上中下])卷_(.+)$')


def chapters():
    out = []
    for vol in sorted(os.listdir('正文'), key=lambda x: (len(x), x)):
        vp = os.path.join('正文', vol)
        if not (os.path.isdir(vp) and re.match(r'^第\d+卷_', vol)):
            continue
        for seg in sorted(os.listdir(vp)):
            sp = os.path.join(vp, seg)
            if not os.path.isdir(sp):
                continue
            tag = '1、上卷_潜龙在渊' if not SEG.match(seg) else seg
            for f in sorted(os.listdir(sp)):
                if CHAP.match(f):
                    out.append((vol, seg, sp.replace('\\', '/') + '/' + f))
    return out


def work(item):
    vol, seg, path = item
    fails, info = gate(path)
    return vol, seg, path, fails, info.get('汉字', 0)

items = chapters()
print('待扫章数', len(items))
res = []
with cf.ThreadPoolExecutor(max_workers=16) as ex:
    for r in ex.map(work, items):
        res.append(r)

vols = {}
for vol, seg, path, fails, n in res:
    d = vols.setdefault(vol, {'n': 0, 'fail': 0, 'bad': []})
    d['n'] += 1
    if fails:
        d['fail'] += 1
        d['bad'].append('%s/%s → %s' % (seg, path.split('/')[-1][1:9], fails[0][:30]))

for vol in sorted(vols, key=lambda v: (len(v), v)):
    d = vols[vol]
    print('%-18s 章%4d  FAIL %d' % (vol, d['n'], d['fail']))
    for b in d['bad'][:4]:
        print('       ', b)
print('合计章数', len(res), '合计FAIL', sum(1 for r in res if r[3]))
