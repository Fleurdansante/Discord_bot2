"""
VC入退室通知ボット（完全動作＋サイレントフォールバック対応版）
- 入退室通知（滞在時間・累計勉強時間付き）
- 23:59（日本時間）に当日入退室した人の勉強時間合計を通知
- 通知音・バッジを抑制（非対応環境では自動フォールバック）
- 再起動時に自動で通知チャンネルを設定
"""

from __future__ import annotations
import asyncio
import json
import logging
import os
import signal
import sys
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands, tasks
from discord import app_commands
from discord.abc import Messageable
from flask import Flask

# ===================== 定数 =====================
DATA_DIR = Path("data")
CONFIG_PATH = DATA_DIR / "config.json"
DAILY_TOTALS_PATH = DATA_DIR / "daily_totals.json"
JST = timezone(timedelta(hours=9))  # 日本時間

# ===================== ログ設定 =====================
def setup_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="[%Y-%m-%d %H:%M:%S]",
        stream=sys.stdout,
    )

# ===================== JSONユーティリティ =====================
def _ensure_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

def load_persisted_dest_channel_id() -> Optional[int]:
    try:
        if CONFIG_PATH.exists():
            with CONFIG_PATH.open("r", encoding="utf-8") as f:
                obj = json.load(f)
            val = obj.get("dest_channel_id")
            if isinstance(val, int):
                return val
            if isinstance(val, str) and val.isdigit():
                return int(val)
    except Exception as e:
        logging.getLogger("Persist").warning("config.json 読み込み失敗: %s", e)
    return None

def save_persisted_dest_channel_id(ch_id: Optional[int]) -> None:
    try:
        _ensure_data_dir()
        data = {"dest_channel_id": ch_id}
        tmp = CONFIG_PATH.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        tmp.replace(CONFIG_PATH)
        logging.getLogger("Persist").info("通知先（dest_channel_id=%s）を保存しました。", ch_id)
    except Exception as e:
        logging.getLogger("Persist").error("config.json 保存失敗: %s", e)

# ===================== Flask（ヘルスチェック） =====================
app = Flask(__name__)

@app.get("/")
def health():
    return "ok", 200

def run_web_server(port: int) -> None:
    app.run(host="0.0.0.0", port=port, threaded=True)

# ===================== Config =====================
@dataclass
class Config:
    token: str
    target_vc_id: int
    guild_id: Optional[int]
    log_level: str
    port: int

    @staticmethod
    def load() -> "Config":
        token = (os.getenv("DISCORD_TOKEN") or "").strip()
        if not token:
            raise RuntimeError("DISCORD_TOKEN が未設定です。")

        vc = os.getenv("TARGET_VOICE_CHANNEL_ID")
        if not vc or not vc.isdigit():
            raise RuntimeError("TARGET_VOICE_CHANNEL_ID が未設定または不正です。")
        target_vc_id = int(vc)

        gid = os.getenv("GUILD_ID")
        guild_id = int(gid) if gid and gid.isdigit() else None

        log_level = os.getenv("LOG_LEVEL", "INFO")
        port = int(os.getenv("PORT", "8000"))
        return Config(token, target_vc_id, guild_id, log_level, port)

# ===================== 共通送信関数 =====================
async def send_to_channel(bot: commands.Bot, channel_id: int, content: str) -> None:
    ch = bot.get_channel(channel_id)
    if ch is None:
        try:
            ch = await bot.fetch_channel(channel_id)
        except Exception as e:
            logging.getLogger("Send").error("チャンネル取得に失敗: %s", e)
            return

    try:
        await ch.send(content, suppress_notifications=True)
    except TypeError:
        await ch.send(content)
    except discord.Forbidden:
        logging.getLogger("Send").error("送信権限が不足しています（Send Messages）。")
    except Exception as e:
        logging.getLogger("Send").error("通知送信失敗: %s", e)

# ===================== VC通知Cog =====================
class VcNotifier(commands.Cog):
    def __init__(self, bot: "VcBot"):
        self.bot = bot
        self.log = logging.getLogger(self.__class__.__name__)
        self.dest_channel_id: Optional[int] = load_persisted_dest_channel_id()
        self.join_times: dict[int, float] = {}
        self.daily_total: dict[int, float] = self._load_daily_totals()
        self.active_users: set[int] = set()

    # ---- 永続化関連 ----
    def _load_daily_totals(self) -> dict[int, float]:
        if DAILY_TOTALS_PATH.exists():
            try:
                with DAILY_TOTALS_PATH.open("r", encoding="utf-8") as f:
                    return {int(k): float(v) for k, v in json.load(f).items()}
            except Exception as e:
                self.log.warning("daily_totals.json 読み込み失敗: %s", e)
        return {}

    def _save_daily_totals(self):
        try:
            _ensure_data_dir()
            with DAILY_TOTALS_PATH.open("w", encoding="utf-8") as f:
                json.dump(self.daily_total, f, ensure_ascii=False, indent=2)
            self.log.info("daily_totals.json に合計滞在時間を保存しました。")
        except Exception as e:
            self.log.error("daily_totals.json 保存失敗: %s", e)

    # ---- 通知 ----
    async def notify(self, message: str):
        if not self.dest_channel_id:
            self.log.warning("通知先が未設定です。/admin setchannel を実行してください。")
            return
        await send_to_channel(self.bot, self.dest_channel_id, message)

    # ---- VCイベント ----
    @commands.Cog.listener()
    async def on_voice_state_update(self, member, before, after):
        if member.bot:
            return

        target = self.bot.config.target_vc_id
        before_id = getattr(before.channel, "id", None)
        after_id = getattr(after.channel, "id", None)
        if target not in {before_id, after_id}:
            return

        # 入室
        if before.channel is None and after.channel is not None:
            self.join_times[member.id] = time.time()
            self.active_users.add(member.id)
            await self.notify(f"**{member.display_name}** が **{after.channel.name}** に参加しました")

        # 退出
        elif before.channel is not None and after.channel is None:
            join_time = self.join_times.pop(member.id, None)
            if join_time:
                stay = time.time() - join_time
                self.daily_total[member.id] = self.daily_total.get(member.id, 0) + stay
                self._save_daily_totals()

                # 滞在時間フォーマット
                def fmt(sec):
                    if sec < 60:
                        return f"{int(sec)}秒"
                    elif sec < 3600:
                        m, s = divmod(int(sec), 60)
                        return f"{m}分{s}秒"
                    else:
                        h, rem = divmod(int(sec), 3600)
                        m, s = divmod(rem, 60)
                        return f"{h}時間{m}分{s}秒"

                stay_str = fmt(stay)
                total_str = fmt(self.daily_total[member.id])
                await self.notify(
                    f"**{member.display_name}** が **{before.channel.name}** から退出しました（滞在 {stay_str}／累計 {total_str}）"
                )

    # ---- 日次まとめ ----
    @tasks.loop(minutes=1)
    async def daily_summary(self):
        now = datetime.now(JST)
        if now.hour == 23 and now.minute == 59 and self.active_users:
            msg_lines = ["📊 **本日の勉強時間まとめ**", ""]
            for uid in self.active_users:
                total = int(self.daily_total.get(uid, 0))
                user = self.bot.get_user(uid)
                name = user.display_name if user else f"<@{uid}>"

                if total < 60:
                    t_str = f"{total}秒"
                elif total < 3600:
                    m, s = divmod(total, 60)
                    t_str = f"{m}分{s}秒"
                else:
                    h, rem = divmod(total, 3600)
                    m, s = divmod(rem, 60)
                    t_str = f"{h}時間{m}分{s}秒"
                msg_lines.append(f"・**{name}**：{t_str}")

            msg_lines.append("")
            msg_lines.append("🌙今日もお疲れさまでした！")

            await self.notify("\n".join(msg_lines))
            self.daily_total.clear()
            self.active_users.clear()
            self._save_daily_totals()

    @daily_summary.before_loop
    async def before_summary(self):
        await self.bot.wait_until_ready()

# ===================== 管理コマンド =====================
class AdminGroup(app_commands.Group):
    def __init__(self, bot: "VcBot"):
        super().__init__(name="admin", description="管理用コマンド")
        self.bot = bot

    @app_commands.command(name="setchannel", description="通知チャンネルを設定")
    async def setchannel(self, interaction: discord.Interaction):
        cog: VcNotifier = self.bot.vc_cog
        cog.dest_channel_id = interaction.channel_id
        save_persisted_dest_channel_id(cog.dest_channel_id)
        await interaction.response.send_message("✅ 通知先を設定しました（保存済み）", ephemeral=True)

    @app_commands.command(name="test", description="通知テスト")
    async def test(self, interaction: discord.Interaction):
        cog: VcNotifier = self.bot.vc_cog
        await interaction.response.send_message("送信テスト中…", ephemeral=True)
        await cog.notify("🔔 テスト通知：このチャンネルに届きます。")

AdminGroup.setchannel.parent = AdminGroup
AdminGroup.test.parent = AdminGroup

# ===================== Bot本体 =====================
class VcBot(commands.Bot):
    def __init__(self, config: Config):
        intents = discord.Intents.default()
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents)
        self.config = config
        self.vc_cog: Optional[VcNotifier] = None

    async def setup_hook(self):
        self.vc_cog = VcNotifier(self)
        await self.add_cog(self.vc_cog)

        admin_group = AdminGroup(self)
        self.tree.add_command(admin_group)

        self.vc_cog.daily_summary.start()

        synced = await self.tree.sync()
        print(f"🔁 Synced {len(synced)} commands to guild {self.config.guild_id}")

    async def on_ready(self):
        print(f"ログイン成功: {self.user} ({self.user.id})")


# ===================== メイン =====================
def main():
    config = Config.load()
    setup_logging(config.log_level)
    _ensure_data_dir()
    threading.Thread(target=run_web_server, args=(config.port,), daemon=True).start()
    bot = VcBot(config)
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        loop.run_until_complete(bot.start(config.token))
    finally:
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.close()

if __name__ == "__main__":
    main()
