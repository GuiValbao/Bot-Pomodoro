import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio
import functions
import bot_commands



load_dotenv()
token = os.getenv('DISCORD_TOKEN')

#logging
file_handler = logging.FileHandler(filename='discord.log', encoding='utf-8', mode='w')
console_handler = logging.StreamHandler()
logging.basicConfig(
    level=logging.DEBUG,
    handlers=[file_handler, console_handler],
    format='%(asctime)s:%(levelname)s:%(name)s: %(message)s'
)

logger = logging.getLogger(__name__)

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
bot_commands.setup(bot)
bot.pomodoro_phase = None  # "work" ou "break"

# --------------------------------
# Eventos e comandos básicos
# --------------------------------
@bot.event
async def on_ready():
    logger.info(f'Ready to go, boss! Logged in as {bot.user} (id: {bot.user.id})')
    print(f'Ready to go, boss! Logged in as {bot.user.name}')


@bot.event
async def on_member_join(member):
    try:
        await member.send(f'Welcome, {member.name}!')
    except discord.Forbidden:
        logger.warning(f"Couldn't DM {member} (permission denied).")



@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if 'shit' in message.content.lower():
        try:
            await message.delete()
            await message.channel.send(f"{message.author.mention}, Não diga isso aqui.")
        except discord.Forbidden:
            logger.warning("Sem permissão para deletar mensagem ou enviar mensagem no canal.")
    await bot.process_commands(message)



# -----------------------------
# Handler global de erro de comando (melhora UX)
# -----------------------------
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        if isinstance(original, RuntimeError) and "PyNaCl" in str(original):
            await ctx.send(
                "Não consigo usar voz: a biblioteca `PyNaCl` não está instalada no ambiente do bot.\n"
                "Instale com `pip install pynacl` e reinicie o bot."
            )
            return
        if isinstance(original, discord.Forbidden):
            await ctx.send("Não tenho permissão para executar essa ação (discord.Forbidden).")
            return
    if isinstance(error, commands.CommandNotFound):
        return
    logger.exception("Erro ao executar comando: %s", error)
    await ctx.send(f"Ocorreu um erro ao executar o comando: `{error}`")



@bot.event
async def on_voice_state_update(member, before, after):
    # só age se houver pomodoro ativo
    if not bot.pomodoro_task or bot.pomodoro_task.done():
        return

    # só durante o trabalho
    if bot.pomodoro_phase != "work":
        return

    # entrou em um canal
    if before.channel is None and after.channel is not None:
        # precisa ser o canal do pomodoro
        if after.channel.id != bot.pomodoro_channel_id:
            return

        # não muta bots
        if member.bot:
            return

        try:
            await member.edit(
                mute=True,
                reason="Pomodoro em andamento (entrada no canal)"
            )
            logger.info(
                "Mutado %s ao entrar no canal durante Pomodoro",
                member.display_name
            )
        except Exception as e:
            logger.exception(
                "Erro ao mutar membro ao entrar no canal: %s",
                e
            )




# ------------------------------
# Controle do Pomodoro (estado)
# ------------------------------
#aguardaremos a task e um Event para parar
bot.pomodoro_task: asyncio.Task | None = None
bot.pomodoro_stop_event: asyncio.Event | None = None



# -----------------------------
#       Inicia o bot
# -----------------------------
if token is None:
    logger.critical("DISCORD_TOKEN não encontrado nas variáveis de ambiente. Verifique o .env.")
    print("DISCORD_TOKEN não encontrado nas variáveis de ambiente. Verifique o .env.")
else:
    bot.run(token, log_handler=file_handler, log_level=logging.DEBUG)
