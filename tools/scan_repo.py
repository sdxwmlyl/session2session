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
SELF = os.path.join("tools", "scan_repo.py")

PATTERNS = {
    "绝对路径": re.compile(r"(?<!<)[CD]:[\\/](?!<)"),
    "用户目录": re.compile(r"Users[\\/]+Administrator", re.I),
    "真实任务id": re.compile(r"t-20(?!26 0101)\d{6}-\d{4}-[0-9a-f]{8}".replace(" ", "")),
    "会话用户id": re.compile(r"o9cq[0-9A-Za-z_\-]{10,}|\d{13}-[a-z0-9]{7,}"),
    "敏感词": re.compile("\\u54fc\\u54fc|\\u6708\\u997c|\\u7eb3\\u897f\\u59b2|\\u5f8b\\u6240"),
    "凭据": re.compile(r"(sk-[A-Za-z0-9]{10,}|ghp_[A-Za-z0-9]{20,}|Bearer\s+[A-Za-z0-9._-]{20,})", re.I),
}
ALLOW = ("<user>", "<host>", "<workspace>", "<path-to>")


def tracked(root):
    out = subprocess.run(["git", "-C", root, "ls-files"], capture_output=True, text=True).stdout
    return [x for x in out.split("\n") if x.strip()]


def main():
    files = tracked(ROOT)
    hits = 0
    for f in files:
        if f.replace("\\", "/").endswith(SELF.replace("\\", "/")):
            continue
        p = os.path.join(ROOT, f)
        if not os.path.isfile(p) or os.path.getsize(p) > 2_000_000:
            continue
        try:
            t = io.open(p, encoding="utf-8", errors="ignore").read()
        except Exception:
            continue
        for name, rx in PATTERNS.items():
            for m in rx.finditer(t):
                ctx = t[max(0, m.start() - 30):m.end() + 30]
                if any(a in ctx for a in ALLOW):
                    continue
                hits += 1
                print("HIT %-10s %-40s %s" % (name, f, m.group(0)[:40]))
                break
    print("tracked=%d hits=%d -> %s" % (len(files), hits, "CLEAN" if hits == 0 else "NEEDS SCRUB"))
    return 0 if hits == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
