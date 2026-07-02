import asyncio
import datetime
import json
import re
from pathlib import Path

import aiohttp
import discord
import git

DATA_DIR = Path("rag_data")
ARCHIVED_LOGS_DIR = DATA_DIR / "archived_logs"
LIVE_LOGS_DIR = DATA_DIR / "live_logs"
REPO_DIR = DATA_DIR / "repo_cache"

ARCHIVED_CHANNELS = ["tf2-help-old", "comfig-feedback", "tf2-help-archived"]
LIVE_CHANNELS = [
    "general-help",
    "newbie-help",
    "mastercomfig",
    "tc2-bugs-and-help",
    "volunteers",
]


def init_dirs():
    DATA_DIR.mkdir(exist_ok=True)
    ARCHIVED_LOGS_DIR.mkdir(exist_ok=True)
    LIVE_LOGS_DIR.mkdir(exist_ok=True)
    REPO_DIR.mkdir(exist_ok=True)


def ensure_repo_cloned():
    init_dirs()
    repo_path = REPO_DIR / "mastercomfig"
    if not repo_path.exists():
        print("Cloning mastercomfig repository...")
        git.Repo.clone_from(
            "https://github.com/mastercomfig/mastercomfig.git", repo_path
        )

    repo = git.Repo(repo_path)

    print("Checking out release branch and pulling...")
    repo.git.checkout("release")
    repo.remotes.origin.pull()
    print("Repo synced.")


async def export_channel_history(channel: discord.abc.GuildChannel, save_path: Path):
    if not isinstance(channel, (discord.TextChannel, discord.ForumChannel)):
        return

    messages = []
    if save_path.exists():
        try:
            with open(save_path, "r", encoding="utf-8") as f:
                messages = json.load(f)
        except json.JSONDecodeError:
            messages = []

    if isinstance(channel, discord.TextChannel):
        print(f"Exporting history for text channel {channel.name}...")
        max_id = max([m["id"] for m in messages], default=None)
        after_obj = discord.Object(id=max_id) if max_id else None

        async for msg in channel.history(
            limit=None, oldest_first=True, after=after_obj
        ):
            if msg.content:
                messages.append(
                    {
                        "id": msg.id,
                        "author": msg.author.name,
                        "content": msg.content,
                        "created_at": msg.created_at.isoformat(),
                        "thread_id": None,
                    }
                )
    elif isinstance(channel, discord.ForumChannel):
        print(f"Exporting history for forum channel {channel.name}...")
        max_ids = {}
        for m in messages:
            tid = m.get("thread_id")
            if tid:
                max_ids[tid] = max(max_ids.get(tid, 0), m["id"])

        async def fetch_thread_history(thread: discord.Thread):
            max_id = max_ids.get(thread.id)
            # Skip fetching if we already have the latest message
            if max_id and thread.last_message_id and thread.last_message_id <= max_id:
                return []

            after_obj = discord.Object(id=max_id) if max_id else None
            thread_msgs = []
            async for msg in thread.history(
                limit=None, oldest_first=True, after=after_obj
            ):
                if msg.content:
                    thread_msgs.append(
                        {
                            "id": msg.id,
                            "author": msg.author.name,
                            "content": msg.content,
                            "created_at": msg.created_at.isoformat(),
                            "thread_id": thread.id,
                            "thread_name": thread.name,
                        }
                    )
            return thread_msgs

        for thread in channel.threads:
            new_msgs = await fetch_thread_history(thread)
            if new_msgs:
                messages.extend(new_msgs)
                await asyncio.sleep(0.5)

        async for thread in channel.archived_threads(limit=None):
            new_msgs = await fetch_thread_history(thread)
            if new_msgs:
                messages.extend(new_msgs)
                await asyncio.sleep(0.5)

    with open(save_path, "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2)


async def export_archived_logs(bot: discord.Client):
    init_dirs()
    exported = False
    for guild in bot.guilds:
        for channel in guild.channels:
            clean_name = re.sub(r"[^a-zA-Z0-9\-]", "", channel.name)
            if clean_name in ARCHIVED_CHANNELS:
                save_path = ARCHIVED_LOGS_DIR / f"{clean_name}.json"
                if not save_path.exists():
                    await export_channel_history(channel, save_path)
                    exported = True
    return exported


async def export_live_forums(bot: discord.Client):
    init_dirs()
    exported = False
    for guild in bot.guilds:
        for channel in guild.channels:
            clean_name = re.sub(r"[^a-zA-Z0-9\-]", "", channel.name)
            if clean_name in LIVE_CHANNELS and isinstance(
                channel, discord.ForumChannel
            ):
                save_path = LIVE_LOGS_DIR / f"{clean_name}.json"
                needs_export = True
                if save_path.exists():
                    mtime = datetime.datetime.fromtimestamp(save_path.stat().st_mtime)
                    if datetime.datetime.now() - mtime < datetime.timedelta(hours=24):
                        needs_export = False

                if needs_export:
                    await export_channel_history(channel, save_path)
                    exported = True
    return exported


async def export_github_releases():
    init_dirs()
    save_path = DATA_DIR / "github_releases.json"

    needs_export = True
    if save_path.exists():
        mtime = datetime.datetime.fromtimestamp(save_path.stat().st_mtime)
        if datetime.datetime.now() - mtime < datetime.timedelta(hours=24):
            needs_export = False

    if needs_export:
        print("Fetching mastercomfig GitHub releases...")
        headers = {"User-Agent": "mastercomfig-bot"}
        try:
            async with aiohttp.ClientSession(headers=headers) as session:
                async with session.get(
                    "https://api.github.com/repos/mastercomfig/mastercomfig/releases"
                ) as response:
                    response.raise_for_status()
                    data = await response.json()

            releases = []
            for rel in data:
                releases.append(
                    {
                        "tag_name": rel.get("tag_name", "unknown"),
                        "name": rel.get("name", "unknown"),
                        "published_at": rel.get("published_at"),
                        "body": rel.get("body", ""),
                    }
                )
            with open(save_path, "w", encoding="utf-8") as f:
                json.dump(releases, f, indent=2)
            return True
        except Exception as e:
            print(f"Failed to fetch GitHub releases: {e}")

    return False
