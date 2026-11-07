# -*- coding: utf-8 -*-
"""单卷体检：在册/缺号/重名/门禁失败/字数分布（取代 qa/vol_gate.py 与 v2_status.py）。

    python 世界设定规则/脚本/qa/vol_status.py 2
    python 世界设定规则/脚本/qa/vol_status.py 2 --target 300 --lo 2600 --hi 3000

卷目录按 `正文/第N卷_*` 自动定位，册目录按 `1、` `2、` `3、` 自动取，均不写死卷名。
控制台请带 PYTHONIOENCODING=utf-8；输出直接打 stdout，不再落 .txt 产物。
"""
import argparse
import collections
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))
sys.path.insert(0, SCRIPTS)

import chapter_gate as g  # noqa: E402

PAT = re.compile(r'^第(\d{3})章_(.+)\.md$')


def vol_dir(n):
    hits = sorted(glob.glob(os.path.join(ROOT, '正文', '第%d卷_*' % n)))
    if not hits:
        sys.exit('找不到 正文/第%d卷_* 目录' % n)
    return hits[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('vol', type=int)
    ap.add_argument('--target', type=int, default=300, help='本卷应有章数（全卷连号 001—N），默认 300')
    ap.add_argument('--lo', type=int, default=2600, help='目标字数下限')
    ap.add_argument('--hi', type=int, default=3000, help='目标字数上限')
    a = ap.parse_args()

    vd = vol_dir(a.vol)
    books = sorted(glob.glob(os.path.join(vd, '[123]、*')))
    if not books:
        sys.exit('%s 下没有 1、2、3 册目录' % vd)

    found = {}
    titles = collections.defaultdict(list)
    per_book = []
    for b in books:
        names = sorted(os.listdir(b))
        cnt = 0
        for name in names:
            m = PAT.match(name)
            if not m:
                continue
            cnt += 1
            n = int(m.group(1))
            if n in found:
                sys.exit('章号跨册重复：第%03d章 同时在 %s 与 %s' % (
                    n, os.path.basename(found[n]), os.path.basename(os.path.join(b, name))))
            found[n] = os.path.join(b, name)
            titles[m.group(2)].append(n)
        stray = [x for x in names if x.endswith('.md') and not PAT.match(x)]
        per_book.append((os.path.basename(b), cnt, stray))

    rows = []
    fails = []
    counts = []
    for n in range(1, a.target + 1):
        if n not in found:
            rows.append('MISS 第%03d章' % n)
            continue
        f, info = g.gate(found[n])
        counts.append(info.get('汉字') or 0)
        if f:
            fails.append(n)
            rows.append('FAIL 第%03d章 %s | %s' % (n, info.get('章名'), '；'.join(str(x) for x in f)))

    out = ['卷%d 体检（%s）' % (a.vol, os.path.basename(vd))]
    out.append('在册 %d / %d；缺号 %d；门禁失败 %d；重名 %d' % (
        len(found), a.target, a.target - len(found), len(fails),
        sum(1 for t, ns in titles.items() if len(ns) > 1)))
    if counts:
        out.append('字数：最小 %d，最大 %d，平均 %d；%d—%d 档外 %d' % (
            min(counts), max(counts), sum(counts) // len(counts), a.lo, a.hi,
            sum(1 for c in counts if c < a.lo or c > a.hi)))
    for b, cnt, stray in per_book:
        out.append('  %-22s %3d 章%s' % (b, cnt, '；册内非正文章文件：%s' % '、'.join(stray) if stray else ''))
    dup = {t: ns for t, ns in titles.items() if len(ns) > 1}
    out.append('重名：' + ('无' if not dup else ''))
    for t, ns in sorted(dup.items(), key=lambda x: x[1][0]):
        out.append('  %s -> %s' % (t, '、'.join('第%03d章' % n for n in sorted(ns))))
    out.extend(rows)
    sys.stdout.write('\n'.join(out).rstrip() + '\n')


if __name__ == '__main__':
    main()
