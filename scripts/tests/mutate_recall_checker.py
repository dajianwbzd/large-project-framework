#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mutate_checker.py — **变异检查器本身**（不是变异数据）

为什么要这个脚本
----------------
`test_recall.py` 里新增了 4 条**反向**断言（`must_not=[...]`）。反向断言有个结构性风险：
**检查器压根不存在时，它们也全绿**——"永不报警"能通过任何 must_not。
（框架自己的原话：永远返回 0 ERROR 的检查器比没有更坏。）

所以必须从**另一边**对拍：把检查器改瞎，看对应的**正向**断言会不会变红；
把检查器改松，看对应的**反向**断言会不会变红。只有两个方向都能被打破，
这组断言才算有判别力。

用法：python mutate_checker.py <框架 scripts 目录>
"""

import os
import shutil
import subprocess
import sys
import tempfile

# (名字, 在 recall.py 里替换的片段, 替换成什么, 期望**变红**的断言关键字)
SABOTAGE = [
    ("把锚点路径校验改成永不检查",
     "def anchor_is_checkable(anchor):\n    \"\"\"",
     "def anchor_is_checkable(anchor):\n    return False\n    \"\"\"",
     ["发现项 anchor 路径不存在", "修复卡锚点路径不存在"]),
    ("取消「格式旧」闸门（旧块也逐行判锚点）",
     "            if not findings_stale:\n                anc =",
     "            if True:\n                anc =",
     ["发现项机读块为旧列集"]),
    ("取消「键缺 vs 键空」区分（旧卡也判 E）",
     "        if not (c[\"has_anchor_key\"] or c[\"has_entry_key\"]):\n            card_unmigrated.append",
     "        if False:\n            card_unmigrated.append",
     ["反·修复卡无 anchor/entry 键"]),
    ("把行号也当成校验对象（剥行号那步去掉）",
     "    a = re.sub(r\":\\d+\\s*$\", \"\", a)             # 剥行号（只剥末尾数字）",
     "    pass",
     ["反·锚点只改行号"]),
]


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    src_dir = os.path.abspath(sys.argv[1])
    recall = os.path.join(src_dir, "recall.py")
    tests = os.path.join(src_dir, "tests", "test_recall.py")
    if not (os.path.exists(recall) and os.path.exists(tests)):
        print("✗ 需要包含 recall.py 与 tests/test_recall.py 的目录：%s" % src_dir)
        return 2
    with open(recall, encoding="utf-8") as f:
        base = f.read()

    bad = 0
    for name, old, new, expect_red in SABOTAGE:
        if old not in base:
            print("✗ 变异目标不存在（检查器可能已改名）：%s\n    %r" % (name, old[:70]))
            bad += 1
            continue
        tmp = tempfile.mkdtemp(prefix="mutcheck_")
        try:
            os.makedirs(os.path.join(tmp, "tests"))
            with open(os.path.join(tmp, "recall.py"), "w", encoding="utf-8",
                      newline="\n") as f:
                f.write(base.replace(old, new, 1))
            shutil.copyfile(tests, os.path.join(tmp, "tests", "test_recall.py"))
            p = subprocess.run([sys.executable, os.path.join(tmp, "tests", "test_recall.py")],
                               capture_output=True, encoding="utf-8")
            out = p.stdout
            red = [ln for ln in out.splitlines() if ln.startswith("✗")]
            hit = [k for k in expect_red if any(k in ln for ln in red)]
            ok = len(hit) == len(expect_red)
            print("%s 检查器变异·%s" % ("✓" if ok else "✗", name))
            if not ok:
                bad += 1
                print("    期望变红的断言 %s；实际变红：%s"
                      % (expect_red, [ln[:90] for ln in red] or "（全绿——说明这组断言没有判别力）"))
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    print("=" * 72)
    print("检查器变异 %d 条：抓住 %d，漏 %d" % (len(SABOTAGE), len(SABOTAGE) - bad, bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
