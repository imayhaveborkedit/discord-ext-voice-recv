import discord
from discord.ext import commands, voice_recv

discord.opus._load_default()

bot = commands.Bot(command_prefix=commands.when_mentioned, intents=discord.Intents.all())

class Testing(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def test(self, ctx):
        def callback(member, packet):
            print(member, packet)

            ## voice power level, how loud the user is speaking
            # ext_data = packet.extension_data.get(voice_recv.ExtensionID.audio_power)
            # value = int.from_bytes(ext_data, 'big')
            # power = 127-(value & 127)
            # print('#' * int(power * (79/128)))
            ## instead of 79 you can use shutil.get_terminal_size().columns-1

        vc = await ctx.author.voice.channel.connect(cls=voice_recv.VoiceRecvClient)
        vc.listen(voice_recv.BasicSink(callback))

    @commands.command()
    async def stop(self, ctx):
        ctx.voice_client.stop_listening()
        await ctx.voice_client.disconnect()

    @commands.command()
    async def die(self, ctx):
        await self.stop(ctx)
        await ctx.bot.close()


@bot.event
async def on_ready():
    print('Logged in as {0.id}/{0}'.format(bot.user))
    print('------')


bot.add_cog(Testing(bot))
bot.run(token)
