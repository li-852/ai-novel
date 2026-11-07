# -*- coding: utf-8 -*-
"""按《卷N补写分槽细案》生成本轮并行补写批次（取代 gen_v2_batches.py，每轮按磁盘实存重算）。

    python 世界设定规则/脚本/qa/gen_vol_batches.py 2
    python 世界设定规则/脚本/qa/gen_vol_batches.py 2 --size 5 --json

批次 = 同册连续缺号若干（默认 5），三册轮流排布，保证「齐头并进」。
只读细案与目录，不写任何文件：默认打可读清单，--json 打 JSON 到 stdout（要留就自己重定向）。
"""
import argparse
import glob
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))
BOOK_KEYS = ('上', '中', '下')


def vol_dir(n):
    hits = sorted(glob.glob(os.path.join(ROOT, '正文', '第%d卷_*' % n)))
    if not hits:
        sys.exit('找不到 正文/第%d卷_* 目录' % n)
    return hits[0]


def books(vd):
    out = {}
    for b in glob.glob(os.path.join(vd, '[123]、*')):
        name = os.path.basename(b)
        key = {'1': '上', '2': '中', '3': '下'}[name[0]]
        out[key] = b
    if set(out) != set(BOOK_KEYS):
        sys.exit('%s 下不是标准三册目录：%s' % (vd, sorted(out.values())))
    return out


def cn_num(n):
    if n < 10:
        return '零一二三四五六七八九'[n]
    tens, ones = divmod(n, 10)
    s = ('' if tens == 1 else '零一二三四五六七八九'[tens]) + '十'
    return s + ('零一二三四五六七八九'[ones] if ones else '')


def slots(n):
    base = os.path.join(ROOT, '世界设定规则', '设定', '00_大纲')
    hits = []
    for tag in ((cn_num(n), str(n)) if n < 100 else (str(n),)):
        hits += glob.glob(os.path.join(base, '卷%s补写分槽细案.md' % tag))
    if not hits:
        sys.exit('找不到 卷%d 的补写分槽细案（00_大纲 下）' % n)
    plan = hits[0]
    rows = {}
    for line in io.open(plan, encoding='utf-8'):
        if not line.startswith('| '):
            continue
        cs = [c.strip() for c in line.strip().strip('|').split('|')]
        if len(cs) < 7 or not re.fullmatch(r'\d{3}', cs[0]):
            continue
        rows[cs[0]] = cs
    return rows, plan


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('vol', type=int)
    ap.add_argument('--size', type=int, default=5, help='每批章数，默认 5')
    ap.add_argument('--json', action='store_true', help='输出 JSON 到 stdout')
    a = ap.parse_args()

    vd = vol_dir(a.vol)
    bs = books(vd)
    rows, plan = slots(a.vol)

    have, names = {}, {}
    for k, d in bs.items():
        ns = [int(m.group(1)) for m in
              (re.match(r'^第(\d{3})章_', f) for f in os.listdir(d)) if m]
        have[k] = sorted(ns)
        names[k] = sorted(m.group(1) for m in
                          (re.match(r'^第\d{3}章_(.+)\.md$', f) for f in os.listdir(d)) if m)

    lohi = {'上': (1, 100), '中': (101, 200), '下': (201, 300)}
    missing = {k: [n for n in range(lohi[k][0], lohi[k][1] + 1)
                   if n not in set(have[k]) and '%03d' % n in rows] for k in BOOK_KEYS}

    batches = []
    for k in BOOK_KEYS:
        lst = missing[k]
        i = 0
        while i < len(lst):
            run = []
            while i < len(lst) and (not run or lst[i] == run[-1] + 1) and len(run) < a.size:
                run.append(lst[i])
                i += 1
            while len(run) < a.size and i < len(lst):
                run.append(lst[i])
                i += 1
            batches.append((k, run))
    pools = {k: [x for x in batches if x[0] == k] for k in BOOK_KEYS}
    order = []
    while any(pools.values()):
        for k in BOOK_KEYS:
            if pools[k]:
                order.append(pools[k].pop(0))

    payload = [{'batch': i, 'book': k, 'dir': os.path.relpath(bs[k], ROOT).replace('\\', '/'),
                'chapters': ['%03d' % n for n in run],
                'slots': [' | '.join(rows['%03d' % n]) for n in run],
                'names': names[k]} for i, (k, run) in enumerate(order, 1)]

    if a.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=1)
        sys.stdout.write('\n')
        return
    print('细案 %s：槽 %d；缺号 %s' % (os.path.basename(plan), len(rows),
                                     {k: len(v) for k, v in missing.items()}))
    print('批次 %d（每批≤%d 章，三册轮流）' % (len(payload), a.size))
    for x in payload:
        print('  批%-3d %s卷  %s' % (x['batch'], x['book'], ' '.join(x['chapters'])))
        for s in x['slots']:
            print('        %s' % s)


if __name__ == '__main__':
    main()
