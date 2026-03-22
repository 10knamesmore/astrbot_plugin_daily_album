"""
astrbot_plugin_daily_album - 每日专辑推荐插件

每天定时向配置的群/私聊推送一张专辑推荐。
专辑来源可插拔：llm（纯 LLM）、web_search（联网+LLM）、script（用户自定义脚本）。
"""

from __future__ import annotations

import asyncio
import json
import random
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import cast

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.star import Context, Star, StarTools

from .sources import AlbumInfo, select_source

PLUGIN_NAME = "astrbot_plugin_daily_album"
HISTORY_FILE = "album_history.json"


def _dedup_key(album_name: str, artist: list[str]) -> str:
    """生成去重 key，忽略大小写和首尾空格"""
    artist_key = ",".join(a.strip().lower() for a in artist)
    return f"{album_name.strip().lower()}:{artist_key}"


class DailyAlbumPlugin(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context)
        self.config = config

        self._data_dir: Path = StarTools.get_data_dir(PLUGIN_NAME)
        self._history_path: Path = self._data_dir / HISTORY_FILE
        self._history: dict = self._load_history()

        self._lock = asyncio.Lock()
        self._cron_job_id: str | None = None

        asyncio.create_task(self._init())

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
        if self._cron_job_id:
            try:
                await self.ctx.cron_manager.delete_job(self._cron_job_id)
            except Exception:
                pass

    # -------------------------------------------------------------------------
    # 持久化
    # -------------------------------------------------------------------------

    def _load_history(self) -> dict:
        if self._history_path.exists():
            try:
                return json.loads(self._history_path.read_text(encoding="utf-8"))
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

    async def _setup_cron(self) -> None:
        push_time = self.config.get("push_time", "10:00")
        try:
            hour_str, minute_str = push_time.split(":")
            hour, minute = int(hour_str), int(minute_str)
        except Exception:
            logger.warning(
                f"[DailyAlbum] push_time 格式无效：{push_time!r}，使用 10:00"
            )
            hour, minute = 10, 0

        try:
            job = await self.ctx.cron_manager.add_basic_job(
                name=f"{PLUGIN_NAME}_daily",
                cron_expression=f"{minute} {hour} * * *",
                handler=self._daily_handler,
                description="每日专辑推荐",
                persistent=False,
            )
            self._cron_job_id = job.job_id
            logger.info(
                f"[DailyAlbum] 定时任务已注册，时间={hour:02d}:{minute:02d}，job_id={job.job_id}"
            )
        except Exception as e:
            logger.error(f"[DailyAlbum] 注册定时任务失败：{e}")

    async def _daily_handler(self, **_kwargs) -> None:
        today = datetime.now().strftime("%Y-%m-%d")
        if self._history.get("last_push_date") == today:
            logger.info("[DailyAlbum] 今日已推送，跳过（防重启重复）")
            return
        await self._run_recommend()

    # -------------------------------------------------------------------------
    # 核心推荐流程
    # -------------------------------------------------------------------------

    async def _run_recommend(self) -> None:
        async with self._lock:
            records = self._history.get("records", [])
            history_list = [
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

            prompt = self.config.get(
                "recommend_prompt",
                "请推荐一张值得深度聆听的经典或当代优秀专辑，涵盖各种音乐风格，注重艺术性和可听性。",
            )

            # 去重重试：rejected 列表追加到 history 末尾，让模型看到刚才被拒的专辑
            MAX_RETRIES = 3
            album = None
            rejected: list[AlbumInfo] = []
            for attempt in range(1, MAX_RETRIES + 1):
                source = select_source(self.ctx, self.config)
                candidate = await source.fetch(prompt, history_list + rejected)
                if not candidate:
                    logger.error("[DailyAlbum] 来源未能返回有效专辑，本次跳过")
                    return
                key = _dedup_key(candidate.album_name, candidate.artist)
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

            today = datetime.now().strftime("%Y-%m-%d")
            key = _dedup_key(album.album_name, album.artist)
            record = {
                **asdict(album),
                "date": today,
                "timestamp": datetime.now().isoformat(timespec="seconds"),
            }
            self._history.setdefault("records", []).append(record)
            self._history.setdefault("seen_keys", []).append(key)
            self._history["last_push_date"] = today
            self._save_history()

            await self._send_to_sessions(album)

    # -------------------------------------------------------------------------
    # 消息构建与发送
    # -------------------------------------------------------------------------

    async def _generate_text(self, album: AlbumInfo, umo: str) -> str:
        provider = self.ctx.get_using_provider()
        if not provider:
            return ""

        # 解析该会话当前生效的人格
        cid = await self.ctx.conversation_manager.get_curr_conversation_id(umo)
        conv_persona_id = None
        if cid:
            conv = await self.ctx.conversation_manager.get_conversation(umo, cid)
            if conv:
                conv_persona_id = getattr(conv, "persona_id", None)
        platform_name = umo.split(":", 1)[0]
        _, persona, _, _ = await self.ctx.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=conv_persona_id,
            platform_name=platform_name,
        )
        persona_prompt = (persona or {}).get("prompt", "")

        album_json = json.dumps(asdict(album), ensure_ascii=False)
        prompt = (
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

    async def _build_chain(self, album: AlbumInfo, umo: str) -> MessageChain:
        text = await self._generate_text(album, umo)
        if not text:
            today = datetime.now().strftime("%Y年%m月%d日")
            lines = [
                f"今日专辑推荐 | {today}",
                "",
                f"{album.album_name}  {' / '.join(album.artist)}",
            ]
            text = "\n".join(lines)
        chain = MessageChain()
        chain.message(text)
        return chain

    async def _is_target_album(
        self,
        candidate_name: str,
        candidate_artist: str,
        target_name: str,
        target_artist: list[str],
    ) -> bool:
        """用 LLM 判断搜索结果是否是目标专辑，返回 True/False。"""
        provider = self.ctx.get_using_provider()
        if not provider:
            return True  # 无 LLM 时直接信任第一个结果
        try:
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=(
                    f"目标专辑：《{target_name}》，艺术家：{', '.join(target_artist)}\n"
                    f"搜索结果：《{candidate_name}》，艺术家：{candidate_artist}\n\n"
                    "判断搜索结果是否是目标专辑（同一张专辑的 Deluxe Edition、Remastered、Anniversary Edition 等版本均视为匹配）。只回答 yes 或 no。"
                ),
                system_prompt="你是音乐数据核验助手，只输出 yes 或 no，不输出任何其他内容。",
            )
            answer = resp.completion_text.strip().lower()
            logger.debug(f"[DailyAlbum] LLM 核验结果：{answer!r}")
            return answer.startswith("y")
        except Exception as e:
            logger.warning(f"[DailyAlbum] LLM 核验失败，信任当前结果：{e}")
            return True

    async def _search_netease_song_id(
        self, album_name: str, artist: list[str]
    ) -> str | None:
        """搜索网易云专辑，返回专辑第一首歌的歌曲 ID；失败返回 None"""
        import aiohttp

        max_attempts = int(self.config.get("netease_search_max_attempts", 3))
        keyword = f"{album_name} {' '.join(artist)}"
        timeout = aiohttp.ClientTimeout(total=8)

        try:
            async with aiohttp.ClientSession(cookies={"appver": "2.0.2"}) as session:
                # 一次拉取多条，按序逐一核验
                async with session.post(
                    "http://music.163.com/api/search/get/web",
                    data={"s": keyword, "limit": max_attempts, "type": 10, "offset": 0},
                    timeout=timeout,
                ) as resp:
                    data = await resp.json(content_type=None)

                albums = data.get("result", {}).get("albums", [])
                if not albums:
                    logger.warning(
                        f"[DailyAlbum] 网易云专辑搜索无结果，keyword={keyword!r}"
                    )
                    return None

                for i, album in enumerate(albums):
                    album_id = album["id"]
                    album_title = album.get("name", "")
                    album_artist = album.get("artist", {}).get("name", "")
                    logger.info(
                        f"[DailyAlbum] 候选专辑 [{i + 1}/{len(albums)}] "
                        f"ID={album_id}，名称={album_title!r}，艺术家={album_artist!r}"
                    )

                    matched = await self._is_target_album(
                        album_title, album_artist, album_name, artist
                    )
                    if not matched:
                        logger.info("[DailyAlbum] LLM 判定不匹配，跳过")
                        continue

                    # 拉专辑详情取第一首歌
                    async with session.get(
                        f"http://music.163.com/api/album/{album_id}",
                        timeout=timeout,
                    ) as resp:
                        detail = await resp.json(content_type=None)

                    songs = detail.get("album", {}).get("songs", [])
                    if not songs:
                        logger.warning(
                            f"[DailyAlbum] 专辑 {album_id} 歌曲列表为空，继续尝试"
                        )
                        continue

                    sid = str(songs[0]["id"])
                    logger.info(
                        f"[DailyAlbum] 取专辑第一首歌 ID={sid}，歌名={songs[0].get('name', '')!r}"
                    )
                    return sid

                logger.warning(f"[DailyAlbum] {len(albums)} 条候选均未通过核验，放弃")
        except Exception as e:
            logger.warning(f"[DailyAlbum] 网易云搜索失败：{e}")
        return None

    async def _send_music_card(self, session_str: str, song_id: str) -> None:
        """发网易云音乐卡片（song_id 为歌曲 ID 字符串）"""
        from astrbot.core.platform.message_session import MessageSession
        from astrbot.core.platform.message_type import MessageType

        try:
            session = MessageSession.from_str(session_str)
        except Exception:
            return

        platform = None
        for p in self.ctx.platform_manager.platform_insts:
            if p.meta().id == session.platform_name:
                platform = p
                break
        if platform is None:
            return
        bot = getattr(platform, "bot", None)
        if bot is None:
            return

        payload = {
            "message": [{"type": "music", "data": {"type": "163", "id": song_id}}]
        }
        try:
            if session.message_type == MessageType.GROUP_MESSAGE:
                await bot.api.call_action(
                    "send_group_msg", group_id=int(session.session_id), **payload
                )
            else:
                await bot.api.call_action(
                    "send_private_msg", user_id=int(session.session_id), **payload
                )
            logger.info(
                f"[DailyAlbum] 音乐卡片已发送：song_id={song_id} → {session_str}"
            )
        except Exception as e:
            logger.warning(f"[DailyAlbum] 音乐卡片发送失败：{e}")

    async def _send_to_sessions(self, album: AlbumInfo) -> None:
        sessions: list[str] = self.config.get("target_sessions", [])
        if not sessions:
            logger.warning("[DailyAlbum] target_sessions 为空，跳过推送")
            return

        song_id = await self._search_netease_song_id(album.album_name, album.artist)
        chain = await self._build_chain(album, sessions[0])
        if not song_id:
            hint = await self._generate_not_found_hint(
                album.album_name, album.artist, sessions[0]
            )
            chain.message(hint)
        for session in sessions:
            try:
                await StarTools.send_message(session, chain)
                if song_id:
                    await self._send_music_card(session, song_id)
                logger.info(
                    f"[DailyAlbum] 已推送到 {session}：{album.album_name} / {album.artist}"
                )
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"[DailyAlbum] 发送到 {session} 失败：{e}")

    async def _generate_not_found_hint(
        self, album_name: str, artist: list[str], umo: str
    ) -> str:
        provider = self.ctx.get_using_provider()
        fallback = (
            f"\n未能在网易云找到「{album_name}」，"
            "可以去 Spotify / Apple Music / 网易云 / BandCamp 手动搜索哦～"
        )
        if not provider:
            return fallback
        _, persona, _, _ = await self.ctx.persona_manager.resolve_selected_persona(
            umo=umo,
            conversation_persona_id=None,
            platform_name=umo.split(":", 1)[0],
        )
        persona_prompt = (persona or {}).get("prompt", "")
        try:
            resp = await self.ctx.llm_generate(
                chat_provider_id=provider.meta().id,
                prompt=(
                    f"今日推荐的专辑是《{album_name}》，艺术家：{', '.join(artist)}。"
                    "在网易云音乐上没有找到这张专辑。"
                    "请用你自己的风格，说一句让用户可以去其他平台"
                    "自行搜索的提示。直接输出这句话，不要加任何前缀或解释。"
                ),
                system_prompt=persona_prompt or "你是一个热爱音乐的推荐者。",
            )
            return "\n" + resp.completion_text.strip()
        except Exception:
            return fallback

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
        persona_prompt = (persona or {}).get("prompt", "")
        try:
            now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            action = random.choice(
                [
                    "正在翻找今日值得一听的专辑",
                    "在音乐库里帮你挑一张好专辑",
                    "正在为你筛选今日的专辑推荐",
                    "正在从浩瀚的唱片里帮你找一张",
                    "稍微想了想，正在为你选一张专辑",
                ]
            )
            wait = random.choice(
                [
                    "稍等一下",
                    "请稍候",
                    "马上就来",
                    "等我一会儿",
                    "等一下下",
                ]
            )
            style = random.choice(
                [
                    "用你自己的风格说这件事",
                    "随性地表达",
                    "带点你的个性说出来",
                    "用你惯常的口吻说",
                ]
            )
            prompt = (
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
    async def cmd_today(self, event: AstrMessageEvent):
        """手动触发，推送到当前会话"""
        waiting = await self._generate_waiting_text(event.unified_msg_origin)
        yield event.plain_result(waiting)
        original_sessions = list(self.config.get("target_sessions", []))
        self.config["target_sessions"] = [event.unified_msg_origin]
        try:
            await self._run_recommend()
        finally:
            self.config["target_sessions"] = original_sessions
        event.stop_event()

    @filter.command("album_history")
    async def cmd_history(self, event: AstrMessageEvent):
        """查看最近 10 条推荐历史"""
        records = self._history.get("records", [])[-10:]
        if not records:
            yield event.plain_result("还没有推荐记录。")
            return
        lines = ["最近推荐："] + [
            f"{r['date']}  {r['album_name']} / {r['artist']}" for r in records
        ]
        yield event.plain_result("\n".join(lines))
        event.stop_event()
