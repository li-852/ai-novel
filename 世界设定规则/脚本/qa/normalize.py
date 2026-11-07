# -*- coding: utf-8 -*-
"""把正文里"单换行分开的段落"补成空行分开的段落，并保证文件末尾只有一个换行。
用法: python 世界设定规则/脚本/qa/normalize.py 正文/.../第NNN章_XXXX.md [...]
"""
import io
import re
import sys

pat = re.compile(u"(?<!\n)\n(?!\n)")

for p in sys.argv[1:]:
    s = io.open(p, encoding='utf-8').read()
    s = s.replace(u"\r\n", u"\n")
    s = pat.sub(u"\n\n", s)
    s = re.sub(u"\n{3,}", u"\n\n", s)
    s = s.rstrip(u"\n") + u"\n"
    io.open(p, 'w', encoding='utf-8', newline='\n').write(s)
    print("normalized", p)
