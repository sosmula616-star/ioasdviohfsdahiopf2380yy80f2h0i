"""
Web Server (Flask)
Provides REST API for the mini-app player and admin panel.
Serves the static HTML/JS front-end.
"""

import os
import asyncio
import json
from functools import wraps
from flask import Flask, jsonify, request, send_from_directory, abort
from flask_cors import CORS


def create_app(bot, player_manager, search_engine, admin_ids: set):
    app = Flask(
        __name__,
        static_folder=os.path.join(os.path.dirname(__file__), "webapp", "static"),
        template_folder=os.path.join(os.path.dirname(__file__), "webapp"),
    )
    # Разрешаем запросы с любого источника (GitHub Pages, localhost и т.д.)
    # Это необходимо так как статика хостится на GitHub Pages отдельно от API
    CORS(app, resources={r"/api/*": {"origins": "*"}}, supports_credentials=True)

    # ── helpers ──────────────────────────────────────────────────────────────

    def run_async(coro):
        """Run an async coroutine from Flask sync context."""
        future = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        return future.result(timeout=15)

    def require_admin(f):
        """Decorator: check X-User-Id header against admin list."""
        @wraps(f)
        def wrapper(*args, **kwargs):
            user_id = request.headers.get("X-User-Id", "")
            try:
                uid = int(user_id)
            except (ValueError, TypeError):
                abort(403)
            if uid not in admin_ids:
                abort(403)
            return f(*args, **kwargs)
        return wrapper

    # ── static files ─────────────────────────────────────────────────────────

    @app.route("/")
    @app.route("/player")
    def serve_player():
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "webapp"),
            "index.html"
        )

    @app.route("/static/<path:filename>")
    def serve_static(filename):
        return send_from_directory(
            os.path.join(os.path.dirname(__file__), "webapp", "static"),
            filename
        )

    # ── player API ───────────────────────────────────────────────────────────

    @app.route("/api/player/<int:guild_id>", methods=["GET"])
    def get_player_state(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"playing": False, "current": None, "queue": [], "queue_count": 0})
        return jsonify(player.get_state())

    @app.route("/api/player/<int:guild_id>/pause", methods=["POST"])
    def pause_player(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404
        run_async(player.pause())
        return jsonify({"ok": True})

    @app.route("/api/player/<int:guild_id>/resume", methods=["POST"])
    def resume_player(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404
        run_async(player.resume())
        return jsonify({"ok": True})

    @app.route("/api/player/<int:guild_id>/skip", methods=["POST"])
    def skip_track(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404
        run_async(player.skip())
        return jsonify({"ok": True})

    @app.route("/api/player/<int:guild_id>/stop", methods=["POST"])
    def stop_player(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404

        async def _stop():
            await player.stop()
            if player.voice_client:
                await player.voice_client.disconnect()
            player_manager.remove_player(guild_id)

        run_async(_stop())
        return jsonify({"ok": True})

    @app.route("/api/player/<int:guild_id>/volume", methods=["POST"])
    def set_volume(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404
        data = request.get_json(silent=True) or {}
        vol = float(data.get("volume", 0.5))
        player.set_volume(vol)
        return jsonify({"ok": True, "volume": player.volume})

    @app.route("/api/player/<int:guild_id>/loop", methods=["POST"])
    def set_loop(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404
        data = request.get_json(silent=True) or {}
        mode = data.get("mode", "none")
        if mode not in ("none", "track", "queue"):
            return jsonify({"error": "Invalid mode"}), 400
        player.loop_mode = mode
        return jsonify({"ok": True, "loop_mode": mode})

    # ── search API ───────────────────────────────────────────────────────────

    @app.route("/api/search", methods=["GET"])
    def search_music():
        query = request.args.get("q", "").strip()
        if not query:
            return jsonify({"results": []})
        try:
            results = run_async(search_engine.search(query, limit=10))
            return jsonify({"results": results})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/play", methods=["POST"])
    def play_track():
        """Add a track to the queue from the web app."""
        data = request.get_json(silent=True) or {}
        guild_id = int(data.get("guild_id", 0))
        track = data.get("track")
        user_id = data.get("user_id")
        channel_id = data.get("channel_id")

        if not guild_id or not track:
            return jsonify({"error": "Missing guild_id or track"}), 400

        guild = bot.get_guild(guild_id)
        if not guild:
            return jsonify({"error": "Guild not found"}), 404

        # Найти голосовой канал пользователя
        voice_channel = None
        if channel_id:
            voice_channel = guild.get_channel(int(channel_id))
        elif user_id:
            member = guild.get_member(int(user_id))
            if member and member.voice:
                voice_channel = member.voice.channel

        async def _play():
            text_ch = guild.text_channels[0] if guild.text_channels else None
            player = player_manager.get_or_create(guild_id, text_ch)
            if voice_channel:
                await player.connect(voice_channel)
            track["requester"] = "Web Player"
            await player.add_to_queue(track)

        try:
            run_async(_play())
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # ── admin API ────────────────────────────────────────────────────────────

    @app.route("/api/admin/check", methods=["GET"])
    def admin_check():
        user_id = request.headers.get("X-User-Id", "")
        try:
            uid = int(user_id)
            is_admin = uid in admin_ids
        except (ValueError, TypeError):
            is_admin = False
        return jsonify({"is_admin": is_admin})

    @app.route("/api/admin/guilds", methods=["GET"])
    @require_admin
    def admin_guilds():
        guilds = []
        for guild in bot.guilds:
            player = player_manager.get_player(guild.id)
            guilds.append({
                "id": str(guild.id),
                "name": guild.name,
                "member_count": guild.member_count,
                "icon": str(guild.icon.url) if guild.icon else None,
                "has_player": player is not None,
                "player_state": player.get_state() if player else None,
            })
        return jsonify({"guilds": guilds})

    @app.route("/api/admin/chains/<int:guild_id>", methods=["GET"])
    @require_admin
    def admin_get_chains(guild_id: int):
        from bot import chain_manager
        chains = chain_manager.get_guild_chains(guild_id)
        guild = bot.get_guild(guild_id)
        result = []
        for follower_id, leader_id in chains.items():
            follower = guild.get_member(follower_id) if guild else None
            leader = guild.get_member(leader_id) if guild else None
            result.append({
                "follower_id": str(follower_id),
                "follower_name": follower.display_name if follower else str(follower_id),
                "leader_id": str(leader_id),
                "leader_name": leader.display_name if leader else str(leader_id),
            })
        return jsonify({"chains": result})

    @app.route("/api/admin/chains/<int:guild_id>", methods=["DELETE"])
    @require_admin
    def admin_remove_chain(guild_id: int):
        from bot import chain_manager
        data = request.get_json(silent=True) or {}
        follower_id = int(data.get("follower_id", 0))
        removed = chain_manager.remove_chain(guild_id, follower_id)
        return jsonify({"ok": removed})

    @app.route("/api/admin/stop/<int:guild_id>", methods=["POST"])
    @require_admin
    def admin_stop_player(guild_id: int):
        player = player_manager.get_player(guild_id)
        if not player:
            return jsonify({"error": "No player"}), 404

        async def _stop():
            await player.stop()
            if player.voice_client:
                await player.voice_client.disconnect()
            player_manager.remove_player(guild_id)

        run_async(_stop())
        return jsonify({"ok": True})

    return app
