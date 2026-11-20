"""收件人匹配：**说「给 AIAA3102 的老师发」「发到私人邮箱」，自己找到那个地址**。

为什么需要它
    Outlook 里写收件人时有个「最近联系人 / 通讯录」下拉，我们这边没有 ——
    让模型直接编一个地址是最坏的结果（信会发给陌生人，而且**发出去收不回来**）。
    所以把「你认识谁」这件事做成数据：从**你自己的邮件索引**里汇总出联系人
    （谁给你发过、你发给过谁），再用可解释的打分把一句话映射到具体地址。

三条不变量
    1. **只从你自己的邮件里推导**，不外发、不查在线通讯录；
    2. 匹配不到或分不清 → **不猜**，把候选列出来让人确认；
    3. 地址里的 ``@`` 是唯一入口判据：没有 ``@`` 的串一律按「名字/机构」去解析。

打分（可解释，自测里钉住）
    - 配置里的别名（``[mail.contacts]`` 私人邮箱 = xxx@163.com）        +120
    - 地址 / 名字完全一致                                               +100
    - 名字包含关系（「王老师」↔「王伟」）                                +70
    - 课程代码 / 机构词命中（AIAA3102、教务处、ITDID…）                  +45
    - 模糊相似度（difflib）                                             +0~40
    - 往来次数（log 缩放）                                              +0~15
    分数差距太小时算「分不清」（``min_gap``），宁可问一句。
"""

from __future__ import annotations

import difflib
import json
import logging
import re
from typing import Any

#: 分数低于这个就不算候选
MIN_SCORE = 45
#: 第一二名差这么多才算「明确」，否则要人确认。
#: 10 是实测出来的：真实邮箱里 itdid@(70) vs gzit@(57) 差 13，差 15 会一直在问；
#: 而真正分不清的情况（分数相同）差距是 0，10 也拦得住。
MIN_GAP = 10
#: 从主题里捞课程代码（AIAA3102 / UFUG 2602）
_COURSE = re.compile(r"\b([A-Z]{4})\s?(\d{4})\b")
#: 机构/职务类词：命中就算「这个地址和这种机构有关」
_ORG_WORDS = (
    "教务", "注册", "学籍", "选课", "考试", "财务", "缴费", "住宿", "宿管", "图书馆",
    "信息中心", "itdid", "it部门", "网络安全", "就业", "职业", "招聘", "国际", "签证",
    "医务", "校医", "后勤", "资产", "采购", "科研", "基金", "实验", "实验室", "学院",
    "办公室", "秘书", "院长", "系主任", "导师", "老师", "助教", "ta", "admission",
    "registrar", "finance", "library", "career", "hr", "helpdesk", "support",
)


#: ★这些地址永远不自动选★：系统通知 / 机器人 / 群发名单。
#: 真机踩过：说「给 AIAA3102 的老师发邮件」，Canvas 的通知机器人因为
#: 「发件人名字写的是课程名」被选中了 —— 发给它等于石沉大海。
_NO_REPLY = re.compile(
    r"no-?reply|do-?not-?reply|noreply|notifications?@|mailer-daemon|postmaster"
    r"|automated|system@|\bbot\b",
    re.IGNORECASE,
)
_MAILING_LIST = re.compile(
    r"^(all[-._]|everyone|everybody|students?@|staff@|faculty@|alumni@|news-?list)"
    r"|[-._](students|staff|faculty|all|list|group)s?@|@{1}.*(listserv|googlegroups)",
    re.IGNORECASE,
)

#: 中文机构词 → 英文线索（教务处 = registrar / academic …，靠这个跨语言对上）
_ORG_SYNONYMS: dict[str, tuple[str, ...]] = {
    "教务": ("registrar", "academic", "registry", "ar@", "jwc"),
    "注册": ("registrar", "registry", "enrol"),
    "学籍": ("registrar", "records"),
    "选课": ("registrar", "course", "cis"),
    "考试": ("exam", "assessment", "testing"),
    "财务": ("finance", "fees", "billing", "payment", "cashier"),
    "缴费": ("fees", "billing", "payment"),
    "住宿": ("housing", "dorm", "residence"),
    "图书馆": ("library", "lib@"),
    "信息中心": ("itdid", "it@", "helpdesk", "itservice"),
    "网络安全": ("cyber", "security", "itdid"),
    "就业": ("career", "careers", "careercenter", "job", "recruit", "employment"),
    "职业": ("career", "careers", "job"),
    "招聘": ("career", "recruit", "hr", "jobs"),
    "国际": ("international", "global", "visa", "abroad"),
    "签证": ("visa", "international", "immigration"),
    "医务": ("health", "clinic", "medical", "doctor"),
    "后勤": ("logistics", "facilities", "support"),
    "资产": ("asset", "procurement", "purchasing"),
    "采购": ("procurement", "purchasing", "tender"),
    "科研": ("research", "grant", "funding", "rd"),
    "基金": ("grant", "funding", "nsfc"),
    "实验室": ("lab", "laboratory"),
    "学院": ("college", "school", "faculty"),
    "办公室": ("office", "admin", "secretary"),
    "院长": ("dean", "office"),
    "导师": ("supervisor", "advisor", "professor"),
    "助教": ("ta", "tutor", "assistant"),
    "老师": ("professor", "instructor", "teacher", "faculty"),
    "it部门": ("itdid", "it@", "helpdesk", "itservice", "itsupport"),
    "it": ("itdid", "it@", "helpdesk", "itservice", "itsupport"),
    "信息": ("itdid", "it@", "helpdesk", "itservice"),
    "网络": ("itdid", "network", "net@", "security"),
    "宿管": ("housing", "dorm", "residence", "accommodation"),
    "宿舍": ("housing", "dorm", "residence"),
    "保卫": ("security", "safety", "guard", "campus"),
    "安保": ("security", "safety", "guard"),
    "校车": ("shuttle", "bus", "transport", "traffic"),
    "交通": ("shuttle", "transport", "traffic"),
    "快递": ("mail", "express", "post", "courier"),
    "招生": ("admission", "admissions", "enrolment", "recruit"),
    "校友": ("alumni", "alumnus"),
    "档案": ("records", "archive", "registry"),
    "学生事务": ("student", "studentaffairs", "osa", " affairs"),
    "团委": ("youth", "league", "studentunion", "su@"),
    "体育": ("sport", "sports", "pe@", "athletic", "gym"),
    "体育馆": ("sport", "sports", "gym", "athletic"),
    "医院": ("health", "clinic", "medical", "hospital"),
    "心理": ("counsel", "psycholog", "wellbeing"),
}


def _norm(text: str) -> str:
    return re.sub(r"\s+", "", str(text or "")).lower()


def _strip_request(text: str) -> str:
    """「给/发给/写封邮件给 某某 发一封邮件」→ 「某某」。"""
    out = str(text or "").strip()
    out = re.sub(r"^(请|帮我|麻烦你?|给我)?", "", out)
    out = re.sub(r"^(给|向|和|跟|发给|发送给|写信给|写封?邮件给)", "", out)
    out = re.sub(r"(发一?封?邮件|写一?封?邮件|发过去|联系一下?|发消息)$", "", out)
    out = re.sub(r"(老师|教授|同学|先生|女士)$", "", out)
    return out.strip(" ，,。.的")


class Contacts:
    """联系人索引（从邮件索引现算，不单独存一份表）。"""

    def __init__(
        self,
        store: Any,
        *,
        aliases: dict[str, str] | None = None,
        self_addresses: set[str] | None = None,
        log: logging.Logger | None = None,
    ) -> None:
        self.store = store
        self.aliases = {_norm(k): str(v) for k, v in (aliases or {}).items() if k and v}
        self.self_addresses = {str(a).lower() for a in (self_addresses or set()) if a}
        self.log = log or logging.getLogger("voice_loop.mail")
        self._cache: list[dict] | None = None

    # ---------------------------------------------------------------- 建索引
    def build(self, *, refresh: bool = False) -> list[dict]:
        """汇总「谁给我发过 / 我发给过谁」。结果按往来次数降序。"""
        if self._cache is not None and not refresh:
            return self._cache
        rows: dict[str, dict] = {}
        for item in self._iter_people():
            address = str(item.get("address") or "").strip().lower()
            if not address or "@" not in address:
                continue
            entry = rows.setdefault(address, {
                "address": address, "name": "", "count": 0,
                "last_seen": "", "domains": set(), "tags": set(),
            })
            name = str(item.get("name") or "").strip()
            if name and not entry["name"]:
                entry["name"] = name
            entry["count"] += int(item.get("n") or 1)
            seen = str(item.get("last_seen") or "")
            if seen > entry["last_seen"]:
                entry["last_seen"] = seen
            domain = address.rsplit("@", 1)[-1]
            entry["domains"].add(domain)
            for tag in self._tags_of(str(item.get("subject") or "")):
                entry["tags"].add(tag)
        out = []
        for entry in rows.values():
            entry["is_self"] = entry["address"] in self.self_addresses
            out.append(entry)
        out.sort(key=lambda x: (-int(x["count"]), x["address"]))
        self._cache = out
        return out

    def _iter_people(self) -> list[dict]:
        """发件人 + 收件人 + 抄送，各算一次往来。"""
        store = self.store
        sql = """
        SELECT address, name, COUNT(*) AS n, MAX(received_at) AS last_seen,
               GROUP_CONCAT(COALESCE(subject, ''), ' ') AS subject
        FROM (
            SELECT sender AS address, sender_name AS name, received_at, subject FROM messages
            UNION ALL
            SELECT json_extract(j.value, '$.address') AS address,
                   json_extract(j.value, '$.name') AS name, received_at, subject
            FROM messages, json_each(messages.to_json) AS j
            UNION ALL
            SELECT json_extract(j.value, '$.address') AS address,
                   json_extract(j.value, '$.name') AS name, received_at, subject
            FROM messages, json_each(messages.cc_json) AS j
        )
        WHERE address IS NOT NULL AND address <> ''
        GROUP BY address
        """
        try:
            return [dict(r) for r in store._query(sql)]        # noqa: SLF001 - 同包内部用
        except Exception as exc:  # noqa: BLE001 - 老 sqlite 没有 json1：退回 Python 扫描
            self.log.info(f"[mail] 联系人汇总走 Python 回退（{exc}）")
            return self._iter_people_fallback()

    def _iter_people_fallback(self) -> list[dict]:
        out = []
        for row in self.store.search("", limit=5000):
            subject = str(row.get("subject") or "")
            if row.get("sender"):
                out.append({"address": row["sender"], "name": row.get("sender_name"),
                            "n": 1, "last_seen": row.get("received_at"), "subject": subject})
            for key in ("to", "cc"):
                for item in row.get(key) or []:
                    address = item.get("address") if isinstance(item, dict) else str(item)
                    name = item.get("name") if isinstance(item, dict) else ""
                    if address:
                        out.append({"address": address, "name": name, "n": 1,
                                    "last_seen": row.get("received_at"), "subject": subject})
        return out

    @staticmethod
    def _tags_of(subject: str) -> set[str]:
        tags = set()
        for match in _COURSE.finditer(subject or ""):
            tags.add(f"{match.group(1)}{match.group(2)}".lower())
            tags.add(f"{match.group(1)} {match.group(2)}".lower())
        low = (subject or "").lower()
        for word in _ORG_WORDS:
            if word in low:
                tags.add(word)
        return tags

    # ---------------------------------------------------------------- 解析
    def resolve(self, query: str, *, limit: int = 5) -> dict[str, Any]:
        """一句话 → ``{ok, address, name, score, candidates, reason}``。

        ``ok=True`` 只在**有明确第一候选**时给；分不清就 ``ok=False`` 并把候选列出来。
        """
        raw = str(query or "").strip()
        if not raw:
            return {"ok": False, "reason": "没说要发给谁", "candidates": []}
        if "@" in raw:                       # 已经是地址：只要在联系人或自己人里就认
            address = raw.strip().strip("<>").lower()
            known = {c["address"]: c for c in self.build()}
            if address in known:
                return {"ok": True, "address": address, "name": known[address]["name"],
                        "score": 100, "reason": "地址和你邮件往来里的人对上了",
                        "candidates": [known[address]]}
            return {"ok": True, "address": address, "name": "", "score": 100,
                    "reason": "用户直接给了地址", "candidates": []}

        needle = _strip_request(raw)
        key = _norm(needle)
        # ① 配置别名（「私人邮箱」这类）
        for alias, address in self.aliases.items():
            if alias and (alias == key or alias in key):
                return {"ok": True, "address": address, "name": alias, "score": 120,
                        "reason": f"配置里的别名「{alias}」", "candidates": []}

        # ★默认排除你自己★：不然「给 AIAA3102 的老师发」会把自己也匹配上
        # （那门课的信你也收到了，于是你也有同一个课程标签），永远分不清。
        allow_self = any(w in key for w in ("我自己", "自己", "我本人", "本人", "给自己"))
        scored: list[dict] = []
        for entry in self.build():
            if entry.get("is_self") and not allow_self:
                continue
            score, why = self._score(entry, needle, key)
            if score >= MIN_SCORE:
                scored.append({**self._public(entry), "score": score, "why": why})
        scored.sort(key=lambda x: -x["score"])
        if not scored:
            # 兜底：问的是机构/职务（教务处、就业中心…）但通讯录里没有明确对上的人，
            # 就把「学校域里的活跃联系人」列出来让模型去问 —— 比一句「找不到」有用得多
            org_query = any(w in key for w in _ORG_WORDS)
            fallback = []
            if org_query:
                fallback = [
                    {**self._public(e), "score": 0, "why": "学校里的活跃地址"}
                    for e in self.build()
                    if not e.get("is_self") and not _NO_REPLY.search(e["address"])
                    and not _MAILING_LIST.search(e["address"])
                    and e["address"].endswith(self._school_domain())
                ][:limit]
            return {"ok": False,
                    "reason": (f"通讯录里没有和「{needle}」对得上的人"
                               + ("（下面是学校里的地址，问用户是哪个）" if fallback else "")),
                    "candidates": fallback}
        top = scored[0]
        gap = top["score"] - (scored[1]["score"] if len(scored) > 1 else 0)
        if gap < MIN_GAP and len(scored) > 1:
            return {"ok": False, "address": "", "score": top["score"],
                    "reason": "有几个人都像，得让用户选",
                    "candidates": scored[:limit]}
        return {"ok": True, "address": top["address"], "name": top["name"],
                "score": top["score"], "reason": top["why"],
                "candidates": scored[:limit]}

    def _score(self, entry: dict, needle: str, key: str) -> tuple[int, str]:
        name = _norm(entry.get("name"))
        address = entry["address"]
        # 机器人/群发名单：只在「用户原话就是这个地址」时才允许（那种情况走 resolve 的 @ 分支）
        if _NO_REPLY.search(address):
            return -999, "系统/机器人地址，不能发"
        if _MAILING_LIST.search(address):
            return -999, "群发名单，不是具体的人"
        local = address.split("@", 1)[0]
        tags = {str(t).lower() for t in entry.get("tags") or ()}
        score = 0
        why: list[str] = []

        if key and (key == name or key == local or key == address):
            score += 100
            why.append("名字/地址完全一致")
        elif key and name and (key in name or name in key):
            score += 70
            why.append(f"名字里含「{needle}」")
        elif key and local and (key in local or local in key):
            score += 60
            why.append("地址里含这几个字")

        # 课程代码 / 机构词：从「这封邮件是关于 AIAA3102 的」推「发信人跟这门课有关」
        hit_tags = {t for t in tags if key and (t in key or key in t)}
        if hit_tags:
            score += 45
            why.append("往来邮件里提到 " + "、".join(sorted(hit_tags)[:2]))
        else:
            # 机构词跨语言对：说「就业中心」，`careercenter@` 也该中
            synonyms: set[str] = set()
            for word, english in _ORG_SYNONYMS.items():
                if key and word in key:
                    synonyms.update(english)
            haystack = f"{name} {address}"
            # 地址/名字里就有这个词 → 强信号；只在「往来邮件的主题里出现过」→ 弱信号
            in_addr = {s for s in synonyms if s and s in haystack}
            in_tags = {s for s in synonyms if s and any(s in t for t in tags)}
            if in_addr:
                score += 55
                why.append("机构对上了（" + "、".join(sorted(in_addr)[:2]) + "）")
            elif in_tags:
                score += 30
                why.append("往来邮件里提到过这类机构")

        if not score:
            ratio = max(difflib.SequenceMatcher(None, key, name or local).ratio() * 40
                        if (key and (name or local)) else 0, 0)
            if ratio >= 20:
                score += int(ratio)
                why.append("名字相似")
        if score:
            score += min(15, int(entry["count"]) * 2)
        return score, "、".join(why) or "只算了往来次数"

    def _school_domain(self) -> str:
        """最常见的那个域名（= 你学校的域名），兜底候选按它过滤。"""
        counts: dict[str, int] = {}
        for entry in self.build():
            if entry.get("is_self"):
                continue
            for domain in entry.get("domains") or ():
                counts[domain] = counts.get(domain, 0) + int(entry["count"])
        return max(counts, key=lambda d: counts[d]) if counts else ""

    @staticmethod
    def _public(entry: dict) -> dict:
        return {
            "address": entry["address"],
            "name": entry.get("name") or "",
            "count": int(entry.get("count") or 0),
            "last_seen": entry.get("last_seen") or "",
        }
