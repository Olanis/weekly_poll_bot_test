#!/usr/bin/env python3
"""
Stepwise expansion from minimal bot:
- Adds scheduled-event handlers (create/update/delete)
- Persists tracked_events and prevents duplicate posts
- Defensive handling for discord.NotFound when verifying posted messages
Keep other functionality minimal for now; we'll add reminders, RSVP UI and polls next.
"""
from __future__ import annotations

import os
import io
import sqlite3
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler

# basic logging
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

# -------------------------
# DB helpers
# -------------------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    # keep tracked_events and event_rsvps (minimal)
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

# -------------------------
# Simple placeholders
# -------------------------
scheduler = AsyncIOScheduler(timezone=ZoneInfo(POST_TIMEZONE))

def schedule_reminders_for_event(bot_inst, scheduler_inst, discord_event_id: str, start_time, events_channel_id):
    # placeholder: we'll implement full reminders in a later step
    log.debug("schedule_reminders_for_event called for %s (stub)", discord_event_id)
    return

# -------------------------
# Event View (placeholder for future RSVP UI)
# -------------------------
class EventViewPlaceholder(discord.ui.View):
    def __init__(self, discord_event_id: str):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id

# -------------------------
# Handlers: create / update / delete
# -------------------------
@bot.event
async def on_guild_scheduled_event_create(event: discord.ScheduledEvent):
    log.info("Received GUILD_SCHEDULED_EVENT_CREATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event create")
        return

    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        guild_id = event.guild.id if getattr(event, "guild", None) else None
        now_iso = datetime.now(timezone.utc).isoformat()

        # ensure tracked row exists (insert or update)
        db_execute(
            "INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)",
            (guild_id, discord_event_id, start_iso, now_iso)
        )

        # check existing posted message
        tracked = db_execute(
            "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?",
            (discord_event_id,), fetch=True
        )
        if tracked:
            posted_ch_id, posted_msg_id = tracked[0]
            if posted_msg_id:
                try:
                    ch_check = bot.get_channel(posted_ch_id) if posted_ch_id else None
                    if ch_check:
                        try:
                            _ = await ch_check.fetch_message(posted_msg_id)
                            log.info("Event %s already posted as message %s — skipping", discord_event_id, posted_msg_id)
                            # still schedule reminders
                            try:
                                schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
                            except Exception:
                                log.exception("Failed to schedule reminders (create)")
                            return
                        except discord.NotFound:
                            # message missing — clear DB refs and continue to post
                            db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                        except Exception:
                            log.exception("Error while verifying existing posted message")
        # Post message to EVENTS_CHANNEL_ID
        ch = bot.get_channel(EVENTS_CHANNEL_ID)
        if not ch:
            log.warning("Events channel %s not found", EVENTS_CHANNEL_ID)
            return
        embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
        if event.start_time:
            try:
                embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
            except Exception:
                embed.add_field(name="Start", value=str(event.start_time), inline=False)
        view = EventViewPlaceholder(discord_event_id)
        sent = await ch.send(embed=embed, view=view)
        db_execute("UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, sent.id, now_iso, discord_event_id))
        try:
            schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
        except Exception:
            log.exception("Failed to schedule reminders (after post)")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_create")

@bot.event
async def on_guild_scheduled_event_update(event: discord.ScheduledEvent):
    log.info("Received GUILD_SCHEDULED_EVENT_UPDATE id=%s name=%s", getattr(event, "id", None), getattr(event, "name", None))
    if not EVENTS_CHANNEL_ID:
        log.info("EVENTS_CHANNEL_ID not set; ignoring scheduled event update")
        return

    try:
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        now_iso = datetime.now(timezone.utc).isoformat()
        db_execute("UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?", (start_iso, now_iso, discord_event_id))
        # reschedule reminders (stub)
        try:
            schedule_reminders_for_event(bot, scheduler, discord_event_id, event.start_time, EVENTS_CHANNEL_ID)
        except Exception:
            log.exception("Failed to reschedule reminders (update)")

        tracked = db_execute("SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id) if ch_id else None
            if ch and msg_id:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
                    if event.start_time:
                        try:
                            embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo(POST_TIMEZONE)).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
                        except Exception:
                            embed.add_field(name="Start", value=str(event.start_time), inline=False)
                    await msg.edit(embed=embed, view=EventViewPlaceholder(discord_event_id))
                except discord.NotFound:
                    log.info("Previously tracked message missing for %s; clearing DB refs", discord_event_id)
                    db_execute("UPDATE tracked_events SET posted_channel_id = NULL, posted_message_id = NULL WHERE discord_event_id = ?", (discord_event_id,))
                except Exception:
                    log.exception("Failed to update tracked event message")
    except Exception:
        log.exception("Error in on_guild_scheduled_event_update")

@bot.event
async def on_guild_scheduled_event_delete(event: discord.ScheduledEvent):
    log.info("Received GUILD_SCHEDULED_EVENT_DELETE id=%s", getattr(event, "id", None))
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
                    log.info("Tracked message already deleted for %s", discord_event_id)
                except Exception:
                    log.exception("Failed deleting tracked message on event delete")
        db_execute("DELETE FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,))
        db_execute("DELETE FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,))
    except Exception:
        log.exception("Error in on_guild_scheduled_event_delete")

# -------------------------
# Basic debug commands
# -------------------------
@bot.command()
async def checkevents(ctx):
    rows = db_execute("SELECT discord_event_id, start_time, posted_channel_id, posted_message_id FROM tracked_events", fetch=True)
    await ctx.send(f"tracked_events: {rows}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

# -------------------------
# startup
# -------------------------
@bot.event
async def on_ready():
    log.info("Bot ready: %s (id=%s)", bot.user, bot.user.id)
    init_db()
    if not scheduler.running:
        scheduler.start()
    # reschedule tracked events reminders on startup (will call stub)
    if EVENTS_CHANNEL_ID:
        try:
            rows = db_execute("SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
            for discord_event_id, start_iso in rows:
                try:
                    start_dt = datetime.fromisoformat(start_iso) if start_iso else None
                except Exception:
                    continue
                schedule_reminders_for_event(bot, scheduler, discord_event_id, start_dt, EVENTS_CHANNEL_ID)
        except Exception:
            log.exception("Failed to reschedule events on startup")

if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
        raise SystemExit(1)
    init_db()
    bot.run(BOT_TOKEN)
