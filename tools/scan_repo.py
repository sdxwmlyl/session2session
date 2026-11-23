# -*- coding: utf-8 -*-
"""发布前脱敏扫描：确认仓库里没有本机路径 / 会话数据 / 真实案例。

用法：python tools\\scan_repo.py [仓库目录]
退出码 0 = 干净；1 = 有命中（逐条列出）
"""
import io
import os
import re
import subprocess
import sys

ROOT = sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PATS = {
    # 占位符（<...>、<user>、<host>、<workspace>、<path-to>）不算命中
    "绝对路径": re.compile(r"(?<!<)[CD]:\\(?!<)|\b[A-Z]:/(?!<)"),  # PLACEHOLDER_OK
    "用户目录": re.compile(r"Users\\+Administrator|Users/Administrator", re.I),
    "真实任务 id": re.compile(r"t-20\d{2}(?!0101)\d{4}-\d{4}-[0-9a-f]{8}"),
    "会话/用户 id": re.compile(r"o9cq[0-9A-Za-z_\-]{10,}|\d{13}-[a-z0-9]{7,}"),
    "敏感词": re.compile(r"哼哼|月饼|奖励小铺|纳西妲|律所|客户名"),
    "凭据": re.compile(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})", re.I),
}


def tracked(root):
    out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout
    return [x for x in out.split("\n") if x.strip()]


def main():
    files = tracked(ROOT)
    hits = 0
    for f in files:
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            t = io.open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for name, rx in PATS.items():
            for m in rx.finditer(t):
                ctx = t[max(0, m.start() - 30):m.end() + 30]
                if "<user>" in ctx or "<host>" in ctx or "<workspace>" in ctx or "<path-to>" in ctx:
                    continue
                hits += 1
                print("HIT %-14s %-40s %s" % (name, f, m.group(0)[:40]))
                break
    print("tracked=%d hits=%d -> %s" % (len(files), hits, "CLEAN" if hits == 0 else "NEEDS SCRUB"))
    return 0 if hits == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
