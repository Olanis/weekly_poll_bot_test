#!/usr/bin/env python3
"""
Stepwise bot — added RSVP UI (buttons) and persistence.

Replace your current bot.py with this file and restart the container.
This file extends the previously running reminder-enabled bot:
- Adds EventView with RSVP buttons that persist to event_rsvps table.
- Updates posted event message embed after RSVP changes.
"""
from __future__ import annotations

import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

# logging
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")

# intents & bot
intents = discord.Intents.default()
intents.message_content = True
intents.guild_scheduled_events = True

bot = commands.Bot(command_prefix="!", intents=intents)

# config
DB_PATH = os.getenv("POLL_DB", "polls.sqlite")
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHANNEL_ID = int(os.getenv("CHANNEL_ID", "0")) if os.getenv("CHANNEL_ID") else None
EVENTS_CHANNEL_ID = int(os.getenv("EVENTS_CHANNEL_ID", "0")) if os.getenv("EVENTS_CHANNEL_ID") else None
POST_TIMEZONE = os.getenv("POST_TIMEZONE", "Europe/Berlin")

# DB helpers
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS tracked_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            guild_id INTEGER,
            discord_event_id TEXT NOT NULL UNIQUE,
            posted_channel_id INTEGER,
            posted_message_id INTEGER,
            start_time TEXT,
            updated_at TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS event_rsvps (
            discord_event_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            UNIQUE(discord_event_id, user_id)
        )
    """)
    con.commit()
    con.close()

def db_execute(query: str, params=(), fetch=False, many=False):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    try:
        if many:
            cur.executemany(query, params)
        else:
            cur.execute(query, params)
        rows = cur.fetchall() if fetch else None
        con.commit()
        return rows
    finally:
        con.close()

# scheduler (reminder scheduling kept)
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

# -------------------------
# Helper: build event embed (shows RSVP counts)
# -------------------------
def build_event_embed_from_db(discord_event_id: str, guild: discord.Guild | None = None):
    rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    start_time = rows[0][1] if rows else None
    r = db_execute("SELECT status, COUNT(*) FROM event_rsvps WHERE discord_event_id = ? GROUP BY status", (discord_event_id,), fetch=True) or []
    counts = {row[0]: row[1] for row in r}
    interested = counts.get("interested", 0)
    going = counts.get("going", 0)
    embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    embed.add_field(name="🔔 Interessiert", value=str(interested), inline=True)
    embed.add_field(name="✅ Nehme teil", value=str(going), inline=True)
    return embed

# -------------------------
# Event view: RSVP buttons
# -------------------------
class EventRSVPView(discord.ui.View):
    def __init__(self, discord_event_id: str, guild: discord.Guild | None):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id
        self.guild = guild

    @discord.ui.button(label="🔔 Interessiert", style=discord.ButtonStyle.secondary, custom_id=None)
    async def btn_interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "interested")

    @discord.ui.button(label="✅ Nehme teil", style=discord.ButtonStyle.success, custom_id=None)
    async def btn_going(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._handle_rsvp(interaction, "going")

    async def _handle_rsvp(self, interaction: discord.Interaction, status: str):
        uid = interaction.user.id
        did = self.discord_event_id
        try:
            # toggle: if same status exists -> remove; else upsert
            existing = db_execute("SELECT status FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (did, uid), fetch=True)
            if existing and existing[0][0] == status:
                db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (did, uid))
                await interaction.response.send_message(f"Deine {status} RSVP wurde entfernt.", ephemeral=True)
            else:
                db_execute("INSERT OR REPLACE INTO event_rsvps(discord_event_id, user_id, status) VALUES (?, ?, ?)", (did, uid, status))
                await interaction.response.send_message(f"Dein RSVP wurde gesetzt: {status}.", ephemeral=True)
            # update public posted message if present
            tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (did,), fetch=True)
            if tracked:
                ch_id, msg_id = tracked[0]
                ch = bot.get_channel(ch_id) if ch_id else None
                if ch and msg_id:
                    try:
                        msg = await ch.fetch_message(msg_id)
                        if msg:
                            embed = build_event_embed_from_db(did, self.guild)
                            await msg.edit(embed=embed, view=EventRSVPView(did, self.guild))
                    except discord.NotFound:
                        # message missing: clear DB refs
                        db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (did,))
                    except Exception:
                        log.exception("Failed to update event message after RSVP")
        except Exception:
            log.exception("Error handling RSVP interaction")
            try:
                await interaction.response.send_message("Fehler beim Verarbeiten deines RSVP.", ephemeral=True)
            except Exception:
                pass

# -------------------------
# Reuse previous reminder + event posting handlers (kept minimal and balanced)
# -------------------------
async def reminder_coro(channel_id: int, discord_event_id: str, hours_before: int):
    ch = bot.get_channel(channel_id)
    if not ch:
        log.info("reminder_coro: channel %s not found", channel_id)
        return
    embed = build_event_embed_from_db(discord_event_id)
    embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
    view = EventRSVPView(discord_event_id, None)
    tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    if tracked:
        old_ch_id, old_msg_id = tracked[0]
        if old_ch_id and old_msg_id:
            try:
                old_ch = bot.get_channel(old_ch_id)
                if old_ch:
                    try:
                        old_msg = await old_ch.fetch_message(old_msg_id)
                    except discord.NotFound:
                        db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        old_msg = None
                    except Exception:
                        log.exception("Error fetching old event message during reminder for %s", discord_event_id)
                        old_msg = None
                    if old_msg:
                        try:
                            await old_msg.delete()
                        except discord.NotFound:
                            pass
                        except Exception:
                            log.exception("Failed deleting old event message during reminder for %s", discord_event_id)
    try:
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
    except Exception:
        log.exception("Failed sending reminder message for %s", discord_event_id)

# scheduling helper (kept from prior step)
def schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id: str, start_time):
    try:
        scheduler_inst.remove_job(f"event_reminder_24_{discord_event_id}")
    except Exception:
        pass
    try:
        scheduler_inst.remove_job(f"event_reminder_2_{discord_event_id}")
    except Exception:
        pass
    if not start_time:
        return
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24)),
                               trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 24))
    if t2 > now:
        scheduler_inst.add_job(lambda: bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2)),
                               trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot_inst.loop.create_task(reminder_coro(EVENTS_CHANNEL_ID, discord_event_id, 2))

bot_inst = bot
scheduler_inst = scheduler

# -------------------------
# Event handlers (create/update/delete) - update to use EventRSVPView
# -------------------------
@bot.event
async def on_guild_scheduled_event_create(event: discord.ScheduledEvent):
    log.info("EVENT_CREATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event create")
        return
    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        guild_id = event.guild.id if getattr(event, "guild", None) else None
        now_iso = datetime.now(timezone.utc).isoformat()
        db_execute("INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
                   (guild_id, discord_event_id, start_iso, now_iso))
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            posted_ch_id, posted_msg_id = tracked[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Event %s already posted as message %s — skipping", discord_event_id, posted_msg_id)
                            try:
                                schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
                            except Exception:
                                log.exception("Failed scheduling reminders (create)")
                            return
                        except discord.NotFound:
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Error verifying existing posted message")
        # Post message
        ch = bot.get_channel(EVENTS_CHANNEL_ID)
        if not ch:
            log.warning("Events channel %s not found", EVENTS_CHANNEL_ID)
            return
        embed = build_event_embed_from_db(discord_event_id, event.guild)
        view = EventRSVPView(discord_event_id, event.guild)
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?",
                   (ch.id, sent.id, now_iso, discord_event_id))
        try:
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
        except Exception:
            log.exception("Failed scheduling reminders after post")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_create")

@bot.event
async def on_guild_scheduled_event_update(event: discord.ScheduledEvent):
    log.info("EVENT_UPDATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event update")
        return
    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        now_iso = datetime.now(timezone.utc).isoformat()
        db_execute("UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?", (start_iso, now_iso, discord_event_id))
        try:
            schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, event.start_time)
        except Exception:
            log.exception("Failed to reschedule reminders (update)")
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = build_event_embed_from_db(discord_event_id, event.guild)
                    await msg.edit(embed=embed, view=EventRSVPView(discord_event_id, event.guild))
                except discord.NotFound:
                    db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                except Exception:
                    log.exception("Failed to update event message on event update")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_update")

@bot.event
async def on_guild_scheduled_event_delete(event: discord.ScheduledEvent):
    log.info("EVENT_DELETE id=%s", getattr(event, "id", None))
    try:
        discord_event_id = str(event.id)
        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    await msg.delete()
                except discord.NotFound:
                    log.info("Tracked event message already deleted for %s", discord_event_id)
                except Exception:
                    log.exception("Failed to delete tracked message on event delete")
        db_execute("DELETE FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,))
        db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,))
    except Exception:
        log.exception("Error in on_guild_scheduled_event_delete")

# Debug commands
@bot.command()
async def checkevents(ctx):
    rows = db_execute("SELECT discord_event_id, start_time, posted_channel_id, posted_message_id FROM tracked_events", fetch=True)
    await ctx.send(f"tracked_events: {rows}")

@bot.command()
async def rsvpstatus(ctx, discord_event_id: str):
    rows = db_execute("SELECT user_id, status FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    await ctx.send(f"rsvps for {discord_event_id}: {rows}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# startup
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    init_db()
    if not scheduler.running:
        scheduler.start()
    if EVENTS_CHANNEL_ID:
        try:
            rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
            for discord_event_id, start_iso in rows:
                try:
                    start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                except Exception:
                    continue
                schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id, start_dt)
        except Exception:
            log.exception("Failed to reschedule events on startup")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
