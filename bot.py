import json
import logging
import os
import asyncio
import re
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

try:
    import yt_dlp
except Exception:
    yt_dlp = None


load_dotenv()
TOKEN = os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTROL_CHANNEL_ID = int(os.getenv("CONTROL_CHANNEL_ID", "0") or 0)
CONTROL_CHANNEL_NAME = os.getenv("CONTROL_CHANNEL_NAME", "voice-control")
STATE_FILE = Path(os.getenv("STATE_FILE", "panel_state.json"))
FFMPEG_PATH = os.getenv("FFMPEG_PATH", "ffmpeg")
DEFAULT_VOLUME = float(os.getenv("DEFAULT_VOLUME", "0.55") or 0.55)
COOKIE_FILE = os.getenv("COOKIE_FILE", "cookies.txt")
MUSIC_CHANNEL_ID = int(os.getenv("MUSIC_CHANNEL_ID", "0") or 0)


logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("voice_panel_bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True
intents.message_content = True

EMOJI = {
    "users": "<:users:1494432452557406368>",
    "user": "<:user:1494432427056304438>",
    "unlock": "<:unloc:1494433052976349234>",
    "undeafen": "<:undeafen:1494432366398017828>",
    "deafen": "<:deafen:1494432311226142880>",
    "kick": "<:kick_door:1494432285267591248>",
    "sparkle": "<:sparkd:1494433132353687814>",
    "settings": "<:settings:1494432232994115644>",
    "mute": "<:mte:1494433294526447728>",
    "mic": "<:mic:1494432190681845951>",
    "menu": "<:menu:1494432167646724196>",
    "lock": "<:loc:1494433095959580724>",
    "leader": "<:leader_crown:1494432117269070106>",
    "loud": "<:hearbro:1494439457829556314>",
}


def human_duration(seconds: int) -> str:
    if seconds <= 0:
        return "LIVE"
    minutes, sec = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02}:{sec:02}"
    return f"{minutes}:{sec:02}"

URL_RE = re.compile(r"^https?://", re.IGNORECASE)


def looks_like_url(value: str) -> bool:
    return bool(URL_RE.match(value.strip()))



@dataclass
class RoomState:
    channel_id: int
    leader_id: int
    join_order: List[int] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    panel_message_id: Optional[int] = None

    def add_member(self, member_id: int) -> None:
        if member_id in self.join_order:
            self.join_order.remove(member_id)
        self.join_order.append(member_id)

    def remove_member(self, member_id: int) -> None:
        if member_id in self.join_order:
            self.join_order.remove(member_id)

    def pick_next_leader(self, present_ids: List[int]) -> Optional[int]:
        for member_id in self.join_order:
            if member_id in present_ids:
                return member_id
        return present_ids[0] if present_ids else None


@dataclass
class Track:
    title: str
    url: str
    webpage_url: str
    duration: int
    requester_id: int
    thumbnail: Optional[str] = None


class GuildMusicState:
    def __init__(self, bot_ref: "VoiceRoomBot", guild_id: int) -> None:
        self.bot_ref = bot_ref
        self.guild_id = guild_id
        self.queue: Deque[Track] = deque()
        self.current: Optional[Track] = None
        self.volume: float = DEFAULT_VOLUME
        self.text_channel_id: Optional[int] = None
        self.announce_channel_id: Optional[int] = MUSIC_CHANNEL_ID or None
        self.lock = asyncio.Lock()
        self.is_looping: bool = False
        self.panel_message_id: Optional[int] = None

    def get_voice_client(self) -> Optional[discord.VoiceClient]:
        guild = self.bot_ref.get_guild(self.guild_id)
        return guild.voice_client if guild else None

    def queue_preview(self, limit: int = 5) -> str:
        if not self.queue:
            return "Очередь пустая"
        lines = []
        for i, track in enumerate(list(self.queue)[:limit], start=1):
            lines.append(f"`{i}.` {track.title} · `{human_duration(track.duration)}`")
        if len(self.queue) > limit:
            lines.append(f"… и ещё **{len(self.queue) - limit}**")
        return "\n".join(lines)

    async def connect_to(self, voice_channel: discord.VoiceChannel | discord.StageChannel) -> discord.VoiceClient:
        existing = self.get_voice_client()
        if existing and existing.is_connected():
            if existing.channel != voice_channel:
                await existing.move_to(voice_channel)
            return existing
        return await voice_channel.connect()

    async def stop(self) -> None:
        vc = self.get_voice_client()
        self.queue.clear()
        self.current = None
        if vc and vc.is_connected():
            vc.stop()
            await vc.disconnect(force=True)

    async def skip(self) -> bool:
        vc = self.get_voice_client()
        if vc and vc.is_playing():
            vc.stop()
            return True
        return False

    async def play_next(self) -> None:
        async with self.lock:
            vc = self.get_voice_client()
            if not vc or not vc.is_connected():
                self.current = None
                return

            if self.is_looping and self.current:
                self.queue.appendleft(self.current)

            if not self.queue:
                self.current = None
                return

            track = self.queue.popleft()
            self.current = track

            source = discord.PCMVolumeTransformer(
                discord.FFmpegPCMAudio(track.url, executable=FFMPEG_PATH, before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5"),
                volume=self.volume,
            )

            def after_play(error: Optional[Exception]) -> None:
                if error:
                    log.warning("Player error in guild %s: %s", self.guild_id, error)
                self.bot_ref.loop.create_task(self.play_next())

            vc.play(source, after=after_play)
            await self.announce_now_playing(track)

    def get_announce_channel(self) -> Optional[discord.TextChannel]:
        target_id = self.announce_channel_id or self.text_channel_id
        channel = self.bot_ref.get_channel(target_id) if target_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    async def announce_now_playing(self, track: Track) -> None:
        guild = self.bot_ref.get_guild(self.guild_id)
        if guild:
            await sync_music_panel(guild)


class VoiceRoomBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.room_states: Dict[int, RoomState] = {}
        self.control_channel_id: int = CONTROL_CHANNEL_ID
        self.temp_actions: Dict[int, Dict[str, object]] = {}
        self.music_states: Dict[int, GuildMusicState] = {}

    async def setup_hook(self) -> None:
        self.load_state()
        self.add_view(RoomPanelView(self))
        if GUILD_ID:
            guild = discord.Object(id=GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("Synced commands to guild %s", GUILD_ID)
        else:
            await self.tree.sync()
            log.info("Synced commands globally")

    def get_music_state(self, guild_id: int) -> GuildMusicState:
        state = self.music_states.get(guild_id)
        if state is None:
            state = GuildMusicState(self, guild_id)
            self.music_states[guild_id] = state
        return state

    def save_state(self) -> None:
        payload = {
            "control_channel_id": self.control_channel_id,
            "rooms": {
                str(channel_id): {
                    "channel_id": state.channel_id,
                    "leader_id": state.leader_id,
                    "join_order": state.join_order,
                    "created_at": state.created_at,
                    "panel_message_id": state.panel_message_id,
                }
                for channel_id, state in self.room_states.items()
            },
        }
        STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_state(self) -> None:
        if not STATE_FILE.exists():
            return
        try:
            data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
            self.control_channel_id = int(data.get("control_channel_id") or self.control_channel_id or 0)
            for channel_id_str, raw in data.get("rooms", {}).items():
                channel_id = int(channel_id_str)
                self.room_states[channel_id] = RoomState(
                    channel_id=channel_id,
                    leader_id=int(raw["leader_id"]),
                    join_order=[int(x) for x in raw.get("join_order", [])],
                    created_at=raw.get("created_at") or datetime.now(timezone.utc).isoformat(),
                    panel_message_id=raw.get("panel_message_id"),
                )
        except Exception as exc:
            log.warning("Could not load state file: %s", exc)


bot = VoiceRoomBot()


def is_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.move_members or perms.manage_channels


async def safe_send(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


def trunc(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def format_member_line(member: discord.Member, leader_id: int) -> str:
    badges: List[str] = []
    if member.id == leader_id:
        badges.append(EMOJI["leader"])
    if member.voice:
        if member.voice.mute:
            badges.append(EMOJI["mute"])
        if member.voice.deaf:
            badges.append(EMOJI["deafen"])
        if member.voice.self_stream:
            badges.append(EMOJI["sparkle"])
        if member.voice.self_video:
            badges.append(EMOJI["settings"])
    prefix = " ".join(badges) + " " if badges else ""
    return f"{prefix}{member.mention}"


async def get_control_channel(guild: discord.Guild, create_if_missing: bool = False) -> Optional[discord.TextChannel]:
    if bot.control_channel_id:
        channel = guild.get_channel(bot.control_channel_id)
        if isinstance(channel, discord.TextChannel):
            return channel

    named = discord.utils.get(guild.text_channels, name=CONTROL_CHANNEL_NAME)
    if isinstance(named, discord.TextChannel):
        bot.control_channel_id = named.id
        bot.save_state()
        return named

    me = guild.me or guild.guild_permissions
    if create_if_missing and getattr(me, "guild_permissions", None) and guild.me.guild_permissions.manage_channels:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=True, send_messages=False),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True, manage_messages=True),
        }
        try:
            channel = await guild.create_text_channel(CONTROL_CHANNEL_NAME, overwrites=overwrites)
            bot.control_channel_id = channel.id
            bot.save_state()
            return channel
        except discord.HTTPException:
            return None
    return None


async def get_or_create_room_state(channel: discord.VoiceChannel | discord.StageChannel) -> Optional[RoomState]:
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        return None

    state = bot.room_states.get(channel.id)
    if state is None:
        state = RoomState(channel_id=channel.id, leader_id=humans[0].id, join_order=[m.id for m in humans])
        bot.room_states[channel.id] = state
    else:
        for member in humans:
            state.add_member(member.id)
        present_ids = [m.id for m in humans]
        if state.leader_id not in present_ids:
            state.leader_id = state.pick_next_leader(present_ids) or humans[0].id

    bot.save_state()
    return state


def make_room_embed(guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel, state: RoomState) -> discord.Embed:
    humans = [m for m in channel.members if not m.bot]
    leader = guild.get_member(state.leader_id)
    leader_text = leader.mention if leader else f"<@{state.leader_id}>"
    member_lines = "\n".join(format_member_line(m, state.leader_id) for m in humans) or "Пусто"
    limit_text = "∞" if channel.user_limit == 0 else str(channel.user_limit)

    perms_everyone = channel.overwrites_for(guild.default_role)
    locked = perms_everyone.connect is False
    status_text = "Закрыта" if locked else "Открыта"
    type_text = "Stage" if isinstance(channel, discord.StageChannel) else "Voice"

    music_state = bot.get_music_state(guild.id)
    voice_client = guild.voice_client
    bot_voice_text = voice_client.channel.mention if voice_client and voice_client.channel else "Не подключён"
    now_playing = music_state.current.title if music_state.current else "Ничего не играет"
    queue_size = len(music_state.queue)
    volume_text = f"{int(music_state.volume * 100)}%"
    loop_text = "Вкл" if music_state.is_looping else "Выкл"
    if voice_client and voice_client.is_paused():
        playback_state = "На паузе"
    elif voice_client and voice_client.is_playing():
        playback_state = "Играет"
    else:
        playback_state = "Ожидание"

    queue_preview = music_state.queue_preview(3)

    embed = discord.Embed(
        title="✦ CRYPTA VOICE SUITE",
        description=(
            f"> **Комната:** {channel.mention}\n"
            f"> **Лидер:** {leader_text}\n"
            f"> **Формат:** `{type_text}` • **Доступ:** `{status_text}`\n"
            f"> **Управление:** меню сверху + кнопки ниже"
        ),
        color=0x181C34,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(
        name="✦ Состояние комнаты",
        value=(
            f"**Участников:** `{len(humans)}`\n"
            f"**Лимит:** `{limit_text}`\n"
            f"**Статус:** `{status_text}`\n"
            f"**Канал:** {channel.mention}"
        ),
        inline=True,
    )
    embed.add_field(
        name="✦ Музыкальный модуль",
        value=(
            f"**Плеер:** `{playback_state}`\n"
            f"**Сейчас:** {trunc(now_playing, 120)}\n"
            f"**Очередь:** `{queue_size}`\n"
            f"**Громкость:** `{volume_text}` • **Loop:** `{loop_text}`"
        ),
        inline=True,
    )
    embed.add_field(
        name="✦ Подключение бота",
        value=f"{bot_voice_text}\n\n`Музыка` — подключить бота и открыть отдельную музыкальную панель",
        inline=False,
    )
    embed.add_field(name="✦ Участники", value=trunc(member_lines, 1024), inline=False)
    embed.add_field(name="✦ Очередь", value=trunc(queue_preview, 1024), inline=False)
    embed.add_field(
        name="✦ Быстрые действия",
        value=(
            "`Лидер` • `Состав` • `Онлайн` • `Доступ`\n"
            "`Лимит` • `Обновить` • `Кик` • `Звук`\n"
            "`Музыка` — подключить бота и открыть муз-панель"
        ),
        inline=False,
    )
    if guild.icon:
        embed.set_author(name=f"{guild.name} • система управления", icon_url=guild.icon.url)
        embed.set_thumbnail(url=guild.icon.url)
    else:
        embed.set_author(name=f"{guild.name} • система управления")
    embed.set_footer(text=f"room:{channel.id} | CRYPTA panel")
    return embed

async def sync_room_panel(channel: discord.VoiceChannel | discord.StageChannel, force_repost: bool = False) -> None:
    state = await get_or_create_room_state(channel)
    control_channel = await get_control_channel(channel.guild, create_if_missing=True)
    if not state or not control_channel:
        return

    embed = make_room_embed(channel.guild, channel, state)
    view = RoomPanelView(bot, channel.id)
    message = None

    if state.panel_message_id:
        try:
            message = await control_channel.fetch_message(state.panel_message_id)
        except discord.HTTPException:
            message = None

    try:
        if message and not force_repost:
            await message.edit(content=None, embed=embed, view=view)
        else:
            if message:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            msg = await control_channel.send(embed=embed, view=view)
            state.panel_message_id = msg.id
            bot.save_state()
    except discord.HTTPException as exc:
        log.warning("Failed to sync panel for room %s: %s", channel.id, exc)


async def remove_room_panel(guild: discord.Guild, channel_id: int) -> None:
    state = bot.room_states.get(channel_id)
    if not state:
        return
    control_channel = await get_control_channel(guild)
    if control_channel and state.panel_message_id:
        try:
            message = await control_channel.fetch_message(state.panel_message_id)
            await message.delete()
        except discord.HTTPException:
            pass
    bot.room_states.pop(channel_id, None)
    bot.save_state()


async def refresh_room_state(channel: discord.VoiceChannel | discord.StageChannel) -> Optional[int]:
    humans = [m for m in channel.members if not m.bot]
    if not humans:
        await remove_room_panel(channel.guild, channel.id)
        return None

    state = await get_or_create_room_state(channel)
    if not state:
        return None

    old_leader = state.leader_id
    state.leader_id = state.pick_next_leader([m.id for m in humans]) or humans[0].id
    bot.save_state()
    await sync_room_panel(channel, force_repost=old_leader != state.leader_id)
    return state.leader_id


def find_member_in_channel(channel: discord.VoiceChannel | discord.StageChannel, member_id: int) -> Optional[discord.Member]:
    for member in channel.members:
        if member.id == member_id:
            return member
    return None


async def ensure_control_rights(interaction: discord.Interaction, room_channel: discord.VoiceChannel | discord.StageChannel) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await safe_send(interaction, "Это работает только на сервере.")
        return False
    if not member.voice or member.voice.channel != room_channel:
        await safe_send(interaction, "Чтобы управлять комнатой, нужно быть в этом войсе.")
        return False
    if is_admin(member):
        return True
    state = await get_or_create_room_state(room_channel)
    if state and state.leader_id == member.id:
        return True
    await safe_send(interaction, "У тебя нет прав на управление этой комнатой.")
    return False


async def ensure_music_panel_rights(interaction: discord.Interaction, guild_id: int) -> Optional[discord.Member]:
    if not interaction.guild or interaction.guild.id != guild_id:
        await safe_send(interaction, "Это работает только на нужном сервере.")
        return None
    member = interaction.user
    if not isinstance(member, discord.Member):
        await safe_send(interaction, "Это работает только на сервере.")
        return None
    music = bot.get_music_state(guild_id)
    vc = music.get_voice_client()
    if not vc or not vc.channel:
        await safe_send(interaction, "Музыкальный бот сейчас не подключён к войсу.")
        return None
    if not member.voice or member.voice.channel != vc.channel:
        await safe_send(interaction, f"Управлять музыкой могут только участники войса {vc.channel.mention}.")
        return None
    return member


def get_music_announce_channel(guild: discord.Guild) -> Optional[discord.TextChannel]:
    music = bot.get_music_state(guild.id)
    channel = bot.get_channel(music.announce_channel_id) if music.announce_channel_id else None
    return channel if isinstance(channel, discord.TextChannel) else None


class SafeView(discord.ui.View):
    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        log.exception("View interaction failed: %s", error)
        try:
            await safe_send(interaction, f"Ошибка взаимодействия: {type(error).__name__}: {error}")
        except Exception:
            pass


class SafeModal(discord.ui.Modal):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        log.exception("Modal interaction failed: %s", error)
        try:
            await safe_send(interaction, f"Ошибка взаимодействия: {type(error).__name__}: {error}")
        except Exception:
            pass


class MemberActionSelect(discord.ui.Select):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int, action: str, actor_id: int) -> None:
        self.bot_ref = bot_ref
        self.room_id = room_id
        self.action = action
        self.actor_id = actor_id
        options = self._build_options()
        placeholders = {
            "kick": "Кого отключить от войса?",
            "mute": "Кого замутить?",
            "unmute": "С кого снять мут?",
            "deafen": "Кого заглушить?",
            "undeafen": "С кого снять заглушение?",
            "leader": "Кому передать лидерство?",
        }
        super().__init__(
            placeholder=placeholders.get(action, "Выбери участника"),
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="Нет доступных участников", value="none")],
            custom_id=f"room_action_select:{room_id}:{action}:{actor_id}",
            disabled=not bool(options),
        )

    def _build_options(self) -> List[discord.SelectOption]:
        channel = self.bot_ref.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            return []
        options: List[discord.SelectOption] = []
        for member in channel.members:
            if member.bot or member.id == self.actor_id:
                continue
            description = []
            if member.voice and member.voice.mute:
                description.append("mute")
            if member.voice and member.voice.deaf:
                description.append("deafen")
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=(", ".join(description) or f"ID: {member.id}")[:100],
                    emoji="👤",
                )
            )
        return options[:25]

    async def callback(self, interaction: discord.Interaction) -> None:
        if self.values[0] == "none":
            await safe_send(interaction, "Нет доступных участников.")
            return

        channel = self.bot_ref.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return

        actor = interaction.user
        if not isinstance(actor, discord.Member):
            await safe_send(interaction, "Нужно использовать это на сервере.")
            return

        target = find_member_in_channel(channel, int(self.values[0]))
        if not target:
            await safe_send(interaction, "Участник уже не в этом войсе.")
            return
        if target.top_role >= actor.top_role and not actor.guild_permissions.administrator:
            await safe_send(interaction, "Нельзя управлять участником с такой же или более высокой ролью.")
            return

        if self.action in {"mute", "deafen", "kick"}:
            bot.temp_actions[actor.id] = {
                "room_id": channel.id,
                "action": self.action,
                "target_id": target.id,
            }
            await interaction.response.send_modal(ActionReasonModal(self.action, target.display_name))
            return

        try:
            if self.action == "unmute":
                await target.edit(mute=False, reason=f"Voice panel unmute by {actor}")
                text = f"{EMOJI['loud']} С {target.mention} снят серверный мут."
            elif self.action == "undeafen":
                await target.edit(deafen=False, reason=f"Voice panel undeafen by {actor}")
                text = f"{EMOJI['loud']} С {target.mention} снято заглушение."
            elif self.action == "leader":
                state = await get_or_create_room_state(channel)
                if not state:
                    await safe_send(interaction, "Не удалось получить состояние комнаты.")
                    return
                changed = state.leader_id != target.id
                state.leader_id = target.id
                state.add_member(target.id)
                bot.save_state()
                await sync_room_panel(channel, force_repost=changed)
                text = f"{EMOJI['leader']} Лидер комнаты теперь {target.mention}."
                await safe_send(interaction, text)
                return
            else:
                await safe_send(interaction, "Неизвестное действие.")
                return
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает прав для этого действия.")
            return
        except discord.HTTPException:
            await safe_send(interaction, "Discord отклонил это действие.")
            return

        await sync_room_panel(channel)
        await safe_send(interaction, text)


class MemberActionView(SafeView):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int, action: str, actor_id: int) -> None:
        super().__init__(timeout=90)
        self.add_item(MemberActionSelect(bot_ref, room_id, action, actor_id))


class ActionReasonModal(SafeModal):
    def __init__(self, action: str, target_name: str) -> None:
        super().__init__(title=f"{target_name} · {action}", timeout=180)
        self.reason = discord.ui.TextInput(
            label="Причина",
            placeholder="Необязательно",
            required=False,
            max_length=120,
        )
        self.duration = discord.ui.TextInput(
            label="Время в минутах",
            placeholder="0 = навсегда",
            required=False,
            max_length=4,
            default="0",
        )
        self.add_item(self.reason)
        self.add_item(self.duration)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        data = bot.temp_actions.pop(interaction.user.id, None)
        if not data:
            await safe_send(interaction, "Действие устарело, попробуй ещё раз.")
            return

        room_id = int(data["room_id"])
        action = str(data["action"])
        target_id = int(data["target_id"])

        channel = bot.get_channel(room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return

        target = find_member_in_channel(channel, target_id)
        if not target:
            await safe_send(interaction, "Участник уже не в комнате.")
            return

        reason_text = str(self.reason).strip() or f"Voice panel {action} by {interaction.user}"
        minutes_raw = str(self.duration).strip() or "0"
        try:
            minutes = int(minutes_raw)
            if minutes < 0:
                raise ValueError
        except ValueError:
            await safe_send(interaction, "Время должно быть числом 0 или больше.")
            return

        try:
            if action == "kick":
                await target.move_to(None, reason=reason_text)
                text = f"{EMOJI['kick']} {target.mention} отключён от войса."
            elif action == "mute":
                await target.edit(mute=True, reason=reason_text)
                text = f"{EMOJI['mute']} {target.mention} получил серверный мут."
                if minutes > 0:
                    bot.loop.create_task(remove_voice_flag_later(target, "mute", minutes * 60))
                    text += f" Снимется через {minutes} мин."
            elif action == "deafen":
                await target.edit(deafen=True, reason=reason_text)
                text = f"{EMOJI['deafen']} {target.mention} заглушён."
                if minutes > 0:
                    bot.loop.create_task(remove_voice_flag_later(target, "deafen", minutes * 60))
                    text += f" Снимется через {minutes} мин."
            else:
                await safe_send(interaction, "Неизвестное действие.")
                return
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает прав для этого действия.")
            return
        except discord.HTTPException:
            await safe_send(interaction, "Discord отклонил это действие.")
            return

        await sync_room_panel(channel)
        await safe_send(interaction, text)


async def _sleep_and_unset(member: discord.Member, *, mute: Optional[bool] = None, deafen: Optional[bool] = None, delay_seconds: int = 0) -> None:
    await asyncio.sleep(delay_seconds)
    try:
        kwargs = {}
        if mute is not None:
            kwargs["mute"] = mute
        if deafen is not None:
            kwargs["deafen"] = deafen
        if kwargs:
            await member.edit(**kwargs, reason="Temporary voice action expired")
            if member.voice and isinstance(member.voice.channel, (discord.VoiceChannel, discord.StageChannel)):
                await sync_room_panel(member.voice.channel)
    except Exception:
        pass


async def remove_voice_flag_later(member: discord.Member, flag: str, delay_seconds: int) -> None:
    if flag == "mute":
        await _sleep_and_unset(member, mute=False, delay_seconds=delay_seconds)
    elif flag == "deafen":
        await _sleep_and_unset(member, deafen=False, delay_seconds=delay_seconds)


class ActionPicker(discord.ui.Select):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int) -> None:
        self.bot_ref = bot_ref
        self.room_id = room_id
        options = [
            discord.SelectOption(label="Кик", value="kick", description="Отключить от войса", emoji="🚪"),
            discord.SelectOption(label="Мут", value="mute", description="Серверный мут", emoji="🔇"),
            discord.SelectOption(label="Размут", value="unmute", description="Снять серверный мут", emoji="🔊"),
            discord.SelectOption(label="Заглушить", value="deafen", description="Полностью заглушить", emoji="🎧"),
            discord.SelectOption(label="Снять заглушение", value="undeafen", description="Вернуть звук", emoji="🎶"),
            discord.SelectOption(label="Передать лидерство", value="leader", description="Назначить нового лидера", emoji="👑"),
        ]
        super().__init__(
            placeholder="Выбрать действие",
            min_values=1,
            max_values=1,
            options=options,
            custom_id=f"action_picker:{room_id}",
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        channel = self.bot_ref.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "Выбери участника:",
            ephemeral=True,
            view=MemberActionView(self.bot_ref, self.room_id, self.values[0], interaction.user.id),
        )


class PlaceholderActionPicker(discord.ui.Select):
    def __init__(self) -> None:
        super().__init__(
            placeholder="Выбрать действие",
            min_values=1,
            max_values=1,
            options=[discord.SelectOption(label="Панель загружается", value="noop", emoji="⌛")],
            custom_id="action_picker:placeholder",
            disabled=True,
            row=0,
        )


class LimitRoomModal(SafeModal, title="Лимит комнаты"):
    def __init__(self, room_id: int, current_limit: int) -> None:
        super().__init__(timeout=180)
        self.room_id = room_id
        self.limit = discord.ui.TextInput(
            label="Лимит (0-99)",
            placeholder="0 = без лимита",
            default=str(current_limit),
            max_length=2,
            required=True,
        )
        self.add_item(self.limit)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = bot.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        try:
            value = int(str(self.limit))
        except ValueError:
            await safe_send(interaction, "Нужно ввести число от 0 до 99.")
            return
        if not 0 <= value <= 99:
            await safe_send(interaction, "Лимит должен быть от 0 до 99.")
            return
        try:
            await channel.edit(user_limit=value, reason=f"Limit change by {interaction.user}")
            await sync_room_panel(channel)
            await safe_send(interaction, f"{EMOJI['users']} Лимит комнаты установлен: **{value}**.")
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает права Manage Channels.")
        except discord.HTTPException:
            await safe_send(interaction, "Не удалось изменить лимит комнаты.")




class MusicAddModal(SafeModal, title="Добавить музыку"):
    def __init__(self, room_id: int) -> None:
        super().__init__(timeout=180)
        self.room_id = room_id
        self.query = discord.ui.TextInput(
            label="Название трека или ссылка",
            placeholder="Например: Miyagi Captain или ссылка на YouTube",
            required=True,
            max_length=300,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = bot.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return

        if not await ensure_control_rights(interaction, channel):
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or not interaction.guild:
            await safe_send(interaction, "Это работает только на сервере.")
            return

        music = bot.get_music_state(interaction.guild.id)
        if not music.text_channel_id:
            music.text_channel_id = interaction.channel_id
        if not music.announce_channel_id:
            music.announce_channel_id = interaction.channel_id

        if not member.voice or member.voice.channel != channel:
            await safe_send(interaction, "Нужно быть именно в этой комнате.")
            return

        try:
            await music.connect_to(channel)
        except discord.ClientException as exc:
            await safe_send(interaction, f"Не удалось подключиться к войсу: {exc}")
            return
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает прав на вход и разговор в войсе.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        track, error = await asyncio.to_thread(extract_media, str(self.query).strip())
        if error or not track:
            await interaction.followup.send(error or "Не удалось загрузить трек.", ephemeral=True)
            return

        track.requester_id = member.id
        music.queue.append(track)

        vc = music.get_voice_client()
        if vc and not vc.is_playing() and not vc.is_paused() and music.current is None:
            await music.play_next()

        await sync_room_panel(channel)
        await interaction.followup.send(
            f"{EMOJI['mic']} Добавлено: **{track.title}**\nОчередь: **{len(music.queue)}**",
            ephemeral=True,
        )


def make_music_panel_embed(guild: discord.Guild) -> discord.Embed:
    music = bot.get_music_state(guild.id)
    vc = guild.voice_client
    voice_text = vc.channel.mention if vc and vc.channel else "Не подключён"
    current = music.current
    current_text = f"**[{current.title}]({current.webpage_url})**\n`{human_duration(current.duration)}`" if current else "Ничего не играет"
    queue_text = music.queue_preview(8)
    loop_text = "Вкл" if music.is_looping else "Выкл"
    volume_text = f"{int(music.volume * 100)}%"
    state_text = "На паузе" if vc and vc.is_paused() else "Играет" if vc and vc.is_playing() else "Ожидание"

    embed = discord.Embed(
        title="✦ CRYPTA MUSIC PANEL",
        description=(
            f"> **Состояние:** `{state_text}`\n"
            f"> **Войс:** {voice_text}\n"
            f"> **Loop:** `{loop_text}` • **Громкость:** `{volume_text}`"
        ),
        color=0x181C34,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="✦ Сейчас играет", value=current_text, inline=False)
    embed.add_field(name="✦ Очередь", value=trunc(queue_text, 1024), inline=False)
    embed.add_field(
        name="✦ Управление",
        value="`Добавить` • `Пауза` • `Скип` • `Стоп` • `Очередь` • `Loop`",
        inline=False,
    )
    if current and current.thumbnail:
        embed.set_thumbnail(url=current.thumbnail)
    elif guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"music_panel:{guild.id}")
    return embed


async def sync_music_panel(guild: discord.Guild, force_repost: bool = False) -> None:
    music = bot.get_music_state(guild.id)
    channel = music.get_announce_channel()
    if not channel:
        return

    embed = make_music_panel_embed(guild)
    view = MusicPanelView(bot, guild.id)
    message = None
    if music.panel_message_id:
        try:
            message = await channel.fetch_message(music.panel_message_id)
        except discord.HTTPException:
            message = None

    try:
        if message and not force_repost:
            await message.edit(embed=embed, view=view, content=None)
        else:
            if message:
                try:
                    await message.delete()
                except discord.HTTPException:
                    pass
            msg = await channel.send(embed=embed, view=view)
            music.panel_message_id = msg.id
    except discord.HTTPException:
        pass


class MusicPanelAddModal(SafeModal, title="Добавить музыку"):
    def __init__(self, guild_id: int) -> None:
        super().__init__(timeout=180)
        self.guild_id = guild_id
        self.query = discord.ui.TextInput(
            label="Название трека или ссылка",
            placeholder="Например: Miyagi Captain или ссылка",
            required=True,
            max_length=300,
        )
        self.add_item(self.query)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.guild or interaction.guild.id != self.guild_id:
            await safe_send(interaction, "Это работает только на сервере.")
            return
        member = interaction.user
        if not isinstance(member, discord.Member) or not member.voice or not member.voice.channel:
            await safe_send(interaction, "Сначала зайди в голосовой канал.")
            return

        channel = member.voice.channel
        music = bot.get_music_state(interaction.guild.id)
        if not music.text_channel_id:
            music.text_channel_id = interaction.channel_id
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await music.connect_to(channel)
        except Exception as exc:
            await interaction.followup.send(f"Ошибка подключения к войсу: {type(exc).__name__}: {exc}", ephemeral=True)
            return

        track, error = await asyncio.to_thread(extract_media, str(self.query).strip())
        if error or not track:
            await interaction.followup.send(error or "Не удалось загрузить трек.", ephemeral=True)
            return
        track.requester_id = member.id
        music.queue.append(track)
        vc = music.get_voice_client()
        if vc and not vc.is_playing() and not vc.is_paused() and music.current is None:
            await music.play_next()
        await sync_music_panel(interaction.guild)
        await interaction.followup.send(f"🎵 Добавлено: **{track.title}**", ephemeral=True)


class MusicPanelView(SafeView):
    def __init__(self, bot_ref: VoiceRoomBot, guild_id: int) -> None:
        super().__init__(timeout=None)
        self.bot_ref = bot_ref
        self.guild_id = guild_id

    def _guild(self) -> Optional[discord.Guild]:
        return self.bot_ref.get_guild(self.guild_id)

    @discord.ui.button(label="Добавить", emoji="🎵", style=discord.ButtonStyle.primary, row=0, custom_id="music_panel_add")
    async def add_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        await interaction.response.send_modal(MusicPanelAddModal(self.guild_id))

    @discord.ui.button(label="Пауза", emoji="⏯️", style=discord.ButtonStyle.secondary, row=0, custom_id="music_panel_pause")
    async def pause_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        music = bot.get_music_state(self.guild_id)
        vc = music.get_voice_client()
        if vc and vc.is_playing():
            vc.pause()
            await safe_send(interaction, "⏸️ Музыка поставлена на паузу.")
        elif vc and vc.is_paused():
            vc.resume()
            await safe_send(interaction, "▶️ Музыка продолжена.")
        else:
            await safe_send(interaction, "Сейчас ничего не играет.")
        await sync_music_panel(interaction.guild)

    @discord.ui.button(label="Скип", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0, custom_id="music_panel_skip")
    async def skip_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        music = bot.get_music_state(self.guild_id)
        if await music.skip():
            await safe_send(interaction, "⏭️ Трек пропущен.")
        else:
            await safe_send(interaction, "Сейчас ничего не играет.")
        await sync_music_panel(interaction.guild)

    @discord.ui.button(label="Стоп", emoji="⏹️", style=discord.ButtonStyle.danger, row=0, custom_id="music_panel_stop")
    async def stop_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        music = bot.get_music_state(self.guild_id)
        await music.stop()
        await safe_send(interaction, "⏹️ Музыка остановлена, очередь очищена.")
        await sync_music_panel(interaction.guild)

    @discord.ui.button(label="Очередь", emoji="📜", style=discord.ButtonStyle.secondary, row=1, custom_id="music_panel_queue")
    async def queue_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        music = bot.get_music_state(self.guild_id)
        current = music.current.title if music.current else "Ничего не играет"
        await safe_send(interaction, f"**Сейчас:** {current}\n\n{music.queue_preview(10)}")

    @discord.ui.button(label="Loop", emoji="🔁", style=discord.ButtonStyle.secondary, row=1, custom_id="music_panel_loop")
    async def loop_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member = await ensure_music_panel_rights(interaction, self.guild_id)
        if not member:
            return
        music = bot.get_music_state(self.guild_id)
        music.is_looping = not music.is_looping
        await safe_send(interaction, f"🔁 Повтор трека: {'включён' if music.is_looping else 'выключен'}")
        await sync_music_panel(interaction.guild)


class RoomPanelView(SafeView):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int = 0) -> None:
        super().__init__(timeout=None)
        self.bot_ref = bot_ref
        self.room_id = room_id
        self.add_item(ActionPicker(bot_ref, room_id) if room_id else PlaceholderActionPicker())

    def _resolve_room_id(self, interaction: discord.Interaction) -> Optional[int]:
        if self.room_id:
            return self.room_id
        embed = interaction.message.embeds[0] if interaction.message and interaction.message.embeds else None
        if embed and embed.footer and embed.footer.text.startswith("room:"):
            try:
                return int(embed.footer.text.split(":", 1)[1].split("|")[0].strip())
            except ValueError:
                return None
        return None

    def _get_channel(self, interaction: discord.Interaction) -> Optional[discord.VoiceChannel | discord.StageChannel]:
        room_id = self._resolve_room_id(interaction)
        channel = self.bot_ref.get_channel(room_id) if room_id else None
        return channel if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)) else None

    @discord.ui.button(label="Лидер", emoji="👑", style=discord.ButtonStyle.primary, row=1, custom_id="room_leader")
    async def leader_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "Выбери нового лидера:",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "leader", interaction.user.id),
        )

    @discord.ui.button(label="Состав", emoji="👥", style=discord.ButtonStyle.secondary, row=1, custom_id="room_user_info")
    async def user_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        humans = [m.mention for m in channel.members if not m.bot]
        await safe_send(interaction, "\n".join(humans) or "Пусто")

    @discord.ui.button(label="Онлайн", emoji="📶", style=discord.ButtonStyle.secondary, row=1, custom_id="room_users")
    async def users_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        await safe_send(interaction, f"{EMOJI['users']} Сейчас в комнате: **{len([m for m in channel.members if not m.bot])}**")

    @discord.ui.button(label="Доступ", emoji="🔐", style=discord.ButtonStyle.secondary, row=1, custom_id="room_lock_toggle")
    async def lock_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        overwrite = channel.overwrites_for(channel.guild.default_role)
        currently_locked = overwrite.connect is False
        overwrite.connect = None if currently_locked else False
        try:
            await channel.set_permissions(channel.guild.default_role, overwrite=overwrite)
            await sync_room_panel(channel)
            await safe_send(interaction, f"{EMOJI['unlock']} Комната открыта." if currently_locked else f"{EMOJI['lock']} Комната закрыта.")
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает права Manage Channels / Manage Roles.")
        except discord.HTTPException:
            await safe_send(interaction, "Не удалось изменить доступ к комнате.")

    @discord.ui.button(label="Лимит", emoji="🎚️", style=discord.ButtonStyle.secondary, row=2, custom_id="room_limit")
    async def settings_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_modal(LimitRoomModal(channel.id, channel.user_limit))

    @discord.ui.button(label="Обновить", emoji="✨", style=discord.ButtonStyle.secondary, row=2, custom_id="room_refresh")
    async def sparkle_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        await sync_room_panel(channel)
        await safe_send(interaction, f"{EMOJI['sparkle']} Панель обновлена.")

    @discord.ui.button(label="Кик", emoji="🚪", style=discord.ButtonStyle.danger, row=2, custom_id="room_kick")
    async def kick_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message("Выбери участника:", ephemeral=True, view=MemberActionView(bot, channel.id, "kick", interaction.user.id))

    @discord.ui.button(label="Звук", emoji="🔊", style=discord.ButtonStyle.success, row=2, custom_id="room_sound_restore")
    async def sound_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message("Выбери участника:", ephemeral=True, view=MemberActionView(bot, channel.id, "undeafen", interaction.user.id))

    @discord.ui.button(label="Музыка", emoji="🎵", style=discord.ButtonStyle.primary, row=3, custom_id="room_music_only")
    async def music_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        if not interaction.guild:
            await safe_send(interaction, "Это работает только на сервере.")
            return

        music = bot.get_music_state(interaction.guild.id)
        await interaction.response.defer(ephemeral=True, thinking=False)
        try:
            await music.connect_to(channel)
            await sync_room_panel(channel)
            if music.announce_channel_id:
                await sync_music_panel(interaction.guild, force_repost=(music.panel_message_id is None))
                announce = music.get_announce_channel()
                announce_text = f" Музыкальная панель: {announce.mention}." if announce else ""
            else:
                announce_text = " Сначала выбери отдельный канал командой /setup_music_channel."
            await interaction.followup.send(f"🎵 Бот подключился к {channel.mention}.{announce_text}", ephemeral=True)
        except discord.ClientException as exc:
            await interaction.followup.send(f"Не удалось подключиться: {exc}", ephemeral=True)
        except discord.Forbidden:
            await interaction.followup.send("Боту не хватает прав на вход и разговор в войсе.", ephemeral=True)
        except Exception as exc:
            await interaction.followup.send(f"Ошибка подключения: {type(exc).__name__}: {exc}", ephemeral=True)


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    for guild in bot.guilds:
        await get_control_channel(guild, create_if_missing=False)
        for voice_channel in guild.voice_channels:
            if [m for m in voice_channel.members if not m.bot]:
                await refresh_room_state(voice_channel)


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState) -> None:
    if member.bot:
        return

    if before.channel and before.channel != after.channel:
        state = bot.room_states.get(before.channel.id)
        if state:
            state.remove_member(member.id)
            bot.save_state()
        await refresh_room_state(before.channel)

    if after.channel and before.channel != after.channel:
        state = await get_or_create_room_state(after.channel)
        if state:
            old_leader = state.leader_id
            state.add_member(member.id)
            humans = [m for m in after.channel.members if not m.bot]
            if len(humans) == 1:
                state.leader_id = member.id
            bot.save_state()
            await sync_room_panel(after.channel, force_repost=(old_leader != state.leader_id))
        else:
            await sync_room_panel(after.channel)

    if after.channel and before.channel == after.channel:
        await sync_room_panel(after.channel)


@bot.tree.command(name="setup_voice_panel", description="Настроить текстовый канал для панелей войсов")
@app_commands.describe(channel="Текстовый канал, куда бот будет отправлять панели")
async def setup_voice_panel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await safe_send(interaction, "Только администратор может менять канал панели.")
        return
    bot.control_channel_id = channel.id
    bot.save_state()
    await safe_send(interaction, f"Канал панелей установлен: {channel.mention}")


@bot.tree.command(name="setup_music_channel", description="Установить отдельный канал для музыкальной панели")
@app_commands.describe(channel="Текстовый канал, куда бот будет отправлять музыкальную панель")
async def setup_music_channel(interaction: discord.Interaction, channel: discord.TextChannel) -> None:
    if not interaction.guild or not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await safe_send(interaction, "Только администратор может менять музыкальный канал.")
        return
    music = bot.get_music_state(interaction.guild.id)
    music.announce_channel_id = channel.id
    music.panel_message_id = None
    await safe_send(interaction, f"Музыкальный канал установлен: {channel.mention}")
    await sync_music_panel(interaction.guild, force_repost=True)


@bot.tree.command(name="force_sync_voice_panels", description="Пересоздать панели активных войсов")
async def force_sync_voice_panels(interaction: discord.Interaction) -> None:
    if not isinstance(interaction.user, discord.Member) or not interaction.user.guild_permissions.administrator:
        await safe_send(interaction, "Только администратор может это использовать.")
        return
    if not interaction.guild:
        await safe_send(interaction, "Используй это на сервере.")
        return
    control_channel = await get_control_channel(interaction.guild, create_if_missing=True)
    if not control_channel:
        await safe_send(interaction, "Не удалось получить канал панелей.")
        return
    for voice_channel in interaction.guild.voice_channels:
        if [m for m in voice_channel.members if not m.bot]:
            await refresh_room_state(voice_channel)
    await safe_send(interaction, f"Панели синхронизированы в {control_channel.mention}.")


def extract_media(query: str) -> Tuple[Optional[Track], Optional[str]]:
    if yt_dlp is None:
        return None, "Не установлен yt-dlp. Установи пакет `yt-dlp`."

    query = query.strip()
    cookie_path = Path(COOKIE_FILE)

    base_opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "source_address": "0.0.0.0",
        "extract_flat": False,
        "skip_download": True,
    }

    if cookie_path.exists() and cookie_path.is_file():
        base_opts["cookiefile"] = str(cookie_path)

    search_candidates: List[str]
    if looks_like_url(query):
        search_candidates = [query]
    else:
        # Как у типичных music-ботов: сначала YouTube поиск, потом SoundCloud, потом generic input
        search_candidates = [
            f"ytsearch1:{query}",
            f"scsearch1:{query}",
            query,
        ]

    last_error: Optional[str] = None

    for candidate in search_candidates:
        ydl_opts = dict(base_opts)
        if candidate.startswith("ytsearch"):
            ydl_opts["default_search"] = "ytsearch1"
        elif candidate.startswith("scsearch"):
            ydl_opts["default_search"] = "scsearch1"
        else:
            ydl_opts["default_search"] = "auto"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(candidate, download=False)
        except Exception as exc:
            err_text = str(exc)
            last_error = err_text
            if "Sign in to confirm you’re not a bot" in err_text or "Use --cookies-from-browser or --cookies" in err_text:
                # Для текстового запроса пробуем следующий источник, для прямой youtube-ссылки сразу выходим
                if looks_like_url(query):
                    return None, (
                        "YouTube запросил подтверждение. Нужен рабочий cookies.txt, а на Railway YouTube всё равно может "
                        "резать поток по IP. Для названий треков бот попробует и другие источники, но для прямой YouTube-ссылки "
                        f"сейчас нужен COOKIE_FILE. Текущий путь: {COOKIE_FILE}"
                    )
                continue
            if "Requested format is not available" in err_text:
                # частая проблема на YouTube/Cloud без EJS или при IP-блоке; пробуем следующий источник
                continue
            continue

        if not info:
            continue

        if "entries" in info:
            info = next((entry for entry in info["entries"] if entry and entry.get("webpage_url") or entry and entry.get("url")), None)

        if not info:
            continue

        formats = info.get("formats") or []

        def _fmt_score(fmt: dict) -> tuple:
            has_url = 1 if fmt.get("url") else 0
            has_audio = 1 if fmt.get("acodec") not in (None, "none") else 0
            audio_only = 1 if fmt.get("vcodec") == "none" else 0
            is_live_friendly = 1 if fmt.get("protocol") not in {"m3u8_native", "m3u8"} else 0
            abr = float(fmt.get("abr") or fmt.get("tbr") or 0)
            return (has_url, has_audio, audio_only, is_live_friendly, abr)

        usable_formats = [f for f in formats if f.get("url") and f.get("acodec") not in (None, "none")]
        selected = max(usable_formats, key=_fmt_score) if usable_formats else None

        audio_url = (selected or info).get("url")
        webpage_url = info.get("webpage_url") or info.get("original_url") or candidate
        title = info.get("title") or "Unknown title"
        duration = int(info.get("duration") or 0)
        thumbnail = info.get("thumbnail")

        if not audio_url:
            # если источник нашёлся, но прямого потока нет — пробуем другой источник
            last_error = "У найденного источника нет прямого аудио-потока."
            continue

        return Track(
            title=title,
            url=audio_url,
            webpage_url=webpage_url,
            duration=duration,
            requester_id=0,
            thumbnail=thumbnail,
        ), None

    if last_error and "Requested format is not available" in last_error:
        return None, (
            "Источник найден, но YouTube/источник не отдал пригодный аудиоформат. Для Railway это часто значит, что нужен "
            "полный yt-dlp EJS/JS runtime или что IP хоста режется. Попробуй другое название, SoundCloud-ссылку или обнови "
            "yt-dlp до default/ejs-варианта."
        )

    if last_error and ("Sign in to confirm you’re not a bot" in last_error or "Use --cookies-from-browser or --cookies" in last_error):
        return None, (
            "YouTube запросил подтверждение. Добавь рядом с bot.py файл cookies.txt или укажи COOKIE_FILE. "
            f"Текущий путь: {COOKIE_FILE}"
        )

    if last_error:
        return None, f"Не удалось получить трек: {last_error}"
    return None, "Ничего не найдено ни на одном источнике."


@bot.command(name="play")
async def play_command(ctx: commands.Context, *, query: str) -> None:
    if not ctx.guild or not isinstance(ctx.author, discord.Member):
        return
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.reply("Сначала зайди в голосовой канал.")
        return

    music = bot.get_music_state(ctx.guild.id)
    music.text_channel_id = ctx.channel.id

    async with ctx.typing():
        track, error = await asyncio.to_thread(extract_media, query)

    if error or not track:
        await ctx.reply(error or "Не удалось загрузить трек.")
        return

    track.requester_id = ctx.author.id

    try:
        await music.connect_to(ctx.author.voice.channel)
    except discord.ClientException as exc:
        await ctx.reply(f"Не удалось подключиться к войсу: {exc}")
        return
    except discord.Forbidden:
        await ctx.reply("Боту не хватает прав на вход в войс.")
        return

    music.queue.append(track)

    embed = discord.Embed(
        title=f"{EMOJI['mic']} Трек добавлен",
        description=f"**{track.title}**\n`{human_duration(track.duration)}`",
        color=0x111827,
    )
    embed.add_field(name="Очередь", value=f"`{len(music.queue)}`", inline=True)
    embed.add_field(name="Запросил", value=ctx.author.mention, inline=True)
    if track.thumbnail:
        embed.set_thumbnail(url=track.thumbnail)
    await ctx.reply(embed=embed)

    vc = music.get_voice_client()
    if vc and not vc.is_playing() and not vc.is_paused() and music.current is None:
        await music.play_next()

    if ctx.author.voice and isinstance(ctx.author.voice.channel, (discord.VoiceChannel, discord.StageChannel)):
        await sync_room_panel(ctx.author.voice.channel)
    await sync_music_panel(ctx.guild)


@bot.command(name="skip")
async def skip_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    if await music.skip():
        await ctx.reply("Текущий трек пропущен.")
    else:
        await ctx.reply("Сейчас ничего не играет.")
    await sync_music_panel(ctx.guild)


@bot.command(name="stop")
async def stop_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    await music.stop()
    await ctx.reply("Музыка остановлена, очередь очищена.")
    await sync_music_panel(ctx.guild)


@bot.command(name="pause")
async def pause_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    vc = ctx.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await ctx.reply("Музыка на паузе.")
    else:
        await ctx.reply("Сейчас нечего ставить на паузу.")
    await sync_music_panel(ctx.guild)


@bot.command(name="resume")
async def resume_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    vc = ctx.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await ctx.reply("Продолжаю проигрывание.")
    else:
        await ctx.reply("Музыка не стоит на паузе.")
    await sync_music_panel(ctx.guild)


@bot.command(name="queue")
async def queue_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    embed = discord.Embed(
        title=f"{EMOJI['users']} Музыкальная очередь",
        description=music.queue_preview(),
        color=0x111827,
    )
    if music.current:
        embed.add_field(name="Сейчас играет", value=music.current.title, inline=False)
    await ctx.reply(embed=embed)


@bot.command(name="nowplaying")
async def nowplaying_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    if not music.current:
        await ctx.reply("Сейчас ничего не играет.")
        return
    await ctx.reply(f"Сейчас играет: **{music.current.title}** | `{human_duration(music.current.duration)}`")


@bot.command(name="shuffle")
async def shuffle_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    if len(music.queue) < 2:
        await ctx.reply("В очереди слишком мало треков для перемешивания.")
        return
    import random
    items = list(music.queue)
    random.shuffle(items)
    music.queue = deque(items)
    await ctx.reply("Очередь перемешана.")


@bot.command(name="clear")
async def clear_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    music.queue.clear()
    await ctx.reply("Очередь очищена.")


@bot.command(name="remove")
async def remove_command(ctx: commands.Context, index: int) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    items = list(music.queue)
    if not 1 <= index <= len(items):
        await ctx.reply("Укажи существующий номер трека из очереди.")
        return
    removed = items.pop(index - 1)
    music.queue = deque(items)
    await ctx.reply(f"Удалён трек: **{removed.title}**")


@bot.command(name="volume")
async def volume_command(ctx: commands.Context, percent: int) -> None:
    if not ctx.guild:
        return
    if not 1 <= percent <= 200:
        await ctx.reply("Громкость должна быть от 1 до 200.")
        return
    music = bot.get_music_state(ctx.guild.id)
    music.volume = percent / 100
    vc = music.get_voice_client()
    if vc and vc.source and isinstance(vc.source, discord.PCMVolumeTransformer):
        vc.source.volume = music.volume
    await ctx.reply(f"Громкость установлена на **{percent}%**.")


@bot.command(name="loop")
async def loop_command(ctx: commands.Context) -> None:
    if not ctx.guild:
        return
    music = bot.get_music_state(ctx.guild.id)
    music.is_looping = not music.is_looping
    await ctx.reply(f"Повтор трека: **{'включён' if music.is_looping else 'выключен'}**.")
    await sync_music_panel(ctx.guild)


@bot.command(name="musichelp")
async def musichelp_command(ctx: commands.Context) -> None:
    embed = discord.Embed(
        title=f"{EMOJI['menu']} Команды музыки",
        description=(
            "`!play название или ссылка` — включить музыку\n"
            "`!skip` — скипнуть трек\n"
            "`!stop` — стоп и очистка очереди\n"
            "`!pause` — пауза\n"
            "`!resume` — продолжить\n"
            "`!queue` — посмотреть очередь\n"
            "`!nowplaying` — текущий трек\n"
            "`!shuffle` — перемешать очередь\n"
            "`!remove <номер>` — удалить трек\n"
            "`!clear` — очистить очередь\n"
            "`!volume 1-200` — громкость\n"
            "`!loop` — повтор текущего трека"
        ),
       color=0x111827,
    )
    await ctx.reply(embed=embed)


@bot.command(name="panel")
async def panel_help(ctx: commands.Context) -> None:
    await ctx.reply("Панель войса создаётся автоматически, когда в канал заходит первый человек.")


if not TOKEN:
    raise RuntimeError("Не найден TOKEN или DISCORD_TOKEN в .env")

bot.run(TOKEN)
