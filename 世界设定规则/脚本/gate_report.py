# -*- coding: utf-8 -*-
"""门禁批量报告：一次跑多章/整册的 chapter_gate 结果，默认打 stdout。

用法：
    PYTHONIOENCODING=utf-8 python 世界设定规则/脚本/gate_report.py <章节md路径或目录...>
    PYTHONIOENCODING=utf-8 python 世界设定规则/脚本/gate_report.py --cross <路径...>  # 附带跨章校验
    PYTHONIOENCODING=utf-8 python 世界设定规则/脚本/gate_report.py --out 某.txt <路径...>  # 需要留档时才写文件

不再固定落 .gate_report.txt；控制台带 PYTHONIOENCODING=utf-8 即无乱码。
"""
import argparse
import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import chapter_gate as g  # noqa: E402


def collect(args):
    out = []
    for a in args:
        if os.path.isdir(a):
            for name in sorted(os.listdir(a)):
                if name.startswith("第") and name.endswith(".md"):
                    out.append(os.path.join(a, name))
        else:
            out.append(a)
    return out


def rel(path):
    return os.path.relpath(path, ROOT).replace("\\", "/")


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('--cross', action='store_true', help='附带跨章校验')
    ap.add_argument('--out', metavar='路径', help='同时把报告写成 UTF-8 文件')
    a, rest = ap.parse_known_args()
    files = collect(rest)

    lines = []
    for path in files:
        if not os.path.isfile(path):
            lines.append("MISS %s | 文件不存在" % rel(path))
            continue
        fails, info = g.gate(path)
        tag = "PASS" if not fails else "FAIL"
        detail = "汉字%s 段数%s 引号%s CRLF%s 逗号%s 拉丁%s" % (
            info.get("汉字"), info.get("段数"), info.get("引号"),
            info.get("CRLF"), info.get("半角逗号"), info.get("拉丁词") or "无",
        )
        note = info.get("字数提示") or ""
        bad = "；".join(str(x) for x in fails) if fails else "无"
        lines.append("%s | %s | %s%s | 问题：%s" % (tag, rel(path), detail, (" | " + note) if note else "", bad))

    if a.cross and len(files) >= 2:
        lines.append("")
        lines.append("=== 跨章校验 ===")
        try:
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                g.cross_report([os.path.abspath(f) for f in files])
            text = buf.getvalue().strip() or "（无输出）"
            lines.append(text)
        except Exception as exc:
            lines.append("跨章校验异常：%r" % (exc,))

    body = "\n".join(lines)
    npass = sum(1 for x in lines if x.startswith("PASS"))
    body += "\n合计 %d 章：PASS %d，FAIL %d" % (len(files), npass, len(files) - npass)
    sys.stdout.write(body + "\n")
    if a.out:
        with io.open(a.out, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(body + "\n")
        sys.stdout.write("written %s\n" % rel(a.out))


if __name__ == "__main__":
    main()
