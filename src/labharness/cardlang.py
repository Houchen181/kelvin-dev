"""Approval cards in the reader's language, translated when they are shown (F3 #25: cards stayed English in a Chinese
chat). Simplified Chinese first; the language is each person's, under Settings → Display.

The gate, the forecast and the runtime write every card in English, and the record keeps it so; nothing here changes
what they write or what a person approves. A line is matched whole against the patterns written for it, else cut at
"; " into parts, each matched whole (a drive's move, a ramp, what the run leaves on...); a part with no pattern keeps
its English, whole (a half-turned sentence reads worse than either). Names, numbers, units and values are copied,
never translated: a line whose numbers, instrument keys or signs (→ < > |) don't all come through is shown in English,
so a translation can never change what a card says will happen."""
from __future__ import annotations

import re
from collections import Counter
from typing import Any, Dict, List, Optional, Pattern, Sequence, Tuple

Rule = Tuple[Pattern[str], str]
LANGUAGES = {"zh-Hans": "简体中文"}

_NUMBER = re.compile(r"\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")
_KEY = re.compile(r"\b[A-Za-z_][\w-]*\.[A-Za-z_]\w*\b")      # instrument.parameter
_SIGNS = "→<>≤≥=±|…"
_FILL = re.compile(r"\{([tl]?)(\d+)\}")


def _rules(pairs: Sequence[Tuple[str, str]]) -> List[Rule]:
    return [(re.compile(r"^(?:%s)$" % p, re.S), t) for p, t in pairs]


# ------------------------------------------------------------------ Simplified Chinese
_K = r"([A-Za-z_][\w-]*\.\w+)"   # an instrument's parameter, instr.param
# {1}: the group as written; {t1}: the group as a part of its own (translated); {l1}: a list ("a, b and c")
_ZH_TITLES = _rules([
    (r"Approve drive change on (\S+)\?", "批准 {1} 的驱动变更？"),
    (r"Change a setting on (\S+)\?", "更改 {1} 的设置？"),
    (r"Run plan (\S+): (.+)", "运行计划 {1}：{t2}"),
    (r"Run the queue: (.+)", "运行队列：{t1}"),
    (r"Send raw command\?", "发送原始命令？"),
    (r"Switch (\S+) on\?", "为 Kelvin 打开 {1}？"),
    (r"Try a card on (\S+)\?", "在 {1} 上试用仪器卡？"),
    (r"Install on this computer\?", "在这台电脑上安装？"),
    (r"Install on this computer for (.+)\?", "在这台电脑上为 {1} 安装？"),
    (r"Run a command on (\S+) over SSH\?", "通过 SSH 在 {1} 上运行命令？"),
    (r"Run code on the board at (\S+)\?", "在 {1} 的板卡上运行代码？"),
    (r"Remove an instrument environment\?", "删除一个仪器环境？"),
    (r"Add (\S+) to the lab\?", "将 {1} 加入实验室？"),
    (r"Let Claude use this computer's screen, mouse and keyboard\?", "让 Claude 使用这台电脑的屏幕、鼠标和键盘？"),
    (r"([A-Za-z_][\w-]*): (.+)\?", "{1}：{2}？"),   # an instrument's action, as its card names it
])

_ZH_LINES = _rules([
    (r"This approval covers every item now waiting; anything outside these ranges asks again\.",
     "此批准涵盖队列中现在等待的每一项；超出这些范围的操作会再次询问。"),
    (r"You switched it off in the Lab panel\. On, Kelvin connects to it again and reads it; output changes still ask "
     r"you as usual\.", "你在实验室面板里把它关掉了。打开后，Kelvin 会重新连接并读取它；输出变更仍会照常询问你。"),
])

_ZH_PARTS = _rules([
    # a run's or a queue's size and time (a card's title)
    (r"(\d+) points(.*)", "{1} 个点{t2}"),
    (r"(\d+) scans? and (\d+) steps?(.*)", "{1} 次扫描和 {2} 个步骤{t3}"),
    (r"(\d+) scans?(.*)", "{1} 次扫描{t2}"),
    (r"measures every (.+?) while (\S+) ramps to (.+) \((.+?)\)(.*)", "在 {2} 变化到 {t3} 期间每 {1} 测量一次（{t4}）{t5}"),
    (r"~(\d+) points", "约 {1} 个点"),
    (r"as many as it takes", "点数视需要而定"),
    (r", at least ~(.+)", "，至少约 {1}"),
    (r", ~(.+)", "，约 {1}"),
    (r", time not known", "，时间未知"),
    (r"", ""),
    # a drive's move, and how it moves
    (_K + r": (.+?) → (.+?)\. Its card is a draft no one has reviewed yet, so Kelvin can't be sure this only "
     r"configures the instrument",
     "{1}：{2} → {3}。它的仪器卡是没人审核过的草稿，所以 Kelvin 无法确定这只是在配置仪器"),
    (_K + r" (.+?) → (.+)", "{1}：{2} → {3}"),
    (_K + r": now (.+?), plan uses (.+?), toward its safe value", "{1}：现为 {2}，计划用到 {3}，朝向其安全值"),
    (_K + r": now (.+?), plan uses (.+?), inside the project envelope", "{1}：现为 {2}，计划用到 {3}，在项目允许范围内"),
    (_K + r": now (.+?), plan uses (.+)", "{1}：现为 {2}，计划用到 {3}"),
    (r"ramp (\S+) (\S+)/s \(~(.+)\)", "斜坡速率 {1} {2}/s（约 {3}）"),
    (r"ramp (\S+) (\S+)/s", "斜坡速率 {1} {2}/s"),
    (r"~(\d[\d.]* (?:s|min|h))", "约 {1}"),
    (r"at its own rate \(its card gives no rate Kelvin can read\)(.*)", "按仪器自身的速率（它的仪器卡没有给出 Kelvin 能读取的速率）{t1}"),
    (r"at its own rate \((\S+) reads (.+)\)", "按仪器自身的速率（{1} 读数为 {2}）"),
    (r"at (.+?) \((\S+), read now\)(.*)", "速率 {1}（{2}，当前读数）{t3}"),
    (r"at (.+?) \((.+) sets (\S+)\)(.*)", "速率 {1}（由{t2}设定 {3}）{t4}"),
    (r"the plan", "计划"),
    (r"the queue", "队列"),
    (r"stops if (.+)", "若 {t1} 则停止"),
    (r"(\|\S+\| > .+?) or (.+)", "{t1} 或 {t2}"),
    (r"(\|\S+\| > [^(]+?) \(now (.+?), past it\)", "{1}（当前 {2}，已超过）"),
    (r"(\|\S+\| > [^(]+?) \(now (.+?)\)", "{1}（当前 {2}）"),
    # a level whose output is off, and what a set point sets off (gate.trips)
    (_K + r" is (.+?), so " + _K + r" won't reach the sample", "{1} 为 {2}，所以 {3} 不会作用到样品上"),
    (_K + r": (at .+)", "{1}：{t2}"),
    (r"at (.+?), " + _K + r"'s guard \((.+)\) fails while " + _K + r" is (.+)",
     "在 {1} 时，{2} 的保护（{3}）不再满足，而 {4} 为 {t5}"),
    (r"away from safe in the run", "运行中偏离安全值"),
    (r"at (.+?), the lab watch on " + _K + r" \(above (.+?)\) (.+)", "在 {1} 时，实验室对 {2} 的监视（高于 {3}）会{t4}"),
    (r"at (.+?), the lab watch on " + _K + r" \(below (.+?)\) (.+)", "在 {1} 时，实验室对 {2} 的监视（低于 {3}）会{t4}"),
    (r"raises an alarm", "报警"),
    (r"sets (.+) and stops (the run|this change)", "设 {l1}，并停止{t2}"),
    (r"sets (.+)", "设 {l1}"),
    (r"stops (the run|this change)", "停止{t1}"),
    (r"the run", "运行"),
    (r"this change", "这次变更"),
    (r"interlock: needs (.+) \(now (.+)\)", "联锁：需要 {1}（当前 {2}）"),
    (r"its interlock needs (.+), which doesn't hold now(.*): an item before it works on that, and it is checked "
     r"again before (\S+) moves \(if it still doesn't hold, the queue pauses there\)",
     "它的联锁需要 {1}，目前不满足{2}：前面的条目会处理这一点，{3} 变化前会再检查一次（若仍不满足，队列会在那里暂停）"),
    (r"it applies (.+)", "它会施加 {l1}"),
    (r"(\S+) at (.+) \(its safe value, set first\)", "{1} 为 {2}（其安全值，先设好）"),
    (r"(\S+) at (.+) \(as it is now\)", "{1} 为 {2}（保持现在的值）"),
    # who may approve, and why Kelvin asks
    (r"Kelvin isn't sure about (\S+): (.+), so even a change toward its safe value \((.+)\) asks",
     "Kelvin 对 {1} 没有把握：{t2}，所以即使朝其安全值（{3}）的变更也要询问"),
    (r"Kelvin isn't sure about (\S+): (.+)", "Kelvin 对 {1} 没有把握：{t2}"),
    (r"(.+), so even a change toward its safe value \((.+)\) asks", "{t1}，所以即使朝其安全值（{2}）的变更也要询问"),
    (r"its card is a draft that nobody has checked against the instrument", "它的仪器卡是草稿，还没有人对照仪器检查过"),
    (r"Kelvin added it to the lab itself, and its limits are the maker's: nobody has reviewed them for this setup",
     "它是 Kelvin 自己加入实验室的，限值是厂家的：还没有人针对这套装置审核过"),
    (r"Auto mode does not cover this: (.+)", "自动模式不涵盖此项：{t1}"),
    (r"it sets off what is listed above", "它会触发上面列出的保护或监视"),
    (r"Not approved by Kelvin itself: (.+)", "Kelvin 不会自行批准：{t1}"),
    # what the run leaves, and what its time doesn't count
    (r"At the end it leaves (.+)", "结束时保持：{l1}"),
    (r"back at safe: (.+)", "回到安全值：{l1}"),
    (r"At the end everything it changed is back at safe: (.+)", "结束时，改动过的都回到安全值：{l1}"),
    (r"At the end it runs (\S+) \((.+)\)", "结束时运行 {1}（{2}）"),
    (r"then it runs (\S+) \((.+)\)", "然后运行 {1}（{2}）"),
    (r"Not in the ~(.+?): (.+)", "约 {1} 中未计入：{t2}"),
    (r"Not in its time: (.+)", "时长中未计入：{t1}"),
    (r"(\S+)'s ramp \((.+)\)", "{1} 的斜坡（{t2}）"),
    (r"its card gives no rate Kelvin can read", "它的仪器卡没有给出 Kelvin 能读取的速率"),
    (r"the time reading (.+) takes", "读取 {l1} 所需的时间"),
    (r"(\S+) is (.+?), and (the plan|the queue) doesn't switch it on: (.+) won't reach the sample",
     "{1} 为 {2}，而{t3}不会打开它：{l4} 不会作用到样品上"),
    (r"At the start: (\S+) \((.+)\), which drives the sample", "开始时：{1}（{2}），它会驱动样品"),
    (r"At the end: (\S+) \((.+)\), which drives the sample", "结束时：{1}（{2}），它会驱动样品"),
    (r"At every point, after settling: (.+), which drive the sample", "每个点稳定之后：{l1}，它们会驱动样品"),
    (r"(\S+) sends (.+), which drives the sample", "{1} 发送 {2}，这会驱动样品"),
    # raw commands, switching on, adding an instrument
    (r"(\S+): ('.*') is not on the \(draft\) card, so it can't be classified", "{1}：{2} 不在（草稿）仪器卡上，无法判断它的作用"),
    (r"(\S+): ('.*') is not on its card, so Kelvin can't tell what it does", "{1}：{2} 不在它的仪器卡上，Kelvin 无法判断它的作用"),
    (r"(\S+) has drives that reach the sample \((.+)\)", "{1} 有会作用到样品的驱动（{2}）"),
    (r"Kelvin needs it: (.+)", "Kelvin 需要它：{1}"),
    (r"Every output change on it still asks you until you review its limits\.", "在你审核它的限值之前，它的每次输出变更仍会询问你。"),
    # the calibration on record (profiles.py)
    (r"Calibration out of date: (\S+) was due (\S+) \(certificate (.+)\)", "校准已过期：{1} 应于 {2} 前校准（证书 {3}）"),
    (r"Calibration out of date: (\S+) was due (\S+)", "校准已过期：{1} 应于 {2} 前校准"),
    (r"Calibration on record is for serial (.+); (\S+) reports serial (.+)", "记录中的校准针对序列号 {1}；{2} 报告的序列号为 {3}"),
    # a ramp's legs ("2 K at 10 K/min, then 300 K")
    (r"([-+\d.]\S* \S+(?: at [-+\d.]\S* \S+)?), then (.+)", "{t1}，然后 {t2}"),
    (r"([-+\d.]\S* \S+) at ([-+\d.]\S* \S+)", "{1}（速率 {2}）"),
])

_ZH_WHERE = {"SIMULATOR": "模拟器", "REAL HARDWARE": "真实硬件"}

LANGS: Dict[str, Dict[str, Any]] = {
    "zh-Hans": {"titles": _ZH_TITLES, "lines": _ZH_LINES, "parts": _ZH_PARTS, "where": _ZH_WHERE, "sep": "；",
                "list": ("、", " 和 ")},
}


# ------------------------------------------------------------------ translating

def _kept(english: str, shown: str) -> bool:
    """Does the translation say every number, instrument key and sign the English says (as many times)?"""
    if Counter(_NUMBER.findall(shown)) & Counter(_NUMBER.findall(english)) != Counter(_NUMBER.findall(english)):
        return False
    if any(k not in shown for k in _KEY.findall(english)):
        return False
    return all(shown.count(c) >= english.count(c) for c in _SIGNS)


def _part(text: str, lang: Dict[str, Any], depth: int = 0) -> str:
    """One part of a line, matched whole; else its English, as it is."""
    if depth < 6:
        for pattern, template in lang["parts"]:
            m = pattern.match(text)
            if m:
                return _fill(template, m, lang, depth + 1)
    return text


def _items(text: str) -> List[str]:
    """ "a, b (c, d) and e" as its items, a, b (c, d) and e: never cut inside brackets."""
    out, depth, start, i = [], 0, 0, 0
    while i < len(text):
        c = text[i]
        depth += c in "([" and 1 or (-1 if c in ")]" and depth else 0)
        sep = 2 if text.startswith(", ", i) else 5 if text.startswith(" and ", i) else 0
        if sep and not depth:
            out.append(text[start:i])
            start = i = i + sep
            continue
        i += 1
    return [s for s in out + [text[start:]] if s]


def _list(text: str, lang: Dict[str, Any], depth: int) -> str:
    """ "a, b and c", each item a part: "a、b 和 c"."""
    items = _items(text)
    comma, last = lang["list"]
    shown = [_part(i, lang, depth) for i in items]
    return comma.join(shown[:-1]) + last + shown[-1] if len(shown) > 1 else (shown[0] if shown else "")


def _fill(template: str, m: "re.Match[str]", lang: Dict[str, Any], depth: int) -> str:
    def one(f: "re.Match[str]") -> str:
        group = m.group(int(f.group(2))) or ""
        if f.group(1) == "t":
            return _part(group, lang, depth)
        if f.group(1) == "l":
            return _list(group, lang, depth)
        return group
    return _FILL.sub(one, template)


def line(text: str, language: str, title: bool = False) -> str:
    """One line of a card (or its title) in that language: the English when it has no translation, or when the
    translation would lose a number, an instrument key or a sign."""
    lang = LANGS.get(language)
    text = str(text)
    if lang is None or not text.strip():
        return text
    shown = None
    for pattern, template in (lang["titles"] if title else []) + lang["lines"]:
        m = pattern.match(text)
        if m:
            shown = _fill(template, m, lang, 1)
            break
    if shown is None:
        shown = lang["sep"].join(_part(p, lang) for p in text.split("; "))
    return shown if _kept(text, shown) else text


def card(title: str, items: Sequence[Any], where: Optional[str], language: str) -> Dict[str, Any]:
    """A card as a person reads it in their language: title, lines, where it acts (SIMULATOR or REAL HARDWARE), and
    how many lines stay in English. An unknown language: the card as written."""
    lang = LANGS.get(language)
    if lang is None:
        return {"title": title, "items": [str(i) for i in items], "where": where, "language": "", "english": 0}
    shown_title = line(title, language, title=True)
    shown_items = [line(str(i), language) for i in items]
    english = sum(1 for a, b in zip([title] + [str(i) for i in items], [shown_title] + shown_items) if a == b)
    return {"title": shown_title, "items": shown_items, "where": lang["where"].get(where or "", where),
            "language": language, "english": english}


def shown(request: Dict[str, Any], language: str) -> Dict[str, Any]:
    """A pending or answered request as the portal and phones get it, with its translation beside the English
    ("shown") when the person reads cards in another language."""
    if language not in LANGS or not isinstance(request, dict):
        return request
    return dict(request, shown=card(str(request.get("title") or ""), request.get("items") or [],
                                    request.get("where"), language))
