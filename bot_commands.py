from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import discord
from discord.ext import commands

logger = logging.getLogger(__name__)
bot: commands.Bot | None = None


def _clear_pomodoro_state(task: asyncio.Task[None] | None = None) -> None:
    active_bot = bot
    if active_bot is None:
        return
    if task is not None and getattr(active_bot, "pomodoro_task", None) is not task:
        return

    active_bot.pomodoro_task = None
    active_bot.pomodoro_stop_event = None
    active_bot.pomodoro_phase = None
    active_bot.pomodoro_channel_id = None


def _clear_stale_pomodoro_state() -> None:
    active_bot = bot
    if active_bot is None:
        return

    task = getattr(active_bot, "pomodoro_task", None)
    if task is not None and task.done():
        _clear_pomodoro_state(task)


def _author_voice_channel(ctx: commands.Context) -> discord.VoiceChannel | None:
    voice_state = getattr(ctx.author, "voice", None)
    return getattr(voice_state, "channel", None)


async def _sleep_with_stop(stop_event: asyncio.Event, seconds: int) -> None:
    for _ in range(max(0, seconds)):
        if stop_event.is_set():
            break
        await asyncio.sleep(1)


async def _ensure_voice_client(
    ctx: commands.Context,
    channel: discord.VoiceChannel,
    *,
    connect_message: str | None = None,
    move_message: str | None = None,
) -> str | None:
    vc = ctx.voice_client
    if vc is None:
        try:
            await channel.connect()
        except RuntimeError:
            logger.exception("Erro ao conectar ao canal de voz (PyNaCl?).")
            await ctx.send(
                "Nao consigo conectar ao canal de voz: verifique se a biblioteca `PyNaCl` esta instalada."
            )
            return None
        except discord.Forbidden:
            logger.exception("Sem permissao para conectar ao canal de voz.")
            await ctx.send("Nao tenho permissao para conectar ao canal de voz.")
            return None
        except Exception as exc:
            logger.exception("Erro ao conectar ao canal de voz: %s", exc)
            await ctx.send(f"Erro ao conectar ao canal de voz: {exc}")
            return None

        if connect_message:
            await ctx.send(connect_message.format(channel=channel.name))
        return "connected"

    if vc.channel.id != channel.id:
        try:
            await vc.move_to(channel)
        except discord.Forbidden:
            logger.exception("Sem permissao para mover para o canal de voz.")
            await ctx.send("Nao tenho permissao para mover o bot para o canal de voz.")
            return None
        except Exception as exc:
            logger.exception("Erro ao mover para o canal de voz: %s", exc)
            await ctx.send(f"Erro ao mover para o canal de voz: {exc}")
            return None

        if move_message:
            await ctx.send(move_message.format(channel=channel.name))
        return "moved"

    return "same"


async def play_alarm(ctx: commands.Context) -> None:
    """Toca alarm.mp3 se existir e o bot estiver em canal de voz."""
    vc = ctx.voice_client
    alarm_file = "alarm.mp3"

    if vc is not None and vc.is_connected() and os.path.exists(alarm_file):
        try:
            if vc.is_playing():
                vc.stop()

            source = discord.FFmpegPCMAudio(alarm_file)
            vc.play(source)
            logger.info("Tocando alarm.mp3 no canal de voz.")

            while vc.is_playing():
                await asyncio.sleep(0.5)
            return
        except Exception as exc:
            logger.exception("Erro ao tocar alarm.mp3: %s", exc)

    try:
        await ctx.send("@here ⏰ **Alarme!**")
    except Exception as exc:
        logger.warning("Nao foi possivel enviar mensagem de alarme no canal: %s", exc)


async def set_mute_for_channel(
    channel: discord.VoiceChannel,
    mute: bool,
    reason: str = "Pomodoro",
) -> None:
    """Aplica server mute/unmute em todos os membros humanos do canal."""
    me = channel.guild.me
    if me is None and bot is not None and bot.user is not None:
        me = channel.guild.get_member(bot.user.id)

    if me is None or not me.guild_permissions.mute_members:
        logger.warning(
            "Bot NAO possui permissao 'mute_members' neste servidor; nao sera possivel mutar usuarios."
        )
        return

    for member in channel.members:
        if member.bot or member.mute == mute:
            continue

        try:
            await member.edit(mute=mute, reason=reason)
        except discord.Forbidden:
            logger.warning("Sem permissao para alterar mute de %s", member)
        except Exception as exc:
            logger.exception("Erro ao alterar mute de %s: %s", member, exc)


async def pomodoro_loop(
    ctx: commands.Context,
    work_minutes: int,
    break_minutes: int,
    stop_event: asyncio.Event,
) -> None:
    """Loop de Pomodoro: foco -> alarme -> pausa -> alarme -> repete."""
    active_bot = bot
    if active_bot is None:
        raise RuntimeError("Bot nao inicializado.")

    current_task = asyncio.current_task()
    channel = None

    try:
        channel = _author_voice_channel(ctx)
        if channel is None:
            await ctx.send("Voce precisa estar em um canal de voz para iniciar o Pomodoro.")
            return

        status = await _ensure_voice_client(
            ctx,
            channel,
            connect_message="Conectei ao canal de voz **{channel}** para gerenciar mutes do Pomodoro.",
            move_message="Movido para o canal **{channel}** para iniciar Pomodoro.",
        )
        if status is None:
            return

        active_bot.pomodoro_channel_id = channel.id

        work_seconds = max(1, int(work_minutes)) * 60
        break_seconds = max(1, int(break_minutes)) * 60
        cycle = 1

        logger.info(
            "Iniciando loop Pomodoro: trabalho %d min, pausa %d min",
            work_minutes,
            break_minutes,
        )

        while not stop_event.is_set():
            active_bot.pomodoro_phase = "work"
            await ctx.send(
                f"\n🚀 **Pomodoro {cycle}**: focar por **{work_minutes}** minutos. Mutando usuarios..."
            )

            voice_chan = ctx.voice_client.channel if ctx.voice_client else channel
            if voice_chan is None:
                logger.warning("Nenhum canal de voz encontrado para mutar.")
                break

            await set_mute_for_channel(voice_chan, True, reason="Inicio do Pomodoro")
            await _sleep_with_stop(stop_event, work_seconds)
            if stop_event.is_set():
                break

            await play_alarm(ctx)

            active_bot.pomodoro_phase = "break"
            await ctx.send(
                f"\n☕ **Pausa**: relaxe por **{break_minutes}** minutos. Desmutando usuarios..."
            )

            voice_chan = ctx.voice_client.channel if ctx.voice_client else channel
            if voice_chan is None:
                logger.warning("Nenhum canal de voz encontrado para desmutar.")
                break

            await set_mute_for_channel(voice_chan, False, reason="Pausa do Pomodoro")
            await _sleep_with_stop(stop_event, break_seconds)
            if stop_event.is_set():
                break

            await play_alarm(ctx)
            cycle += 1

    except asyncio.CancelledError:
        logger.info("Pomodoro task cancelada.")
    except Exception as exc:
        logger.exception("Erro no loop do Pomodoro: %s", exc)
        with contextlib.suppress(Exception):
            await ctx.send(f"Ocorreu um erro no loop do Pomodoro: {exc}")
    finally:
        should_cleanup = active_bot.pomodoro_task is current_task
        if should_cleanup:
            _clear_pomodoro_state(current_task)

        try:
            if should_cleanup:
                vc_channel = ctx.voice_client.channel if ctx.voice_client else channel
                if vc_channel is not None:
                    await set_mute_for_channel(vc_channel, False, reason="Pomodoro finalizado")
        except Exception:
            logger.exception("Erro ao desmutar membros no final.")

        logger.info("Loop Pomodoro finalizado.")


async def _stop_active_pomodoro() -> bool:
    active_bot = bot
    if active_bot is None:
        return False

    task = getattr(active_bot, "pomodoro_task", None)
    if task is None or task.done():
        _clear_stale_pomodoro_state()
        return False

    stop_event = getattr(active_bot, "pomodoro_stop_event", None)
    if stop_event is not None:
        stop_event.set()

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await task

    _clear_pomodoro_state(task)
    return True


def register_commands(bot_instance: commands.Bot) -> None:
    global bot
    bot = bot_instance
    bot_instance.pomodoro_task = None
    bot_instance.pomodoro_stop_event = None
    bot_instance.pomodoro_phase = None
    bot_instance.pomodoro_channel_id = None

    @bot_instance.event
    async def on_ready() -> None:
        print(f"Logado como {bot_instance.user}")
        logger.info("Bot pronto como %s", bot_instance.user)

    @bot_instance.event
    async def on_voice_state_update(member: discord.Member, _before, after) -> None:
        if member.bot or after.channel is None:
            return

        if bot is None:
            return

        if bot.pomodoro_phase != "work":
            return

        if bot.pomodoro_channel_id != after.channel.id:
            return

        if after.mute:
            return

        try:
            await member.edit(mute=True, reason="Entrou durante Pomodoro em foco")
        except discord.Forbidden:
            logger.warning("Sem permissao para alterar mute de %s", member)
        except Exception as exc:
            logger.exception("Erro ao alterar mute de %s: %s", member, exc)

    @bot_instance.command(name="ping")
    async def ping(ctx: commands.Context) -> None:
        await ctx.send("Pong!")

    @bot_instance.command(name="join")
    async def join(ctx: commands.Context) -> None:
        _clear_stale_pomodoro_state()

        channel = _author_voice_channel(ctx)
        if channel is None:
            await ctx.send("Voce precisa estar em um canal de voz para usar esse comando.")
            return

        status = await _ensure_voice_client(
            ctx,
            channel,
            connect_message="Entrei no canal **{channel}**.",
            move_message="Me movi para o canal **{channel}**.",
        )
        if status is None:
            return

        bot_instance.pomodoro_channel_id = channel.id

        if bot_instance.pomodoro_task is not None and not bot_instance.pomodoro_task.done():
            if bot_instance.pomodoro_phase == "work":
                await set_mute_for_channel(channel, True, reason="Sincronizando canal do Pomodoro")
            elif bot_instance.pomodoro_phase == "break":
                await set_mute_for_channel(channel, False, reason="Sincronizando canal do Pomodoro")

        if status == "same":
            await ctx.send("Ja estou no seu canal de voz.")

    @bot_instance.command(name="pomodoro")
    async def pomodoro(
        ctx: commands.Context,
        work_minutes: int = 25,
        break_minutes: int = 5,
    ) -> None:
        _clear_stale_pomodoro_state()

        if bot_instance.pomodoro_task is not None and not bot_instance.pomodoro_task.done():
            await ctx.send("Ja existe um Pomodoro em execucao. Use `!stop` antes de iniciar outro.")
            return

        channel = _author_voice_channel(ctx)
        if channel is None:
            await ctx.send("Voce precisa estar em um canal de voz para iniciar o Pomodoro.")
            return

        status = await _ensure_voice_client(
            ctx,
            channel,
            connect_message="Conectei ao canal de voz **{channel}** para gerenciar mutes do Pomodoro.",
            move_message="Movido para o canal **{channel}** para iniciar Pomodoro.",
        )
        if status is None:
            return

        bot_instance.pomodoro_channel_id = channel.id

        stop_event = asyncio.Event()
        bot_instance.pomodoro_stop_event = stop_event
        task = asyncio.create_task(
            pomodoro_loop(ctx, work_minutes, break_minutes, stop_event)
        )
        bot_instance.pomodoro_task = task

        await ctx.send(
            f"Pomodoro iniciado: foco por **{work_minutes}** min e pausa por **{break_minutes}** min."
        )

    @bot_instance.command(name="stop")
    async def stop(ctx: commands.Context) -> None:
        stopped = await _stop_active_pomodoro()
        if stopped:
            await ctx.send("Pomodoro interrompido.")
        else:
            await ctx.send("Nao ha Pomodoro em execucao.")

    @bot_instance.command(name="leave")
    async def leave(ctx: commands.Context) -> None:
        stopped = await _stop_active_pomodoro()

        vc = ctx.voice_client
        if vc is None:
            if stopped:
                await ctx.send("Pomodoro interrompido.")
            else:
                await ctx.send("Nao estou conectado a nenhum canal de voz.")
            return

        try:
            await vc.disconnect()
        except Exception as exc:
            logger.exception("Erro ao desconectar do canal de voz: %s", exc)
            await ctx.send(f"Erro ao sair do canal de voz: {exc}")
            return

        bot_instance.pomodoro_channel_id = None

        if stopped:
            await ctx.send("Pomodoro interrompido e desconectado do canal de voz.")
        else:
            await ctx.send("Desconectei do canal de voz.")
