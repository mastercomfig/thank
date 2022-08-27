import os

import discord

from discord import Intents
from thefuzz import fuzz

intents = Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

THANKING_WORDS = ["thank", "vroom", "zoom", "7"]
client.thank_channels = []
client.thank_pairs = {}


@client.event
async def on_guild_join(guild):
    collect_from_guild(guild)


def collect_from_guild(guild):
    thank_channel = discord.utils.find(lambda c: c.name == 'thank', guild.channels)

    if thank_channel is None:
        return

    client.thank_channels.append(thank_channel)

    client.thank_pairs[guild.id] = {}


@client.event
async def on_ready():
    for guild in client.guilds:
        collect_from_guild(guild)


@client.event
async def on_message(message):
    if message.author == client.user or message.author.bot:
        return

    text = message.content

    if message.channel in client.thank_channels:
        if get_thankness(text, THANKING_WORDS) > 0.85:
            thank_msg = await message.channel.send(message.content.lower())
            client.thank_pairs[message.guild.id][message.id] = thank_msg


@client.event
async def on_message_delete(message):
    await delete_from_message(message)


@client.event
async def on_message_edit(before, after):
    await delete_from_message(before)


async def delete_from_message(message):
    if message.author == client.user or message.author.bot:
        return

    thank_msg = client.thank_pairs[message.guild.id].get(message.id)

    if thank_msg is not None:
        await thank_msg.delete()


def get_thankness(text, keywords):
    thankness = 0
    max_thankness = len(keywords)

    for keyword in keywords:
        partial = fuzz.partial_ratio(keyword, text)
        if partial > 70:
            # if the phrase fuzzes a keyword, calculate thankness for that keyword
            thankness += fuzz.ratio(keyword, text) / 100
        else:
            # if we don't have that phrase, don't consider it for calculating our final thankness ratio
            max_thankness -= 1

    # we didn't fuzz any keywords, no thankness
    if max_thankness < 1:
        return 0

    return thankness / max_thankness


client.run(os.environ['THANK_TOKEN'])
