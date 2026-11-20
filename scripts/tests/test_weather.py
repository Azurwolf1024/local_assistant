"""天气自测（离线，一个字节都不联网）。

为什么值得钉住：天气是「会念给用户听的真数据」，错得最隐蔽的是**两级**
——地名解析错（问了上海拿到深圳）和数值格式化错（把最高温念成最低温）。
所以这里把网络整个换掉（``weather._get`` 打桩），只验三件事：

    1. 解析：原话 → （哪几天，哪个地名）是确定的；
    2. 说话：给定一份预报，念出来的中文是固定的（含「记得带伞」这种提示）；
    3. 退路：没网 / 地名查不到 / 关掉功能 —— 都要说人话，绝不编一个温度。

    python scripts\\tests\\test_weather.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from voice_loop import weather as wx  # noqa: E402
from voice_loop import tool_presets  # noqa: E402
from voice_loop.mcp.servers import agent as agent_server  # noqa: E402
from voice_loop.settings import load_settings  # noqa: E402
from voice_loop.skills import Skills  # noqa: E402
from voice_loop.tools import ToolRegistry  # noqa: E402

PASS, FAIL = "√", "×"
_failures: list[str] = []


def check(name: str, got, want=None, detail: str = "") -> None:
    ok = bool(got) if want is None else got == want
    tail = "" if ok or want is None else f"（期望 {want!r}）"
    print(f"  {PASS if ok else FAIL} {name}：{detail or got!r}{tail}")
    if not ok:
        _failures.append(name)


# --------------------------------------------------------------------------- #
# 假数据：形状与 Open-Meteo 一致（真接口少一层也照样能跑）
# --------------------------------------------------------------------------- #
GEO = {"results": [{"name": "上海", "latitude": 31.22, "longitude": 121.46,
                    "admin1": "上海市", "country": "中国"}]}
FINE = {
    "current": {"temperature_2m": 18.4, "apparent_temperature": 17.1,
                "relative_humidity_2m": 62, "precipitation": 0.0,
                "weather_code": 0, "wind_speed_10m": 3.2, "is_day": 1},
    "daily": {
        "weather_code": [2, 61, 0],
        "temperature_2m_max": [21.2, 19.6, 24.0],
        "temperature_2m_min": [12.3, 11.8, 14.5],
        "precipitation_probability_max": [10, 80, 0],
        "precipitation_sum": [0.0, 6.2, 0.0],
        "sunrise": ["2026-11-20T06:31", "2026-11-21T06:32", "2026-11-22T06:33"],
        "sunset": ["2026-11-20T16:58", "2026-11-21T16:57", "2026-11-22T16:57"],
    },
}


class FakeNet:
    """把 ``_get`` 换成这个：记次数、按 URL 返回假数据、可以指定报错。"""

    def __init__(self, *, geo: dict | None = None, forecast_data: dict | None = None,
                 error: Exception | None = None) -> None:
        self.geo = GEO if geo is None else geo
        self.forecast_data = FINE if forecast_data is None else forecast_data
        self.error = error
        self.calls: list[str] = []

    def __call__(self, url: str, params: dict, timeout: float) -> dict:
        self.calls.append(url)
        if self.error is not None:
            raise self.error
        if "geocoding" in url:
            return json.loads(json.dumps(self.geo))
        return json.loads(json.dumps(self.forecast_data))


def _cfg(**kw):
    base = dict(enabled=True, default_city="", latitude=0.0, longitude=0.0,
                days_max=7, timeout=5.0, cache_seconds=600, unit="c",
                geocode_language="zh", auto_ip=False)
    base.update(kw)

    class C:
        pass

    cfg = C()
    for key, value in base.items():
        setattr(cfg, key, value)
    return cfg


# --------------------------------------------------------------------------- #
def test_parse_scope() -> None:
    print("\n[1] 问的是哪几天（纯字符串，跨零点也不会飘）")
    check("「今天天气」→ 1 天，焦点今天", (wx.parse_scope("今天天气怎么样").days,
                                        wx.parse_scope("今天天气怎么样").focus), (1, 0))
    check("「明天」→ 2 天，焦点明天", (wx.parse_scope("明天要带伞吗").days,
                                     wx.parse_scope("明天要带伞吗").focus), (2, 1))
    check("「后天」→ 3 天，焦点后天", (wx.parse_scope("后天下雨吗").days,
                                     wx.parse_scope("后天下雨吗").focus), (3, 2))
    check("「未来三天」→ 3 天，整段都要（focus=None）",
          (wx.parse_scope("未来三天天气").days, wx.parse_scope("未来三天天气").focus), (3, None))
    check("「这周」→ 7 天", wx.parse_scope("这周会下雨吗").days, 7)
    check("什么都没说 → 默认今天", (wx.parse_scope("外面冷吗").days,
                                   wx.parse_scope("外面冷吗").focus), (1, 0))


def test_parse_place() -> None:
    print("\n[2] 地名怎么抠出来（抠不出就给空串，交给配置兜底）")
    cases = {
        "明天上海要带伞吗": "上海",
        "后天北京冷不冷": "北京",
        "未来三天杭州天气": "杭州",
        "查一下纽约天气": "纽约",
        "上海现在多少度": "上海",
        "今天天气怎么样": "",
        "这周会下雨吗": "",
    }
    for text, want in cases.items():
        check(f"「{text}」→ {want or '（没地名）'}", wx.parse_place(text), want)


def test_code_text() -> None:
    print("\n[3] 天气代码说人话（不念 WMO 编号）")
    check("0 → 晴", wx.code_text(0), "晴")
    check("61 → 小雨", wx.code_text(61), "小雨")
    check("95 → 雷阵雨", wx.code_text(95), "雷阵雨")
    check("没见过的代码 → 天气不明", wx.code_text(1234), "天气不明")
    check("None 也不炸", wx.code_text(None), "天气不明")


def test_describe() -> None:
    print("\n[4] 念出来的中文（给定数据就是固定输出）")
    place = wx.Place(name="上海", latitude=31.22, longitude=121.46, admin="上海市")
    today = wx.describe(place, FINE, wx.parse_scope("今天天气怎么样"))
    check("今天先报实况", today.startswith("上海现在 18 度"), detail=today)
    check("实况里带体感/湿度/风速", ("体感 17 度" in today, "湿度 62%" in today,
                                   "风速 3 米每秒" in today), (True, True, True))
    check("今天的高低温顺序是 低→高", "12 到 21 度" in today, True, detail=today)

    tomorrow = wx.describe(place, FINE, wx.parse_scope("明天要带伞吗"))
    check("只问明天就不报实况", tomorrow.startswith("上海明天"), detail=tomorrow)
    check("明天是小雨 + 降水概率 80%", ("小雨" in tomorrow, "80%" in tomorrow), (True, True))
    check("★概率高会提醒带伞★", "记得带伞" in tomorrow, True, detail=tomorrow)

    week = wx.describe(place, FINE, wx.parse_scope("未来三天天气"))
    check("三天都说到了", [w in week for w in ("今天", "明天", "后天")], [True, True, True])
    check("华氏度会换词", "华氏度" in wx.describe(place, FINE, wx.parse_scope("今天天气"), unit="f"),
          True)


def test_service(monkeypatch_ok: bool = True) -> None:
    print("\n[5] 门面：缓存 / 关掉 / 没网 / 地名查不到")
    net = FakeNet()
    wx._get = net  # type: ignore[assignment]
    svc = wx.WeatherService(_cfg(default_city="上海"))
    first = svc.ask("明天要带伞吗")
    check("没说地名 → 用配置里的城市", first.ok, True)
    calls_after_first = len(net.calls)
    second = svc.ask("明天要带伞吗")
    check("第二次命中缓存（不再打网络）", len(net.calls), calls_after_first)
    check("两次答复一致", second.reply, first.reply)
    check("data 里留了出处与范围", (first.data.get("source"), first.data.get("scope")),
          ("open-meteo", "今明两天"))

    # 地名从原话里来：新的一份服务会多打一次 geocoding（地名有缓存，同一个 service 只查一次）
    net.calls.clear()
    fresh = wx.WeatherService(_cfg(default_city="上海"))
    third = fresh.ask("后天上海冷不冷")
    check("句子里有地名 → 先 geocode 再取预报", len(net.calls), 2)
    check("答复里是那个地名", third.reply.startswith("上海后天"), True, detail=third.reply)

    # 关掉
    off = wx.WeatherService(_cfg(enabled=False))
    check("enabled=false → 明说关着", (off.ask("今天天气").ok,
                                     "关着" in off.ask("今天天气").reply), (False, True))

    # 没网
    wx._get = FakeNet(error=RuntimeError("没有网络"))  # type: ignore[assignment]
    dead = wx.WeatherService(_cfg(default_city="上海")).ask("今天天气")
    check("断网 → ok=False 且不说温度", (dead.ok, "度" not in dead.reply), (False, True))
    check("断网原因写进 error", "RuntimeError" in dead.error or "没有网络" in dead.error, True)

    # 地名查不到、也没有默认城市
    wx._get = FakeNet(geo={}, forecast_data=FINE)  # type: ignore[assignment]
    lost = wx.WeatherService(_cfg()).ask("瓦坎达天气")
    check("查不到地名 → 请用户说清楚", (lost.ok, "瓦坎达" in lost.reply), (False, True))
    check("顺便告诉他怎么配默认城市", "default_city" in lost.reply, True)

    # 配置里直接给经纬度：跳过地名解析（只打一次网络）
    net2 = FakeNet()
    wx._get = net2  # type: ignore[assignment]
    pinned = wx.WeatherService(_cfg(latitude=31.22, longitude=121.46, default_city="上海"))
    pinned.ask("今天天气")
    check("★给了经纬度就不 geocode★", len(net2.calls), 1)


def test_tool_and_skill() -> None:
    print("\n[6] 工具层与技能层都接上了（同一套解析）")
    wx._get = FakeNet()  # type: ignore[assignment]
    settings = load_settings()
    settings.weather = _cfg(default_city="上海")
    settings.skills.data_dir = tempfile.mkdtemp(prefix="weather_skills_")
    skills = Skills(settings)
    registry = ToolRegistry(settings, skills)

    check("工具清单里有 weather", "weather" in registry.names(), True)
    spec = [s for s in registry.specs() if s["function"]["name"] == "weather"][0]
    check("参数只要 text（原话）", list(spec["function"]["parameters"]["properties"]), ["text"])

    got = registry.call({"function": {"name": "weather",
                                      "arguments": {"text": "明天上海要带伞吗"}}})
    check("工具调用成功", got.ok, True)
    check("工具答复是能念的中文", "上海明天" in got.reply, True, detail=got.reply)

    r = skills.handle("明天上海要带伞吗")
    check("技能层也答得出来", getattr(r, "action", ""), "weather")
    check("「看看天气」不会被看图抢走", getattr(skills.handle("看看天气"), "action", ""), "weather")
    check("「现在几点」不受影响", getattr(skills.handle("现在几点"), "action", ""), "clock")


def test_mcp_and_presets() -> None:
    print("\n[7] MCP harness 工具 + 预设")
    wx._get = FakeNet()  # type: ignore[assignment]
    settings = load_settings()
    settings.weather = _cfg(default_city="上海")
    server = agent_server.build_server(settings, None)
    names = [getattr(t, "name", "") for t in server.tools()]
    check("harness 服务器里有 weather", "weather" in names, True, detail=str(names))

    handler_line = [t for t in server.tools() if getattr(t, "name", "") == "weather"]
    check("weather 工具带描述", bool(getattr(handler_line[0], "description", "")), True)
    built = agent_server.make_weather(settings, None)
    out = built({"query": "明天要带伞吗"})
    check("handler 返回 text 而不是抛异常", isinstance(out.get("text"), str), True)
    check("handler 说人话", "上海明天" in out["text"], True, detail=out["text"])

    check("预设里有 weather", sorted(tool_presets.expand(["weather"])), ["weather"])
    check("harness 预设包含 weather",
          "weather" in tool_presets.expand(["harness"]), True)
    check("预设描述能列给控制台", any(p["name"] == "weather"
                                    for p in tool_presets.describe()), True)


def main() -> int:
    saved = wx._get
    try:
        test_parse_scope()
        test_parse_place()
        test_code_text()
        test_describe()
        test_service()
        test_tool_and_skill()
        test_mcp_and_presets()
    finally:
        wx._get = saved  # type: ignore[assignment]
    print()
    if _failures:
        print(f" {len(_failures)} 项未通过：")
        for name in _failures:
            print(f"   - {name}")
        return 1
    print(" 天气自测全部通过 √")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
