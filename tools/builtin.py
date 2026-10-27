"""本地工具函数注册表（技能用 local 类型工具时按名字调用）。

约定：函数签名 (text: str, **args) -> str，返回可直接说给用户的一小段文本。
网络类函数（如天气）失败时抛异常，由 tools/executor.py 统一降级。
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request


def _get_json(url: str, timeout: float = 10) -> dict:
    request = urllib.request.Request(url, headers={"User-Agent": "companion-ai/1.0"})
    with urllib.request.urlopen(request, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


WEATHER_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴", 45: "有雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨", 56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨", 66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "米雪",
    80: "阵雨", 81: "强阵雨", 82: "暴雨", 85: "小阵雪", 86: "大阵雪",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷雨伴冰雹",
}
_WEATHER_FALLBACK = {0: "晴", 1: "多云", 2: "多云", 3: "阴", 45: "有雾", 51: "有小雨",
                     61: "有雨", 71: "有雪", 80: "有阵雨", 95: "有雷阵雨"}


def _describe_weather(code: int) -> str:
    if code in WEATHER_TEXT:
        return WEATHER_TEXT[code]
    # 不在表里时按十位归类，别说得太肯定
    return _WEATHER_FALLBACK.get(code // 10 * 10, "天气一般")


DEFAULT_CITY = "杭州"      # 猜不出城市时用它，但**必须在回话里说出来**（见 weather 注释）


def weather(text: str, city: str = DEFAULT_CITY, **_) -> str:
    """查当前天气（open-meteo，免密钥）。从文本里尽量猜城市名。

    2026-10-13 修两处（用户反馈"把杭州天气当成用户所在地的事实"）：
      ① `_guess_city` 原来会把**时间词当城市**——「今天天气怎么样」曾猜出城市"今天"
         （地理编码查不到 → 抛错 → 降级成"天气我这儿没查到"）；
      ② 猜不出城市时用默认城市，但以前输出是「杭州现在…」，模型很容易把地点省略掉，
         于是「晴间多云，24.2℃」被用户当成自己所在地的天气。
         现在固定输出「【城市】今天…」的形式，地点永远在句首，模型省不掉。
    """
    guessed = _guess_city(text)
    name = guessed or city
    geo = _get_json("https://geocoding-api.open-meteo.com/v1/search?"
                    + urllib.parse.urlencode({"name": name, "count": 1, "language": "zh"}))
    results = geo.get("results") or []
    if not results:
        raise RuntimeError(f"没找到城市「{name}」")
    place = results[0]
    weather_data = _get_json(
        "https://api.open-meteo.com/v1/forecast?"
        + urllib.parse.urlencode({
            "latitude": place["latitude"], "longitude": place["longitude"],
            "current": "temperature_2m,relative_humidity_2m,apparent_temperature,"
                       "weather_code,wind_speed_10m",
            "timezone": "auto"})).get("current") or {}
    code = int(weather_data.get("weather_code") or -1)
    desc = _describe_weather(code) if code >= 0 else "天气一般"
    temp = weather_data.get("temperature_2m")
    feels = weather_data.get("apparent_temperature")
    wind = weather_data.get("wind_speed_10m")
    shown = place.get("name") or name
    # 一律带上城市名：省略地点时用户会默认成"我这里"
    bits = [f"{shown}今天{desc}"]
    if temp is not None:
        bits.append(f"{temp}℃")
    if feels is not None and temp is not None and abs(float(feels) - float(temp)) >= 2:
        bits.append(f"体感{feels}℃")
    if wind is not None:
        bits.append(f"风{wind}km/h")
    return "，".join(str(b) for b in bits) + "。"


_CITY_RE = re.compile(r"([\u4e00-\u9fff]{2,8}?)(?:的天气|天气|下雨|冷不冷|气温|多少度)")

_TIME_WORDS = ("今天", "明天", "后天", "现在", "此刻", "今晚", "昨天", "这周", "周末",
               "早上", "上午", "中午", "下午", "晚上", "最近", "这两天", "这几天")

# 不是城市名的词：时间词之外，还有问句残渣与虚指。命中就不当成城市。
_NOT_CITY = _TIME_WORDS + (
    "怎么样", "如何", "什么", "多少", "有没有", "是不是", "会不会", "要不要",
    "这里", "那里", "这边", "那边", "哪儿", "哪里", "外面", "当地", "这边儿",
    "一阵", "一点", "有点", "还行", "不错", "挺好", "好不", "咋样", "怎样",
)
# 中国城市后缀：能大幅提高"这看起来像个地名"的把握
_CITY_SUFFIX = ("市", "省", "区", "县", "州", "阳", "阴", "口", "门", "岛", "湾",
                "港", "江", "河", "湖", "山", "海", "宁", "安", "昌", "兴", "都",
                "京", "津", "沪", "渝", "穗", "蓉", "汉", "杭", "苏", "深")


# 常见城市短名：没带后缀也认（「北京」「上海」这类）
_KNOWN_CITIES = frozenset((
    "北京", "上海", "广州", "深圳", "成都", "南京", "武汉", "西安", "重庆", "苏州",
    "天津", "长沙", "青岛", "厦门", "昆明", "合肥", "济南", "福州", "大连", "沈阳",
    "哈尔滨", "郑州", "石家庄", "杭州", "宁波", "无锡", "佛山", "东莞", "烟台",
    "温州", "常州", "南通", "徐州", "南昌", "贵阳", "兰州", "太原", "长春",
))


def _guess_city(text: str) -> str:
    """从「XX天气怎么样」这类句子里猜城市名。

    2026-10-13 修：「今天天气怎么样」原来会猜出城市 **"今天"**（时间词没剔干净），
    导致地理编码查不到、技能降级成"天气我这儿没查到"。

    现在的判定顺序：
      1. 按 `的天气/天气/下雨/…` 切出候选；
      2. 剔时间词、疑问尾词、请求前缀；
      3. 候选若是虚指词、或属于已知城市短名、或带城市后缀 → 认；
      4. 其余以「天/气/样/么/哪/里/边」结尾的一律不认（这就是"今天"被认成城市的原因）。
    """
    text = (text or "").strip()
    match = _CITY_RE.search(text)
    if not match:
        return ""
    name = match.group(1)
    # 请求前缀要先剔，且长的优先：只剔「帮我查」会把「帮我查一下成都」留成「一下成都」
    for prefix in ("帮我查一下", "帮我查下", "帮我查", "查一下", "查下", "问一下", "查", "看看"):
        if name.startswith(prefix):
            name = name[len(prefix):]
            break
    for word in _TIME_WORDS:            # 时间词可能在前面也可能在后面
        name = name.replace(word, "")
    for tail in ("会", "要", "有没有", "是不是", "怎么样", "如何", "呢", "吗", "的"):
        if name.endswith(tail):
            name = name[: -len(tail)]
    name = name.strip()
    if not (2 <= len(name) <= 8):
        return ""
    if name in _NOT_CITY or any(word in name for word in _NOT_CITY):
        return ""
    if name in _KNOWN_CITIES or name.endswith(_CITY_SUFFIX):
        return name
    # 剩下的：只有不像"时间/疑问残渣"才认
    if re.search(r"(天|气|样|么|哪|里|边|候)$", name):
        return ""
    return name


def echo(text: str, **_) -> str:
    """测试用：原样返回（也用于验证工具链路）。"""
    return (text or "").strip()


def time_now(text: str = "", **_) -> str:
    """当前时间——给模型一份**它可以直接引用**的完整时间串。

    ## 为什么已经有 `now_line()` 还要这个

    `engine/generate.py: now_line()` 确实把「现在的时间：… 周X。」放进了系统提示，
    **而且模型多数时候读得出来**（实测「现在几点」→「20:16 周三了」✅、
    「今年是哪一年」→「2026 年了」✅）。

    但**问"几号"会答错**：实测「今天几号」→「周三了。」——把"号"当成了周几。
    因为提示里那句偏向"日期 + 星期"，没把每个字段摆清楚。

    所以这个工具不负责"给模型时间"，负责**把字段摆清楚 + 答案由程序算**：
    年/月/日/时/分/星期/第几周/时段/今天还剩几小时。

    ⚠ 只读本地时钟：不联网、不落库、不会失败。
    """
    import datetime as _dt
    now = _dt.datetime.now()
    week = "一二三四五六日"[now.weekday()]
    week_of_year = now.isocalendar()[1]
    left = 24 - now.hour - 1
    if now.hour < 6:
        slot = "凌晨"
    elif now.hour < 9:
        slot = "早上"
    elif now.hour < 12:
        slot = "上午"
    elif now.hour < 14:
        slot = "中午"
    elif now.hour < 18:
        slot = "下午"
    elif now.hour < 19:
        slot = "傍晚"
    elif now.hour < 23:
        slot = "晚上"
    else:
        slot = "深夜"
    clock = f"{now:%Y-%m-%d %H:%M}"
    return (f"现在：{clock}（{now.year} 年 {now.month} 月 {now.day} 日，"
            f"星期{week}，今年第 {week_of_year} 周）；"
            f"时段：{slot}"
            + (f"，今天还剩约 {left} 小时" if left > 0 else "，今天快过完了"))
