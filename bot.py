from __future__ import annotations

import asyncio
import datetime
import os
import string
from typing import TYPE_CHECKING

import cachetools
import discord
import profanity_check
from discord import AllowedMentions, Intents
from thefuzz import fuzz

if TYPE_CHECKING:
    from collections.abc import Coroutine

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


class ThankBot(discord.Client):
    def __init__(self, *args, **kwargs):
        super().__init__(args, kwargs)
        self.thank_channels: set[discord.TextChannel] = set()
        self.thank_pairs: dict[int, cachetools.TTLCache[int, discord.Message]] = {}
        self.reddit_channels: list[discord.TextChannel] = []
        self.help_forums: set[discord.ForumChannel] = set()
        self.volunteer_roles: dict[int, discord.Role] = {}


client = ThankBot(
    intents=intents,
    allowed_mentions=mentions,
)

THANKING_WORDS = ["thamk", "vroom", "zoom", "nyoom"]


class TaskWrapper:
    def __init__(self, task: asyncio.Task):
        self.task: asyncio.Task = task
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


class ChannelCollector:
    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        return None


class ChannelNameCollector(ChannelCollector):
    def __init__(self, channel_name: str):
        self.channel_name = channel_name

    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        channel = discord.utils.find(
            lambda c: c.name.startswith(self.channel_name), guild.channels
        )
        if channel is None:
            return None
        return [channel]


class ChannelCategoryCollector(ChannelCollector):
    def __init__(self, category_name: str):
        self.category_name = category_name

    def collect(self, guild: discord.Guild) -> list[discord.abc.GuildChannel] | None:
        category = discord.utils.find(
            lambda c: c.name.startswith(self.category_name), guild.categories
        )
        if category is None:
            return None
        return category.channels


def collect_channels_from_guild(
    guild: discord.Guild,
    collector: ChannelCollector,
    channel_type: discord.ChannelType = discord.ChannelType.text,
) -> list[discord.abc.GuildChannel]:
    channels = collector.collect(guild)

    if not channels:
        return []

    return [c for c in channels if c.type == channel_type]


def collect_channel_from_guild(
    guild: discord.Guild,
    collector: ChannelCollector,
    channel_type: discord.ChannelType = discord.ChannelType.text,
) -> discord.abc.GuildChannel | None:
    channels = collect_channels_from_guild(guild, collector, channel_type)
    if not channels:
        return None
    return channels[0]


def collect_from_guild(guild: discord.Guild):
    thank_channel = collect_channel_from_guild(guild, ChannelNameCollector("thamk"))

    if thank_channel:
        client.thank_channels.add(thank_channel)

    # limit thank message pairs to 1 day and 100 messages
    client.thank_pairs[guild.id] = cachetools.TTLCache(
        maxsize=100, ttl=datetime.timedelta(days=1).total_seconds()
    )

    reddit_channel = collect_channel_from_guild(guild, ChannelNameCollector("reddit"))
    if reddit_channel:
        client.reddit_channels.append(reddit_channel)

    help_forums = collect_channels_from_guild(
        guild, ChannelCategoryCollector("Help & Support"), discord.ChannelType.forum
    )
    if help_forums:
        client.help_forums.update(help_forums)

    client.volunteer_roles[guild.id] = discord.utils.get(guild.roles, name="Volunteer")


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


reddit_clear_inst: TaskWrapper | None = None


def schedule_reddit_clear():
    global reddit_clear_inst
    if reddit_clear_inst is not None:
        reddit_clear_inst.task.cancel()
    reddit_clear_inst = create_task(reddit_clear_job(), name="Reddit Clear Job")


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
async def on_thread_create(thread: discord.Thread):
    thread_owner = thread.owner
    if thread_owner is None:
        thread_owner = thread.guild.get_member(thread.guild.owner_id)

    if thread_owner is None:
        return

    if thread_owner == client.user or thread_owner.bot:
        return

    if not thread.guild:
        return

    volunteer_role = client.volunteer_roles.get(thread.guild.id)
    if volunteer_role is None:
        return

    if thread.parent not in client.help_forums:
        return

    await thread.send(f"""Hello {thread.owner.mention}! I see you need some assistance. Make sure to supply as much detail as possible in your post so that someone may help you at their earliest convenience.

I have also pinged {volunteer_role.mention} so that they see your thread and can help you as soon as possible!

Once you're done, tag this thread as :white_check_mark: Solved.""")


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
    if length < 1 or length > 1019:
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
        await bait_msg(message)
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

    if not message.guild:
        return

    guild_pairs = client.thank_pairs.get(message.guild.id)
    if not guild_pairs:
        return

    thank_msg = guild_pairs.pop(message.id, None)

    if thank_msg is not None:
        try:
            await thank_msg.delete()
        except discord.errors.NotFound:
            pass


def get_thankness(text: str) -> float:
    words = text.split()

    length = len(words)

    if length < 1 or length > 170:
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
