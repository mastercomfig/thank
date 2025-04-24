from __future__ import annotations

import asyncio
import datetime
import os
import string
from typing import TYPE_CHECKING

import discord
import profanity_check
from discord import AllowedMentions, Intents
from thefuzz import fuzz

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine

if os.name == "nt":
    # handle Windows imports
    # for colored terminal
    import colorama

    colorama.init()
else:
    # handle POSIX imports
    # for uvloop
    # while we have steam client, we cannot use uvloop due to gevent
    import uvloop

    uvloop.install()


intents = Intents.none()
intents.guilds = True
intents.guild_messages = True
intents.message_content = True

mentions = AllowedMentions.none()

client = discord.Client(
    intents=intents,
    allowed_mentions=mentions,
)

THANKING_WORDS = ["thamk", "vroom", "zoom", "nyoom"]
client.thank_channels = set()
client.thank_pairs = {}
client.reddit_channels = []


class TaskWrapper:
    def __init__(self, task):
        self.task = task
        task.add_done_callback(self.on_task_done)

    def __getattr__(self, name):
        return getattr(self.task, name)

    def __await__(self):
        self.task.remove_done_callback(self.on_task_done)
        return self.task.__await__()

    def on_task_done(self, fut: asyncio.Future):
        if fut.cancelled() or not fut.done():
            return
        fut.result()

    def __str__(self):
        return f"TaskWrapper<task={self.task}>"


def create_task(coro: Coroutine, *, name: str = None) -> TaskWrapper:
    task = asyncio.create_task(coro, name=name)
    return TaskWrapper(task)


@client.event
async def on_guild_join(guild: discord.Guild):
    collect_from_guild(guild)


def collect_channel_from_guild(guild: discord.Guild, channel_name: str):
    channel = discord.utils.find(
        lambda c: c.name.startswith(channel_name), guild.channels
    )

    if channel is None:
        return None

    if not isinstance(channel, discord.TextChannel):
        return None

    return channel


def collect_from_guild(guild: discord.Guild):
    thank_channel = collect_channel_from_guild(guild, "thamk")

    if thank_channel:
        client.thank_channels.add(thank_channel)

    client.thank_pairs[guild.id] = {}

    reddit_channel = collect_channel_from_guild(guild, "reddit")
    if reddit_channel:
        client.reddit_channels.append(reddit_channel)


window = datetime.timedelta(hours=1)


async def clear_reddit_channels():
    clear_time = datetime.datetime.now(tz=datetime.timezone.utc) - window
    for channel in client.reddit_channels:
        messages = True
        tries = 3
        while messages:
            try:
                messages = await channel.purge(
                    before=clear_time, oldest_first=True, reason="reddit"
                )
            except Exception as e:
                print(e)
                tries -= 1
                if tries <= 0:
                    break
            await asyncio.sleep(0.5)


clear_interval = 60 * 10


async def reddit_clear_job():
    while True:
        await clear_reddit_channels()
        await asyncio.sleep(clear_interval)


def schedule_reddit_clear():
    create_task(reddit_clear_job(), name="Reddit Clear Job")


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)

    schedule_reddit_clear()

    print("Ready.")


bad_chars = set("/{}\\%$[]#()-=<>|^@`*_")


def interpret_int(txt: str):
    if not txt:
        return None
    try:
        return int(txt)
    except ValueError:
        return None


THANK_BAIT_USER_ID = interpret_int(os.getenv("THANK_BAIT_USER_ID"))


async def bait_msg(message: discord.Message):
    await message.channel.send("bait used to be believable")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    is_bait = message.author.id == THANK_BAIT_USER_ID
    is_thank_channel = message.channel in client.thank_channels
    is_valid_target = is_bait or is_thank_channel
    if not is_valid_target:
        return

    length = len(message.content)
    if 1 > length > 1019:
        return

    text = message.content

    text = "".join(filter(lambda x: x in string.printable, text))
    if not text:
        return

    text = text.strip()
    if not text:
        return

    if any((c in bad_chars) for c in text):
        return

    if "http" in text:
        return

    text = text.lower()

    if not text:
        return

    if profanity_check.predict([text])[0] > 0.5:
        return

    if is_bait:
        await bait_msg(message, text)
        return

    if get_thankness(text) > 70:
        thank_msg = await message.channel.send(text.replace("n", "m"))
        client.thank_pairs[message.guild.id][message.id] = thank_msg


@client.event
async def on_message_delete(message: discord.Message):
    await delete_from_message(message)


@client.event
async def on_message_edit(before: discord.Message, _after: discord.Message):
    await delete_from_message(before)


async def delete_from_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    thank_msg = client.thank_pairs[message.guild.id].get(message.id)

    if thank_msg is not None:
        try:
            await thank_msg.delete()
        except discord.errors.NotFound:
            pass


def get_thankness(text: str) -> float:
    words = text.split()

    length = len(words)

    if 1 > length > 170:
        return 0.0

    thankness = 0.0

    for word in words:
        best = 0.0
        for keyword in THANKING_WORDS:
            if keyword == word:
                best = 100.0
                break
            ratio = fuzz.ratio(keyword, word)
            if ratio > best:
                best = ratio
        thankness += best

    return thankness / length


if __name__ == "__main__":
    client.run(os.environ["THANK_TOKEN"])
