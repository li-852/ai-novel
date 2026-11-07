# -*- coding: utf-8 -*-
"""指定章的重复句明细：列出与同卷其他章逐字相同的长句（>=12 汉字）。取代 qa/dup_lines.py 与 qa/dup_list.py。

    python 世界设定规则/脚本/qa/chap_dup.py 正文/第2卷_锋芒暗露 195 229        # 共用句清单
    python 世界设定规则/脚本/qa/chap_dup.py 正文/第2卷_锋芒暗露 --lines 195    # 另给行号定位

--lines 会打印「行号<TAB>该行<TAB>->撞到的章号」，用于就地改写；不带则只列共用句本身。
"""
import collections
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SCRIPTS))


def hz_sentences(t):
    out = []
    for seg in re.split(r'[。！？；]', t):
        h = ''.join(re.findall(r'[一-鿿]', seg))
        if len(h) >= 12:
            out.append(h)
    return out


def main():
    argv = sys.argv[1:]
    if not argv:
        sys.exit(__doc__)
    mode_lines = '--lines' in argv
    args = [a for a in argv if a != '--lines']
    vol, targets = args[0], args[1:]
    if not targets:
        sys.exit('用法见 docstring：至少要给一个章号')

    vol_abs = vol if os.path.isabs(vol) else os.path.join(ROOT, vol)
    files = []
    for b in sorted(glob.glob(os.path.join(vol_abs, '[123]、*'))):
        files += sorted(glob.glob(os.path.join(b, '第*章_*.md')))
    if not files:
        sys.exit('%s 下没找到章节文件' % vol)

    sent = {p: hz_sentences(open(p, encoding='utf-8').read()) for p in files}
    owner = collections.defaultdict(list)
    for p, ss in sent.items():
        for s in ss:
            owner[s].append(p)

    for t in targets:
        key = t if t.startswith('第') else '第%s章' % t
        hit = [f for f in files if os.path.basename(f).startswith(key)]
        if not hit:
            print('===== %s 未找到' % key)
            continue
        p = hit[0]
        print('=====', os.path.relpath(p, ROOT).replace('\\', '/'))
        if mode_lines:
            lines = open(p, encoding='utf-8').read().split('\n')
            n = 0
            for i, ln in enumerate(lines):
                for s in hz_sentences(ln):
                    if any(o != p for o in owner[s]) and s not in [hz_sentences(x) for x in lines[:i]]:
                        others = sorted(set(os.path.basename(o)[:6] for o in owner[s] if o != p))
                        print('%d\t%s\t->%s' % (i + 1, ln[:70], ','.join(others)))
                        n += 1
            print('TOTAL', n)
        else:
            seen = set()
            for s in sent[p]:
                others = [o for o in owner[s] if o != p]
                if others and s not in seen:
                    seen.add(s)
                    print('  [%d章共用] %s' % (len(set(others)), s[:40]))
            if not seen:
                print('  无跨章共用长句')


if __name__ == '__main__':
    main()
