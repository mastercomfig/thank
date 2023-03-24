from __future__ import annotations

import os
import string

import discord
import profanity_check

from discord import Intents, AllowedMentions
from thefuzz import fuzz

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

THANKING_WORDS = ["thank", "vroom", "zoom", "nyoom"]
client.thank_channels = set()
client.thank_pairs = {}


@client.event
async def on_guild_join(guild: discord.Guild):
    collect_from_guild(guild)


def collect_from_guild(guild: discord.Guild):
    thank_channel = discord.utils.find(lambda c: c.name == 'thank', guild.channels)

    if thank_channel is None:
        return

    if not isinstance(thank_channel, discord.TextChannel):
        return

    client.thank_channels.add(thank_channel)

    client.thank_pairs[guild.id] = {}


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)


bad_chars = set('/{}\\%$[]#()-=<>|^@`*_')

@client.event
async def on_message(message: discord.Message):
    if message.author == client.user or message.author.bot:
        return

    if message.channel not in client.thank_channels:
        return

    length = len(message.content)
    if 1 > length > 1019:
        return

    text = message.content

    text = ''.join(filter(lambda x: x in string.printable, text))
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

    if get_thankness(text) > 70:
        thank_msg = await message.channel.send(text)
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
    client.run(os.environ['THANK_TOKEN'])
