import discord
from discord.ext import commands
import logging
from dotenv import load_dotenv
import os
import asyncio



load_dotenv()
token = os.getenv('DISCORD_TOKEN')

# Logging
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

# --------------------------------
# eventos e comandos básicos
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
# Controle do Pomodoro (estado)
# -----------------------------
# guardaremos a task e um Event para parar
bot.pomodoro_task: asyncio.Task | None = None
bot.pomodoro_stop_event: asyncio.Event | None = None

# -----------------------------
# Função utilitária: tocar alarme
# -----------------------------
async def play_alarm(ctx):
    """
    Toca alarm.mp3 se existir e o bot estiver em canal de voz.
    Caso contrário, usa fallback de mensagem no canal.
    Observação: requer FFmpeg + PyNaCl para tocar áudio com sucesso.
    """
    vc = ctx.voice_client
    alarm_file = "alarm.mp3"

    # se estiver conectado ao canal de voz e houver o arquivo, tenta tocar
    if vc is not None and vc.is_connected() and os.path.exists(alarm_file):
        try:
            if vc.is_playing():
                vc.stop()
            source = discord.FFmpegPCMAudio(alarm_file)
            vc.play(source)
            logger.info("Tocando alarm.mp3 no canal de voz.")
            # espera terminar de tocar
            while vc.is_playing():
                await asyncio.sleep(0.5)
            return
        except Exception as e:
            logger.exception("Erro ao tocar alarm.mp3: %s", e)
            # continua para fallback

    # fallback simples: enviar mensagem no canal (mais confiável)
    try:
        await ctx.send("@here ⏰ **Alarme!**")
    except Exception as e:
        logger.warning("Não foi possível enviar mensagem de alarme no canal: %s", e)

    # fallback: enviar mensagem e mencionar canal
    try:
        await ctx.send("@here ⏰ **Alarme!**")
    except Exception:
        logger.warning("Não foi possível enviar mensagem de alarme no canal.")

# -----------------------------
# Função utilitária: mutar/desmutar membros no canal de voz
# -----------------------------
async def set_mute_for_channel(channel: discord.VoiceChannel, mute: bool, reason: str = None):
    """
    Tenta setar server mute/unmute para todos os membros do channel (exceto bots).
    Verifica se o bot tem permissão antes de tentar.
    """
    # checa permissão do bot no guild
    me = channel.guild.me
    if not me.guild_permissions.mute_members:
        logger.warning("Bot NÃO possui permissão 'mute_members' neste servidor; não será possível mutar usuários.")
        return

    for member in channel.members:
        if member.bot:
            continue
        try:
            # server mute (edit) — requer permissão de Mute Members
            await member.edit(mute=mute, reason=reason)
        except discord.Forbidden:
            logger.warning("Sem permissão para alterar mute de %s", member)
        except Exception as e:
            logger.exception("Erro ao alterar mute de %s: %s", member, e)

# -----------------------------
# Loop principal do Pomodoro
# -----------------------------
async def pomodoro_loop(ctx, work_minutes: int, break_minutes: int, stop_event: asyncio.Event):
    """
    Loop infinito: work -> alarm -> break -> alarm -> repeat
    stop_event é verificado para interromper o loop.
    """
    author = ctx.author
    channel = None

    # tenta determinar o canal de voz do autor (se não encontrar, erro)
    if author.voice and author.voice.channel:
        channel = author.voice.channel
    else:
        await ctx.send("Você precisa estar em um canal de voz para iniciar o Pomodoro.")
        return

    # garante que o bot esteja no mesmo canal (tenta conectar)
    try:
        if ctx.voice_client is None:
            await channel.connect()
            await ctx.send(f"Conectei ao canal de voz **{channel.name}** para gerenciar mutes do Pomodoro.")
        else:
            # se já conectado em outro canal, move para o canal do autor
            if ctx.voice_client.channel.id != channel.id:
                await ctx.voice_client.move_to(channel)
                await ctx.send(f"Movido para o canal **{channel.name}** para iniciar Pomodoro.")
    except RuntimeError as e:
        # comum quando falta PyNaCl
        logger.exception("Erro ao conectar ao canal de voz (PyNaCl?).")
        await ctx.send("Não consigo conectar ao canal de voz: verifique se a biblioteca `PyNaCl` está instalada.")
        return
    except discord.Forbidden:
        logger.exception("Sem permissão para conectar ao canal de voz.")
        await ctx.send("Não tenho permissão para conectar ao canal de voz.")
        return
    except Exception as e:
        logger.exception("Erro ao conectar/mover para canal de voz: %s", e)
        await ctx.send(f"Erro ao conectar ao canal de voz: {e}")
        return

    # converte minutos para segundos
    work_seconds = max(1, int(work_minutes)) * 60
    break_seconds = max(1, int(break_minutes)) * 60

    logger.info("Iniciando loop Pomodoro: trabalho %d min, pausa %d min", work_minutes, break_minutes)

    cycle = 1
    try:
        while not stop_event.is_set():
            # --- POMODORO (trabalho) ---
            await ctx.send(f"\n🚀 **Pomodoro {cycle}**: focar por **{work_minutes}** minutos. Mutando usuários...")
            # recaptura canal (pode ter mudado)
            voice_chan = ctx.voice_client.channel if ctx.voice_client else channel
            if voice_chan:
                await set_mute_for_channel(voice_chan, True, reason="Início do Pomodoro")
            else:
                logger.warning("Nenhum canal de voz encontrado para mutar.")

            # espera o tempo de trabalho verificando stop_event
            for _ in range(work_seconds):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
            if stop_event.is_set():
                break

            # alarme no fim do pomodoro
            await play_alarm(ctx)

            # --- PAUSA (break) ---
            await ctx.send(f"\n☕ **Pausa**: relaxe por **{break_minutes}** minutos. Desmutando usuários...")
            if voice_chan:
                await set_mute_for_channel(voice_chan, False, reason="Pausa do Pomodoro")

            for _ in range(break_seconds):
                if stop_event.is_set():
                    break
                await asyncio.sleep(1)
            if stop_event.is_set():
                break

            # alarme no fim da pausa
            await play_alarm(ctx)

            cycle += 1

    except asyncio.CancelledError:
        logger.info("Pomodoro task cancelada.")
    except Exception as e:
        logger.exception("Erro no loop do Pomodoro: %s", e)
        await ctx.send(f"Ocorreu um erro no loop do Pomodoro: {e}")
    finally:
        # tenta desmutar ao sair pra garantir que ninguém fique preso mutado
        try:
            vc_channel = ctx.voice_client.channel if ctx.voice_client else channel
            if vc_channel:
                await set_mute_for_channel(vc_channel, False, reason="Pomodoro finalizado")
        except Exception:
            logger.exception("Erro ao desmutar membros no final.")
        logger.info("Loop Pomodoro finalizado.")

# -----------------------------
# Comando !join
# -----------------------------

@bot.command()
async def join(ctx):
    # Check if the user is in a voice channel
    if ctx.author.voice is None:
        await ctx.send("Você precisa estar em um canal de voz para eu entrar!")
        return

    channel = ctx.author.voice.channel

    # If already connected, move to the user's channel
    if ctx.voice_client:
        await ctx.voice_client.move_to(channel)
        await ctx.send(f"Movido para: **{channel}**")
    else:
        await channel.connect()
        await ctx.send(f"Entrei no canal: **{channel}**")


# -----------------------------
# Comando !pomodoro
# -----------------------------
@bot.command(name="pomodoro")
async def pomodoro(ctx, *, times: str = None):
    """
    Inicia um loop de pomodoro infinito.
    Uso:
      !pomodoro                 -> usa defaults 30,10
      !pomodoro 45, 15          -> pomodoro 45min, pausa 15min
      !pomodoro 25 5            -> também funciona
    """
    if bot.pomodoro_task and not bot.pomodoro_task.done():
        await ctx.send("Já existe um Pomodoro rodando. Use `!stop` para parar antes de iniciar outro.")
        return

    # parse dos argumentos
    work_min = 30
    break_min = 10
    if times:
        # aceita "45, 15" ou "45 15" ou "45,15"
        raw = times.replace(",", " ").split()
        try:
            if len(raw) >= 1 and raw[0].strip():
                work_min = int(raw[0])
            if len(raw) >= 2 and raw[1].strip():
                break_min = int(raw[1])
        except ValueError:
            await ctx.send("Formato inválido. Use `!pomodoro 45, 15` (minutos, separados por vírgula).")
            return

    # cria evento de parada e task
    stop_event = asyncio.Event()
    bot.pomodoro_stop_event = stop_event
    task = asyncio.create_task(pomodoro_loop(ctx, work_min, break_min, stop_event))
    bot.pomodoro_task = task
    await ctx.send(f"Iniciando Pomodoro: {work_min} min foco / {break_min} min pausa. Use `!stop` para encerrar ou `!leave` para eu sair do canal.")

# -----------------------------
# Comando !stop para parar o pomodoro
# -----------------------------
@bot.command(name="stop")
async def stop_pomodoro(ctx):
    if not bot.pomodoro_task or bot.pomodoro_task.done():
        await ctx.send("Nenhum Pomodoro ativo no momento.")
        return

    # sinaliza parada
    if bot.pomodoro_stop_event:
        bot.pomodoro_stop_event.set()

    # tenta cancelar a task e aguardar
    try:
        bot.pomodoro_task.cancel()
    except Exception:
        logger.exception("Erro ao cancelar a task do Pomodoro.")

    # espera a task terminar (com timeout)
    try:
        await asyncio.wait_for(bot.pomodoro_task, timeout=10.0)
    except asyncio.TimeoutError:
        logger.warning("Task do Pomodoro não terminou em 10s; continuará em background até encerrar.")
    except Exception:
        logger.exception("Erro aguardando término da task do Pomodoro.")

    bot.pomodoro_task = None
    bot.pomodoro_stop_event = None
    await ctx.send("Pomodoro parado. Tentei desmutar os usuários do canal.")

# -----------------------------
# Comando !leave já existente (mantive com pequena melhoria)
# -----------------------------
@bot.command(name="leave")
async def leave(ctx):
    # se houver pomodoro rodando, pare antes de sair
    if bot.pomodoro_task and not bot.pomodoro_task.done():
        # pedir para parar automaticamente para limpar mutes
        if bot.pomodoro_stop_event:
            bot.pomodoro_stop_event.set()
        try:
            bot.pomodoro_task.cancel()
            await asyncio.wait_for(bot.pomodoro_task, timeout=5.0)
        except Exception:
            logger.debug("Task do Pomodoro não terminou imediatamente ao sair.")
        bot.pomodoro_task = None
        bot.pomodoro_stop_event = None

    if ctx.voice_client is None:
        await ctx.send("Eu não estou em nenhum canal de voz.")
        return

    try:
        # tenta desmutar canal antes de sair
        vc_channel = ctx.voice_client.channel
        if vc_channel:
            await set_mute_for_channel(vc_channel, False, reason="Bot saindo do canal")
        await ctx.voice_client.disconnect()
        await ctx.send("Saí do canal de voz.")
    except Exception:
        logger.exception("Erro ao desconectar do canal de voz.")
        await ctx.send("Ocorreu um erro ao tentar sair do canal de voz.")

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

# -----------------------------
# Inicia o bot
# -----------------------------
if token is None:
    logger.critical("DISCORD_TOKEN não encontrado nas variáveis de ambiente. Verifique o .env.")
    print("DISCORD_TOKEN não encontrado nas variáveis de ambiente. Verifique o .env.")
else:
    bot.run(token, log_handler=file_handler, log_level=logging.DEBUG)
