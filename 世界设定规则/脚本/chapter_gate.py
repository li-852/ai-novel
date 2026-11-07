# -*- coding: utf-8 -*-
"""章节交付门禁：每写完一章立刻跑一次。

用法：
  python 世界设定规则/脚本/chapter_gate.py 正文/第1卷_潜龙在渊/第008章_XXXX.md
用法附 --cross：额外打印与同卷其它章逐字撞车的长句占比（跨章复制）。
检查项：章名四字、首行汉字序号与文件名章号一致、汉字数 2300-3700（目标 2600-3000）、CRLF=0、半角逗号=0、拉丁字母=0、
引号成对、段内无完全重复、无近似重复（长段前 12 汉字相同；短对话不计）、文件末尾单换行。
任一不过即以非零码退出，并打印可核对的数字。
"""
import re, sys, os

LO, HI, TGT_LO, TGT_HI = 2300, 3700, 2600, 3000
_CN = '零一二三四五六七八九'


def int2cn(n):
    """1→一，11→十一，110→一百一十，208→二百零八。"""
    if n < 10:
        return _CN[n]
    if n < 20:
        return '十' + (_CN[n % 10] if n % 10 else '')
    if n < 100:
        return _CN[n // 10] + '十' + (_CN[n % 10] if n % 10 else '')
    h, t, o = n // 100, n // 10 % 10, n % 10
    if not t and not o:
        return _CN[h] + '百'
    if not t:
        return _CN[h] + '百零' + _CN[o]
    return _CN[h] + '百' + _CN[t] + '十' + (_CN[o] if o else '')


def gate(path):
    fails, info = [], {}
    if not os.path.isfile(path):
        return ['文件不存在: %s' % path], info
    base = os.path.basename(path)
    m = re.match(r'第(\d{3})章_(.+)\.md$', base)
    if not m:
        fails.append('文件名不合式（应如 第008章_以工换工.md）')
        return fails, info
    num, title = int(m.group(1)), m.group(2)
    info['章号'] = num
    cn_title = re.findall(r'[一-鿿]', title)
    info['章名'] = title
    if len(cn_title) != 4:
        fails.append('章名须四字，现为%d字：%s' % (len(cn_title), title))

    raw = open(path, 'rb').read()
    txt = raw.decode('utf-8')
    want_first = '# 第%s章 %s' % (int2cn(num), title)
    got_first = txt.split('\n', 1)[0].strip()
    info['首行'] = got_first
    if got_first != want_first:
        fails.append('首行须为「%s」，现为「%s」' % (want_first, got_first))
    info['CRLF'] = txt.count('\r')
    if info['CRLF']:
        fails.append('CRLF=%d（须 LF 保存）' % info['CRLF'])
    if not raw.endswith(b'\n') or raw.endswith(b'\n\n'):
        fails.append('文件末尾须恰好一个换行')

    info['汉字'] = len(re.findall(r'[一-鿿]', txt))
    if info['汉字'] < LO:
        fails.append('字数 %d < %d' % (info['汉字'], LO))
    elif info['汉字'] > HI:
        fails.append('字数 %d > %d' % (info['汉字'], HI))
    elif not (TGT_LO <= info['汉字'] <= TGT_HI):
        info['字数提示'] = '在允许区间但偏离目标档 2600-3000'

    info['半角逗号'] = len(re.findall(r'[一-鿿],|,[一-鿿]', txt))
    if info['半角逗号']:
        fails.append('半角逗号=%d（中文之间须全角）' % info['半角逗号'])

    lat = re.findall(r'[A-Za-z]+', txt)
    info['拉丁词'] = lat[:5]
    if lat:
        fails.append('拉丁字母残留=%d 处，例：%s' % (len(lat), ', '.join(lat[:3])))

    info['引号'] = txt.count('"') % 2
    if info['引号']:
        fails.append('半角双引号不成对（%d 个）' % txt.count('"'))

    paras = [p.strip() for p in txt.split('\n') if p.strip()]
    body = [p for p in paras if not p.startswith('#')]
    hz = lambda x: len(re.findall(r'[\u4e00-\u9fff]', x))
    long_p = [x for x in body if hz(x) >= 30]
    exact = sorted({x for x in long_p if long_p.count(x) > 1})
    if exact:
        fails.append('存在完全重复段 %d 处：%s' % (len(exact), exact[0][:24]))
    mid_p = [x for x in body if hz(x) >= 25]
    near = set()
    for i, a in enumerate(mid_p):
        for b in mid_p[i + 1:]:
            if re.sub(r'[^\u4e00-\u9fff]', '', a)[:12] == re.sub(r'[^\u4e00-\u9fff]', '', b)[:12]:
                near.add(a[:16])
    if near:
        fails.append('存在近似重复段（前12汉字同）%d 处：%s' % (len(near), list(near)[0]))

    info['段数'] = len(body)
    return fails, info



def cross_report(paths):
    """跨章复用检测：同卷其它章里出现过的连续汉字串（>=12字）占本章长句总数的比例。
    审计报告点名的 P0 是跨章逐字复制，这里把它变成可量化的数。"""
    import os, glob, collections
    def norm(x): return x.replace('\\', '/').replace(os.sep, '/')
    def hz_sentences(t):
        out = []
        for seg in re.split(r'[。！？；]', t):
            h = ''.join(re.findall(r'[\u4e00-\u9fff]', seg))
            if len(h) >= 12: out.append(h)
        return out
    vols = collections.defaultdict(dict)
    for pth in paths:
        d = os.path.dirname(norm(pth))
        for f in glob.glob(d + '/第*章_*.md'):
            fn = norm(f)
            vols[d][fn] = hz_sentences(open(fn, encoding='utf-8').read())
    print('--- 跨章复用（本章长句中与同卷其它章逐字相同的占比，>=15% 标"注意"）---')
    for pth in paths:
        n = norm(pth)
        d = os.path.dirname(n)
        if d not in vols or n not in vols[d]:
            print('%s 未在卷内匹配（路径请用 正文/第N卷_x/第NNN章_x.md）' % pth); continue
        mine = vols[d][n]
        if not mine: continue
        pool = set()
        for other, sents in vols[d].items():
            if other != n: pool.update(sents)
        dup = [m for m in mine if m in pool]
        pct = 100.0 * len(dup) / len(mine)
        print('%s 长句%d 复用%d (%.1f%%) %s %s' % (os.path.basename(n), len(mine), len(dup), pct,
              ('首处:' + dup[0][:18]) if dup else '-', '注意' if pct >= 15 else '正常'))


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    args = [a for a in sys.argv[1:] if not a.startswith('--')]
    if '--cross' in sys.argv:
        cross_report(args)
    bad = 0
    for path in args:
        fails, info = gate(path)
        stat = ' '.join('%s=%s' % (k, v) for k, v in sorted(info.items()))
        print(('通过  ' if not fails else '未通过') + ' | ' + os.path.basename(path) + ' | ' + stat)
        for f in fails:
            print('        - ' + f)
        bad += len(fails)
    sys.exit(1 if bad else 0)
