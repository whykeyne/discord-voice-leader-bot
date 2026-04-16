import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()
TOKEN = os.getenv("TOKEN") or os.getenv("DISCORD_TOKEN", "")
GUILD_ID = int(os.getenv("GUILD_ID", "0") or 0)
CONTROL_CHANNEL_ID = int(os.getenv("CONTROL_CHANNEL_ID", "0") or 0)
CONTROL_CHANNEL_NAME = os.getenv("CONTROL_CHANNEL_NAME", "voice-control")
STATE_FILE = Path(os.getenv("STATE_FILE", "panel_state.json"))

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("voice_panel_bot")

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.voice_states = True


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


class VoiceRoomBot(commands.Bot):
    def __init__(self) -> None:
        super().__init__(command_prefix="!", intents=intents)
        self.room_states: Dict[int, RoomState] = {}
        self.control_channel_id: int = CONTROL_CHANNEL_ID
        self.temp_tasks: Dict[Tuple[int, str], asyncio.Task] = {}

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


BADGE_LABELS = {
    "mute": "🔇 Мут",
    "deafen": "🎧 Заглушён",
    "stream": "📺 Стрим",
    "camera": "📹 Камера",
}


async def safe_send(interaction: discord.Interaction, text: str, ephemeral: bool = True) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(text, ephemeral=ephemeral)
    else:
        await interaction.response.send_message(text, ephemeral=ephemeral)


async def safe_defer(interaction: discord.Interaction, ephemeral: bool = True) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=ephemeral)


def trunc(text: str, limit: int = 1024) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def is_admin(member: discord.Member) -> bool:
    perms = member.guild_permissions
    return perms.administrator or perms.manage_guild or perms.move_members or perms.manage_channels


def voice_badges(member: discord.Member, leader_id: int) -> List[str]:
    badges: List[str] = []
    if member.id == leader_id:
        badges.append("👑")
    if member.voice:
        if member.voice.mute:
            badges.append("🔇")
        if member.voice.deaf:
            badges.append("🎧")
        if member.voice.self_stream:
            badges.append("📺")
        if member.voice.self_video:
            badges.append("📹")
    return badges


def format_member_line(member: discord.Member, leader_id: int) -> str:
    badges = " ".join(voice_badges(member, leader_id))
    prefix = f"{badges} " if badges else ""
    return f"{prefix}{member.mention}"


def member_status_text(member: discord.Member) -> str:
    if not member.voice:
        return "без статуса"
    bits = []
    if member.voice.mute:
        bits.append(BADGE_LABELS["mute"])
    if member.voice.deaf:
        bits.append(BADGE_LABELS["deafen"])
    if member.voice.self_stream:
        bits.append(BADGE_LABELS["stream"])
    if member.voice.self_video:
        bits.append(BADGE_LABELS["camera"])
    return " • ".join(bits) if bits else "в войсе"


def can_manage_target(actor: discord.Member, target: discord.Member) -> bool:
    return actor.guild_permissions.administrator or target.top_role < actor.top_role


def parse_duration_minutes(value: str) -> Optional[int]:
    raw = value.strip().lower()
    if not raw:
        return None
    try:
        minutes = int(raw)
    except ValueError:
        return None
    if minutes < 0:
        return None
    return minutes


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

    if create_if_missing and guild.me and guild.me.guild_permissions.manage_channels:
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


def panel_colour(channel: discord.VoiceChannel | discord.StageChannel, locked: bool, humans_count: int) -> discord.Colour:
    if humans_count == 0:
        return discord.Colour.dark_grey()
    if locked:
        return discord.Colour.orange()
    if isinstance(channel, discord.StageChannel):
        return discord.Colour.purple()
    return discord.Colour.blurple()


def make_room_embed(guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel, state: RoomState) -> discord.Embed:
    humans = [m for m in channel.members if not m.bot]
    leader = guild.get_member(state.leader_id)
    leader_text = leader.mention if leader else f"<@{state.leader_id}>"
    member_lines = "\n".join(format_member_line(m, state.leader_id) for m in humans) or "Никого нет"
    limit_text = "∞" if channel.user_limit == 0 else str(channel.user_limit)
    perms_everyone = channel.overwrites_for(guild.default_role)
    locked = perms_everyone.connect is False
    created_ts = int(datetime.fromisoformat(state.created_at).timestamp())

    embed = discord.Embed(
        title=f"✨ Премиум-панель комнаты · {channel.name}",
        description=(
            "**Управляй войсом через красивые кнопки ниже.**\n"
            "Лидер комнаты назначается автоматически, когда первым заходит в пустой канал."
        ),
        colour=panel_colour(channel, locked, len(humans)),
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name="👑 Лидер", value=leader_text, inline=True)
    embed.add_field(name="👥 Онлайн", value=str(len(humans)), inline=True)
    embed.add_field(name="🚪 Лимит", value=limit_text, inline=True)
    embed.add_field(
        name="📍 Состояние комнаты",
        value=(
            f"{'🔒 Закрыта' if locked else '🔓 Открыта'}\n"
            f"{'🎭 Stage' if isinstance(channel, discord.StageChannel) else '🔊 Voice'}\n"
            f"Создана: <t:{created_ts}:R>"
        ),
        inline=True,
    )
    embed.add_field(
        name="🛠️ Что можно делать",
        value=(
            "• мут / размут\n"
            "• заглушить / снять заглушение\n"
            "• кик из войса\n"
            "• передать лидерство\n"
            "• сменить лимит\n"
            "• закрыть / открыть комнату"
        ),
        inline=True,
    )
    embed.add_field(
        name="💡 Подсказка",
        value=(
            "Нажимаешь действие → выбираешь участника → указываешь причину и время.\n"
            "Временные меры снимаются автоматически, но сбросятся после перезапуска бота."
        ),
        inline=True,
    )
    embed.add_field(name="🧑‍🤝‍🧑 Участники", value=trunc(member_lines), inline=False)
    embed.set_footer(text=f"room:{channel.id} | premium-voice-panel")
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    return embed


async def sync_room_panel(channel: discord.VoiceChannel | discord.StageChannel) -> None:
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
        if message:
            await message.edit(content=None, embed=embed, view=view)
        else:
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
    state.leader_id = state.pick_next_leader([m.id for m in humans]) or humans[0].id
    bot.save_state()
    await sync_room_panel(channel)
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
        await safe_send(interaction, "Чтобы управлять комнатой, нужно находиться именно в этом войсе.")
        return False
    if is_admin(member):
        return True
    state = await get_or_create_room_state(room_channel)
    if state and state.leader_id == member.id:
        return True
    await safe_send(interaction, "У тебя нет прав на управление этой комнатой.")
    return False


async def schedule_voice_revert(target: discord.Member, action: str, minutes: Optional[int]) -> None:
    if not minutes or minutes <= 0:
        return
    key = (target.id, action)
    existing = bot.temp_tasks.get(key)
    if existing and not existing.done():
        existing.cancel()

    async def worker() -> None:
        try:
            await asyncio.sleep(minutes * 60)
            if action == "mute" and target.voice:
                await target.edit(mute=False, reason="Auto unmute by voice panel timer")
            elif action == "deafen" and target.voice:
                await target.edit(deafen=False, reason="Auto undeafen by voice panel timer")
            if target.voice and isinstance(target.voice.channel, (discord.VoiceChannel, discord.StageChannel)):
                await sync_room_panel(target.voice.channel)
        except asyncio.CancelledError:
            return
        except Exception as exc:
            log.warning("Temp action revert failed for %s %s: %s", target.id, action, exc)
        finally:
            bot.temp_tasks.pop(key, None)

    bot.temp_tasks[key] = asyncio.create_task(worker())


class ActionConfigModal(discord.ui.Modal):
    def __init__(self, room_id: int, action: str, target_id: int) -> None:
        title_map = {
            "kick": "⛔ Отключение из войса",
            "mute": "🔇 Настройка мута",
            "unmute": "🔊 Снятие мута",
            "deafen": "🎧 Настройка заглушения",
            "undeafen": "🎙️ Снятие заглушения",
            "leader": "👑 Передача лидерства",
        }
        super().__init__(title=title_map.get(action, "Действие"), timeout=180)
        self.room_id = room_id
        self.action = action
        self.target_id = target_id

        reason_placeholder = {
            "kick": "Например: шумит, мешает, рейд",
            "mute": "Например: спам, орёт в войсе",
            "deafen": "Например: временно убрать звук",
            "leader": "Например: передаю владельцу комнаты",
        }.get(action, "Необязательно")

        self.reason = discord.ui.TextInput(
            label="Причина",
            placeholder=reason_placeholder,
            required=False,
            max_length=120,
        )
        self.add_item(self.reason)

        if action in {"mute", "deafen"}:
            self.duration = discord.ui.TextInput(
                label="Время в минутах",
                placeholder="Оставь пустым для бессрочно. Например: 5",
                required=False,
                max_length=4,
            )
            self.add_item(self.duration)
        else:
            self.duration = None

    async def on_submit(self, interaction: discord.Interaction) -> None:
        channel = bot.get_channel(self.room_id)
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return

        actor = interaction.user
        if not isinstance(actor, discord.Member):
            await safe_send(interaction, "Нужно использовать это на сервере.")
            return

        target = find_member_in_channel(channel, self.target_id)
        if not target and self.action != "leader":
            await safe_send(interaction, "Участник уже не в этом войсе.")
            return
        if target and not can_manage_target(actor, target):
            await safe_send(interaction, "Нельзя управлять участником с такой же или более высокой ролью.")
            return

        minutes = None
        if self.duration is not None:
            minutes = parse_duration_minutes(str(self.duration))
            if str(self.duration).strip() and minutes is None:
                await safe_send(interaction, "Время должно быть целым числом минут, например 5.")
                return

        reason_text = str(self.reason).strip() or "без причины"
        audit_reason = f"Voice panel by {actor} | {self.action} | {reason_text}"

        try:
            if self.action == "kick":
                assert target is not None
                await target.move_to(None, reason=audit_reason)
                result = f"⛔ {target.mention} отключён от войса. Причина: **{reason_text}**."
            elif self.action == "mute":
                assert target is not None
                await target.edit(mute=True, reason=audit_reason)
                await schedule_voice_revert(target, "mute", minutes)
                timer_text = f" на **{minutes} мин.**" if minutes else ""
                result = f"🔇 {target.mention} получил мут{timer_text}. Причина: **{reason_text}**."
            elif self.action == "unmute":
                assert target is not None
                await target.edit(mute=False, reason=audit_reason)
                task = bot.temp_tasks.pop((target.id, "mute"), None)
                if task and not task.done():
                    task.cancel()
                result = f"🔊 С {target.mention} снят мут. Причина: **{reason_text}**."
            elif self.action == "deafen":
                assert target is not None
                await target.edit(deafen=True, reason=audit_reason)
                await schedule_voice_revert(target, "deafen", minutes)
                timer_text = f" на **{minutes} мин.**" if minutes else ""
                result = f"🎧 {target.mention} заглушён{timer_text}. Причина: **{reason_text}**."
            elif self.action == "undeafen":
                assert target is not None
                await target.edit(deafen=False, reason=audit_reason)
                task = bot.temp_tasks.pop((target.id, "deafen"), None)
                if task and not task.done():
                    task.cancel()
                result = f"🎙️ С {target.mention} снято заглушение. Причина: **{reason_text}**."
            elif self.action == "leader":
                state = await get_or_create_room_state(channel)
                if not state:
                    await safe_send(interaction, "Не удалось получить состояние комнаты.")
                    return
                target = find_member_in_channel(channel, self.target_id)
                if not target:
                    await safe_send(interaction, "Новый лидер уже не в комнате.")
                    return
                state.leader_id = target.id
                state.add_member(target.id)
                bot.save_state()
                result = f"👑 Лидер комнаты теперь {target.mention}. Причина: **{reason_text}**."
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
        await safe_send(interaction, result)


class MemberActionSelect(discord.ui.Select):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int, action: str, actor_id: int) -> None:
        self.bot_ref = bot_ref
        self.room_id = room_id
        self.action = action
        self.actor_id = actor_id
        options = self._build_options()
        placeholders = {
            "kick": "⛔ Выбери, кого отключить",
            "mute": "🔇 Выбери, кого замутить",
            "unmute": "🔊 Выбери, кому снять мут",
            "deafen": "🎧 Выбери, кого заглушить",
            "undeafen": "🎙️ Выбери, кому снять заглушение",
            "leader": "👑 Выбери нового лидера",
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
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=member_status_text(member)[:100],
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
        await interaction.response.send_modal(ActionConfigModal(self.room_id, self.action, int(self.values[0])))


class MemberActionView(discord.ui.View):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int, action: str, actor_id: int) -> None:
        super().__init__(timeout=120)
        self.add_item(MemberActionSelect(bot_ref, room_id, action, actor_id))


class LimitRoomModal(discord.ui.Modal, title="👥 Изменение лимита комнаты"):
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
            await safe_send(interaction, f"👥 Лимит комнаты установлен: **{value}**.")
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает права Manage Channels.")
        except discord.HTTPException:
            await safe_send(interaction, "Не удалось изменить лимит комнаты.")


class RoomPanelView(discord.ui.View):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int = 0) -> None:
        super().__init__(timeout=None)
        self.bot_ref = bot_ref
        self.room_id = room_id

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

    @discord.ui.button(label="Обновить", style=discord.ButtonStyle.secondary, emoji="🔄", row=0, custom_id="room_refresh")
    async def refresh_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        await sync_room_panel(channel)
        await safe_send(interaction, "✨ Панель обновлена.")

    @discord.ui.button(label="Кик", style=discord.ButtonStyle.danger, emoji="⛔", row=0, custom_id="room_kick")
    async def kick_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**Кого отключить от войса?**\nВыбери участника ниже, затем откроется форма с причиной.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "kick", interaction.user.id),
        )

    @discord.ui.button(label="Мут", style=discord.ButtonStyle.primary, emoji="🔇", row=0, custom_id="room_mute")
    async def mute_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**Кого замутить?**\nВыбери участника — потом сможешь указать причину и время.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "mute", interaction.user.id),
        )

    @discord.ui.button(label="Размут", style=discord.ButtonStyle.success, emoji="🔊", row=0, custom_id="room_unmute")
    async def unmute_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**С кого снять мут?**\nВыбери участника ниже.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "unmute", interaction.user.id),
        )

    @discord.ui.button(label="Лидер", style=discord.ButtonStyle.primary, emoji="👑", row=0, custom_id="room_leader_transfer")
    async def leader_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**Кому передать лидерство?**\nВыбери участника, потом подтверди передачу.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "leader", interaction.user.id),
        )

    @discord.ui.button(label="Заглушить", style=discord.ButtonStyle.secondary, emoji="🎧", row=1, custom_id="room_deafen")
    async def deafen_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**Кого заглушить?**\nВыбери участника — потом сможешь указать причину и время.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "deafen", interaction.user.id),
        )

    @discord.ui.button(label="Снять заглуш.", style=discord.ButtonStyle.success, emoji="🎙️", row=1, custom_id="room_undeafen")
    async def undeafen_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message(
            "**С кого снять заглушение?**\nВыбери участника ниже.",
            ephemeral=True,
            view=MemberActionView(bot, channel.id, "undeafen", interaction.user.id),
        )

    @discord.ui.button(label="Лимит", style=discord.ButtonStyle.secondary, emoji="👥", row=1, custom_id="room_limit")
    async def limit_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_modal(LimitRoomModal(channel.id, channel.user_limit))

    @discord.ui.button(label="Lock / Unlock", style=discord.ButtonStyle.secondary, emoji="🔐", row=1, custom_id="room_lock_toggle")
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
            await safe_send(interaction, "🔓 Комната открыта." if currently_locked else "🔒 Комната закрыта.")
        except discord.Forbidden:
            await safe_send(interaction, "Боту не хватает права Manage Channels / Manage Roles.")
        except discord.HTTPException:
            await safe_send(interaction, "Не удалось изменить доступ к комнате.")


@bot.event
async def on_ready() -> None:
    log.info("Logged in as %s (%s)", bot.user, bot.user.id if bot.user else "unknown")
    for guild in bot.guilds:
        await get_control_channel(guild, create_if_missing=True)
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
            state.add_member(member.id)
            humans = [m for m in after.channel.members if not m.bot]
            if len(humans) == 1:
                state.leader_id = member.id
            bot.save_state()
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
    await safe_send(interaction, f"✨ Канал панелей установлен: {channel.mention}")


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
    await safe_send(interaction, f"✨ Панели синхронизированы в {control_channel.mention}.")


if not TOKEN:
    raise RuntimeError("Не найден TOKEN или DISCORD_TOKEN в .env")

bot.run(TOKEN)
