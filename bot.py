"""
Discord Music Bot - Main Bot Logic
Features:
 - Music playback via yt-dlp (YouTube, SoundCloud, Spotify via search)
 - /play, /skip, /stop, /queue, /chain commands
 - Chain system: follow user between voice channels
 - Web mini-app (Discord Activity) for player & admin panel
"""

import discord
from discord.ext import commands, tasks
from discord import app_commands
import asyncio
import os
import json
import aiohttp
from dotenv import load_dotenv
from music_search import MusicSearchEngine
from player_manager import PlayerManager, GuildPlayer
from chain_manager import ChainManager

load_dotenv()

# ───────────────────────────────────────────────
#  CONFIG
# ───────────────────────────────────────────────
TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
PUBLIC_URL = os.getenv("PUBLIC_URL", "http://localhost:8080")

# Admin IDs – вписаны прямо в код (дополнительно через .env)
HARDCODED_ADMIN_IDS = {
    123456789012345678,   # ← замени на свои Discord User ID
    987654321098765432,   # ← можно добавить ещё
}

# Загружаем из .env и объединяем
env_admins = set()
for aid in os.getenv("ADMIN_IDS", "").split(","):
    aid = aid.strip()
    if aid.isdigit():
        env_admins.add(int(aid))

ADMIN_IDS = HARDCODED_ADMIN_IDS | env_admins


# ───────────────────────────────────────────────
#  BOT SETUP
# ───────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.guilds = True

bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree

# Global managers
search_engine = MusicSearchEngine()
player_manager = PlayerManager(bot)
chain_manager = ChainManager(bot)


# ───────────────────────────────────────────────
#  EVENTS
# ───────────────────────────────────────────────
@bot.event
async def on_ready():
    print(f"✅ Bot ready: {bot.user} (ID: {bot.user.id})")
    try:
        synced = await tree.sync()
        print(f"⚡ Synced {len(synced)} slash commands")
    except Exception as e:
        print(f"❌ Sync error: {e}")

    # Запускаем веб-сервер в отдельном потоке
    import threading
    from web_server import create_app
    app = create_app(bot, player_manager, search_engine, ADMIN_IDS)

    def run_web():
        host = os.getenv("WEB_HOST", "0.0.0.0")
        port = int(os.getenv("WEB_PORT", 8080))
        app.run(host=host, port=port, debug=False, use_reloader=False)

    web_thread = threading.Thread(target=run_web, daemon=True)
    web_thread.start()
    print(f"🌐 Web server started on {PUBLIC_URL}")


@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Handle voice state changes for chain system."""
    # Не реагируем на самого бота
    if member == bot.user:
        return

    # Обрабатываем цепочки (chain)
    if before.channel != after.channel:
        await chain_manager.handle_voice_move(member, before.channel, after.channel)

    # Если бот остался один в канале — отключаемся
    guild_player = player_manager.get_player(member.guild.id)
    if guild_player and guild_player.voice_client:
        vc = guild_player.voice_client
        if vc.channel and len([m for m in vc.channel.members if not m.bot]) == 0:
            await asyncio.sleep(30)  # ждём 30 сек
            # Проверяем снова
            if vc.channel and len([m for m in vc.channel.members if not m.bot]) == 0:
                await guild_player.stop()
                await vc.disconnect()
                print(f"👋 Left {vc.channel.name} (empty channel)")


# ───────────────────────────────────────────────
#  SLASH COMMANDS
# ───────────────────────────────────────────────

@tree.command(name="play", description="Найти и включить музыку из YouTube/Spotify/SoundCloud")
@app_commands.describe(query="Название трека, исполнитель или ссылка")
async def play_cmd(interaction: discord.Interaction, query: str):
    """Search and play music."""
    # Проверяем голосовой канал
    if not interaction.user.voice or not interaction.user.voice.channel:
        await interaction.response.send_message(
            "❌ Зайди в голосовой канал!", ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    voice_channel = interaction.user.voice.channel
    guild_id = interaction.guild_id

    # Ищем треки
    results = await search_engine.search(query, limit=5)
    if not results:
        await interaction.followup.send("❌ Ничего не найдено!")
        return

    # Берём первый результат
    track = results[0]

    # Получаем/создаём плеер для этого сервера
    guild_player = player_manager.get_or_create(guild_id, interaction.channel)

    # Подключаемся к голосовому каналу
    await guild_player.connect(voice_channel)

    # Добавляем в очередь
    await guild_player.add_to_queue(track, interaction.user)

    # Формируем красивый embed
    embed = discord.Embed(
        title="🎵 Добавлено в очередь",
        description=f"**{track['title']}**\n{track['artist']}",
        color=0x9B59B6
    )
    embed.set_thumbnail(url=track.get('thumbnail', ''))
    embed.add_field(name="Платформа", value=track['platform_emoji'] + " " + track['platform'], inline=True)
    embed.add_field(name="Длительность", value=track.get('duration_str', 'N/A'), inline=True)

    # Кнопка открытия мини-приложения
    view = PlayerView(guild_id, PUBLIC_URL)
    await interaction.followup.send(embed=embed, view=view)


@tree.command(name="skip", description="Пропустить текущий трек")
async def skip_cmd(interaction: discord.Interaction):
    guild_player = player_manager.get_player(interaction.guild_id)
    if not guild_player or not guild_player.is_playing():
        await interaction.response.send_message("❌ Сейчас ничего не играет!", ephemeral=True)
        return
    await guild_player.skip()
    await interaction.response.send_message("⏭️ Пропущено!")


@tree.command(name="stop", description="Остановить музыку и покинуть канал")
async def stop_cmd(interaction: discord.Interaction):
    guild_player = player_manager.get_player(interaction.guild_id)
    if not guild_player:
        await interaction.response.send_message("❌ Бот не в канале!", ephemeral=True)
        return
    await guild_player.stop()
    if guild_player.voice_client:
        await guild_player.voice_client.disconnect()
    player_manager.remove_player(interaction.guild_id)
    await interaction.response.send_message("⏹️ Остановлено и покинул канал!")


@tree.command(name="queue", description="Показать текущую очередь треков")
async def queue_cmd(interaction: discord.Interaction):
    guild_player = player_manager.get_player(interaction.guild_id)
    if not guild_player or not guild_player.queue:
        await interaction.response.send_message("📭 Очередь пуста!", ephemeral=True)
        return

    queue_list = []
    for i, track in enumerate(list(guild_player.queue)[:10], 1):
        queue_list.append(f"`{i}.` {track['platform_emoji']} **{track['title']}** — {track['artist']}")

    embed = discord.Embed(
        title="🎶 Очередь воспроизведения",
        description="\n".join(queue_list),
        color=0x9B59B6
    )
    if guild_player.current_track:
        embed.set_footer(text=f"▶️ Сейчас играет: {guild_player.current_track['title']}")

    view = PlayerView(interaction.guild_id, PUBLIC_URL)
    await interaction.response.send_message(embed=embed, view=view)


@tree.command(name="chain", description="Привязать одного пользователя к другому (следовать за ним)")
@app_commands.describe(
    target="Пользователь, которого нужно привязать",
    follow="За кем следовать (оставь пустым для отвязки)"
)
async def chain_cmd(
    interaction: discord.Interaction,
    target: discord.Member,
    follow: discord.Member = None
):
    """Bind target user to follow another user between voice channels."""
    # Только модераторы или администраторы бота
    is_admin = (
        interaction.user.id in ADMIN_IDS
        or interaction.user.guild_permissions.move_members
    )
    if not is_admin:
        await interaction.response.send_message(
            "❌ Нет прав! Нужно право 'Перемещать участников' или быть администратором бота.",
            ephemeral=True
        )
        return

    if follow is None:
        # Отвязываем
        removed = chain_manager.remove_chain(interaction.guild_id, target.id)
        if removed:
            await interaction.response.send_message(
                f"🔓 **{target.display_name}** отвязан от цепочки."
            )
        else:
            await interaction.response.send_message(
                f"ℹ️ **{target.display_name}** не был привязан.", ephemeral=True
            )
        return

    if target.id == follow.id:
        await interaction.response.send_message("❌ Нельзя привязать человека к самому себе!", ephemeral=True)
        return

    chain_manager.add_chain(interaction.guild_id, target.id, follow.id)

    embed = discord.Embed(
        title="🔗 Цепочка создана",
        description=(
            f"**{target.display_name}** теперь следует за **{follow.display_name}**\n\n"
            f"Когда **{follow.display_name}** перейдёт в другой голосовой канал, "
            f"**{target.display_name}** будет автоматически перемещён туда же."
        ),
        color=0xE91E63
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="chains", description="Показать все активные привязки на сервере")
async def chains_cmd(interaction: discord.Interaction):
    is_admin = (
        interaction.user.id in ADMIN_IDS
        or interaction.user.guild_permissions.move_members
    )
    if not is_admin:
        await interaction.response.send_message("❌ Нет прав!", ephemeral=True)
        return

    guild_chains = chain_manager.get_guild_chains(interaction.guild_id)
    if not guild_chains:
        await interaction.response.send_message("📭 Нет активных привязок.", ephemeral=True)
        return

    lines = []
    for target_id, follow_id in guild_chains.items():
        target = interaction.guild.get_member(target_id)
        follow = interaction.guild.get_member(follow_id)
        t_name = target.display_name if target else f"<@{target_id}>"
        f_name = follow.display_name if follow else f"<@{follow_id}>"
        lines.append(f"🔗 **{t_name}** → **{f_name}**")

    embed = discord.Embed(
        title="⛓️ Активные цепочки",
        description="\n".join(lines),
        color=0xE91E63
    )
    await interaction.response.send_message(embed=embed)


@tree.command(name="player", description="Открыть мини-приложение плеера")
async def player_cmd(interaction: discord.Interaction):
    """Open the web player mini-app."""
    view = PlayerView(interaction.guild_id, PUBLIC_URL)
    embed = discord.Embed(
        title="🎵 Music Player",
        description="Нажми кнопку ниже, чтобы открыть плеер.",
        color=0x9B59B6
    )
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


# ───────────────────────────────────────────────
#  UI VIEWS & BUTTONS
# ───────────────────────────────────────────────

class PlayerView(discord.ui.View):
    """Button to open the web mini-app player."""
    def __init__(self, guild_id: int, public_url: str):
        super().__init__(timeout=None)
        url = f"{public_url}/player?guild={guild_id}"
        self.add_item(discord.ui.Button(
            label="🎵 Открыть плеер",
            style=discord.ButtonStyle.link,
            url=url
        ))


# ───────────────────────────────────────────────
#  ENTRY POINT
# ───────────────────────────────────────────────
if __name__ == "__main__":
    if not TOKEN:
        print("❌ DISCORD_TOKEN не задан в .env!")
        exit(1)
    bot.run(TOKEN)
