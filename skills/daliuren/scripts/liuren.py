#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""大六壬排盘引擎：天地盘 / 四课 / 九宗门三传 / 十二天将 / 遁干旬空 / 神煞 / 课体格局。

用法示例：
  # 按公历时间自动定日干支与月将（依太阳黄经过宫）
  python3 liuren.py --datetime "2026-08-18 21:30"
  python3 liuren.py --now

  # 直接指定课式三要素排古例（不依赖历法）
  python3 liuren.py --day-gz 丁未 --general 申 --hour 寅
  python3 liuren.py --day-gz 丁卯 --general 亥 --hour 丑 --json

  # 带年命/行年
  python3 liuren.py --now --benming 子 --xingnian 寅
"""

import argparse
import json
import math
from datetime import datetime, timedelta

STEMS = "甲乙丙丁戊己庚辛壬癸"
BRANCHES = "子丑寅卯辰巳午未申酉戌亥"

# 五行
WU_XING = "木火土金水"
BRANCH_ELEM = [4, 2, 0, 0, 2, 1, 1, 2, 3, 3, 2, 4]  # 子水丑土寅木卯木辰土巳火午火未土申金酉金戌土亥水
STEM_ELEM = [0, 0, 1, 1, 2, 2, 3, 3, 4, 4]          # 甲乙木 丙丁火 戊己土 庚辛金 壬癸水
ELEM_NAME = ["木", "火", "土", "金", "水"]

# 天干寄宫（四正不用）
JI_GONG = [2, 4, 5, 7, 5, 7, 8, 10, 11, 1]  # 甲寅 乙辰 丙巳 丁未 戊巳 己未 庚申 辛戌 壬亥 癸丑
JI_GONG_STEMS = {}   # 地盘宫 -> 所寄天干列表
for _s, _b in enumerate(JI_GONG):
    JI_GONG_STEMS.setdefault(_b, []).append(_s)

# 十二天将
TIAN_JIANG = ["贵人", "螣蛇", "朱雀", "六合", "勾陈", "青龙",
              "天空", "白虎", "太常", "玄武", "太阴", "天后"]
JIANG_SHORT = {"贵人": "贵", "螣蛇": "蛇", "朱雀": "雀", "六合": "合", "勾陈": "勾",
               "青龙": "龙", "天空": "空", "白虎": "虎", "太常": "常", "玄武": "武",
               "太阴": "阴", "天后": "后"}

# 十二月将名
GENERAL_NAME = {0: "神后", 1: "大吉", 2: "功曹", 3: "太冲", 4: "天罡", 5: "太乙",
                6: "胜光", 7: "小吉", 8: "传送", 9: "从魁", 10: "河魁", 11: "登明"}

# 三刑（辰午酉亥自刑）
XING = {0: 3, 3: 0, 2: 5, 5: 8, 8: 2, 1: 10, 10: 7, 7: 1, 4: 4, 6: 6, 9: 9, 11: 11}
SELF_XING = {4, 6, 9, 11}

# 干合
GAN_HE = {0: 5, 5: 0, 1: 6, 6: 1, 2: 7, 7: 2, 3: 8, 8: 3, 4: 9, 9: 4}

MENG = {2, 8, 5, 11}      # 四孟 寅申巳亥
ZHONG = {0, 6, 3, 9}      # 四仲 子午卯酉
JI = {4, 10, 1, 7}        # 四季 辰戌丑未

# 日禄
LU = {0: 2, 1: 3, 2: 5, 3: 6, 4: 5, 5: 6, 6: 8, 7: 9, 8: 11, 9: 0}
# 干墓（水土同墓于辰）
GAN_MU = {0: 7, 1: 7, 2: 10, 3: 10, 4: 4, 5: 4, 6: 1, 7: 1, 8: 4, 9: 4}
# 贵人（昼, 夜）
GUI_REN = {0: (1, 7), 4: (1, 7), 6: (1, 7), 1: (0, 8), 5: (0, 8),
           2: (11, 9), 3: (11, 9), 8: (3, 5), 9: (3, 5), 7: (6, 2)}
# 三合局 -> 驿马 / 咸池
SAN_HE = {8: 0, 0: 0, 4: 0, 5: 1, 9: 1, 1: 1, 2: 2, 6: 2, 10: 2, 11: 3, 3: 3, 7: 3}
YI_MA = [2, 11, 8, 5]      # 申子辰马寅 / 巳酉丑马亥 / 寅午戌马申 / 亥卯未马巳
XIAN_CHI = [9, 6, 3, 0]    # 咸池（桃花）


SHENG_PAIRS = {(0, 1), (1, 2), (2, 3), (3, 4), (4, 0)}  # 木生火 火生土 土生金 金生水 水生木


def sheng(a, b):
    """五行 a 生 b"""
    return (a, b) in SHENG_PAIRS


def ke(a, b):
    """五行 a 克 b：木克土 火克金 土克水 金克木 水克火"""
    return (a, b) in {(0, 2), (1, 3), (2, 4), (3, 0), (4, 1)}


# ---------------------------------------------------------------- 旺衰

# 四季五行旺相休囚死：季节 -> {五行: 状态}
# 春木旺火相水休金囚土死 / 夏火旺土相木休水囚金死
# 秋金旺水相土休火囚木死 / 冬水旺木相金休土囚火死
SEASON_ELEM = {"春": 0, "夏": 1, "秋": 3, "冬": 4}  # 当令五行
WANG_SHUAI_ORDER = ["旺", "相", "休", "囚", "死"]

# 月支 -> 季节（寅卯辰春 巳午未夏 申酉戌秋 亥子丑冬）
# 注：季末之月（辰未戌丑）本季当令五行仍属余气，因归本季故自然判旺；
#     同时土旺四季，故辰未戌丑月土亦作旺论。
MONTH_SEASON = {2: "春", 3: "春", 4: "春", 5: "夏", 6: "夏", 7: "夏",
                8: "秋", 9: "秋", 10: "秋", 11: "冬", 0: "冬", 1: "冬"}


def wang_shuai(elem, month_branch):
    """判五行在某月令下的旺相休囚死。返回 (状态, 是否有力)"""
    if month_branch is None:
        return None, None
    season = MONTH_SEASON[month_branch]
    ling = SEASON_ELEM[season]                    # 当令五行
    # 土旺四季（辰戌丑未月土当令）
    if month_branch in JI and elem == 2:
        return "旺", True
    if elem == ling:
        return "旺", True
    if sheng(ling, elem):
        return "相", True
    if sheng(elem, ling):
        return "休", False
    if ke(elem, ling):
        return "囚", False
    return "死", False


def liu_qin(day_stem_elem, other_elem):
    if other_elem == day_stem_elem:
        return "兄弟"
    if sheng(other_elem, day_stem_elem):
        return "父母"
    if sheng(day_stem_elem, other_elem):
        return "子孙"
    if ke(other_elem, day_stem_elem):
        return "官鬼"
    return "妻财"


# ---------------------------------------------------------------- 历法部分

def julian_day(dt_utc):
    y, m = dt_utc.year, dt_utc.month
    d = (dt_utc.day + dt_utc.hour / 24.0 + dt_utc.minute / 1440.0
         + dt_utc.second / 86400.0)
    if m <= 2:
        y -= 1
        m += 12
    a = y // 100
    b = 2 - a + a // 4
    return int(365.25 * (y + 4716)) + int(30.6001 * (m + 1)) + d + b - 1524.5


def delta_t(year):
    """近似 ΔT（秒），适用 1900-2150。"""
    if year < 1950:
        t = year - 1900
        return -2.79 + 1.494119 * t - 0.0598939 * t ** 2 + 0.0061966 * t ** 3 - 0.000197 * t ** 4
    if year < 2005:
        t = year - 2000
        return 63.86 + 0.3345 * t - 0.060374 * t ** 2 + 0.0017275 * t ** 3
    if year < 2050:
        t = year - 2000
        return 62.92 + 0.32217 * t + 0.005589 * t ** 2
    return 62.92 + 0.32217 * 50 + 0.005589 * 2500 + 0.5 * (year - 2050)


def sun_apparent_longitude(jd_ut):
    """太阳视黄经（度）。Meeus 低精度公式，误差 < 0.01°。"""
    jde = jd_ut + delta_t(2000 + (jd_ut - 2451545.0) / 365.25) / 86400.0
    t = (jde - 2451545.0) / 36525.0
    l0 = 280.46646 + 36000.76983 * t + 0.0003032 * t * t
    m = math.radians(357.52911 + 35999.05029 * t - 0.0001537 * t * t)
    c = ((1.914602 - 0.004817 * t - 0.000014 * t * t) * math.sin(m)
         + (0.019993 - 0.000101 * t) * math.sin(2 * m)
         + 0.000289 * math.sin(3 * m))
    true_long = l0 + c
    omega = math.radians(125.04 - 1934.136 * t)
    return (true_long - 0.00569 - 0.00478 * math.sin(omega)) % 360.0


def month_general_from_longitude(lam):
    """太阳过宫定月将：雨水后登明亥，依次逆行。"""
    order = [11, 10, 9, 8, 7, 6, 5, 4, 3, 2, 1, 0]  # 亥戌酉申未午巳辰卯寅丑子
    idx = int(((lam - 330.0) % 360.0) // 30)
    return order[idx]


def solar_month_index(lam):
    """节气月建：立春(315°)起寅月，返回 0=寅月 ... 11=丑月"""
    return int(((lam - 315.0) % 360.0) // 30)


def ganzhi_of_day(dt_local, night_advance=True):
    """日干支序（0-59，甲子=0）。night_advance: 23:00 后进位次日。"""
    d = dt_local
    if night_advance and d.hour >= 23:
        d = d + timedelta(days=1)
    jdn = int(julian_day(datetime(d.year, d.month, d.day, 12, 0)) + 0.5)
    return (jdn + 49) % 60


def hour_branch_of(dt_local):
    return ((dt_local.hour + 1) % 24) // 2


# ---------------------------------------------------------------- 排盘核心

class LiuRen:
    def __init__(self, day_gz, general, hour, is_day_time=None,
                 benming=None, xingnian=None, meta=None,
                 month_branch=None, bao_shi=False):
        self.day_stem = day_gz // 10 if day_gz >= 10 or day_gz == 0 else day_gz % 10
        self.day_gz = day_gz % 60
        self.day_stem = self.day_gz % 10
        self.day_branch = self.day_gz % 12
        self.general = general              # 月将（天盘加临之神）
        self.hour = hour                    # 占时（地盘落点）
        self.meta = meta or {}
        self.benming = benming
        self.xingnian = xingnian
        self.month_branch = month_branch    # 月令（节气月建），用于旺衰
        self.bao_shi = bao_shi              # 是否报时（活时）起课
        if is_day_time is None:
            is_day_time = 3 <= hour <= 8    # 卯至申为昼
        self.is_day_time = is_day_time

        self.offset = (general - hour) % 12
        # 天盘：地盘 i 宫之上所乘天盘神
        self.tianpan = [(i + self.offset) % 12 for i in range(12)]
        # 天盘神 -> 其所落地盘宫
        self.dipan_of = [0] * 12
        for i, b in enumerate(self.tianpan):
            self.dipan_of[b] = i

        self.pan_type = "伏吟" if self.offset == 0 else ("返吟" if self.offset == 6 else "常盘")
        self.jigong = JI_GONG[self.day_stem]
        self.is_yang_day = self.day_stem % 2 == 0
        self.eight_zhuan = self.jigong == self.day_branch

        self._build_ke()
        self._build_tianjiang()
        self._build_dungan()
        self._build_chuan()
        self._build_shensha()
        self._build_wangshuai()
        self._build_geju()

    # ---------------- 旺衰（天时·地利·人和）
    def _build_wangshuai(self):
        mb = self.month_branch
        self.wangshuai = {}
        if mb is None:
            self.day_wangshuai = None
            return
        # 各神旺衰
        for b in range(12):
            st, strong = wang_shuai(BRANCH_ELEM[b], mb)
            self.wangshuai[b] = st
        # 日干旺衰：天时（月令）+ 地利（寄宫所乘）+ 人和（三传生助）
        de = STEM_ELEM[self.day_stem]
        tian_shi, ts_ok = wang_shuai(de, mb)
        # 地利：日干寄宫之上神与日干的关系
        up = self.gan_up
        ue = BRANCH_ELEM[up]
        if sheng(ue, de) or ue == de:
            di_li, dl_ok = "得地利（上神%s生助）" % BRANCHES[up], True
        elif ke(ue, de):
            di_li, dl_ok = "失地利（上神%s克日干）" % BRANCHES[up], False
        else:
            di_li, dl_ok = "地利平（上神%s无生克）" % BRANCHES[up], False
        # 人和：三传中是否有生助日干者
        helpers = [b for b in self.chuan if sheng(BRANCH_ELEM[b], de) or BRANCH_ELEM[b] == de]
        if helpers:
            ren_he, rh_ok = "得人和（三传%s生助）" % "".join(BRANCHES[b] for b in helpers), True
            ke_count = 0
        else:
            kers = [b for b in self.chuan if ke(BRANCH_ELEM[b], de)]
            ke_count = len(kers)
            if kers:
                ren_he, rh_ok = "失人和（三传%s克日干）" % "".join(BRANCHES[b] for b in kers), False
            else:
                ren_he, rh_ok = "人和平（三传无生助）", False
        score = sum([ts_ok, dl_ok, rh_ok])
        level = ["弱（三者俱失）", "偏弱", "偏旺", "旺（三者俱得）"][score]
        self.day_wangshuai = {
            "日干五行": ELEM_NAME[de],
            "月令": BRANCHES[mb] + "月（" + MONTH_SEASON[mb] + "）",
            "天时": tian_shi,
            "地利": di_li,
            "人和": ren_he,
            "总评": level,
        }

    # ---------------- 四课
    def _build_ke(self):
        gan_up = self.tianpan[self.jigong]
        gan_up2 = self.tianpan[gan_up]
        zhi_up = self.tianpan[self.day_branch]
        zhi_up2 = self.tianpan[zhi_up]
        self.gan_up, self.zhi_up = gan_up, zhi_up
        # 每课：(上神, 下神, 下神五行, 下神显示)
        self.ke = [
            {"idx": 1, "up": gan_up, "down": self.day_stem, "down_elem": STEM_ELEM[self.day_stem],
             "down_label": STEMS[self.day_stem], "name": "干阳课"},
            {"idx": 2, "up": gan_up2, "down": gan_up, "down_elem": BRANCH_ELEM[gan_up],
             "down_label": BRANCHES[gan_up], "name": "干阴课"},
            {"idx": 3, "up": zhi_up, "down": self.day_branch, "down_elem": BRANCH_ELEM[self.day_branch],
             "down_label": BRANCHES[self.day_branch], "name": "支阳课"},
            {"idx": 4, "up": zhi_up2, "down": zhi_up, "down_elem": BRANCH_ELEM[zhi_up],
             "down_label": BRANCHES[zhi_up], "name": "支阴课"},
        ]
        for k in self.ke:
            ue = BRANCH_ELEM[k["up"]]
            k["up_elem"] = ue
            k["xia_ze_shang"] = ke(k["down_elem"], ue)   # 下贼上
            k["shang_ke_xia"] = ke(ue, k["down_elem"])   # 上克下
        # 四课是否全备：日干以其寄宫参与比对（干阳课与支阴课可因寄宫重合而成三课）
        pairs = {(k["up"], self.jigong if k["idx"] == 1 else k["down"]) for k in self.ke}
        self.distinct_ke = len(pairs)

    # ---------------- 十二天将
    def _build_tianjiang(self):
        gr = GUI_REN[self.day_stem][0 if self.is_day_time else 1]
        self.guiren_branch = gr
        pos = self.dipan_of[gr]                     # 贵人所落地盘宫
        forward = pos in (11, 0, 1, 2, 3, 4)        # 亥子丑寅卯辰顺行
        self.jiang_forward = forward
        self.jiang_of_branch = {}
        for k in range(12):
            p = (pos + k) % 12 if forward else (pos - k) % 12
            self.jiang_of_branch[self.tianpan[p]] = TIAN_JIANG[k]

    # ---------------- 遁干与旬空
    def _build_dungan(self):
        xun_head = self.day_gz - (self.day_gz % 10)
        head_branch = xun_head % 12
        self.xun_head = xun_head
        self.kong = {(head_branch + 10) % 12, (head_branch + 11) % 12}
        self.dungan = {}
        for b in range(12):
            off = (b - head_branch) % 12
            if off < 10:
                self.dungan[b] = off
        # 旬丁
        self.ding_shen = (head_branch + 3) % 12

    # ---------------- 三传（九宗门）
    def _build_chuan(self):
        self.method = None
        self.method_detail = []
        self.ke_ti = None
        chu = None

        xia = [k for k in self.ke if k["xia_ze_shang"]]
        shang = [k for k in self.ke if k["shang_ke_xia"]]

        def by_zei_ke():
            """贼克 -> 比用 -> 涉害，返回 (初传, 方法名, 课体名)"""
            if xia:
                group, kind = xia, "下贼上"
            elif shang:
                group, kind = shang, "上克下"
            else:
                return None, None, None
            uniq = {k["up"] for k in group}
            if len(uniq) == 1:
                if kind == "下贼上":
                    return group[0]["up"], "贼克法", "重审课"
                return group[0]["up"], "贼克法", "元首课"
            # 多克：先比用
            n = len(uniq)
            base = ("知一课" if n == 2 else ("度厄课" if n == 3 else
                    ("绝嗣课" if kind == "下贼上" else "无禄课")))
            same = [k for k in group if (k["up"] % 2 == 0) == self.is_yang_day]
            same_uniq = {k["up"] for k in same}
            if len(same_uniq) == 1:
                self.method_detail.append(f"{kind}{n}处，取与日干同阴阳者发用（比用）")
                return same[0]["up"], "比用法", base
            # 俱比俱不比 -> 涉害
            cands = group if len(same_uniq) == 0 else same
            chosen, note = self._she_hai(cands, kind)
            self.method_detail.append(note)
            return chosen["up"], "涉害法", self.ke_ti or "涉害课"

        if self.pan_type == "伏吟":
            chu, m, kt = by_zei_ke()
            if chu is None:
                chu = self.gan_up if self.is_yang_day else self.zhi_up
                self.method = "伏吟法"
                self.ke_ti = "自任课" if self.is_yang_day else "自信课"
                self.method_detail.append(
                    "伏吟无克，%s取%s上神发用" % ("阳日" if self.is_yang_day else "阴日",
                                              "干" if self.is_yang_day else "支"))
            else:
                self.method = "伏吟法（有克依%s取用）" % m
                self.ke_ti = "不虞课"
            zhong, mo = self._fu_yin_zhong_mo(chu)
            self.chuan = [chu, zhong, mo]
            return

        if self.pan_type == "返吟":
            chu, m, kt = by_zei_ke()
            if chu is None:
                ma = YI_MA[SAN_HE[self.day_branch]]
                chu = ma
                self.method = "返吟法"
                self.ke_ti = "无亲课（井栏射）"
                self.method_detail.append("返吟四课无克，取日马%s发用，中传支上神，末传干上神"
                                          % BRANCHES[ma])
                self.chuan = [chu, self.zhi_up, self.gan_up]
                return
            self.method = "返吟法（有克依%s取用）" % m
            self.ke_ti = "无依课"
            self.chuan = self._forward_chuan(chu)
            return

        # 常盘
        chu, m, kt = by_zei_ke()
        if chu is not None:
            self.method, self.ke_ti = m, kt
            self.chuan = self._forward_chuan(chu)
            return

        # 无上下克
        if self.eight_zhuan:
            if self.is_yang_day:
                chu = (self.gan_up + 2) % 12
                self.method_detail.append("八专阳日：干上神%s顺数三位得%s"
                                          % (BRANCHES[self.gan_up], BRANCHES[chu]))
            else:
                si_yin = self.ke[3]["up"]
                chu = (si_yin - 2) % 12
                self.method_detail.append("八专阴日：支阴上神%s逆数三位得%s"
                                          % (BRANCHES[si_yin], BRANCHES[chu]))
            self.method = "八专法"
            self.chuan = [chu, self.gan_up, self.gan_up]
            self.ke_ti = "独足课" if len(set(self.chuan)) == 1 else "帷簿不修课"
            return

        # 遥克
        yao = self._yao_ke()
        if yao is not None:
            chu, self.ke_ti, note = yao
            self.method = "遥克法"
            self.method_detail.append(note)
            self.chuan = self._forward_chuan(chu)
            return

        if self.distinct_ke == 3:
            if self.is_yang_day:
                he = GAN_HE[self.day_stem]
                chu = self.tianpan[JI_GONG[he]]
                self.method_detail.append(
                    "别责阳日：日干%s合%s，%s寄宫%s，取其上神%s发用"
                    % (STEMS[self.day_stem], STEMS[he], STEMS[he],
                       BRANCHES[JI_GONG[he]], BRANCHES[chu]))
            else:
                chu = (self.day_branch + 4) % 12
                self.method_detail.append("别责阴日：日支%s三合前位%s发用"
                                          % (BRANCHES[self.day_branch], BRANCHES[chu]))
            self.method = "别责法"
            self.ke_ti = "别责课（芜淫）"
            self.chuan = [chu, self.gan_up, self.gan_up]
            return

        # 昴星
        if self.is_yang_day:
            chu = self.tianpan[9]
            self.chuan = [chu, self.zhi_up, self.gan_up]
            self.ke_ti = "虎视课"
            self.method_detail.append("昴星阳日：取地盘酉上神%s发用，中传支上神，末传干上神"
                                      % BRANCHES[chu])
        else:
            chu = self.dipan_of[9]
            self.chuan = [chu, self.gan_up, self.zhi_up]
            self.ke_ti = "冬蛇掩目课"
            self.method_detail.append("昴星阴日：取天盘酉下神%s发用，中传干上神，末传支上神"
                                      % BRANCHES[chu])
        self.method = "昴星法"

    def _forward_chuan(self, chu):
        zhong = self.tianpan[chu]
        mo = self.tianpan[zhong]
        return [chu, zhong, mo]

    def _fu_yin_zhong_mo(self, chu):
        if chu in SELF_XING:
            # 初传自刑：中传改取另一宫上神（发用于干上则取支上，反之取干上）
            if chu == self.gan_up:
                zhong, src = self.zhi_up, "支"
            elif chu == self.zhi_up:
                zhong, src = self.gan_up, "干"
            else:
                zhong, src = ((self.zhi_up, "支") if self.is_yang_day
                              else (self.gan_up, "干"))
            self.ke_ti = "杜传课"
            self.method_detail.append("初传%s自刑，中传改取%s上神%s"
                                      % (BRANCHES[chu], src, BRANCHES[zhong]))
        else:
            zhong = XING[chu]
        if zhong in SELF_XING:
            mo = (zhong + 6) % 12
            self.method_detail.append("中传%s自刑，末传取其冲神%s"
                                      % (BRANCHES[zhong], BRANCHES[mo]))
        else:
            mo = XING[zhong]
        return zhong, mo

    def _she_hai(self, cands, kind):
        """涉害法：归本家计克数，深者发用；等则孟->仲->复等（刚日干上，柔日支上）。"""
        details = []
        best, best_n = [], -1
        for k in cands:
            n = self._depth(k["up"], kind)
            details.append("%s加%s涉害%d重" % (BRANCHES[k["up"]], k["down_label"], n))
            if n > best_n:
                best, best_n = [k], n
            elif n == best_n:
                best.append(k)
        note = "涉害归本家：" + "，".join(details)
        if len(best) == 1:
            self.ke_ti = "涉害课"
            return best[0], note + f"；取最深者{BRANCHES[best[0]['up']]}发用"
        for group, label in ((MENG, "见机课"), (ZHONG, "察微课")):
            sel = [k for k in best if self.dipan_of[k["up"]] in group]
            if sel:
                self.ke_ti = label
                return sel[0], note + f"；克数相等，取乘{'孟' if group is MENG else '仲'}位者{BRANCHES[sel[0]['up']]}（{label}）"
        self.ke_ti = "复等课"
        target = self.gan_up if self.is_yang_day else self.zhi_up
        for k in best:
            if k["up"] == target:
                return k, note + f"；复等，{'刚日取干上神' if self.is_yang_day else '柔日取支上神'}{BRANCHES[target]}"
        return best[0], note + "；复等，取先见者"

    def _depth(self, top, kind):
        """天盘神 top 自所落地盘宫顺行归本家，途中所历克数。"""
        start = self.dipan_of[top]
        if start == top:
            return 0
        cnt = 0
        i = (start + 1) % 12
        while i != top:
            elems = [BRANCH_ELEM[i]] + [STEM_ELEM[s] for s in JI_GONG_STEMS.get(i, [])]
            for e in elems:
                if kind == "下贼上":
                    if ke(e, BRANCH_ELEM[top]):
                        cnt += 1
                else:
                    if ke(BRANCH_ELEM[top], e):
                        cnt += 1
            i = (i + 1) % 12
        return cnt

    def _yao_ke(self):
        de = STEM_ELEM[self.day_stem]
        ups = [k["up"] for k in self.ke]
        hao = sorted({u for u in ups if ke(BRANCH_ELEM[u], de)})
        tan = sorted({u for u in ups if ke(de, BRANCH_ELEM[u])})
        for group, name, desc in ((hao, "蒿矢课", "四课无上下克，取上神遥克日干者发用"),
                                  (tan, "弹射课", "四课无上下克，取日干遥克之上神发用")):
            if not group:
                continue
            if len(group) == 1:
                return group[0], name, desc + f"（{BRANCHES[group[0]]}）"
            same = [u for u in group if (u % 2 == 0) == self.is_yang_day]
            if len(same) == 1:
                return same[0], name, desc + f"，二神择比得{BRANCHES[same[0]]}"
            pool = same or group
            kind = "下贼上" if group is hao else "上克下"
            best, best_n = [], -1
            for u in pool:
                n = self._depth(u, kind)
                if n > best_n:
                    best, best_n = [u], n
                elif n == best_n:
                    best.append(u)
            return best[0], name, desc + f"，俱比俱不比依涉害深浅取{BRANCHES[best[0]]}"
        return None

    # ---------------- 神煞
    def _build_shensha(self):
        db, ds = self.day_branch, self.day_stem
        self.shensha = {
            "旬空": [BRANCHES[b] for b in sorted(self.kong)],
            "旬首": STEMS[self.xun_head % 10] + BRANCHES[self.xun_head % 12],
            "驿马": BRANCHES[YI_MA[SAN_HE[db]]],
            "日禄": BRANCHES[LU[ds]],
            "羊刃": BRANCHES[(LU[ds] + 1) % 12],
            "干墓": BRANCHES[GAN_MU[ds]],
            "咸池": BRANCHES[XIAN_CHI[SAN_HE[db]]],
            "丁神": BRANCHES[self.ding_shen],
            "昼贵": BRANCHES[GUI_REN[ds][0]],
            "夜贵": BRANCHES[GUI_REN[ds][1]],
        }

    # ---------------- 课体格局
    def _build_geju(self):
        g = []
        c = self.chuan
        if self.pan_type != "常盘":
            g.append(self.pan_type + "课")
        d1, d2 = (c[1] - c[0]) % 12, (c[2] - c[1]) % 12
        if d1 == d2 == 1:
            g.append("进茹（连茹顺行）")
        elif d1 == d2 == 11:
            g.append("退茹（连茹逆行）")
        elif d1 == d2 == 2:
            g.append("顺间传")
        elif d1 == d2 == 10:
            g.append("逆间传")
        if len({SAN_HE[b] for b in c}) == 1 and len(set(c)) == 3:
            g.append("三合成局")
        if len(set(c)) == 1:
            g.append("三传一神（独足）")
        e = [BRANCH_ELEM[b] for b in c]
        if sheng(e[0], e[1]) and sheng(e[1], e[2]):
            g.append("递生（脱气/生气相续）")
        if ke(e[0], e[1]) and ke(e[1], e[2]):
            g.append("递克")
        kong_in = [BRANCHES[b] for b in c if b in self.kong]
        if kong_in:
            g.append("三传落空：" + "".join(kong_in))
        if c[0] in self.kong:
            g.append("空亡发用")
        if c[0] == YI_MA[SAN_HE[self.day_branch]]:
            g.append("驿马发用")
        if c[0] == GAN_MU[self.day_stem]:
            g.append("墓神发用")
        if c[0] == LU[self.day_stem]:
            g.append("禄神发用")
        if c[0] == self.guiren_branch:
            g.append("贵人发用")
        if self.ding_shen in c:
            g.append("丁马入传" if YI_MA[SAN_HE[self.day_branch]] in c else "丁神入传")
        self.geju = g

    # ---------------- 输出
    def branch_info(self, b, with_down=None):
        info = {
            "支": BRANCHES[b],
            "天将": self.jiang_of_branch.get(b),
            "遁干": STEMS[self.dungan[b]] if b in self.dungan else None,
            "六亲": liu_qin(STEM_ELEM[self.day_stem], BRANCH_ELEM[b]),
            "五行": ELEM_NAME[BRANCH_ELEM[b]],
            "旺衰": self.wangshuai.get(b),
            "旬空": b in self.kong,
        }
        if with_down is not None:
            info["下"] = with_down
        return info

    def to_dict(self):
        return {
            "输入": self.meta,
            "日干支": STEMS[self.day_stem] + BRANCHES[self.day_branch],
            "占时": BRANCHES[self.hour] + ("（报时/活时）" if self.bao_shi else "（正时）"),
            "月将": BRANCHES[self.general] + f"（{GENERAL_NAME[self.general]}）",
            "昼夜": "昼占" if self.is_day_time else "夜占",
            "盘式": self.pan_type,
            "天地盘": {BRANCHES[i]: BRANCHES[self.tianpan[i]] for i in range(12)},
            "四课": [
                {"课": k["name"], "上神": BRANCHES[k["up"]], "下神": k["down_label"],
                 "天将": self.jiang_of_branch.get(k["up"]),
                 "关系": ("下贼上" if k["xia_ze_shang"] else
                          ("上克下" if k["shang_ke_xia"] else "无克")),
                 "六亲": liu_qin(STEM_ELEM[self.day_stem], BRANCH_ELEM[k["up"]]),
                 "旺衰": self.wangshuai.get(k["up"]),
                 "旬空": k["up"] in self.kong}
                for k in self.ke
            ],
            "三传": [dict(self.branch_info(b), 位=n)
                     for b, n in zip(self.chuan, ["初传", "中传", "末传"])],
            "取用": self.method,
            "课体": self.ke_ti,
            "推演": self.method_detail,
            "格局": self.geju,
            "神煞": self.shensha,
            "日干旺衰": self.day_wangshuai,
            "贵人": BRANCHES[self.guiren_branch] + ("（顺行）" if self.jiang_forward else "（逆行）"),
            "年命": ({"本命": BRANCHES[self.benming],
                      "本命上神": BRANCHES[self.tianpan[self.benming]]} if self.benming is not None else None),
            "行年": ({"行年": BRANCHES[self.xingnian],
                      "行年上神": BRANCHES[self.tianpan[self.xingnian]]} if self.xingnian is not None else None),
        }

    def render(self):
        L = []
        m = self.meta
        if m.get("时间"):
            L.append("占时：%s%s" % (m["时间"], f"  ({m.get('说明')})" if m.get("说明") else ""))
        if m.get("年干支") or m.get("月干支"):
            L.append("年月：%s年 %s月" % (m.get("年干支", "-"), m.get("月干支", "-")))
        L.append("日干支：%s%s　　占时：%s%s　　月将：%s（%s）　　%s　　%s"
                 % (STEMS[self.day_stem], BRANCHES[self.day_branch], BRANCHES[self.hour],
                    "（报时）" if self.bao_shi else "",
                    BRANCHES[self.general], GENERAL_NAME[self.general],
                    "昼占" if self.is_day_time else "夜占", self.pan_type))
        L.append("")
        L.append("【天地盘】上行为天盘（含天将），下行为地盘")

        def cell(i):
            b = self.tianpan[i]
            return "%s%s" % (JIANG_SHORT[self.jiang_of_branch[b]], BRANCHES[b])

        rows = [[5, 6, 7, 8], [4, None, None, 9], [3, None, None, 10], [2, 1, 0, 11]]
        for r in rows:
            top, bot = [], []
            for i in r:
                if i is None:
                    top.append("    ")
                    bot.append("    ")
                else:
                    top.append(" %s " % cell(i))
                    bot.append("  %s " % BRANCHES[i])
            L.append("│".join(top))
            L.append("│".join(bot))
            L.append("─" * 27)
        L.pop()
        L.append("")
        L.append("【四课】（右起第一课）")
        cells = []
        for k in reversed(self.ke):
            mark = {"下贼上": "▲", "上克下": "▽"}.get(
                "下贼上" if k["xia_ze_shang"] else ("上克下" if k["shang_ke_xia"] else ""), "  ")
            cells.append("%s%s%s%s" % (JIANG_SHORT[self.jiang_of_branch[k["up"]]],
                                       BRANCHES[k["up"]], mark, ""))
        L.append("  ".join(cells))
        L.append("  ".join("  %s  " % k["down_label"] for k in reversed(self.ke)))
        L.append("  第四课  第三课  第二课  第一课　（▲下贼上 ▽上克下）")
        L.append("")
        L.append("【三传】%s → %s" % (self.method, self.ke_ti))
        for b, n in zip(self.chuan, ["初传", "中传", "末传"]):
            dg = STEMS[self.dungan[b]] if b in self.dungan else "－"
            ws = self.wangshuai.get(b)
            L.append("  %s：%s%s  %s  %s  %s%s%s"
                     % (n, dg, BRANCHES[b], self.jiang_of_branch[b],
                        liu_qin(STEM_ELEM[self.day_stem], BRANCH_ELEM[b]),
                        ELEM_NAME[BRANCH_ELEM[b]],
                        "·" + ws if ws else "",
                        "  ●旬空" if b in self.kong else ""))
        for d in self.method_detail:
            L.append("  · " + d)
        if self.geju:
            L.append("")
            L.append("【格局】" + "；".join(self.geju))
        L.append("")
        ss = self.shensha
        L.append("【神煞】旬首%s　旬空%s　驿马%s　日禄%s　羊刃%s　干墓%s　丁神%s　咸池%s"
                 % (ss["旬首"], "".join(ss["旬空"]), ss["驿马"], ss["日禄"],
                    ss["羊刃"], ss["干墓"], ss["丁神"], ss["咸池"]))
        L.append("　　　　贵人%s%s（%s）"
                 % (BRANCHES[self.guiren_branch], "" ,
                    "顺行" if self.jiang_forward else "逆行"))
        if self.day_wangshuai:
            w = self.day_wangshuai
            L.append("")
            L.append("【日干旺衰】%s%s（%s）　月令%s　→ %s"
                     % (STEMS[self.day_stem], w["日干五行"], w["天时"],
                        w["月令"], w["总评"]))
            L.append("　　天时：月令%s，日干%s气　地利：%s"
                     % (w["月令"], w["天时"], w["地利"]))
            L.append("　　人和：%s" % w["人和"])
        if self.benming is not None:
            L.append("【本命】%s，上神%s（%s）"
                     % (BRANCHES[self.benming], BRANCHES[self.tianpan[self.benming]],
                        self.jiang_of_branch[self.tianpan[self.benming]]))
        if self.xingnian is not None:
            L.append("【行年】%s，上神%s（%s）"
                     % (BRANCHES[self.xingnian], BRANCHES[self.tianpan[self.xingnian]],
                        self.jiang_of_branch[self.tianpan[self.xingnian]]))
        return "\n".join(L)


# ---------------------------------------------------------------- CLI

def parse_branch(s):
    if s is None:
        return None
    s = s.strip()
    if s in BRANCHES:
        return BRANCHES.index(s)
    raise SystemExit("地支无法识别：%s" % s)


def parse_gz(s):
    s = s.strip()
    if len(s) != 2 or s[0] not in STEMS or s[1] not in BRANCHES:
        raise SystemExit("干支无法识别：%s" % s)
    st, br = STEMS.index(s[0]), BRANCHES.index(s[1])
    for i in range(60):
        if i % 10 == st and i % 12 == br:
            return i
    raise SystemExit("非六十甲子组合：%s" % s)


def build_from_datetime(dt, tz_hours, night_advance=True, hour_override=None):
    dt_utc = dt - timedelta(hours=tz_hours)
    jd = julian_day(dt_utc)
    lam = sun_apparent_longitude(jd)
    general = month_general_from_longitude(lam)
    day_gz = ganzhi_of_day(dt, night_advance)
    hour = hour_override if hour_override is not None else hour_branch_of(dt)
    # 年月干支（按节气）
    mi = solar_month_index(lam)
    year_for_gz = dt.year if lam >= 315 or lam < 315 else dt.year
    # 立春前属上一年
    year_gz_year = dt.year if not (lam < 315 and lam >= 270) else dt.year
    if lam < 315 and lam >= 270:
        year_gz_year = dt.year - 1 if dt.month <= 2 else dt.year
    y_idx = (year_gz_year - 1984) % 60
    y_stem = y_idx % 10
    month_stem = (y_stem * 2 + 2 + mi) % 10
    month_branch = (2 + mi) % 12
    meta = {
        "时间": dt.strftime("%Y-%m-%d %H:%M") + " (UTC%+g)" % tz_hours,
        "太阳黄经": round(lam, 3),
        "年干支": STEMS[y_idx % 10] + BRANCHES[y_idx % 12],
        "月干支": STEMS[month_stem] + BRANCHES[month_branch],
        "说明": "月将依太阳过宫（中气）取；23时后日干支进位次日" if night_advance else "月将依太阳过宫取",
    }
    return day_gz, general, hour, meta, month_branch


def main():
    p = argparse.ArgumentParser(description="大六壬排盘")
    p.add_argument("--datetime", help='占时，格式 "YYYY-MM-DD HH:MM"')
    p.add_argument("--now", action="store_true", help="用当前系统时间")
    p.add_argument("--tz", type=float, default=8.0, help="时区，默认 +8")
    p.add_argument("--day-gz", help="直接指定日干支，如 丁未")
    p.add_argument("--general", help="直接指定月将地支，如 申")
    p.add_argument("--hour", help="直接指定占时地支，如 寅")
    p.add_argument("--baoshi", help="报时（活时）起课：日干支与月将不动，只以所报时辰为占时，如 --baoshi 申")
    p.add_argument("--month", help="月令地支（用于旺衰判断），如 申；按公历起课时自动推定")
    p.add_argument("--night", action="store_true", help="强制夜占（用夜贵）")
    p.add_argument("--day", action="store_true", help="强制昼占（用昼贵）")
    p.add_argument("--no-night-advance", action="store_true",
                   help="23时后不进位次日（用 0 时换日）")
    p.add_argument("--benming", help="本命地支（生年支）")
    p.add_argument("--xingnian", help="行年地支")
    p.add_argument("--json", action="store_true", help="输出 JSON")
    a = p.parse_args()

    meta = {}
    month_branch = parse_branch(a.month) if a.month else None
    bao_shi = a.baoshi is not None
    if a.day_gz and a.general and (a.hour or a.baoshi):
        day_gz = parse_gz(a.day_gz)
        general = parse_branch(a.general)
        hour = parse_branch(a.baoshi if a.baoshi else a.hour)
        meta = {"说明": "手工指定日干支/月将/占时"
                + ("；报时（活时）起课" if bao_shi else "")}
    else:
        if a.now:
            dt = datetime.now()
        elif a.datetime:
            s = a.datetime.strip()
            fmt = "%Y-%m-%d %H:%M" if len(s) > 10 else "%Y-%m-%d"
            dt = datetime.strptime(s, fmt)
        else:
            raise SystemExit("需提供 --now / --datetime，或同时提供 --day-gz --general --hour")
        day_gz, general, hour, meta, auto_mb = build_from_datetime(
            dt, a.tz, not a.no_night_advance, parse_branch(a.hour) if a.hour else None)
        if month_branch is None:
            month_branch = auto_mb
        if a.day_gz:
            day_gz = parse_gz(a.day_gz)
        if a.general:
            general = parse_branch(a.general)
        # 报时：日干支与月将不动，只换占时
        if bao_shi:
            hour = parse_branch(a.baoshi)
            meta["说明"] = (meta.get("说明", "") + "；报时（活时）起课，日干支与月将不变").lstrip("；")

    is_day = None
    if a.day:
        is_day = True
    if a.night:
        is_day = False

    k = LiuRen(day_gz, general, hour, is_day,
               parse_branch(a.benming), parse_branch(a.xingnian), meta,
               month_branch=month_branch, bao_shi=bao_shi)
    if a.json:
        print(json.dumps(k.to_dict(), ensure_ascii=False, indent=2))
    else:
        print(k.render())


if __name__ == "__main__":
    main()
