# -*- coding: utf-8 -*-

from __future__ import annotations

import os
import time
import wave
import logging
import threading
from collections import defaultdict

import discord
from discord import app_commands
from discord.ext import commands, voice_recv


def _require_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


DISCORD_BOT_TOKEN = _require_env("DISCORD_BOT_TOKEN")
DISCORD_GUILD_ID = int(_require_env("DISCORD_GUILD_ID"))
TARGET_GUILD = discord.Object(id=DISCORD_GUILD_ID)
VOICE_RECV_DEBUG_WS_PATH = os.getenv("VOICE_RECV_DEBUG_WS_PATH", "").strip()


discord.opus._load_default()


intents = discord.Intents.default()
intents.guilds = True
intents.voice_states = True
intents.members = True

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=intents)
log = logging.getLogger("discord.ext.voice_recv.example.audio_recv_test")


class PerUserWaveSink(voice_recv.AudioSink):
    def __init__(self, output_dir: str, file_prefix: str, on_packet, on_stream_open):
        super().__init__()
        self.output_dir = output_dir
        self.file_prefix = file_prefix
        self._on_packet = on_packet
        self._on_stream_open = on_stream_open
        self._writers: dict[str, wave.Wave_write] = {}
        self._paths: dict[str, str] = {}
        self._lock = threading.Lock()
        os.makedirs(self.output_dir, exist_ok=True)

    def wants_opus(self) -> bool:
        return False

    def _stream_key(self, user, ssrc: int) -> str:
        user_id = getattr(user, "id", None)
        if user_id is not None:
            return f"user_{int(user_id)}"
        return f"ssrc_{ssrc}"

    def _ensure_writer(self, stream_key: str) -> wave.Wave_write:
        with self._lock:
            writer = self._writers.get(stream_key)
            if writer is not None:
                return writer

            output_path = os.path.join(self.output_dir, f"{self.file_prefix}_{stream_key}.wav")
            writer = wave.open(output_path, "wb")
            writer.setnchannels(2)
            writer.setsampwidth(2)
            writer.setframerate(48000)
            self._writers[stream_key] = writer
            self._paths[stream_key] = output_path

        self._on_stream_open(stream_key, output_path)
        return writer

    def write(self, user, data: voice_recv.VoiceData) -> None:
        self._on_packet(user, data)
        pcm = data.pcm or b""
        if not pcm:
            return

        stream_key = self._stream_key(user, data.packet.ssrc)
        writer = self._ensure_writer(stream_key)
        writer.writeframes(pcm)

    def get_paths(self) -> dict[str, str]:
        with self._lock:
            return dict(self._paths)

    def cleanup(self):
        with self._lock:
            writers = list(self._writers.values())
            self._writers.clear()
            self._paths.clear()

        for writer in writers:
            try:
                writer.close()
            except Exception:
                pass


class ReceiveTraffic(commands.Cog):
    def __init__(self, client: commands.Bot):
        self.bot = client
        self._lock = threading.Lock()
        self._packet_count = defaultdict(int)
        self._recording_paths: dict[int, dict[str, str]] = {}
        self._recording_patterns: dict[int, str] = {}
        self._diag_last_emit: dict[int, float] = defaultdict(float)

    def _recording_stream_open_callback(self, guild_id: int):
        def callback(stream_key: str, output_path: str) -> None:
            with self._lock:
                mapping = self._recording_paths.setdefault(guild_id, {})
                mapping[stream_key] = output_path

        return callback

    @staticmethod
    def _format_recording_paths(paths: dict[str, str], *, limit: int = 6) -> str:
        if not paths:
            return "(No recordings created yet)"

        items = sorted(paths.items(), key=lambda item: item[0])
        preview = items[:limit]
        lines = [f"{key}: `{path}`" for key, path in preview]
        if len(items) > limit:
            lines.append(f"... and {len(items) - limit} more files")
        return "\n".join(lines)

    def _get_diag(self, guild_id: int) -> dict:
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            return {}
        voice_client = guild.voice_client
        if isinstance(voice_client, voice_recv.VoiceRecvClient):
            return voice_client.get_recv_diagnostics()
        return {}

    @staticmethod
    def _diag_summary(diag: dict) -> str:
        ws_recent = diag.get('voice_ws_recent_ops')
        ws_tail = "-"
        if isinstance(ws_recent, list) and ws_recent:
            ws_tail = ",".join(str(op) for op in ws_recent[-5:])

        return (
            f"strip_ok={diag.get('dave_strip_success', 0)} "
            f"parse_fail={diag.get('dave_parse_fail', 0)} "
            f"ranges_nonzero={diag.get('dave_ranges_nonzero', 0)} "
            f"inner_needed={diag.get('dave_needs_inner_decrypt_packets', 0)} "
            f"inner_ok={diag.get('dave_inner_decrypt_ok', 0)} "
            f"inner_err={diag.get('dave_inner_decrypt_err', 0)} "
            f"inner_no_session={diag.get('dave_inner_decrypt_no_session', 0)} "
            f"inner_no_uid={diag.get('dave_inner_decrypt_no_user_id', 0)} "
            f"inner_no_davey={diag.get('dave_inner_decrypt_no_davey', 0)} "
            f"inner_skipped={diag.get('dave_inner_decode_skipped', 0)} "
            f"nonce_gap={diag.get('dave_nonce_gap_events', 0)} "
            f"nonce_rewind={diag.get('dave_nonce_rewind_events', 0)} "
            f"nonce_seq_mismatch={diag.get('dave_nonce_seq_mismatch_events', 0)} "
            f"ws_total={diag.get('voice_ws_total', 0)} "
            f"ws_json={diag.get('voice_ws_transport_json', 0)} "
            f"ws_bin={diag.get('voice_ws_transport_binary', 0)} "
            f"ws_last={diag.get('voice_ws_last_op', '-')} "
            f"ws_tail={ws_tail} "
            f"dave_ws_total={diag.get('dave_ws_total', 0)} "
            f"dave_ws_last={diag.get('dave_ws_last_op', '-')} "
            f"ws_jsonl_ok={diag.get('voice_ws_jsonl_write_ok', 0)} "
            f"ws_jsonl_err={diag.get('voice_ws_jsonl_write_err', 0)} "
            f"media_total={diag.get('rtp_media_packets_total', 0)} "
            f"media_audio={diag.get('rtp_media_audio_packets', 0)} "
            f"media_video={diag.get('rtp_media_video_packets', 0)} "
            f"media_screen={diag.get('rtp_media_screen_packets', 0)} "
            f"media_rtx={diag.get('rtp_media_rtx_packets', 0)} "
            f"non_audio={diag.get('rtp_non_audio_packets_total', 0)} "
            f"unknown_drop={diag.get('rtp_unknown_ssrc_dropped', 0)} "
            f"unknown_q={diag.get('unknown_ssrc_queued', 0)} "
            f"unknown_flush={diag.get('unknown_ssrc_flushed', 0)} "
            f"unknown_flush_non_audio={diag.get('unknown_ssrc_flushed_non_audio', 0)} "
            f"unknown_expired={diag.get('unknown_ssrc_expired', 0)} "
            f"unknown_overflow={diag.get('unknown_ssrc_overflow', 0)} "
            f"opus_probe_hdr_err={diag.get('opus_probe_header_err', 0)} "
            f"opus_err_ratio={diag.get('opus_decode_err_ratio', 0.0)} "
            f"pcm_avg_bytes={diag.get('pcm_avg_bytes', 0.0)} "
            f"pcm_empty={diag.get('pcm_empty', 0)} "
            f"err_samples={diag.get('decode_err_sample_count', 0)}"
        )

    def _packet_callback(self, guild_id: int):
        def callback(user, data: voice_recv.VoiceData):
            now = time.time()
            with self._lock:
                self._packet_count[guild_id] += 1
                count = self._packet_count[guild_id]
                last_emit = self._diag_last_emit[guild_id]

            if count % 200 == 0:
                who = getattr(user, "id", None)
                packet = data.packet
                log.info(
                    "recv.diag.traffic guild=%s packets=%s user=%s ssrc=%s seq=%s ts=%s",
                    guild_id,
                    count,
                    who,
                    packet.ssrc,
                    packet.sequence,
                    packet.timestamp,
                )

            if now - last_emit >= 5.0:
                diag = self._get_diag(guild_id)
                latest_err = None
                err_samples = diag.get('decode_err_samples')
                if isinstance(err_samples, list) and err_samples:
                    latest_err = err_samples[-1]
                non_audio_samples = diag.get('non_audio_rtp_samples')
                latest_non_audio = None
                if isinstance(non_audio_samples, list) and non_audio_samples:
                    latest_non_audio = non_audio_samples[-1]
                log.info(
                    "recv.diag guild=%s dave_marker=%s %s",
                    guild_id,
                    diag.get('dave_marker_packets', 0),
                    self._diag_summary(diag),
                )
                if latest_err:
                    log.info(
                        "recv.diag.sample guild=%s stage=%s seq=%s len=%s frames=%s spf=%s head=%s",
                        guild_id,
                        latest_err.get('stage'),
                        latest_err.get('seq'),
                        latest_err.get('payload_len'),
                        latest_err.get('frames'),
                        latest_err.get('samples_per_frame'),
                        latest_err.get('head_hex'),
                    )
                if latest_non_audio:
                    log.info(
                        "recv.diag.media guild=%s kind=%s ssrc=%s seq=%s pt=%s len=%s known_user=%s",
                        guild_id,
                        latest_non_audio.get('kind'),
                        latest_non_audio.get('ssrc'),
                        latest_non_audio.get('seq'),
                        latest_non_audio.get('payload_type'),
                        latest_non_audio.get('packet_len'),
                        latest_non_audio.get('known_user'),
                    )
                with self._lock:
                    self._diag_last_emit[guild_id] = now

        return callback

    @app_commands.command(name="join", description="Join your current voice channel and start receiving traffic")
    @app_commands.guilds(TARGET_GUILD)
    async def join(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        member = interaction.user
        if not isinstance(member, discord.Member) or member.voice is None or member.voice.channel is None:
            await interaction.response.send_message("Please join a voice channel first.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        target_channel = member.voice.channel
        voice_client = guild.voice_client

        if voice_client is None:
            voice_client = await target_channel.connect(cls=voice_recv.VoiceRecvClient)
        elif voice_client.channel != target_channel:
            await voice_client.move_to(target_channel)

        if not isinstance(voice_client, voice_recv.VoiceRecvClient):
            await interaction.followup.send(
                "The active voice client is not VoiceRecvClient. Disconnect and try again.",
                ephemeral=True,
            )
            return

        if not voice_client.is_listening():
            session_ts = int(time.time())
            prefix = f"voice_recv_{guild.id}_{session_ts}"
            pattern = f"/tmp/{prefix}_<user_or_ssrc>.wav"
            sink = PerUserWaveSink(
                output_dir="/tmp",
                file_prefix=prefix,
                on_packet=self._packet_callback(guild.id),
                on_stream_open=self._recording_stream_open_callback(guild.id),
            )
            voice_client.listen(
                sink,
                debug_ws_path=VOICE_RECV_DEBUG_WS_PATH if VOICE_RECV_DEBUG_WS_PATH else None,
            )
            with self._lock:
                self._recording_paths[guild.id] = {}
                self._recording_patterns[guild.id] = pattern
        else:
            pattern = self._recording_patterns.get(guild.id)
            existing_sink = voice_client.sink
            if isinstance(existing_sink, PerUserWaveSink):
                with self._lock:
                    current = self._recording_paths.setdefault(guild.id, {})
                    current.update(existing_sink.get_paths())
                if pattern is None:
                    pattern = f"{existing_sink.output_dir}/{existing_sink.file_prefix}_<user_or_ssrc>.wav"
                    with self._lock:
                        self._recording_patterns[guild.id] = pattern
            if pattern is None:
                pattern = "(unknown)"

        with self._lock:
            file_count = len(self._recording_paths.get(guild.id, {}))

        diag = self._get_diag(guild.id)
        ws_jsonl_path = diag.get('voice_ws_jsonl_path', '') or '(disabled)'
        await interaction.followup.send(
            f"Joined `{target_channel}` and started receiving traffic."
            f"\nRecording pattern: `{pattern}`"
            f"\nRecording files created: {file_count}"
            f"\nWS JSONL: `{ws_jsonl_path}`",
            ephemeral=True,
        )

    @app_commands.command(name="leave", description="Leave the voice channel")
    @app_commands.guilds(TARGET_GUILD)
    async def leave(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None or guild.voice_client is None:
            await interaction.response.send_message("No active voice connection.", ephemeral=True)
            return

        voice_client = guild.voice_client
        if isinstance(voice_client, voice_recv.VoiceRecvClient) and isinstance(voice_client.sink, PerUserWaveSink):
            with self._lock:
                current = self._recording_paths.setdefault(guild.id, {})
                current.update(voice_client.sink.get_paths())

        with self._lock:
            output_paths = dict(self._recording_paths.get(guild.id, {}))
            pattern = self._recording_patterns.get(guild.id, "(unknown)")

        await guild.voice_client.disconnect()
        with self._lock:
            self._recording_paths.pop(guild.id, None)
            self._recording_patterns.pop(guild.id, None)

        await interaction.response.send_message(
            "Left the voice channel."
            f"\nRecording pattern: `{pattern}`"
            f"\nRecording files created: {len(output_paths)}"
            f"\n{self._format_recording_paths(output_paths)}",
            ephemeral=True,
        )

    @app_commands.command(name="stats", description="Show current received packet count")
    @app_commands.guilds(TARGET_GUILD)
    async def stats(self, interaction: discord.Interaction):
        guild = interaction.guild
        if guild is None:
            await interaction.response.send_message("This command can only be used in a server.", ephemeral=True)
            return

        with self._lock:
            count = self._packet_count[guild.id]
            output_paths = dict(self._recording_paths.get(guild.id, {}))
            pattern = self._recording_patterns.get(guild.id, "(unknown)")
        diag = self._get_diag(guild.id)

        await interaction.response.send_message(
            f"Current received packets: {count}\n"
            f"Recording pattern: `{pattern}`\n"
            f"Recording files created: {len(output_paths)}\n"
            f"{self._format_recording_paths(output_paths)}\n"
            f"WS JSONL: `{diag.get('voice_ws_jsonl_path', '') or '(disabled)'}`\n"
            f"diag: {self._diag_summary(diag)}",
            ephemeral=True,
        )


@bot.event
async def setup_hook():
    await bot.add_cog(ReceiveTraffic(bot))
    synced = await bot.tree.sync(guild=TARGET_GUILD)
    log.info("recv.setup synced=%s guild=%s", len(synced), DISCORD_GUILD_ID)


@bot.event
async def on_ready():
    assert bot.user is not None
    log.info("recv.ready user=%s user_id=%s", bot.user, bot.user.id)


if __name__ == "__main__":
    bot.run(DISCORD_BOT_TOKEN)
