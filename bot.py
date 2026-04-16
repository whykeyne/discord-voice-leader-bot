import json
import logging
import os
import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

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

# exactly with the names user provided
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
        self.temp_actions: Dict[int, Dict[str, object]] = {}

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


def make_room_embed(guild: discord.Guild, channel: discord.VoiceChannel | discord.StageChannel, state: RoomState) -> discord.Embed:
    humans = [m for m in channel.members if not m.bot]
    leader = guild.get_member(state.leader_id)
    leader_text = leader.mention if leader else f"<@{state.leader_id}>"
    member_lines = "\n".join(format_member_line(m, state.leader_id) for m in humans) or "Пусто"
    limit_text = "∞" if channel.user_limit == 0 else str(channel.user_limit)

    perms_everyone = channel.overwrites_for(guild.default_role)
    locked = perms_everyone.connect is False
    status_text = f"{EMOJI['lock']} Закрыта" if locked else f"{EMOJI['unlock']} Открыта"
    type_text = "Stage" if isinstance(channel, discord.StageChannel) else "Voice"

    embed = discord.Embed(
        title=f"{EMOJI['sparkle']}  {EMOJI['sparkle']}  ПАНЕЛЬ КОМНАТЫ",
        description=(
            f"{EMOJI['menu']}  Выбрать действие — меню сверху\n"
            "━━━━━━━━━━━━━━━━━━\n\n"
            f"{EMOJI['leader']} {EMOJI['leader']} — передать лидерство\n"
            f"{EMOJI['mute']} {EMOJI['mute']} — серверный мут\n"
            f"{EMOJI['deafen']} {EMOJI['deafen']} — заглушить\n"
            f"{EMOJI['loud']} {EMOJI['loud']} — вернуть звук\n"
            f"{EMOJI['kick']} {EMOJI['kick']} — отключить от войса\n"
            f"{EMOJI['lock']} {EMOJI['lock']} — закрыть комнату\n"
            f"{EMOJI['unlock']} {EMOJI['unlock']} — открыть комнату\n"
            f"{EMOJI['users']} {EMOJI['users']} — посмотреть лимит\n"
        ),
        color=0x0B1220,
        timestamp=datetime.now(timezone.utc),
    )
    embed.add_field(name=f"{EMOJI['leader']} Лидер", value=leader_text, inline=True)
    embed.add_field(name=f"{EMOJI['users']} Участники", value=str(len(humans)), inline=True)
    embed.add_field(name=f"{EMOJI['user']} Лимит", value=limit_text, inline=True)
    embed.add_field(name=f"{EMOJI['menu']} Статус", value=f"{status_text} · {type_text}", inline=False)
    embed.add_field(name=f"{EMOJI['users']} Сейчас в комнате", value=trunc(member_lines, 1024), inline=False)
    embed.add_field(name=f"{EMOJI['settings']} Канал", value=channel.mention, inline=False)
    if guild.icon:
        embed.set_thumbnail(url=guild.icon.url)
    embed.set_footer(text=f"room:{channel.id}")
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
        await safe_send(interaction, "Чтобы управлять комнатой, нужно быть в этом войсе.")
        return False
    if is_admin(member):
        return True
    state = await get_or_create_room_state(room_channel)
    if state and state.leader_id == member.id:
        return True
    await safe_send(interaction, "У тебя нет прав на управление этой комнатой.")
    return False


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
                    emoji=EMOJI["user"],
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
                state.leader_id = target.id
                state.add_member(target.id)
                bot.save_state()
                text = f"{EMOJI['leader']} Лидер комнаты теперь {target.mention}."
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


class MemberActionView(discord.ui.View):
    def __init__(self, bot_ref: VoiceRoomBot, room_id: int, action: str, actor_id: int) -> None:
        super().__init__(timeout=90)
        self.add_item(MemberActionSelect(bot_ref, room_id, action, actor_id))


class ActionReasonModal(discord.ui.Modal):
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
            discord.SelectOption(label="Кик", value="kick", description="Отключить от войса", emoji=EMOJI["kick"]),
            discord.SelectOption(label="Мут", value="mute", description="Серверный мут", emoji=EMOJI["mute"]),
            discord.SelectOption(label="Размут", value="unmute", description="Снять серверный мут", emoji=EMOJI["loud"]),
            discord.SelectOption(label="Заглушить", value="deafen", description="Полностью заглушить", emoji=EMOJI["deafen"]),
            discord.SelectOption(label="Снять заглушение", value="undeafen", description="Вернуть звук", emoji=EMOJI["undeafen"]),
            discord.SelectOption(label="Передать лидерство", value="leader", description="Назначить нового лидера", emoji=EMOJI["leader"]),
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
            options=[discord.SelectOption(label="Панель загружается", value="noop", emoji=EMOJI["menu"])],
            custom_id="action_picker:placeholder",
            disabled=True,
            row=0,
        )


class LimitRoomModal(discord.ui.Modal, title="Лимит комнаты"):
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


class RoomPanelView(discord.ui.View):
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

    @discord.ui.button(label="Лидер", emoji=EMOJI["leader"], style=discord.ButtonStyle.secondary, row=1, custom_id="room_leader")
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

    @discord.ui.button(label="Участник", emoji=EMOJI["user"], style=discord.ButtonStyle.secondary, row=1, custom_id="room_user_info")
    async def user_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        humans = [m.mention for m in channel.members if not m.bot]
        await safe_send(interaction, "\n".join(humans) or "Пусто")

    @discord.ui.button(label="Онлайн", emoji=EMOJI["users"], style=discord.ButtonStyle.secondary, row=1, custom_id="room_users")
    async def users_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        await safe_send(interaction, f"{EMOJI['users']} Сейчас в комнате: **{len([m for m in channel.members if not m.bot])}**")

    @discord.ui.button(label="Доступ", emoji=EMOJI["lock"], style=discord.ButtonStyle.secondary, row=1, custom_id="room_lock_toggle")
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

    @discord.ui.button(label="Лимит", emoji=EMOJI["settings"], style=discord.ButtonStyle.secondary, row=2, custom_id="room_limit")
    async def settings_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_modal(LimitRoomModal(channel.id, channel.user_limit))

    @discord.ui.button(label="Обновить", emoji=EMOJI["sparkle"], style=discord.ButtonStyle.secondary, row=2, custom_id="room_refresh")
    async def sparkle_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        await sync_room_panel(channel)
        await safe_send(interaction, f"{EMOJI['sparkle']} Панель обновлена.")

    @discord.ui.button(label="Кик", emoji=EMOJI["kick"], style=discord.ButtonStyle.danger, row=2, custom_id="room_kick")
    async def kick_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message("Выбери участника:", ephemeral=True, view=MemberActionView(bot, channel.id, "kick", interaction.user.id))

    @discord.ui.button(label="Звук", emoji=EMOJI["loud"], style=discord.ButtonStyle.success, row=2, custom_id="room_sound_restore")
    async def sound_btn(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        channel = self._get_channel(interaction)
        if not channel:
            await safe_send(interaction, "Комната уже недоступна.")
            return
        if not await ensure_control_rights(interaction, channel):
            return
        await interaction.response.send_message("Выбери участника:", ephemeral=True, view=MemberActionView(bot, channel.id, "undeafen", interaction.user.id))


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
    await safe_send(interaction, f"Канал панелей установлен: {channel.mention}")


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


if not TOKEN:
    raise RuntimeError("Не найден TOKEN или DISCORD_TOKEN в .env")

bot.run(TOKEN)
