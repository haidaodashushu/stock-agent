#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""古例回归测试：覆盖九宗门各法。运行 python3 scripts/test_cases.py"""
import subprocess, sys, os, json

HERE = os.path.dirname(os.path.abspath(__file__))
ENGINE = os.path.join(HERE, "liuren.py")

# (说明, 日干支, 月将, 占时, 期望三传, 期望取用关键字)
CASES = [
    ("涉害·丁卯日亥将丑时（六壬详解）", "丁卯", "亥", "丑", "亥酉未", "涉害"),
    ("比用知一·辛巳日寅将戌时（袖里乾坤）", "辛巳", "寅", "戌", "酉丑巳", "比用"),
    ("返吟井栏·丁未日申将寅时", "丁未", "申", "寅", "巳丑丑", "返吟"),
    ("返吟井栏·丁丑日卯将酉时", "丁丑", "卯", "酉", "亥未丑", "返吟"),
    ("返吟无依·庚戌日（将时相冲，下克上用寅）", "庚戌", "寅", "申", "寅申寅", "返吟"),
    ("八专阳日·甲寅日寅将巳时", "甲寅", "寅", "巳", "丑亥亥", "八专"),
    ("八专阴日·丁未日（干上戌）", "丁未", "戌", "未", "亥戌戌", "八专"),
    ("昴星虎视·戊寅日（阳日取地盘酉上神）", "戊寅", "辰", "子", "丑午酉", "昴星"),
    ("昴星冬蛇掩目·丁亥日（阴日取天盘酉下神）", "丁亥", "戌", "未", "午戌寅", "昴星"),
    ("别责阳日·丙辰日午将巳时（丙合辛，辛寄戌）", "丙辰", "午", "巳", "亥午午", "别责"),
    ("别责阴日·辛酉日（支前三合取丑）", "辛酉", "酉", "戌", "丑酉酉", "别责"),
    ("伏吟自任·丙辰日（六丙伏吟巳申寅）", "丙辰", "辰", "辰", "巳申寅", "伏吟"),
    ("伏吟杜传·壬辰日（初传自刑改取支上）", "壬辰", "辰", "辰", "亥辰戌", "伏吟"),
    ("伏吟不虞·癸丑日（有克，六癸丑戌未）", "癸丑", "丑", "丑", "丑戌未", "伏吟"),
    ("伏吟自信·丁未日（阴日取支上神）", "丁未", "未", "未", "未丑戌", "伏吟"),
    ("伏吟·六甲日寅巳申", "甲子", "子", "子", "寅巳申", "伏吟"),
    ("伏吟·六庚日申寅巳", "庚午", "午", "午", "申寅巳", "伏吟"),
    ("伏吟·乙丑日辰丑戌", "乙丑", "丑", "丑", "辰丑戌", "伏吟"),
    ("伏吟·辛酉日酉戌未", "辛酉", "酉", "酉", "酉戌未", "伏吟"),
    ("伏吟·壬子日亥子卯", "壬子", "子", "子", "亥子卯", "伏吟"),
    ("遥克弹射·壬申日亥时寅将（干克巳）", "壬申", "寅", "亥", "巳申亥", "遥克"),
]


def run(day_gz, general, hour):
    out = subprocess.run([sys.executable, ENGINE, "--day-gz", day_gz,
                          "--general", general, "--hour", hour, "--json"],
                         capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(out.stderr)
    return json.loads(out.stdout)


def test_wangshuai():
    """旺衰表校验：四季五行旺相休囚死 + 土旺四季"""
    sys.path.insert(0, HERE)
    from liuren import wang_shuai, BRANCHES
    expect = {
        "寅": ["旺", "相", "死", "囚", "休"],   # 春：木旺火相土死金囚水休
        "巳": ["休", "旺", "相", "死", "囚"],   # 夏：火旺土相木休水囚金死
        "申": ["死", "囚", "休", "旺", "相"],   # 秋：金旺水相土休火囚木死
        "亥": ["相", "死", "囚", "休", "旺"],   # 冬：水旺木相金休土囚火死
    }
    ok = fail = 0
    print("\n--- 旺衰表校验（五行序：木火土金水）---")
    for mb, want in expect.items():
        i = BRANCHES.index(mb)
        got = [wang_shuai(e, i)[0] for e in range(5)]
        good = got == want
        print("%s %s月  %s" % ("PASS" if good else "FAIL", mb, " ".join(got)))
        ok += good
        fail += not good
    # 土旺四季
    for mb in "辰未戌丑":
        i = BRANCHES.index(mb)
        st = wang_shuai(2, i)[0]
        good = st == "旺"
        print("%s %s月 土=%s（土旺四季）" % ("PASS" if good else "FAIL", mb, st))
        ok += good
        fail += not good
    return ok, fail


def test_baoshi():
    """报时起课：日干支与月将不变，只换占时"""
    print("\n--- 报时（活时）起课校验 ---")
    base = subprocess.run([sys.executable, ENGINE, "--day-gz", "乙丑",
                           "--general", "午", "--hour", "午", "--json"],
                          capture_output=True, text=True)
    bao = subprocess.run([sys.executable, ENGINE, "--day-gz", "乙丑",
                          "--general", "午", "--baoshi", "申", "--json"],
                         capture_output=True, text=True)
    b0, b1 = json.loads(base.stdout), json.loads(bao.stdout)
    checks = [
        ("日干支不变", b0["日干支"] == b1["日干支"] == "乙丑"),
        ("月将不变", b0["月将"] == b1["月将"]),
        ("占时已换", "申" in b1["占时"] and "报时" in b1["占时"]),
        ("盘式随之改变", b0["盘式"] == "伏吟" and b1["盘式"] == "常盘"),
    ]
    ok = fail = 0
    for name, good in checks:
        print("%s %s" % ("PASS" if good else "FAIL", name))
        ok += good
        fail += not good
    return ok, fail


def main():
    ok = fail = 0
    for desc, gz, gen, hr, want, method in CASES:
        d = run(gz, gen, hr)
        got = "".join(x["支"] for x in d["三传"])
        good = got == want and method in (d["取用"] or "")
        print("%s %-42s 期望%s 实得%s  [%s / %s]"
              % ("PASS" if good else "FAIL", desc, want, got, d["取用"], d["课体"]))
        if good:
            ok += 1
        else:
            fail += 1
            for n in d["推演"]:
                print("       · " + n)
    o, f = test_wangshuai()
    ok, fail = ok + o, fail + f
    o, f = test_baoshi()
    ok, fail = ok + o, fail + f
    print("\n合计 %d 项：通过 %d，失败 %d" % (ok + fail, ok, fail))
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
