"""天气查询（Open-Meteo，免 API key）。

为什么用它（参考成熟 harness agent 的做法）
    - 成熟实现里 ``get_weather`` 这类工具通常是「地名 → 经纬度 → 预报」两跳，
      数据源选免 key 的公共 API。Open-Meteo 正是这种：Geocoding 与 Forecast
      两个公开接口、不用注册、CC-BY 署名即可，条款也允许家用频率的调用。
    - ★离线优先仍然是底线★：没网就是「现在查不到天气」，绝不编一个温度出来。

分三层（和 asr/tts 一样，外层谁用都行）
    1. 解析：``parse_scope`` / ``parse_place`` —— 把「明天上海要带伞吗」拆成
       「地点 = 上海」「范围 = 明天」；纯字符串、纯确定性，可单测。
    2. 取数：``geocode`` / ``forecast`` —— 两个 HTTP 跳，参数与返回都原样保留。
    3. 说话：``describe`` —— 把数值翻成能念的中文（``18 度``，不是 ``18°C``）。

调用方（技能层 / 工具层 / MCP harness）只碰 :class:`WeatherService`：

    svc = WeatherService(settings.weather, logger)
    ans = svc.ask("明天要带伞吗")      # -> WeatherAnswer(ok, reply, data)
"""

from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
# 只有显式打开 auto_ip 才会用到（要不要让第三方看到你的 IP，由你决定）
IP_URL = "https://ipapi.co/json/"

# WMO 天气代码 → 中文（念出来要顺口，所以不用「间歇性中雨」这类术语）
WMO: dict[int, str] = {
    0: "晴", 1: "晴间少云", 2: "多云", 3: "阴",
    45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "雷暴伴冰雹",
}

_RAIN_CODES = frozenset({51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99})
_SNOW_CODES = frozenset({71, 73, 75, 77, 85, 86})

# 「几号之前」这类范围词 → 要取几天
_SCOPE_RULES: tuple[tuple[re.Pattern[str], int, str], ...] = (
    (re.compile(r"(未来|接下来|近)\s*[七7]\s*天|一周(的)?天气|这周|本周|下周"), 7, "未来一周"),
    (re.compile(r"(未来|接下来|近)\s*[三3]\s*天|三天(的)?天气|这几天(的)?天气"), 3, "未来三天"),
    (re.compile(r"大后天"), 4, "未来四天"),
    (re.compile(r"后天"), 3, "未来三天"),
    (re.compile(r"明天|明日|明儿|明早|明晚"), 2, "今明两天"),
    (re.compile(r"今天|今日|今儿|今晚|今早|现在|当前|此刻"), 1, "今天"),
)

# 要当「范围/提问」剥掉的词（剩下的才可能是地名）
_STRIP = re.compile(
    r"请问|麻烦|帮我|给我|告诉我|查一下|查查|查个|看一下|看看|瞧瞧|问一下|问下"
    r"|的天气|天气怎么样|天气如何|天气预报|天气|气温|温度|多少度|几度|几多度"
    r"|下不下雨|下不下雪|会不会下雨|会不会下雪|下雨|下雪|雪|雨|风大不大|刮风|大风"
    r"|冷不冷|热不热|穿什么|带伞|要带伞吗|雨伞|紫外线|空气质量|湿度|阴晴|晴|阴|多云"
    r"|未来|接下来|近|[零一二三四五六七八九十百\d]+天|一周|这周|本周|下周"
    r"|今天|今日|今儿|今晚|今早|明天|明日|明儿|明早|明晚|后天|大后天|昨天|前天"
    r"|现在|当前|此刻|白天|晚上|夜里|上午|下午|中午|早上"
    r"|那边|那里|这儿|这里|当地|市区|天气呢|怎么样|如何|怎样|是多少|是什么"
    r"|吗|呢|啊|呀|吧|哦|的|了|会|要|需不需要|需|用不用|能|有|没有|是|在|去|到|和"
)


@dataclass
class Place:
    """一个地点（经纬度是 Open-Meteo 要的形状）。"""

    name: str
    latitude: float
    longitude: float
    admin: str = ""          # 省 / 州
    country: str = ""

    def label(self) -> str:
        """念给人听的名字：``上海`` / ``朝阳，北京``。"""
        admin = self.admin
        if admin and admin not in self.name and self.name not in admin:
            return f"{self.name}，{admin}"
        return self.name

    def as_dict(self) -> dict:
        return {"name": self.name, "latitude": self.latitude,
                "longitude": self.longitude, "admin": self.admin,
                "country": self.country}


@dataclass
class Scope:
    """要查几天 + 用户真正问的是哪一天（``focus=None`` = 整段都要）。"""

    days: int = 1
    focus: int | None = 0     # 0 = 今天，1 = 明天…
    label: str = "今天"


@dataclass
class WeatherAnswer:
    ok: bool
    reply: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""


# --------------------------------------------------------------------------- #
# 解析（纯函数，可单测）
# --------------------------------------------------------------------------- #
def parse_scope(text: str) -> Scope:
    """句子问的是哪天。默认「今天」。"""
    raw = str(text or "")
    for pattern, days, label in _SCOPE_RULES:
        hit = pattern.search(raw)
        if hit:
            focus = None
            word = hit.group(0)
            if days == 1:
                focus = 0
            elif word in ("明天", "明日", "明儿", "明早", "明晚"):
                focus = 1
            elif word == "后天":
                focus = 2
            elif word == "大后天":
                focus = 3
            return Scope(days=days, focus=focus, label=label)
    return Scope()


def parse_place(text: str) -> str:
    """从原话里抠出地名（抠不出来就返回空串，由配置的默认城市兜底）。"""
    raw = str(text or "")
    left = _STRIP.sub(" ", raw)
    # 只留中文/拉丁字母/空格，去掉标点
    left = re.sub(r"[^\w\u4e00-\u9fff ]", " ", left)
    left = re.sub(r"\s+", " ", left).strip()
    if not left:
        return ""
    # 「上海北京」这种粘在一起的，留给 geocoding 去猜第一个命中项
    return left[:34]


# --------------------------------------------------------------------------- #
# 取数
# --------------------------------------------------------------------------- #
def _get(url: str, params: dict, timeout: float) -> dict:
    """一次 GET（★单测会替换这个函数，所以别在别处直接 requests）★。"""
    import requests  # noqa: PLC0415 - 只有真要联网时才 import

    resp = requests.get(url, params=params, timeout=timeout,
                        headers={"User-Agent": "local_ai-weather/1.0"})
    resp.raise_for_status()
    return dict(resp.json() or {})


def geocode(name: str, *, timeout: float = 8.0, language: str = "zh",
            count: int = 1) -> Place | None:
    """地名 → 经纬度。查不到返回 None（不抛，交给调用方说人话）。"""
    query = str(name or "").strip()
    if not query:
        return None
    raw = _get(GEOCODE_URL, {"name": query, "count": max(1, min(10, count)),
                            "language": language, "format": "json"}, timeout)
    results = list(raw.get("results") or [])
    if not results:
        return None
    top = results[0]
    try:
        lat = float(top["latitude"])
        lon = float(top["longitude"])
    except (KeyError, TypeError, ValueError):
        return None
    return Place(
        name=str(top.get("name") or query),
        latitude=lat, longitude=lon,
        admin=str(top.get("admin1") or ""),
        country=str(top.get("country") or ""),
    )


def forecast(place: Place, days: int = 1, *, timeout: float = 8.0,
             unit: str = "c") -> dict:
    """取当前实况 + 逐日预报（原样返回，格式化交给 :func:`describe`）。"""
    params = {
        "latitude": place.latitude,
        "longitude": place.longitude,
        "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                   "precipitation,weather_code,wind_speed_10m,is_day",
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,"
                 "precipitation_probability_max,precipitation_sum,sunrise,sunset",
        "timezone": "auto",
        "forecast_days": max(1, min(16, int(days))),
    }
    if str(unit).lower().startswith("f"):
        params["temperature_unit"] = "fahrenheit"
        params["wind_speed_unit"] = "mph"
    return _get(FORECAST_URL, params, timeout)


# --------------------------------------------------------------------------- #
# 说话
# --------------------------------------------------------------------------- #
def _num(value: Any, digits: int = 0) -> str:
    try:
        num = float(value)
    except (TypeError, ValueError):
        return "—"
    if math.isnan(num) or math.isinf(num):
        return "—"
    return f"{num:.{digits}f}" if digits else f"{num:.0f}"


def code_text(code: Any) -> str:
    try:
        return WMO.get(int(code), "天气不明")
    except (TypeError, ValueError):
        return "天气不明"


def _day_word(index: int) -> str:
    return {0: "今天", 1: "明天", 2: "后天", 3: "大后天"}.get(index, f"{index} 天后")


def _unit_word(unit: str) -> str:
    return "华氏度" if str(unit).lower().startswith("f") else "度"


def _wind_word(unit: str) -> str:
    return "英里每小时" if str(unit).lower().startswith("f") else "米每秒"


def _daily_lines(data: dict, start: int, count: int, unit: str) -> list[str]:
    daily = data.get("daily") or {}
    codes = list(daily.get("weather_code") or [])
    highs = list(daily.get("temperature_2m_max") or [])
    lows = list(daily.get("temperature_2m_min") or [])
    probs = list(daily.get("precipitation_probability_max") or [])
    out: list[str] = []
    uw = _unit_word(unit)
    for i in range(start, min(start + count, len(codes) if codes else 0)):
        text = code_text(codes[i] if i < len(codes) else None)
        hi = _num(highs[i]) if i < len(highs) else "—"
        lo = _num(lows[i]) if i < len(lows) else "—"
        line = f"{_day_word(i)}{text}，{lo} 到 {hi} {uw}"
        try:
            prob = int(probs[i])
        except (IndexError, TypeError, ValueError):
            prob = -1
        if prob >= 0:
            line += f"，降水概率 {prob}%"
        if prob >= 60:
            line += "，记得带伞"
        out.append(line)
    return out


def describe(place: Place, data: dict, scope: Scope, *, unit: str = "c") -> str:
    """把一份预报翻成能直接念的中文。"""
    unit_word = _unit_word(unit)
    head = place.label()
    parts: list[str] = []

    current = data.get("current") or {}
    if scope.focus == 0 and current:
        now_bits = [f"现在 {_num(current.get('temperature_2m'))} {unit_word}"]
        feel = current.get("apparent_temperature")
        if feel is not None and _num(feel) != _num(current.get("temperature_2m")):
            now_bits.append(f"体感 {_num(feel)} {unit_word}")
        now_bits.append(code_text(current.get("weather_code")))
        humidity = current.get("relative_humidity_2m")
        if humidity is not None:
            now_bits.append(f"湿度 {_num(humidity)}%")
        wind = current.get("wind_speed_10m")
        if wind is not None:
            now_bits.append(f"风速 {_num(wind)} {_wind_word(unit)}")
        parts.append(f"{head}" + "，".join(now_bits) + "。")

    days = scope.days
    if scope.focus is not None:
        lines = _daily_lines(data, scope.focus, 1, unit)
        if lines:
            lines = [place.name + lines[0]]   # 「杭州明天晴，3 到 15 度」——先把地名说出来
    else:
        lines = _daily_lines(data, 0, days, unit)
        if lines:
            lines[0] = f"{head}，" + lines[0]
    if lines:
        parts.append("；".join(lines) + "。")
    if not parts:
        return f"{head}的天气数据没读到。"
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 门面
# --------------------------------------------------------------------------- #
class WeatherService:
    """带缓存的门面：技能层 / 工具层 / MCP 都用它，缓存只在进程内。"""

    def __init__(self, cfg: Any, logger: logging.Logger | None = None) -> None:
        self.cfg = cfg
        self.log = logger or logging.getLogger("voice_loop")
        self._cache: dict[str, tuple[float, dict]] = {}
        self._places: dict[str, tuple[float, Place | None]] = {}
        self._last_error = ""       # 上一次网络失败的原因（用来把「没网」和「没这个地名」分开）

    # ---------------------------------------------------------------- 配置
    @property
    def enabled(self) -> bool:
        return bool(getattr(self.cfg, "enabled", True))

    @property
    def unit(self) -> str:
        return str(getattr(self.cfg, "unit", "c") or "c")

    @property
    def timeout(self) -> float:
        try:
            return max(2.0, float(getattr(self.cfg, "timeout", 8.0) or 8.0))
        except (TypeError, ValueError):
            return 8.0

    @property
    def cache_seconds(self) -> float:
        try:
            return max(0.0, float(getattr(self.cfg, "cache_seconds", 600) or 0))
        except (TypeError, ValueError):
            return 600.0

    def _cfg_place(self) -> Place | None:
        """配置里直接写了经纬度就用它（跳过地名解析，最稳）。"""
        try:
            lat = float(getattr(self.cfg, "latitude", 0.0) or 0.0)
            lon = float(getattr(self.cfg, "longitude", 0.0) or 0.0)
        except (TypeError, ValueError):
            return None
        if not lat and not lon:
            return None
        name = str(getattr(self.cfg, "default_city", "") or "").strip() or "这里"
        return Place(name=name, latitude=lat, longitude=lon)

    # ---------------------------------------------------------------- 查询
    def resolve_place(self, text: str) -> tuple[Place | None, str]:
        """决定查哪里的天气：句子里的地名 → 配置经纬度 → 配置城市 → IP 定位。"""
        asked = parse_place(text)
        if asked:
            place = self._geocode_cached(asked)
            if place is not None:
                return place, asked
        named = self._cfg_place() or self._city_place()
        if named is not None:
            return named, named.name
        if bool(getattr(self.cfg, "auto_ip", False)):
            place = self._ip_place()
            if place is not None:
                return place, place.name
        if asked:
            return None, asked
        return None, ""

    def _city_place(self) -> Place | None:
        city = str(getattr(self.cfg, "default_city", "") or "").strip()
        if not city:
            return None
        return self._geocode_cached(city)

    def _ip_place(self) -> Place | None:
        """按出口 IP 猜城市（★要用户显式打开 auto_ip★，默认关）。"""
        key = "__ip__"
        hit = self._places.get(key)
        if hit and (time.monotonic() - hit[0]) < self.cache_seconds:
            place = hit[1]
            return place
        try:
            raw = _get(IP_URL, {}, self.timeout)
            lat = float(raw.get("latitude") or 0.0)
            lon = float(raw.get("longitude") or 0.0)
        except Exception as exc:  # noqa: BLE001 - 定位失败就退化成「问用户」
            self.log.warning(f"[天气] IP 定位失败：{exc}")
            return None
        if not lat and not lon:
            return None
        place = Place(name=str(raw.get("city") or "这里"), latitude=lat, longitude=lon,
                      admin=str(raw.get("region") or ""), country=str(raw.get("country_name") or ""))
        self._places[key] = (time.monotonic(), place)
        return place

    def _geocode_cached(self, name: str) -> Place | None:
        key = name.strip().lower()
        hit = self._places.get(key)
        if hit and (time.monotonic() - hit[0]) < self.cache_seconds:
            return hit[1]
        try:
            place = geocode(name, timeout=self.timeout,
                            language=str(getattr(self.cfg, "geocode_language", "zh") or "zh"))
        except Exception as exc:  # noqa: BLE001 - 没网/接口变了都只是「查不到」
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning(f"[天气] 地名「{name}」查不到：{exc}")
            return None
        self._places[key] = (time.monotonic(), place)
        return place

    def _forecast_cached(self, place: Place, days: int) -> dict:
        key = f"{place.latitude:.3f},{place.longitude:.3f},{days},{self.unit}"
        hit = self._cache.get(key)
        if hit and (time.monotonic() - hit[0]) < self.cache_seconds:
            return hit[1]
        data = forecast(place, days, timeout=self.timeout, unit=self.unit)
        self._cache[key] = (time.monotonic(), data)
        return data

    def ask(self, text: str) -> WeatherAnswer:
        """一句话问答：``ask("明天上海要带伞吗")``。"""
        if not self.enabled:
            return WeatherAnswer(False, "天气查询现在是关着的（config.toml 的 [weather] enabled）。",
                                 error="disabled")
        scope = parse_scope(text)
        max_days = 7
        try:
            max_days = max(1, min(16, int(getattr(self.cfg, "days_max", 7) or 7)))
        except (TypeError, ValueError):
            pass
        scope.days = min(scope.days, max_days)
        place, asked = self.resolve_place(text)
        if place is None:
            if self._last_error:      # 是网络/接口的问题，别怪用户没说清楚
                return WeatherAnswer(
                    False,
                    "天气没查到：现在可能没网，或者天气接口不通，稍后再问一次。",
                    data={"asked": asked}, error=self._last_error,
                )
            where = f"「{asked}」这个地名我没查到" if asked else "还不知道你在哪"
            return WeatherAnswer(
                False,
                f"{where}。可以说得具体点（比如「上海天气」），或者在 config.toml 的 "
                "[weather] default_city 里填上你常待的城市。",
                data={"asked": asked}, error="no_place",
            )
        try:
            data = self._forecast_cached(place, scope.days)
        except Exception as exc:  # noqa: BLE001 - 断网就是查不到，不编数字
            self._last_error = f"{type(exc).__name__}: {exc}"
            self.log.warning(f"[天气] 取预报失败（{place.label()}）：{exc}")
            return WeatherAnswer(
                False,
                f"天气没查到（{place.label()}）：现在可能没网，或者天气接口不通。",
                data={"place": place.as_dict()}, error=f"{type(exc).__name__}: {exc}",
            )
        reply = describe(place, data, scope, unit=self.unit)
        self.log.info(f"[天气] {place.label()} {scope.label} -> {reply[:60]}")
        return WeatherAnswer(True, reply, data={
            "place": place.as_dict(), "scope": scope.label,
            "days": scope.days, "focus": scope.focus,
            "current": data.get("current") or {},
            "daily": data.get("daily") or {},
            "source": "open-meteo",
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
        })

