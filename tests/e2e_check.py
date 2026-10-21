"""hermes-time-context 的端到端验证脚本。

不 mock Hermes：用真实的 PluginManager 发现/加载插件，再调用真实的
apply_llm_request_middleware / invoke_hook 走一遍请求链。

跑法（在你的 hermes-agent 源码目录下执行，或用 HERMES_REPO 指定）：
    HERMES_REPO=~/hermes-agent python tests/e2e_check.py

注意：会临时把 HERMES_HOME 指到一个 temp 目录，插件从本仓库复制进去，
不会动真实配置。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

PLUGIN_SRC = Path(__file__).resolve().parents[1]
REPO = Path(os.environ.get("HERMES_REPO") or Path.home() / "hermes-agent").expanduser().resolve()

if not (REPO / "hermes_cli").is_dir():
    raise SystemExit(
        f"找不到 hermes-agent 源码：{REPO}\n"
        "请设置 HERMES_REPO 指向 hermes-agent 仓库根目录，例如：\n"
        "  HERMES_REPO=~/hermes-agent python tests/e2e_check.py"
    )

sys.path.insert(0, str(REPO))


def main() -> int:
    home = Path(tempfile.mkdtemp(prefix="hermes-time-context-"))
    os.environ["HERMES_HOME"] = str(home)
    (home / "plugins").mkdir(parents=True, exist_ok=True)
    dst = home / "plugins" / "time-context"
    shutil.copytree(PLUGIN_SRC, dst, ignore=shutil.ignore_patterns("__pycache__", "tests", "*.md"))

    # 插件是 opt-in 的：写一份最小 config.yaml 把 time-context 放进 plugins.enabled
    (home / "config.yaml").write_text(
        "plugins:\n  enabled:\n    - time-context\n", encoding="utf-8"
    )

    from hermes_cli.plugins import discover_plugins, get_plugin_manager, invoke_hook
    from hermes_cli.middleware import apply_llm_request_middleware

    discover_plugins(force=True)
    mgr = get_plugin_manager()
    loaded = {k: v for k, v in mgr._plugins.items() if "time-context" in k}
    print("== discovered ==")
    for key, rec in loaded.items():
        print(f"  {key}: enabled={rec.enabled} error={getattr(rec, 'error', None)}")
    if not loaded:
        print("!! plugin not discovered")
        for key, rec in mgr._plugins.items():
            if "time" in key:
                print("   near-miss:", key, rec.enabled, getattr(rec, "error", None))
        return 1

    from hermes_cli.plugins import has_middleware
    print("has llm_request middleware:", has_middleware("llm_request"))
    assert has_middleware("llm_request"), "middleware 未注册"

    # 模拟真实请求：system + 历史 + 本轮 user（带 cache_control 的文本块形态）
    base = {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "you are helpful"},
            {"role": "user", "content": [{"type": "text", "text": "你好", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": "你好，有什么可以帮你？"},
            {"role": "user", "content": "昨天那个脚本跑完了吗"},
        ],
        "temperature": 0.7,
    }
    sid = "test-session-1"

    r1 = apply_llm_request_middleware(json.loads(json.dumps(base)), session_id=sid, model="deepseek-v4-flash")
    print("\n== turn 1 ==")
    print("changed:", r1.changed, "trace:", r1.trace)
    msgs = r1.payload["messages"]
    for m in msgs:
        print("  ", m["role"], "->", repr(m["content"])[:110])
    assert msgs[-1]["content"].startswith("【当前时间】"), "末条 user 未注入"
    assert msgs[-1]["content"].endswith("昨天那个脚本跑完了吗")
    assert msgs[0]["content"] == "you are helpful", "system 被改动了！"
    assert msgs[1]["content"][0]["text"] == "你好", "历史被改动了！"
    assert msgs[1]["content"][0].get("cache_control"), "cache_control 丢了"

    # 第二轮：同一 session 应带上间隔（把 last_seen 往回拨 3 小时模拟）
    import hermes_plugins.time_context as tc  # noqa: F401  (加载后取模块)
    mod = sys.modules.get("hermes_plugins.time_context")
    assert mod is not None, "plugin module 未加载为 hermes_plugins.time_context"
    from datetime import datetime, timedelta, timezone
    mod._last_seen[sid] = datetime.now(timezone.utc) - timedelta(days=1, hours=3)

    second = json.loads(json.dumps(base))
    second["messages"][-1]["content"] = "继续吧"
    r2 = apply_llm_request_middleware(second, session_id=sid)
    print("\n== turn 2 (间隔 27 小时) ==")
    print("  ", repr(r2.payload["messages"][-1]["content"])[:160])
    assert "距上一轮：1 天" in r2.payload["messages"][-1]["content"]

    # 工具结果轮：role=user 且全是 tool_result 块 → 不注入
    tool_turn = {
        "messages": [
            {"role": "user", "content": "跑一下"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function",
                                                                   "function": {"name": "terminal", "arguments": "{}"}}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "done"}]},
        ]
    }
    r3 = apply_llm_request_middleware(tool_turn, session_id=sid)
    print("\n== tool_result turn ==")
    print("changed:", r3.changed)
    assert r3.changed is False, "tool_result 轮不应注入"

    # 观测 hook
    invoke_hook("post_api_request", usage={"prompt_tokens": 1000, "cache_read_tokens": 900,
                                          "output_tokens": 10}, model="deepseek-v4-flash")
    print("\nstats:", mod._stats)

    # 斜杠命令
    print("\n== /time-context preview ==")
    print(mod._handle_command("preview"))
    print("\n== /time-context stats ==")
    print(mod._handle_command("stats"))

    print("\nALL CHECKS PASSED")
    shutil.rmtree(home, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
