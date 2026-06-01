import os

import discord
from discord.ext import commands
from dotenv import load_dotenv

from bot_commands import register_commands

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    raise RuntimeError("DISCORD_TOKEN nao definido no arquivo .env.")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.voice_states = True

bot = commands.Bot(command_prefix="!", intents=intents)
register_commands(bot)

if __name__ == "__main__":
    bot.run(TOKEN)
