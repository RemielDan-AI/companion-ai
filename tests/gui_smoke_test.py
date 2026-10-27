#!/usr/bin/env python
"""窗口冒烟测试：真正构建一次 Tk 窗口，验证界面逻辑与端到端对话。

用独立的临时数据文件，不碰你的真实记忆/价值观库。
需要 8080 在跑才会跑端到端部分（不在跑就跳过）。

用法：python tests/gui_smoke_test.py
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys
import time
import tkinter as tk
import urllib.request
from tkinter import ttk
ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import gui  # noqa: E402
from app import Companion  # noqa: E402
from cognition import values as values_engine  # noqa: E402
from voice import tts as tts_engine  # noqa: E402

TMP = pathlib.Path(__file__).resolve().parent / "_tmp"
KANA = re.compile(r"[\u3040-\u309f\u30a0-\u30ff]")


def model_ready() -> bool:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/health", timeout=3):
            return True
    except Exception:
        return False


def send_and_wait(window, root, text: str, timeout: int = 180) -> str:
    """发一句话，等回复落到界面，返回新增的回复文本。"""
    before = len(window.replies)
    window.entry.delete(0, "end")
    window.entry.insert(0, text)
    window._on_send()
    deadline = time.time() + timeout
    while time.time() < deadline:
        root.update()
        if not window.busy and len(window.replies) > before:
            break
        time.sleep(0.1)
    return window.replies[-1] if len(window.replies) > before else ""


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    paths = {name: TMP / f"gui_{name}" for name in
             ("db", "index.json", "profile.json", "values.yaml",
              "scenarios.jsonl", "preferences.jsonl")}
    for path in paths.values():
        if path.exists():
            path.unlink()
    tts_engine.save_ui_state = lambda state: None      # 不改动 voice/ui_state.json

    companion = Companion(use_memory=True, session="guitest",
                          memory_path=paths["db"], index_path=paths["index.json"],
                          profile_path=paths["profile.json"],
                          values_path=paths["values.yaml"],
                          scenarios_path=paths["scenarios.jsonl"],
                          preferences_path=paths["preferences.jsonl"])

    checks: list[tuple[str, bool, str]] = []
    root = tk.Tk()
    root.withdraw()                      # 不真的弹到屏幕上
    try:
        window = gui.Window(root, companion=companion)
        root.update()
        checks.append(("窗口构建成功", True, ""))

        # --- 对话必须能用鼠标选中、复制（2026-10-19 用户反馈"选不中"） ---
        # ⚠ 根因：每行都挂了带 background 的 tag，而 Tk 里**后建的 tag 优先级更高**，
        #   内建的 `sel` 是最早建的 → 选区被气泡底色盖住，**选中了也看不见**。
        #   修法：`selectbackground` 用明显不同的色 + `tag_raise("sel")`。
        try:
            from gui_theme import COLORS as _C
        except Exception:
            _C = {}
        names = list(window.chat.tag_names())
        checks.append(("**选区 sel 优先级最高**（否则选中被气泡底色盖住）",
                       bool(names) and names[-1] == "sel", str(names)))
        sel_bg = str(window.chat.cget("selectbackground")).lower()
        bubbles = {str(_C.get(k, "")).lower()
                   for k in ("bubble_me", "bubble_ai", "bg", "panel")}
        checks.append(("选区底色与所有气泡色都不同（否则看不出选中）",
                       bool(sel_bg) and sel_bg not in bubbles,
                       f"sel={sel_bg} bubbles={sorted(bubbles)}"))
        checks.append(("允许选中内容进系统剪贴板",
                       bool(window.chat.cget("exportselection")), ""))
        checks.append(("有右键菜单（复制/全选）", hasattr(window, "_chat_menu"), ""))
        window.chat.insert("end", "冒烟测试一行\n", "ai")
        window._chat_select_all()
        rng = window.chat.tag_ranges("sel")
        checks.append(("全选能选中内容", len(rng) >= 2,
                       f"{[str(x) for x in rng]}"))

        backends = list(window.backend_box.cget("values"))
        checks.append(("语音下拉含四种选择",
                       backends == ["不语音", "Edge TTS", "GSV TTS Lite", "OpenAI 兼容"],
                       str(backends)))
        reply_langs = list(window.text_lang_box.cget("values"))
        checks.append(("文本语言下拉含三种",
                       reply_langs == ["中文", "日本語", "English"], str(reply_langs)))
        voice_langs = list(window.voice_lang_box.cget("values"))
        checks.append(("朗读语言下拉含四种（含自动）",
                       voice_langs == ["自动", "中文", "日本語", "English"], str(voice_langs)))

        # --- Edge：声音列表 + 按语言过滤 ---
        window.text_lang_var.set("中文")
        window.voice_lang_var.set("自动")
        window.backend_var.set("Edge TTS")
        window._on_backend_change()
        root.update()
        zh_voices = list(window.edge_box.cget("values"))
        checks.append(("Edge 中文声音列表已加载",
                       len(zh_voices) >= 5 and all("zh-" in v for v in zh_voices),
                       f"{len(zh_voices)} 个"))
        cfg = window._voice_cfg()
        checks.append(("Edge 配置带上了所选声音",
                       cfg and cfg["backend"] == "edge"
                       and cfg["edge"]["voice"] == window.edge_voice_var.get().split("（")[0],
                       str(cfg.get("edge", {}).get("voice")) if cfg else "无"))

        # --- 文本语言与朗读语言彼此独立 ---
        window.text_lang_var.set("日本語")
        window._on_text_lang_change()
        root.update()
        checks.append(("朗读语言=自动时跟随文本语言",
                       window.voice_lang_var.get() == "自动" and window._voice_lang() == "ja",
                       f"文本 {window._text_lang()}／朗读 {window._voice_lang()}"))
        ja_voices = list(window.edge_box.cget("values"))
        checks.append(("Edge 声音按有效朗读语言过滤为日语",
                       bool(ja_voices) and all("ja-" in v for v in ja_voices),
                       f"{len(ja_voices)} 个"))

        # 关键：把朗读语言单独设成 English，文本语言应保持日本語不动
        window.voice_lang_var.set("English")
        window._on_voice_lang_change()
        root.update()
        checks.append(("朗读语言可独立于文本语言",
                       window._text_lang() == "ja" and window._voice_lang() == "en",
                       f"文本 {window._text_lang()}／朗读 {window._voice_lang()}"))
        en_voices = list(window.edge_box.cget("values"))
        checks.append(("切朗读语言后 Edge 声音跟着切到英语",
                       bool(en_voices) and all("en-" in v for v in en_voices),
                       f"{len(en_voices)} 个"))
        checks.append(("日语文本提示词存在",
                       bool(gui.LANG_PROMPTS["ja"]) and "日语" in gui.LANG_PROMPTS["ja"], ""))

        # --- GSV 配置：朗读语言独立生效，prompt_lang 固定 ja ---
        window.backend_var.set("GSV TTS Lite")
        window._on_backend_change()
        root.update()
        cfg = window._voice_cfg()
        checks.append(("GSV text_lang 用朗读语言、prompt_lang 固定 ja",
                       cfg and cfg["backend"] == "gsv"
                       and cfg["gsv"]["text_lang"] == "en"
                       and cfg["gsv"]["prompt_lang"] == "ja",
                       f"text_lang={cfg['gsv']['text_lang']} prompt_lang={cfg['gsv']['prompt_lang']}"
                       if cfg else "无"))

        # --- 朗读前翻译：文本语言≠朗读语言时才翻 ---
        checks.append(("翻译判定：同语言不翻、异语言要翻",
                       not gui.translate_engine.needs_translation("zh", "zh")
                       and gui.translate_engine.needs_translation("zh", "ja")
                       and gui.translate_engine.needs_translation("ja", "en"), ""))

        class FakeGenerator:
            """假生成器：直接按目标语言回一句，用来验证翻译链路与缓存。"""

            def __init__(self):
                self.calls = 0

            def complete(self, system, user, max_tokens=400, temperature=0.2):
                self.calls += 1
                return "今日は本当に疲れたね。" if "日语" in user else "Today I'm really tired."

        fake = FakeGenerator()
        original_generator = companion.generator
        companion.generator = fake
        window.text_lang_var.set("中文")
        window.voice_lang_var.set("日本語")
        spoken, translated = window._speech_text("今天真的很累。")
        checks.append(("异语言时朗读用译文",
                       translated and "疲" in spoken, spoken))
        window._speech_text("今天真的很累。")           # 再来一次应命中缓存
        checks.append(("译文有缓存，不重复调用模型", fake.calls == 1, f"调用 {fake.calls} 次"))

        window.voice_lang_var.set("自动")
        spoken_same, translated_same = window._speech_text("今天真的很累。")
        checks.append(("朗读语言=自动（等于文本语言）时不翻译",
                       spoken_same == "今天真的很累。" and not translated_same, ""))
        companion.generator = original_generator

        # --- 播放通道：不叠加、可打断、有播放时限 ---
        import wave as wave_mod
        beep = TMP / "beep.wav"
        with wave_mod.open(str(beep), "wb") as fh:
            fh.setnchannels(1)
            fh.setsampwidth(2)
            fh.setframerate(16000)
            fh.writeframes(b"\x00\x00" * 16000 * 3)      # 3 秒静音
        checks.append(("测试音频已生成", beep.exists() and beep.stat().st_size > 1000, ""))

        real_play = tts_engine.play_file
        real_synth = tts_engine.synthesize
        played: list[str] = []

        class FakeProc:
            def poll(self):
                return None

        def fake_play(path, stop_current=True):
            played.append(str(path))
            return FakeProc()

        tts_engine.play_file = fake_play
        tts_engine.synthesize = lambda text, cfg=None, **kw: beep
        try:
            window.backend_var.set("GSV TTS Lite")
            window._on_backend_change()
            window._speak("第一句测试。", "T1")
            token1 = window._speech_token
            window._speak("第二句测试。", "T2")
            token2 = window._speech_token
            checks.append(("连续朗读请求会作废前一次",
                           token2 > token1, f"token {token1} → {token2}"))
            checks.append(("朗读中按钮变成「停止朗读」",
                           window.replay_button.cget("text").startswith("⏹"),
                           window.replay_button.cget("text")))
            deadline = time.time() + 8
            while time.time() < deadline and not played:
                root.update()
                time.sleep(0.1)
            checks.append(("只有最新一次被播放（旧结果被丢弃）",
                           len(played) == 1, f"播放 {len(played)} 次"))
            checked_stop = 0
            while time.time() < deadline and checked_stop < 3:
                root.update()
                time.sleep(0.1)
                checked_stop += 1
            checks.append(("同一时刻不会叠加播放", len(played) <= 1, f"{len(played)} 次"))

            # 文本与声音同时出现：合成期间只有占位，语音就绪后才显示正文
            def slow_synth(text, cfg=None, **kw):
                time.sleep(1.2)          # 假装合成要花一点时间
                return beep

            tts_engine.synthesize = slow_synth
            window.autoplay_var.set(True)
            window.queue.put(("reply", "测试输入", "这是一句要被朗读的回复。", {}))
            deadline = time.time() + 5
            while time.time() < deadline and "合成语音" not in window.chat.get("1.0", "end"):
                root.update()
                time.sleep(0.1)
            during = window.chat.get("1.0", "end")
            checks.append(("合成期间只显示占位、正文不提前出现",
                           "合成语音" in during and "这是一句要被朗读的回复" not in during,
                           "占位已显示" if "合成语音" in during else "没看到占位"))
            deadline = time.time() + 10
            while (time.time() < deadline
                   and "这是一句要被朗读的回复" not in window.chat.get("1.0", "end")):
                root.update()
                time.sleep(0.1)
            after = window.chat.get("1.0", "end")
            checks.append(("语音就绪后正文才出现，占位被替换掉",
                           "这是一句要被朗读的回复" in after and "合成语音" not in after,
                           ""))
            checks.append(("这条回复也进了回听列表",
                           bool(window.replies)
                           and window.replies[-1] == "这是一句要被朗读的回复。", ""))
            window.autoplay_var.set(False)
        finally:
            tts_engine.play_file = real_play
            tts_engine.synthesize = real_synth

        # 真播放器（ffplay）层面的打断与停止
        if shutil.which("ffplay"):
            first = tts_engine.play_file(beep)
            time.sleep(0.4)
            checks.append(("播放中 is_playing 为真", tts_engine.is_playing(), ""))
            second = tts_engine.play_file(beep)          # 应打断第一段
            time.sleep(0.3)
            checks.append(("新播放会打断旧播放",
                           first is not None and first.poll() is not None
                           and tts_engine.is_playing(), ""))
            checks.append(("stop_playback 能停住", tts_engine.stop_playback()
                           and not tts_engine.is_playing(), ""))
            # 时限兜底：把上限设成 0.5 秒，让状态机自己掐掉
            window.max_play_seconds = 0.5
            tts_engine.play_file(beep)
            window._speech_state = "playing"
            window._play_started = time.time()
            deadline = time.time() + 6
            while time.time() < deadline and window._speech_state == "playing":
                root.update()
                time.sleep(0.15)
            checks.append(("超过播放时限会自动停止",
                           window._speech_state == "idle" and not tts_engine.is_playing(),
                           f"state={window._speech_state}"))
        else:
            print("… 没有 ffplay，跳过真播放器层面的检查")

        # --- 清除记忆（调试用）：摘要 + 对话框 + 真清干净 ---
        # 先埋一条，才能验证"点取消不会删东西"
        companion.memory.add_episode("调试埋点", "好", summary="调试用埋点", turn=1)
        seeded = companion.memory.episode_count()
        summary = companion.wipe_summary()
        checks.append(("清除前能看到数据概览",
                       all(key in summary for key in
                           ("episodes", "semantic", "vectors", "profile", "values")),
                       f"情景 {summary['episodes']}／语义 {summary['semantic']}"))

        window._wipe_dialog()
        root.update()
        dialogs = [w for w in window.root.winfo_children() if isinstance(w, tk.Toplevel)]
        buttons: list[str] = []
        checks_state: list[bool] = []

        def collect(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    buttons.append(str(child.cget("text")))
                if isinstance(child, ttk.Checkbutton):
                    checks_state.append(bool(child.instate(["selected"])))
                collect(child)

        if dialogs:
            collect(dialogs[0])
        checks.append(("确认对话框弹出且必须二选一",
                       bool(dialogs) and "确认清除" in buttons and "取消" in buttons,
                       "／".join(buttons)))
        # 默认：清记忆、清画像、清对话窗口，但【保留价值观】
        checks.append(("默认保留价值观、其余默认清除",
                       checks_state == [True, True, False, True]
                       or checks_state[:3] == [True, True, False],
                       f"勾选状态 {checks_state}（第3项=连价值观一起清）"))
        for dlg in dialogs:                       # 取消路径：不该删任何东西
            dlg.destroy()
        root.update()
        checks.append(("取消后什么都没删",
                       seeded > 0 and companion.memory.episode_count() == seeded,
                       f"情景仍有 {companion.memory.episode_count()} 轮"))

        # 默认路径（保留价值观）：先埋一条价值观，再清记忆
        companion.store["values"] = [{"id": "V1", "content": "对方累了就别追问",
                                      "status": "active", "confidence": 0.9, "evidence": 1,
                                      "turn": 1, "created_at": "", "updated_at": ""}]
        values_engine.save_store(companion.store, companion.values_path)
        companion.memory.add_episode("再埋一条", "好", summary="再埋一条", turn=2)
        report_keep = companion.wipe(memory=True, profile=True, values=False)
        after_keep = companion.wipe_summary()
        checks.append(("默认清除会保住价值观",
                       "values" not in report_keep and after_keep["values"] == 1
                       and after_keep["episodes"] == 0,
                       f"价值观剩 {after_keep['values']} 条、情景剩 {after_keep['episodes']} 轮"))
        checks.append(("保留价值观时 values.yaml 没被删",
                       companion.values_path.exists(), str(companion.values_path.name)))

        # 真的清一遍（用隔离路径，不碰真实数据）
        if companion.profile_path.exists() or True:
            companion.profile_path.write_text('{"facts": [{"k": 1}], "preferences": []}',
                                              encoding="utf-8")
        companion.store["values"] = [{"id": "V1", "content": "测试", "status": "active",
                                      "confidence": 0.9, "evidence": 1, "turn": 1,
                                      "created_at": "", "updated_at": ""}]
        values_engine.save_store(companion.store, companion.values_path)
        report = companion.wipe(memory=True, profile=True, values=True)
        summary_after = companion.wipe_summary()
        checks.append(("清除后记忆/向量/画像/价值观都归零",
                       summary_after["episodes"] == 0 and summary_after["semantic"] == 0
                       and summary_after["vectors"] == 0 and summary_after["profile"] == 0
                       and summary_after["values"] == 0,
                       f"{summary_after}"))
        checks.append(("清除报告有具体数字",
                       isinstance(report.get("memory"), dict)
                       and "vectors" in report and "profile" in report,
                       str({k: v for k, v in report.items() if k != "memory"})))
        checks.append(("画像文件与偏好文件已删除",
                       not companion.profile_path.exists()
                       and not companion.preferences_path.exists(), ""))
        checks.append(("短期上下文也清空了", not companion.generator.history, ""))

        # --- 启动恢复历史（改进 1）+ 记忆管理窗口（改进 23/24） ---
        companion.memory.add_episode("上次聊过的话题", "嗯，我记着呢", summary="上次话题",
                                     importance=3, turn=9)
        window._restore_history()
        root.update()
        checks.append(("启动能把最近对话恢复进窗口",
                       "上次聊过的话题" in window.chat.get("1.0", "end"), ""))

        window._memory_manager()
        root.update()
        managers = [w for w in window.root.winfo_children() if isinstance(w, tk.Toplevel)]
        manager_labels: list[str] = []
        manager_rows = 0

        def collect_manager(widget):
            nonlocal manager_rows
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    try:
                        manager_labels.append(str(child.cget("text")))
                    except Exception:
                        pass
                if child.winfo_class() == "Treeview":
                    manager_rows = len(child.get_children())
                collect_manager(child)

        for win in managers:
            collect_manager(win)
        checks.append(("记忆管理窗口带查看/标记/编辑/删除/导出",
                       len(manager_labels) >= 8, "／".join(manager_labels)))
        checks.append(("记忆列表列出了条目", manager_rows >= 1, f"{manager_rows} 条"))
        for win in managers:
            win.destroy()
        root.update()

        # --- 模型服务面板（模块四：选模型/参数/预设/启停） ---
        #
        # ⚠ 2026-10-21 修：原来这里直接断言"服务面板能列出模型与预设"，
        #   但 `services/presets.py: BUILTIN = {}`（**设计上就没有内置预设**，
        #   "全部由使用者添加"），所以预设下拉永远是空的 → 这条**必然失败**。
        #   它在 `--quick` 模式被跳过，所以一直没暴露。
        #
        #   正确的测法是：**先自己建一个预设**，再验证它能出现在面板里——
        #   这既尊重"预设由用户创建"的设计，又真的测到了"列出预设"这条能力。
        try:
            from services.presets import PresetStore
            _store = PresetStore()
            # 建**三条**：这条断言要求预设下拉里 ≥3 个且名字带「·」
            # （「对话·基线」「对话·9B」「嵌入·bge」就是真实可能的命名）
            _store.data = {"presets": {
                "对话·基线": {"kind": "chat", "port": 8080, "ctx": 4096},
                "对话·9B": {"kind": "chat", "port": 8085, "ctx": 4096},
                "嵌入·bge": {"kind": "embed", "port": 8081, "ctx": 512},
            }, "default": "对话·基线"}
            _store.save()
            # ⚠ 窗口的 PresetStore 是 __init__ 时加载的，会缓存旧内容 →
            #   必须让它**重新读一次**，否则下拉里还是空的。
            window.presets = PresetStore()
        except Exception:
            pass

        window._service_window()
        root.update()
        svc_wins = [w for w in window.root.winfo_children() if isinstance(w, tk.Toplevel)]
        svc_labels: list[str] = []
        svc_combos: list[list[str]] = []

        def collect_svc(widget):
            for child in widget.winfo_children():
                if isinstance(child, ttk.Button):
                    try:
                        svc_labels.append(str(child.cget("text")))
                    except Exception:
                        pass
                if isinstance(child, ttk.Combobox):
                    try:
                        svc_combos.append(list(child.cget("values")))
                    except Exception:
                        pass
                collect_svc(child)

        for win in svc_wins:
            collect_svc(win)
        checks.append(("服务面板有启停/重启/日志/预设按钮",
                       all(any(label.startswith(prefix) for label in svc_labels)
                           for prefix in ("启动", "停止", "重启", "查看日志", "另存为")),
                       "／".join(svc_labels)))
        checks.append(("服务面板能列出模型与预设",
                       any(len(vals) >= 3 and any(v.endswith(".gguf") for v in vals)
                           for vals in svc_combos)
                       and any(len(vals) >= 3 and any("·" in v for v in vals)
                               for vals in svc_combos),
                       f"{[len(v) for v in svc_combos]}"))
        for win in svc_wins:
            win.destroy()
        window.svc_win = None
        root.update()

        # --- 只读拦截 ---
        class FakeKey:
            def __init__(self, keysym, state=0):
                self.keysym, self.state = keysym, state

        checks.append(("对话区拦截打字、放行复制",
                       window._block_typing(FakeKey("a")) == "break"
                       and window._block_typing(FakeKey("c", state=0x4)) is None
                       and window._block_typing(FakeKey("Up")) is None, ""))

        # --- 端到端（需要 8080）---
        if not model_ready():
            print("… 对话模型未启动，跳过端到端部分")
        else:
            window.autoplay_var.set(False)          # 测试时别出声
            # 文本语言=中文：回复不该含假名
            window.text_lang_var.set("中文")
            window.voice_lang_var.set("自动")
            window._on_text_lang_change()
            reply = send_and_wait(window, root, "今天有点累，什么都不想干。")
            checks.append(("端到端（中文）：生成并显示到窗口", bool(reply), reply[:60]))
            checks.append(("中文回复不含假名", bool(reply) and not KANA.search(reply),
                           reply[:40]))

            # 文本语言=日本語：回复应含假名（朗读语言如何都不影响文本）
            window.text_lang_var.set("日本語")
            window.voice_lang_var.set("自动")
            window._on_text_lang_change()
            reply_ja = send_and_wait(window, root, "今天有点累，什么都不想干。")
            has_kana = bool(KANA.search(reply_ja))
            checks.append(("端到端（日语文本）：回复含假名", has_kana, reply_ja[:60]))

            chat_text = window.chat.get("1.0", "end")
            checks.append(("每条回复后面有「回听」入口",
                           chat_text.count("回听") >= 2 and len(window.replies) >= 2,
                           f"回听 {chat_text.count('回听')} 处，已记 {len(window.replies)} 条"))
            try:
                window._replay_last()
                replays_ok = True
            except Exception as exc:
                replays_ok = False
                print("  重听异常：", exc)
            checks.append(("重听上一句不报错", replays_ok, ""))

            # 真机链路：中文回复 → 真翻译成日语 → GSV 合成音频（只写文件、不播放）
            window.text_lang_var.set("中文")
            window.voice_lang_var.set("日本語")
            spoken, translated = window._speech_text(window.replies[0])
            checks.append(("真机翻译：中文回复 → 日文译文",
                           translated and bool(KANA.search(spoken)), spoken[:50]))
            cfg = window._voice_cfg()
            if cfg:
                try:
                    path = tts_engine.synthesize(spoken, cfg, name="zh2ja")
                    ok_audio = path.exists() and path.stat().st_size > 2000
                    detail = f"{path.name} {path.stat().st_size // 1024}KB"
                except Exception as exc:
                    ok_audio, detail = False, str(exc)[:60]
                checks.append(("译文能被 GSV 合成成音频", ok_audio, detail))
            else:
                print("… 未选语音后端，跳过译文合成检查")
    finally:
        try:
            root.destroy()
        except Exception:
            pass

    passed = sum(1 for _, ok, _ in checks if ok)
    for name, ok, extra in checks:
        print(f"{'✓' if ok else '✗'} {name}" + (f"　{extra}" if extra else ""))
    print(f"\n通过 {passed}/{len(checks)}")
    companion.memory.close()
    shutil.rmtree(TMP, ignore_errors=True)
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    raise SystemExit(main())
