#!/usr/bin/env python3
"""
events.py
- Listens to Guild Scheduled Events and posts an embed into a configured EVENTS_CHANNEL_ID.
- Schedules reminders 24h and 2h before start; replaces the previous message for that event.
- Provides a "Interessiert" button (ephemeral interaction) to register interest; shows interested users in the embed.
- Persists event -> posted message mapping and RSVPs in SQLite.
Usage:
- Import and call init_events(bot, scheduler, db_path, events_channel_id) from your bot.py on startup (after init_db).
"""
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
import sqlite3
import discord
from discord.ext import commands

EVENTS_TABLES_SQL = [
    """
    CREATE TABLE IF NOT EXISTS tracked_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        discord_event_id TEXT NOT NULL UNIQUE,
        posted_channel_id INTEGER,
        posted_message_id INTEGER,
        start_time TEXT,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS event_rsvps (
        discord_event_id TEXT NOT NULL,
        user_id INTEGER NOT NULL,
        status TEXT NOT NULL,
        UNIQUE(discord_event_id, user_id)
    )
    """
]

def _db_execute(db_path, query, params=(), fetch=False, many=False):
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    if many:
        cur.executemany(query, params)
    else:
        cur.execute(query, params)
    rows = None
    if fetch:
        rows = cur.fetchall()
    con.commit()
    con.close()
    return rows

class EventView(discord.ui.View):
    def __init__(self, discord_event_id: str, db_path: str, guild: discord.Guild | None):
        super().__init__(timeout=None)
        self.discord_event_id = discord_event_id
        self.db_path = db_path
        self.guild = guild

    @discord.ui.button(label="⚜️ Interessiert", style=discord.ButtonStyle.primary)
    async def interested(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = interaction.user.id
        # toggle interested status
        rows = _db_execute(self.db_path, "SELECT status FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (self.discord_event_id, user_id), fetch=True)
        if rows and rows[0][0] == "interested":
            _db_execute(self.db_path, "DELETE FROM event_rsvps WHERE discord_event_id = ? AND user_id = ?", (self.discord_event_id, user_id))
            await interaction.response.send_message("Deine Interesse wurde entfernt.", ephemeral=True)
        else:
            _db_execute(self.db_path, "INSERT OR REPLACE INTO event_rsvps(discord_event_id, user_id, status) VALUES (?, ?, ?)", (self.discord_event_id, user_id, "interested"))
            await interaction.response.send_message("Du bist als interessiert vermerkt.", ephemeral=True)

        # try to update the public message for this event (best-effort)
        try:
            # find tracked event to know posted message/channel
            tracked = _db_execute(self.db_path, "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (self.discord_event_id,), fetch=True)
            if tracked:
                channel_id, message_id = tracked[0]
                ch = interaction.client.get_channel(channel_id)
                if ch:
                    msg = await ch.fetch_message(message_id)
                    if msg:
                        # rebuild embed
                        embed = build_event_embed_from_db(self.db_path, self.discord_event_id, self.guild)
                        await msg.edit(embed=embed, view=EventView(self.discord_event_id, self.db_path, self.guild))
        except Exception:
            pass

def build_event_embed_from_db(db_path: str, discord_event_id: str, guild: discord.Guild | None):
    rows = _db_execute(db_path, "SELECT discord_event_id, start_time FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
    # we will get event details from guild if possible; fallback to stored start_time
    start_time = None
    if rows:
        start_time = rows[0][1]
    # gather rsvps
    r = _db_execute(db_path, "SELECT user_id FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,), fetch=True) or []
    user_ids = [x[0] for x in r]
    names = []
    for uid in user_ids:
        if guild:
            m = guild.get_member(uid)
            if m:
                names.append(m.display_name)
                continue
        # fallback to mention by id
        names.append(str(uid))
    embed = discord.Embed(title="📣 Event", description="Details", color=discord.Color.blurple(), timestamp=datetime.now(timezone.utc))
    if start_time:
        try:
            dt = datetime.fromisoformat(start_time)
            embed.add_field(name="Start", value=dt.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
        except Exception:
            embed.add_field(name="Start", value=start_time, inline=False)
    embed.add_field(name="Interessierte", value=", ".join(names) if names else "Keine", inline=False)
    return embed

def schedule_event_reminders(scheduler, db_path, discord_event_id, start_dt):
    """Schedule two reminders (24h, 2h) before start_dt (aware dt)."""
    from apscheduler.triggers.date import DateTrigger

    def _job_fn(event_id=discord_event_id):
        # the job will call a helper that posts the event message (post_or_update_event_message)
        import asyncio
        # We need access to bot instance — scheduled job will call a coro via the scheduler function wrapper injected in init_events
        pass

# Public init function to be called from bot.py
def init_events(bot: commands.Bot, scheduler, db_path: str, events_channel_id: int):
    # create tables
    for sql in EVENTS_TABLES_SQL:
        _db_execute(db_path, sql)
    # attach listeners
    @bot.event
    async def on_guild_scheduled_event_create(event: discord.GuildScheduledEvent):
        # store event and post initial message
        guild = event.guild
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        _db_execute(db_path, "INSERT OR REPLACE INTO tracked_events(guild_id, discord_event_id, start_time, updated_at) VALUES (?, ?, ?, ?)", (guild.id, discord_event_id, start_iso, datetime.now(timezone.utc).isoformat()))
        # post initial message
        ch = bot.get_channel(events_channel_id)
        if ch:
            embed = discord.Embed(title=event.name or "Event", description=event.description or "", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
            if event.start_time:
                embed.add_field(name="Start", value=event.start_time.astimezone(ZoneInfo("Europe/Berlin")).strftime("%d.%m.%Y %H:%M %Z"), inline=False)
            view = EventView(discord_event_id, db_path, guild)
            msg = await ch.send(embed=embed, view=view)
            _db_execute(db_path, "UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, msg.id, datetime.now(timezone.utc).isoformat(), discord_event_id))
            # schedule reminders
            schedule_reminders_for_event(bot, scheduler, db_path, events_channel_id, discord_event_id, event.start_time)

    @bot.event
    async def on_guild_scheduled_event_update(event: discord.GuildScheduledEvent):
        # update stored start time and reschedule reminders
        guild = event.guild
        discord_event_id = str(event.id)
        start_iso = event.start_time.isoformat() if event.start_time else None
        _db_execute(db_path, "UPDATE tracked_events SET start_time = ?, updated_at = ? WHERE discord_event_id = ?", (start_iso, datetime.now(timezone.utc).isoformat(), discord_event_id))
        # reschedule reminders
        schedule_reminders_for_event(bot, scheduler, db_path, events_channel_id, discord_event_id, event.start_time)
        # update message content
        tracked = _db_execute(db_path, "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            ch = bot.get_channel(ch_id)
            if ch:
                try:
                    msg = await ch.fetch_message(msg_id)
                    embed = build_event_embed_from_db(db_path, discord_event_id, guild)
                    await msg.edit(embed=embed, view=EventView(discord_event_id, db_path, guild))
                except Exception:
                    pass

    @bot.event
    async def on_guild_scheduled_event_delete(event: discord.GuildScheduledEvent):
        discord_event_id = str(event.id)
        tracked = _db_execute(db_path, "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            ch_id, msg_id = tracked[0]
            try:
                ch = bot.get_channel(ch_id)
                if ch:
                    msg = await ch.fetch_message(msg_id)
                    await msg.delete()
            except Exception:
                pass
        _db_execute(db_path, "DELETE FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,))
        _db_execute(db_path, "DELETE FROM event_rsvps WHERE discord_event_id = ?", (discord_event_id,))

def schedule_reminders_for_event(bot: commands.Bot, scheduler, db_path: str, events_channel_id: int, discord_event_id: str, start_time):
    """Schedule/replace reminder jobs 24h and 2h before start_time. If start_time is None or in past, do nothing or post immediate reminder."""
    from apscheduler.triggers.date import DateTrigger

    # remove existing jobs for this event (if any)
    try:
        scheduler.remove_job(f"event_reminder_24_{discord_event_id}")
    except Exception:
        pass
    try:
        scheduler.remove_job(f"event_reminder_2_{discord_event_id}")
    except Exception:
        pass

    if not start_time:
        return

    # ensure start_time is aware datetime
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=ZoneInfo("UTC"))

    t24 = start_time - timedelta(hours=24)
    t2 = start_time - timedelta(hours=2)

    async def reminder_coro(channel_id: int, discord_event_id: str, hours_before: int):
        # posts/updates the event message for this event with a header "Event startet in X Stunden"
        ch = bot.get_channel(channel_id)
        if not ch:
            return
        # delete old (if any) and post updated
        # build embed from db and prepend title
        embed = build_event_embed_from_db(db_path, discord_event_id, None)
        embed.title = f"📣 Event — startet in ~{hours_before} Stunden"
        view = EventView(discord_event_id, db_path, None)
        # delete previous message for this event if recorded
        tracked = _db_execute(db_path, "SELECT posted_channel_id, posted_message_id FROM tracked_events WHERE discord_event_id = ?", (discord_event_id,), fetch=True)
        if tracked:
            old_ch_id, old_msg_id = tracked[0]
            try:
                old_ch = bot.get_channel(old_ch_id)
                if old_ch:
                    old_msg = await old_ch.fetch_message(old_msg_id)
                    if old_msg:
                        await old_msg.delete()
            except Exception:
                pass
        # send updated message
        sent = await ch.send(embed=embed, view=view)
        _db_execute(db_path, "UPDATE tracked_events SET posted_channel_id = ?, posted_message_id = ?, updated_at = ? WHERE discord_event_id = ?", (ch.id, sent.id, datetime.now(timezone.utc).isoformat(), discord_event_id))

    # schedule if in future
    now = datetime.now(timezone.utc)
    if t24 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24)), trigger=DateTrigger(run_date=t24), id=f"event_reminder_24_{discord_event_id}", replace_existing=True)
    elif t24 <= now < start_time:
        # if within window, post immediate 24h-style reminder
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 24))

    if t2 > now:
        scheduler.add_job(lambda: bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2)), trigger=DateTrigger(run_date=t2), id=f"event_reminder_2_{discord_event_id}", replace_existing=True)
    elif t2 <= now < start_time:
        bot.loop.create_task(reminder_coro(events_channel_id, discord_event_id, 2))

# expose helper for startup to schedule existing future events from DB (call from on_ready)
def reschedule_all_events(bot, scheduler, db_path, events_channel_id):
    rows = _db_execute(db_path, "SELECT discord_event_id, start_time FROM tracked_events", fetch=True) or []
    for discord_event_id, start_iso in rows:
        try:
            start_dt = datetime.fromisoformat(start_iso)
        except Exception:
            continue
        schedule_reminders_for_event(bot, scheduler, db_path, events_channel_id, discord_event_id, start_dt)
