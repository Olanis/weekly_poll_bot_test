#!/usr/bin/env python3
"""
Minimal test bot to verify Python/discord environment starts cleanly.
Replace your current bot.py with this file and restart the container.
If this starts, reply "minimal started". Then I will add the next small step.
"""
import os
import logging
import asyncio
from datetime import datetime, timezone
import discord
from discord.ext import commands

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot-minimal")

intents = discord.Intents.default()
intents.message_content = True

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    print("Bitte BOT_TOKEN als Umgebungsvariable setzen.")
    raise SystemExit(1)

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    log.info(f"✅ Minimal bot logged in as {bot.user} (id={bot.user.id})")
    # Post a simple health message to stdout
    print(f"MINIMAL BOT READY at {datetime.now(timezone.utc).isoformat()}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong")

if __name__ == "__main__":
    bot.run(BOT_TOKEN)
