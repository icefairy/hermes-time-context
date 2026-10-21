"""hermes time-context 插件 — 让模型感知现实时间与跨轮间隔。

问题：多轮会话里每条消息本身不带时间，用户隔一天甚至一周再接着说，
模型仍按"刚才"的连贯语义理解，容易答非所问。

方案：不碰系统提示词、不改持久化历史，只在**发往模型的本轮请求**上，
给最后一条 user 文本前面加一行时间标记（可含距上一轮的间隔）：

    【当前时间】2026-10-02 10:30（周五，Asia/Shanghai）｜距上一轮：1 天 3 小时

为什么这么放（缓存角度）：
- 时间前缀只落在"最后一条 user 消息"里。历史消息逐字节不变，上游前缀缓存
  仍然命中，失效的只有"新增那一轮"几十个 token 的 prefill。
- 绝不能放进 system 提示词：那会让每次请求的缓存前缀整体变动，命中率归零。
  Hermes 的插件文档也是基于同一理由把注入点定在"当前轮 user 消息"。
- 关掉 ``only_last_user_message`` 就会改写全部 user 消息，历史前缀作废 ——
  这是为什么它默认开启。

实现要点（均为框架公开面）：
- ``llm_request`` 中间件：改写 provider 请求 kwargs 里的 ``messages``。
  在 prompt-cache 装饰之后、``pre_api_request`` 之前执行，所以 ``cache_control``
  断点已经就位，只管替换字符串文本。
- ``post_api_request`` observer hook：读 ``usage`` 里的缓存桶统计命中率，
  验证这层改动确实没伤缓存。
- ``/time-context`` 斜杠命令：状态 / 试算 / 统计。
- 间隔时间记录在插件自己的 state 里（按 session_id），不解析 Hermes 历史里的
  ``timestamp`` 字段 —— 那是持久化内部字段，插件不应依赖。

失败一律静默降级：任何异常都不影响正常对话。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

PROVIDER_TIMEOUT = 1.0  # 间隔记录的读写在请求路径上，读走内存缓存，写走短超时

# 配置键（manifest config_schema 声明的默认值）
_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "format": "【当前时间】{datetime} {weekday}（{tz}）",
    "date_format": "%Y-%m-%d %H:%M",
    "tz": "Asia/Shanghai",
    "gap_enabled": True,
    "gap_threshold_minutes": 30,
    "inject_on_first_turn": True,
    "only_last_user_message": True,
    "min_gap_tokens_alert": 0.0,
}

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")

# ── 模块级状态（进程内）────────────────────────────────────────────────
_cfg: dict[str, Any] = dict(_DEFAULTS)
_ctx = None                 # PluginContext，用于按需读配置 / 写 state
_last_seen: dict[str, datetime] = {}   # session_id -> 上一轮的 UTC 时间
_last_seen_loaded = False
_stats: dict[str, Any] = {"requests": 0, "prompt_tokens": 0, "cache_read": 0, "miss_note": 0}


# ── 配置 ────────────────────────────────────────────────────────────────
def _load_config(ctx) -> dict[str, Any]:
    """读一次插件配置；缺项用 schema 默认值兜底，非法值回退默认。"""
    out = dict(_DEFAULTS)
    for key, default in _DEFAULTS.items():
        try:
            value = ctx.get_config(key, default)
        except Exception:
            value = default
        if value is None or type(value) is not type(default):
            # bool/int/float/str 严格对齐，避免 "false" 字符串被当成真值
            out[key] = default
        else:
            out[key] = value
    return out


def _tzinfo(name: str):
    """IANA 时区；不可用时退回固定 UTC+8（Windows 上 tzdata 可能缺失）。"""
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name or "UTC")
    except Exception:
        return timezone.utc if (name or "UTC").upper() == "UTC" else timezone(timedelta(hours=8))


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


# ── 间隔记录 ────────────────────────────────────────────────────────────
def _load_last_seen(ctx) -> None:
    """从插件 state 载入上次见过的会话时间（进程重启后续上）。"""
    global _last_seen_loaded
    if _last_seen_loaded:
        return
    _last_seen_loaded = True
    try:
        raw = ctx.state.get("last_seen", {})
        if isinstance(raw, dict):
            for sid, iso in raw.items():
                try:
                    _last_seen[str(sid)] = datetime.fromisoformat(str(iso))
                except Exception:
                    continue
    except Exception:
        pass


def _store_last_seen(ctx, session_id: str, when: datetime) -> None:
    """只保留最近 200 个会话，避免 state 无限增长。"""
    _last_seen[session_id] = when
    if len(_last_seen) > 200:
        for sid, _ in sorted(_last_seen.items(), key=lambda kv: kv[1])[: len(_last_seen) - 200]:
            _last_seen.pop(sid, None)
    try:
        ctx.state.set("last_seen", {sid: dt.isoformat() for sid, dt in _last_seen.items()})
    except Exception as exc:  # 状态写失败不影响本轮请求
        logger.debug("time-context: state write failed: %s", exc)


# ── 文本渲染 ────────────────────────────────────────────────────────────
def _format_gap(delta: timedelta) -> str:
    """把间隔渲染成人话：<1 分钟 → 刚刚；否则按 天/小时/分钟 取最高两档。"""
    secs = int(delta.total_seconds())
    if secs < 60:
        return "刚刚"
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    minutes = rem // 60
    if days:
        return f"{days} 天 {hours} 小时" if hours else f"{days} 天"
    if hours:
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    return f"{minutes} 分钟"


def build_prefix(now: datetime, gap: timedelta | None, cfg: dict[str, Any] | None = None) -> str:
    """构造时间前缀（不含结尾换行）。纯函数，便于测试与 /time-context preview。"""
    cfg = cfg or _cfg
    local = now.astimezone(_tzinfo(str(cfg["tz"])))
    try:
        dt_text = local.strftime(str(cfg["date_format"]))
    except Exception:
        dt_text = local.strftime("%Y-%m-%d %H:%M")
    prefix = str(cfg["format"]).format(
        datetime=dt_text,
        date=local.strftime("%Y-%m-%d"),
        time=local.strftime("%H:%M:%S"),
        weekday=_WEEKDAYS[local.weekday()],
        tz=str(cfg["tz"]),
    )
    threshold = max(0, int(cfg["gap_threshold_minutes"]))
    if cfg["gap_enabled"] and gap is not None and gap >= timedelta(minutes=threshold):
        prefix += f"｜距上一轮：{_format_gap(gap)}"
    return prefix


def _inject(content: Any, prefix: str) -> Any:
    """把前缀加到一条 user 消息的 content 上，保持原有形状。

    - 字符串：前缀 + 换行 + 原文
    - 内容块数组：并入**第一个 text 块**（不新增块，避免多出一个 cache_control 位置）
    - 其他形状（None / 非结构化）：原样返回，宁可不加也不破坏
    """
    if isinstance(content, str):
        return f"{prefix}\n{content}" if content else prefix
    if isinstance(content, list) and content:
        parts = list(content)
        for i, part in enumerate(parts):
            if isinstance(part, dict) and part.get("type") == "text" and isinstance(part.get("text"), str):
                parts[i] = {**part, "text": f"{prefix}\n{part['text']}" if part["text"] else prefix}
                return parts
        # 没有 text 块（纯图片）：在最前面插一个 text 块
        return [{"type": "text", "text": prefix}, *parts]
    return content


def _should_skip_messages(messages: list) -> bool:
    """出现下列情况时不注入，避免破坏 agent 内部语义：
    - Claude 工具结果：``role=user`` 且整条 content 都是 ``tool_result`` 块
    - 其他 agent 合成的同义 user 行由 only_last_user_message 兜底（只改最后一条）
    """
    if not messages:
        return True
    last = messages[-1]
    if not isinstance(last, dict) or last.get("role") != "user":
        return True
    content = last.get("content")
    if isinstance(content, list) and content:
        if all(isinstance(p, dict) and p.get("type") == "tool_result" for p in content):
            return True
    return False


# ── 中间件：本轮请求注入 ────────────────────────────────────────────────
def inject_time(request: Any = None, original_request: Any = None, session_id: str = "", **kwargs: Any) -> Any:
    """``llm_request`` 中间件入口。返回 ``{"request": {...}}`` 或 None（不改）。"""
    try:
        cfg = _cfg
        if not cfg.get("enabled", True) or not isinstance(request, dict):
            return None
        messages = request.get("messages")
        if not isinstance(messages, list) or _should_skip_messages(messages):
            return None

        ctx = _ctx
        sid = str(session_id or "")
        now = _now_utc()

        gap = None
        is_first = False
        if ctx is not None and sid:
            _load_last_seen(ctx)
            prev = _last_seen.get(sid)
            if prev is None:
                is_first = True
            else:
                gap = now - prev
        if is_first and not cfg.get("inject_on_first_turn", True):
            return None

        prefix = build_prefix(now, gap, cfg)

        if cfg.get("only_last_user_message", True):
            targets = [len(messages) - 1]
        else:
            targets = [i for i, m in enumerate(messages)
                       if isinstance(m, dict) and m.get("role") == "user"]

        new_messages = list(messages)
        changed = False
        for idx in targets:
            msg = new_messages[idx]
            if not isinstance(msg, dict) or msg.get("role") != "user":
                continue
            injected = _inject(msg.get("content"), prefix)
            if injected is not msg.get("content"):
                new_messages[idx] = {**msg, "content": injected}
                changed = True
        if not changed:
            return None

        if ctx is not None and sid:
            _store_last_seen(ctx, sid, now)

        new_request = dict(request)
        new_request["messages"] = new_messages
        logger.debug("time-context: inject [%s] gap=%s", prefix, gap)
        return {"request": new_request, "source": "time-context", "reason": "inject local time"}
    except Exception as exc:  # 永不打断对话
        logger.debug("time-context: inject skipped: %s", exc)
        return None


# ── 观测：缓存命中率 ────────────────────────────────────────────────────
def observe_usage(usage: Any = None, model: str = "", **kwargs: Any) -> None:
    """``post_api_request`` observer：统计 prompt/cache_read，验证缓存未被伤到。"""
    try:
        if not isinstance(usage, dict):
            return
        prompt = int(usage.get("prompt_tokens") or 0)
        cache_read = int(usage.get("cache_read_tokens") or 0)
        _stats["requests"] += 1
        _stats["prompt_tokens"] += prompt
        _stats["cache_read"] += cache_read
        alert = float(_cfg.get("min_gap_tokens_alert") or 0)
        if alert > 0 and prompt > 0 and (prompt - cache_read) / prompt > alert:
            _stats["miss_note"] += 1
            logger.info(
                "time-context: 缓存未命中偏高 model=%s prompt=%d cache_read=%d (未命中 %.0f%%)",
                model, prompt, cache_read, 100.0 * (prompt - cache_read) / prompt,
            )
    except Exception:
        pass


def on_session_end(session_id: str = "", **kwargs: Any) -> None:
    """会话结束不清理间隔记录：下次接着说仍然要算"隔了多久"。只做无操作占位。"""
    return None


# ── 斜杠命令 ────────────────────────────────────────────────────────────
def _handle_command(raw_args: str) -> str | None:
    """/time-context [status|preview|stats|test [分钟]]"""
    arg = (raw_args or "").strip().lower()
    if arg in ("", "status"):
        cfg = _cfg
        tz_ok = True
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo(str(cfg["tz"]))
        except Exception:
            tz_ok = False
        return "\n".join([
            "time-context 插件状态",
            f"  启用：{'是' if cfg['enabled'] else '否'}",
            f"  模板：{cfg['format']}",
            f"  时区：{cfg['tz']}{'' if tz_ok else '（zoneinfo 不可用，回退 UTC+8）'}",
            f"  间隔说明：{'开' if cfg['gap_enabled'] else '关'}，阈值 {cfg['gap_threshold_minutes']} 分钟",
            f"  只改最后一条 user 消息：{'是' if cfg['only_last_user_message'] else '否（会破坏历史缓存！）'}",
            f"  首轮注入：{'是' if cfg['inject_on_first_turn'] else '否'}",
            f"  已记录会话数：{len(_last_seen)}",
        ])
    if arg == "preview":
        now = _now_utc()
        return "\n".join([
            "当前会注入的前缀：",
            "  " + build_prefix(now, None),
            "  " + build_prefix(now, timedelta(hours=1, minutes=5)),
            "  " + build_prefix(now, timedelta(days=1, hours=3)),
            "",
            "（依次为：无间隔 / 隔 1 小时 / 隔 1 天，后者仅在超过阈值时出现）",
        ])
    if arg == "stats":
        n = _stats["requests"] or 1
        prompt = _stats["prompt_tokens"] or 1
        return "\n".join([
            "time-context 缓存观测（本进程累计）",
            f"  请求数：{_stats['requests']}",
            f"  prompt tokens：{_stats['prompt_tokens']}",
            f"  命中缓存 tokens：{_stats['cache_read']}（{100.0 * _stats['cache_read'] / prompt:.1f}%）",
            f"  未命中偏高告警次数：{_stats['miss_note']}",
            "  注：命中率取决于上游与历史长度，插件本身只改最后一条消息。",
        ])
    if arg.startswith("test"):
        parts = arg.split()
        minutes = 0
        if len(parts) > 1:
            try:
                minutes = int(float(parts[1]))
            except Exception:
                minutes = 0
        gap = timedelta(minutes=minutes) if minutes else None
        return "试算：" + build_prefix(_now_utc(), gap)
    return "用法：/time-context [status|preview|stats|test <间隔分钟>]"


# ── 注册 ────────────────────────────────────────────────────────────────
def register(ctx) -> None:
    global _ctx, _cfg
    _ctx = ctx
    try:
        _cfg = _load_config(ctx)
    except Exception:
        _cfg = dict(_DEFAULTS)

    ctx.register_middleware("llm_request", inject_time)
    ctx.register_hook("post_api_request", observe_usage)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_command(
        "time-context",
        handler=_handle_command,
        description="时间上下文插件：状态 / 试算 / 缓存统计",
        args_hint="[status|preview|stats|test <分钟>]",
    )
    logger.info("time-context plugin registered (enabled=%s, tz=%s)", _cfg["enabled"], _cfg["tz"])


__all__ = ["register", "build_prefix", "inject_time", "observe_usage"]
