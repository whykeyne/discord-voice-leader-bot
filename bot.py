# bot.py
# версия с сохранёнными именами эмодзи как у пользователя

import discord
from discord.ext import commands
from discord import app_commands
import os

intents = discord.Intents.all()
bot = commands.Bot(command_prefix="!", intents=intents)

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
    "leader": "<:leader_crown:1494432117269070106>"
}

class ActionSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Mute", emoji="🔇", value="mute"),
            discord.SelectOption(label="Kick", emoji="🚪", value="kick"),
            discord.SelectOption(label="Leader", emoji="👑", value="leader"),
        ]
        super().__init__(placeholder="Выбрать действие", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_message(f"Выбрал: {self.values[0]}", ephemeral=True)

class VoicePanel(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(ActionSelect())

    @discord.ui.button(emoji=EMOJI["leader"], style=discord.ButtonStyle.secondary, row=1)
    async def leader(self, interaction, button):
        await interaction.response.send_message("Лидер", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["user"], style=discord.ButtonStyle.secondary, row=1)
    async def user(self, interaction, button):
        await interaction.response.send_message("Пользователь", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["users"], style=discord.ButtonStyle.secondary, row=1)
    async def users(self, interaction, button):
        await interaction.response.send_message("Участники", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["lock"], style=discord.ButtonStyle.secondary, row=1)
    async def lock(self, interaction, button):
        await interaction.response.send_message("Lock", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["settings"], style=discord.ButtonStyle.secondary, row=2)
    async def settings(self, interaction, button):
        await interaction.response.send_message("Settings", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["sparkle"], style=discord.ButtonStyle.secondary, row=2)
    async def sparkle(self, interaction, button):
        await interaction.response.send_message("Effect", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["kick"], style=discord.ButtonStyle.danger, row=2)
    async def kick(self, interaction, button):
        await interaction.response.send_message("Kick", ephemeral=True)

    @discord.ui.button(emoji=EMOJI["mic"], style=discord.ButtonStyle.success, row=2)
    async def mic(self, interaction, button):
        await interaction.response.send_message("Mic", ephemeral=True)

@bot.tree.command(name="panel")
async def panel(interaction: discord.Interaction):
    embed = discord.Embed(
        title=f"{EMOJI['sparkle']} Voice Panel",
        description=f"{EMOJI['menu']} Управление",
        color=0x0f172a
    )
    await interaction.response.send_message(embed=embed, view=VoicePanel())

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"Запущен как {bot.user}")

bot.run(os.getenv("TOKEN"))
