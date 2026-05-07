"""
astrbot_plugin_daily_album - 每日专辑推荐插件

每天定时向配置的群/私聊推送一张专辑推荐。
专辑来源可插拔：llm（纯 LLM）、web_search（联网+LLM）、script（用户自定义脚本）。
"""

from __future__ import annotations

import asyncio
import json
import random
from collections.abc import AsyncGenerator
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, TypedDict, cast

from astrbot.api import llm_tool, logger
from astrbot.api.event import (
    AstrMessageEvent,
    MessageEventResult,
    filter,
)
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.db.po import CronJob

from .senders import SendContext, select_sender
from .sources import AlbumInfo, select_source
from .utils.netease import search_netease_song_id

PLUGIN_NAME = "astrbot_plugin_daily_album"
HISTORY_FILE = "album_history.json"


class RecordDict(TypedDict):
    """单条历史推荐记录（持久化形态）。"""

    album_name: str
    artist: list[str]
    year: str
    genre: list[str]
    cover_url: str
    description: str
    listen_tip: str
    date: str
    timestamp: str


class HistoryDict(TypedDict):
    """`album_history.json` 的整体结构。"""

    last_push_date: str
    records: list[RecordDict]
    seen_keys: list[str]


def _dedup_key(album_name: str, artist: list[str]) -> str:
    """生成去重 key，忽略大小写和首尾空格"""
    artist_key = ",".join(a.strip().lower() for a in artist)
    return f"{album_name.strip().lower()}:{artist_key}"


class DailyAlbumPlugin(Star):
    def __init__(self, context: Context, config: dict[str, Any]) -> None:
        super().__init__(context)
        self.config: dict[str, Any] = config

        self._data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self._history_path: Path = self._data_dir / HISTORY_FILE
        self._history: HistoryDict = self._load_history()

        self._lock: asyncio.Lock = asyncio.Lock()
        self._cron_job_id: str | None = None
        self._cron_job_name: str = f"{PLUGIN_NAME}_daily"

        self._bg_tasks: set[asyncio.Task[None]] = set()
        self._init_task: asyncio.Task[None] = asyncio.create_task(self._init())

    @property
    def ctx(self) -> Context:
        """返回具备完整类型提示的 Context。"""
        return cast(Context, self.context)

    # -------------------------------------------------------------------------
    # 初始化
    # -------------------------------------------------------------------------

    async def _init(self) -> None:
        await asyncio.sleep(5)  # 等待框架就绪
        await self._setup_cron()

    async def terminate(self) -> None:
        if not self._init_task.done():
            self._init_task.cancel()
            try:
                await self._init_task
            except (asyncio.CancelledError, Exception):
                pass
        # add_basic_job(persistent=False) 仍会向 DB 写一行，框架没有自动清理路径；
        # 不在 terminate 显式 delete，每次 reload / 重启都会留一行残骸长期累积。
        if self._cron_job_id:
            try:
                await self.ctx.cron_manager.delete_job(self._cron_job_id)
            except Exception as e:
                logger.warning(
                    f"[DailyAlbum] terminate 时删除 cron job 失败：{e}"
                )
            self._cron_job_id = None
        for t in list(self._bg_tasks):
            t.cancel()
        if self._bg_tasks:
            await asyncio.gather(*self._bg_tasks, return_exceptions=True)

    # -------------------------------------------------------------------------
    # 持久化
    # -------------------------------------------------------------------------

    def _load_history(self) -> HistoryDict:
        if self._history_path.exists():
            try:
                raw: dict[str, Any] = json.loads(
                    self._history_path.read_text(encoding="utf-8")
                )
                return cast(HistoryDict, raw)
            except Exception as e:
                logger.warning(f"[DailyAlbum] 读取历史文件失败：{e}，使用空历史")
        return {"last_push_date": "", "records": [], "seen_keys": []}

    def _save_history(self) -> None:
        try:
            self._history_path.write_text(
                json.dumps(self._history, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as e:
            logger.error(f"[DailyAlbum] 写入历史文件失败：{e}")

    # -------------------------------------------------------------------------
    # 定时任务
    # -------------------------------------------------------------------------

    async def _list_own_jobs(self) -> list[CronJob]:
        """列出所有 name 等于本插件常量的 basic job。"""
        try:
            jobs: list[CronJob] = await self.ctx.cron_manager.list_jobs("basic")
        except Exception as e:
            logger.warning(f"[DailyAlbum] 列出 cron job 失败：{e}")
            return []
        return [j for j in jobs if getattr(j, "name", None) == self._cron_job_name]

    async def _setup_cron(self) -> None:
        push_time: str = self.config.get("push_time", "10:00")
        try:
            hour_str, minute_str = push_time.split(":")
            hour, minute = int(hour_str), int(minute_str)
        except Exception:
            logger.warning(
                f"[DailyAlbum] push_time 格式无效：{push_time!r}，使用 10:00"
            )
            hour, minute = 10, 0
        cron_expression: str = f"{minute} {hour} * * *"

        # 进入前先把所有同名残留清干净——包括上一次没正常 terminate 留下的、
        # 或更早期版本累积下来的。delete_job 会同时清 scheduler + DB。
        # 不维护"幂等复用"分支：add_basic_job 本身就是一次轻量操作，
        # 简化为"清理 + 新建"让 DB 里同名行 ≤ 1 成为简单不变量。
        existing: list[CronJob] = await self._list_own_jobs()
        for j in existing:
            try:
                await self.ctx.cron_manager.delete_job(j.job_id)
                logger.info(f"[DailyAlbum] 清理残留 cron job：{j.job_id}")
            except Exception as e:
                logger.warning(f"[DailyAlbum] 删除残留 job {j.job_id} 失败：{e}")
        self._cron_job_id = None

        try:
            new_job: CronJob = await self.ctx.cron_manager.add_basic_job(
                name=self._cron_job_name,
                cron_expression=cron_expression,
                handler=self._daily_handler,
                description="每日专辑推荐",
                persistent=False,
            )
            self._cron_job_id = new_job.job_id
            logger.info(
                f"[DailyAlbum] 定时任务已注册，时间={hour:02d}:{minute:02d}，"
                f"job_id={new_job.job_id}"
            )
        except Exception as e:
            logger.error(f"[DailyAlbum] 注册定时任务失败：{e}")

    async def _daily_handler(self, **_kwargs: Any) -> None:
        today: str = datetime.now().strftime("%Y-%m-%d")
        if self._history.get("last_push_date") == today:
            logger.info("[DailyAlbum] 今日已推送，跳过（防重启重复）")
            return
        await self._run_recommend()

    # -------------------------------------------------------------------------
    # 核心推荐流程
    # -------------------------------------------------------------------------

    async def _run_recommend(
        self,
        *,
        target_sessions: list[str] | None = None,
        prompt_override: str | None = None,
    ) -> None:
        async with self._lock:
            records: list[RecordDict] = self._history.get("records", [])
            history_list: list[AlbumInfo] = [
                AlbumInfo(
                    album_name=r["album_name"],
                    artist=r["artist"],
                    year=r.get("year", ""),
                    genre=r.get("genre", []),
                    cover_url=r.get("cover_url", ""),
                    description=r.get("description", ""),
                    listen_tip=r.get("listen_tip", ""),
                )
                for r in records
            ]
            seen_keys: set[str] = set(self._history.get("seen_keys", []))

            prompt: str = prompt_override or self.config.get(
                "recommend_prompt",
                "请推荐一张值得深度聆听的经典或当代优秀专辑，涵盖各种音乐风格，注重艺术性和可听性。",
            )

            # 去重重试：rejected 列表追加到 history 末尾，让模型看到刚才被拒的专辑
            MAX_RETRIES: int = 3
            album: AlbumInfo | None = None
            rejected: list[AlbumInfo] = []
            for attempt in range(1, MAX_RETRIES + 1):
                source = select_source(self.ctx, self.config)
                candidate: AlbumInfo | None = await source.fetch(
                    prompt, history_list + rejected
                )
                if not candidate:
                    logger.error("[DailyAlbum] 来源未能返回有效专辑，本次跳过")
                    return
                key: str = _dedup_key(candidate.album_name, candidate.artist)
                if key not in seen_keys:
                    album = candidate
                    break
                logger.info(
                    f"[DailyAlbum] 命中重复专辑 {candidate.album_name}/{candidate.artist}，"
                    f"重新生成（{attempt}/{MAX_RETRIES}）"
                )
                rejected.append(candidate)

            if album is None:
                logger.error(
                    f"[DailyAlbum] 重试 {MAX_RETRIES} 次后仍返回重复专辑，本次跳过"
                )
                return

            today: str = datetime.now().strftime("%Y-%m-%d")
            final_key: str = _dedup_key(album.album_name, album.artist)
            record: RecordDict = cast(
                RecordDict,
                {
                    **asdict(album),
                    "date": today,
                    "timestamp": datetime.now().isoformat(timespec="seconds"),
                },
            )
            self._history.setdefault("records", []).append(record)
            self._history.setdefault("seen_keys", []).append(final_key)
            self._history["last_push_date"] = today
            self._save_history()

            await self._send_to_sessions(album, sessions_override=target_sessions)

    # -------------------------------------------------------------------------
    # 消息构建与发送
    # -------------------------------------------------------------------------

    async def _generate_text(self, album: AlbumInfo, umo: str) -> str:
        provider = self.ctx.get_using_provider()
        if not provider:
            return ""

        # 解析该会话当前生效的人格
        cid: str | None = await self.ctx.conversation_manager.get_curr_conversation_id(
            umo
        )
        conv_persona_id: str | None = None
        if cid:
            conv = await self.ctx.conversation_manager.get_conversation(umo, cid)
            if conv:
                conv_persona_id = getattr(conv, "persona_id", None)
        platform_name: str = umo.split(":", 1)[0]
        _, persona, _, _ = await self.ctx.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conv_persona_id,
            platform_name=platform_name,
        )
        persona_prompt: str = (persona or {}).get("prompt", "")

        album_json: str = json.dumps(asdict(album), ensure_ascii=False)
        prompt: str = (
            f"以下是今日推荐的专辑信息（JSON）：\n{album_json}\n\n"
            "请用你自己的风格写一段今日专辑推荐文案，要自然、有感染力，"
            "不要逐字复述字段，像是在跟朋友分享, 但是可以自然地说明包含发行时间, 风格等信息。直接输出文案，不要加任何前缀或解释。"
        )
        try:
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=prompt,
                system_prompt=persona_prompt or "你是一个热爱音乐的推荐者。",
            )
            return resp.completion_text.strip()
        except Exception as e:
            logger.warning(f"[DailyAlbum] 文案生成失败：{e}")
            return ""

    async def _build_text(self, album: AlbumInfo, umo: str) -> str:
        """生成推荐文案；LLM 不可用时回退到结构化展示。"""
        text: str = await self._generate_text(album, umo)
        if not text:
            today: str = datetime.now().strftime("%Y年%m月%d日")
            lines: list[str] = [
                f"今日专辑推荐 | {today}",
                "",
                f"{album.album_name}  {' / '.join(album.artist)}",
            ]
            text = "\n".join(lines)
        return text

    async def _send_to_sessions(
        self,
        album: AlbumInfo,
        *,
        sessions_override: list[str] | None = None,
    ) -> None:
        """Per-session orchestrator：解析平台 → 生成文案 → 调 sender → 写历史。

        网易云搜索在循环外做一次，结果广播给所有 sender；
        每条 session 的文案 per-session 生成（人格化），互不影响。
        """
        sessions: list[str] = sessions_override or self.config.get(
            "target_sessions", []
        )
        if not sessions:
            logger.warning("[DailyAlbum] target_sessions 为空，跳过推送")
            return

        # 所有 sender 共享的预查询：网易云首歌 ID（找不到为 None）
        netease_song_id: str | None = await search_netease_song_id(
            self.ctx, self.config, album.album_name, album.artist
        )

        write_history: bool = bool(self.config.get("record_history", True))

        for session_str in sessions:
            resolved = select_sender(self.ctx, session_str)
            if resolved is None:
                # warn 已在 select_sender 内部打印
                continue
            sender, platform, platform_type = resolved

            recommend_text: str = await self._build_text(album, session_str)

            sctx = SendContext(
                album=album,
                session_str=session_str,
                platform=platform,
                platform_type=platform_type,
                recommend_text=recommend_text,
                netease_song_id=netease_song_id,
                config=self.config,
                ctx=self.ctx,
            )

            try:
                result = await sender.send(sctx)
            except Exception as e:
                # sender 实现已自行兜底；这里防御式再 catch 一层
                logger.error(
                    f"[DailyAlbum] sender 未捕获异常 ({session_str}): {e}",
                    exc_info=True,
                )
                continue

            if not result.success:
                logger.error(
                    f"[DailyAlbum] 发送到 {session_str} 失败：{result.error}"
                )
                continue

            logger.info(
                f"[DailyAlbum] 已推送到 {session_str}（{platform_type}）："
                f"{album.album_name} / {album.artist}"
            )

            if write_history:
                await self._record_to_history(
                    session_str, album, result.assistant_text_for_history
                )

            await asyncio.sleep(1)

    async def _record_to_history(
        self, umo: str, album: AlbumInfo, assistant_text: str
    ) -> None:
        """把今日推荐写入指定 session 的对话历史，供后续 LLM 上下文引用。"""
        try:
            cid: str | None = (
                await self.ctx.conversation_manager.get_curr_conversation_id(umo)
            )
            if not cid:
                cid = await self.ctx.conversation_manager.new_conversation(umo)
            artist_str: str = " / ".join(album.artist)
            user_message: dict[str, str] = {
                "role": "user",
                "content": (
                    "[系统标记：此条由 daily_album 插件的每日定时任务触发，"
                    "不是用户实际发送的消息。仅用于让你记住本日的专辑推荐内容，"
                    "便于后续被用户问起时引用。]"
                ),
            }
            assistant_message: dict[str, str] = {
                "role": "assistant",
                "content": (
                    f"今日推荐《{album.album_name}》——{artist_str}。\n{assistant_text}"
                ),
            }
            await self.ctx.conversation_manager.add_message_pair(
                cid, user_message, assistant_message
            )
            logger.info(
                f"[DailyAlbum] 已写入会话历史 → {umo} (cid={cid})"
            )
        except Exception as e:
            logger.warning(f"[DailyAlbum] 写入会话历史失败 ({umo}): {e}")

    # -------------------------------------------------------------------------
    # 命令
    # -------------------------------------------------------------------------

    async def _generate_waiting_text(self, umo: str) -> str:
        provider = self.ctx.get_using_provider()
        if not provider:
            return "正在生成今日专辑推荐，请稍候..."
        _, persona, _, _ = await self.ctx.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=None,
            platform_name=umo.split(":", 1)[0],
        )
        persona_prompt: str = (persona or {}).get("prompt", "")
        try:
            now: str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action: str = random.choice(
                [
                    "正在翻找今日值得一听的专辑",
                    "在音乐库里帮你挑一张好专辑",
                    "正在为你筛选今日的专辑推荐",
                    "正在从浩瀚的唱片里帮你找一张",
                    "稍微想了想，正在为你选一张专辑",
                ]
            )
            wait: str = random.choice(
                [
                    "稍等一下",
                    "请稍候",
                    "马上就来",
                    "等我一会儿",
                    "等一下下",
                ]
            )
            style: str = random.choice(
                [
                    "用你自己的风格说这件事",
                    "随性地表达",
                    "带点你的个性说出来",
                    "用你惯常的口吻说",
                ]
            )
            prompt: str = (
                f"现在是 {now}。{action}，需要让用户{wait}。"
                f"请{style}，直接输出这句话，不要加任何前缀或解释。"
                f"不要在这一部分推荐任何音乐"
            )
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=prompt,
                system_prompt=persona_prompt or "你是一个热爱音乐的推荐者。",
            )
            return resp.completion_text.strip()
        except Exception:
            return "正在生成今日专辑推荐，请稍候..."

    @filter.command("album_today")
    async def cmd_today(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """手动触发，推送到当前会话；可附带参数覆盖推荐偏好，如 /album_today 推荐一张emo专辑"""
        waiting: str = await self._generate_waiting_text(event.unified_msg_origin)
        yield event.plain_result(waiting)
        # 命令后的文本作为临时 prompt
        custom_prompt: str = event.message_str.removeprefix("album_today").strip()
        await self._run_recommend(
            target_sessions=[event.unified_msg_origin],
            prompt_override=custom_prompt or None,
        )
        event.stop_event()

    @llm_tool("recommend_album")
    async def tool_recommend_album(
        self, event: AstrMessageEvent, prompt: str = ""
    ) -> str:
        """推荐一张专辑并发送到当前会话。当用户希望获得音乐或专辑推荐时调用此工具。

        Args:
            prompt(string): 可选。描述期望的专辑风格、流派、情绪或年代等偏好。留空则使用默认推荐偏好。
        """
        # 推荐流水线本身可达 1~2 分钟，超过 agent 工具调用 120s 硬超时；
        # 这里立即返回 ack，把真正的推荐放到后台任务里跑，由 sender 自行送达。
        target: str = event.unified_msg_origin
        eff_prompt: str | None = prompt or None

        async def _bg() -> None:
            try:
                await self._run_recommend(
                    target_sessions=[target],
                    prompt_override=eff_prompt,
                )
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error(
                    f"[DailyAlbum] tool 后台推荐任务失败：{e}", exc_info=True
                )

        task: asyncio.Task[None] = asyncio.create_task(_bg())
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)

        return "已开始为你挑选今日专辑，结果稍后会发到当前会话。"

    @filter.command("album_history")
    async def cmd_history(
        self, event: AstrMessageEvent
    ) -> AsyncGenerator[MessageEventResult, None]:
        """查看最近 10 条推荐历史"""
        records: list[RecordDict] = self._history.get("records", [])[-10:]
        if not records:
            yield event.plain_result("还没有推荐记录。")
            return
        lines: list[str] = ["最近推荐："] + [
            f"{r['date']}  {r['album_name']} / {r['artist']}" for r in records
        ]
        yield event.plain_result("\n".join(lines))
        event.stop_event()
