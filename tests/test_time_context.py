"""time-context 插件的单元测试（不依赖 Hermes 运行时）。

跑法（在仓库根目录）：
    python -m pytest tests/test_time_context.py -q
或直接用标准库、不依赖 pytest：
    python tests/test_time_context.py
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

PLUGIN_INIT = Path(__file__).resolve().parents[1] / "__init__.py"


def _load():
    spec = importlib.util.spec_from_file_location("time_context_under_test", PLUGIN_INIT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


tc = _load()
UTC = timezone.utc
NOW = datetime(2026, 10, 2, 2, 30, tzinfo=UTC)  # 北京 10:30 周五


def test_prefix_no_gap():
    out = tc.build_prefix(NOW, None, {**tc._DEFAULTS})
    assert out == "【当前时间】2026-10-02 10:30 周五（Asia/Shanghai）", out


def test_prefix_gap_below_threshold():
    out = tc.build_prefix(NOW, timedelta(minutes=5), {**tc._DEFAULTS})
    assert "距上一轮" not in out


def test_prefix_gap_day():
    out = tc.build_prefix(NOW, timedelta(days=1, hours=3), {**tc._DEFAULTS})
    assert out.endswith("｜距上一轮：1 天 3 小时"), out


def test_prefix_gap_minutes():
    assert tc._format_gap(timedelta(minutes=7)) == "7 分钟"
    assert tc._format_gap(timedelta(seconds=30)) == "刚刚"
    assert tc._format_gap(timedelta(hours=2)) == "2 小时"
    assert tc._format_gap(timedelta(days=2)) == "2 天"


def test_inject_string_and_parts():
    assert tc._inject("hi", "P") == "P\nhi"
    parts = [{"type": "text", "text": "hi", "cache_control": {"type": "ephemeral"}}]
    out = tc._inject(parts, "P")
    assert out[0]["text"] == "P\nhi"
    assert out[0]["cache_control"] == {"type": "ephemeral"}
    assert parts[0]["text"] == "hi", "必须不改动入参（内存里的原始消息）"
    img_only = [{"type": "image_url", "image_url": {"url": "x"}}]
    out2 = tc._inject(img_only, "P")
    assert out2[0] == {"type": "text", "text": "P"} and len(out2) == 2


def test_should_skip_tool_result():
    assert tc._should_skip_messages([])
    assert tc._should_skip_messages([{"role": "assistant", "content": "x"}])
    assert tc._should_skip_messages(
        [{"role": "user", "content": [{"type": "tool_result", "content": "ok"}]}])
    assert not tc._should_skip_messages([{"role": "user", "content": "ok"}])


def test_middleware_only_touches_last_user():
    tc._cfg = dict(tc._DEFAULTS)
    tc._ctx = None  # 无 state，走首轮路径
    req = {"messages": [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "a"},
        {"role": "user", "content": "new"},
    ]}
    out = tc.inject_time(request=req, session_id="s")
    msgs = out["request"]["messages"]
    assert msgs[1]["content"] == "old", "历史消息不许动"
    assert msgs[0]["content"] == "SYS", "system 不许动"
    assert msgs[3]["content"].endswith("\nnew")
    assert req["messages"][3]["content"] == "new", "入参原请求不许被改"


def test_middleware_disabled():
    tc._cfg = {**tc._DEFAULTS, "enabled": False}
    assert tc.inject_time(request={"messages": [{"role": "user", "content": "x"}]}, session_id="s") is None
    tc._cfg = dict(tc._DEFAULTS)


def test_middleware_never_raises():
    tc._cfg = dict(tc._DEFAULTS)
    assert tc.inject_time(request="not a dict") is None
    assert tc.inject_time(request={"messages": "nope"}) is None


if __name__ == "__main__":
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            try:
                fn()
                print("ok  ", name)
            except AssertionError as exc:
                failed += 1
                print("FAIL", name, exc)
    sys.exit(1 if failed else 0)
