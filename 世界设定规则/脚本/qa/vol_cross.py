# -*- coding: utf-8 -*-
"""全卷跨章复用检测：以三册 300 章为一个池，逐章统计长句（>=12汉字）与整段重复。
用法: python 世界设定规则/脚本/qa/vol_cross.py [卷目录，默认 正文/第1卷_潜龙在渊]
"""
import re, os, glob, collections, sys

vol = sys.argv[1] if len(sys.argv) > 1 else '正文/第1卷_潜龙在渊'
folders = sorted(glob.glob(os.path.join(vol, '[123]、*')))
files = []
for f in folders:
    files += sorted(glob.glob(os.path.join(f, '第*章_*.md')))

def hz_sentences(t):
    out = []
    for seg in re.split(r'[。！？；]', t):
        h = ''.join(re.findall(r'[一-鿿]', seg))
        if len(h) >= 12:
            out.append(h)
    return out

txts = {p: open(p, encoding='utf-8').read() for p in files}
sent = {p: hz_sentences(t) for p, t in txts.items()}
hz = lambda x: len(re.findall(r'[一-鿿]', x))
paras = {}
for p, t in txts.items():
    body = [x.strip() for x in t.split('\n') if x.strip() and not x.startswith('#')]
    paras[p] = [x for x in body if hz(x) >= 30]

owner = collections.defaultdict(list)
for p, ss in sent.items():
    for s in ss:
        owner[s].append(p)

print('池内章数', len(files), '长句总数', sum(len(v) for v in sent.values()))
worst = []
for p in files:
    ss = sent[p]
    if not ss:
        continue
    dup = [s for s in ss if any(o != p for o in owner[s])]
    pct = 100.0 * len(dup) / len(ss)
    worst.append((pct, p, len(ss), len(dup), dup[0][:20] if dup else ''))
worst.sort(reverse=True)
print('复用>=15%%：%d 章' % sum(1 for w in worst if w[0] >= 15))
for pct, p, n, d, sample in worst[:18]:
    print('%.1f%% %s 长句%d 复用%d %s' % (pct, os.path.basename(p), n, d, sample))

exact = collections.defaultdict(list)
for p, ps in paras.items():
    for x in ps:
        exact[x].append(p)
rep = {k: v for k, v in exact.items() if len(set(v)) > 1}
print('跨章整段逐字重复：%d 处' % len(rep))
for k, v in list(rep.items())[:12]:
    print('  ', [os.path.basename(x) for x in sorted(set(v))], '|', k[:28])
