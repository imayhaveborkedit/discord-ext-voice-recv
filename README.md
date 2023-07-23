# discord-ext-voice-recv
Voice receive extension package for discord.py

### Warning
**This extension should be more or less functional, but the code is not yet feature complete.  No guarantees are given for stability or random breaking changes.**

## Installing
**Python 3.8 or higher is required**

`python -m pip install git+https://github.com/imayhaveborkedit/discord-ext-voice-recv`

This package will be uploaded to pypi eventually.

Naturally, this extension depends on `discord.py` being installed with voice support.

## Example
See the [example script](examples/recv.py).

## Usage
### VoiceRecvClient
The class `voice_recv.VoiceRecvClient` must be used in `VoiceChannel.connect()` to use voice receive functionality.
```python
voice_client = await voice_channel.connect(cls=voice_recv.VoiceRecvClient)
```

### New voice client functions
```python
def listen(sink: voice_recv.AudioSink, *, after=None) -> None
```
Receives audio data into an `AudioSink`.  A sink is similar to the `AudioSource` class, where most of the logic is done in a single callback function, but in reverse.  Sinks are explained in detail in the [Sinks](#sinks) section below.

The finalizer, `after` is called after the sink has been exhausted or an error occurred.  The callback signature is the same as the after callback for `play()`, one parameter for an optional Exception object.

```python
def is_listening() -> bool
```
Returns `True` if the voice client is currently receiving audio.  Specifically, if the bot is reading from the voice socket.

```python
def stop() -> None
```
This function has been altered to stop both receiving and sending of audio.

```python
def stop_listening() -> None
```
Stops receiving audio.

```python
def stop_playing() -> None
```
Stops playing audio.  This function is identical to `discord.VoiceClient.stop()`.

## Sinks
The api of this extension is designed to mirror the discord.py voice send api.  Sending audio uses the `AudioSource` class, while receiving audio uses the `AudioSink` class.  A sink is designed to be the inverse of a source.  Essentially, a source is a callback called by discord.py to produce a chunk of audio data.  Conversely, a sink is a callback called by the library to handle a chunk of audio.  Sinks can be composed in the same fashion as sources, creating an audio processing pipeline.  Sources and sinks can even combined into one object to handle both tasks, such as creating a feedback loop.

Special care should be taken not to write excessively computationally expensive code, as python is not particularly well suited to realtime audio processing.

Due to voice receive being somewhat more complex than voice sending, sinks have additional functionality compared to sources.  However, the core sink functions should look relatively familiar.

```python
class MySink(voice_recv.AudioSink):
    def __init__(self):
        super().__init__()

    def wants_opus(self) -> bool:
        return False

    def write(self, user: Optional[User | Member], data: VoiceData):
        ...

    def cleanup(self):
        ...
```

These are the main functions of a sink, names and purpose reflecting that of their source counterparts.  It is important to note that `super().__init__()` must be called when inheriting from `AudioSink`, in contrast to `AudioSource` which does not have a default `__init__` function.

- The `wants_opus()` function determines if the sink should receive opus packets or decoded PCM packets.  Care should be taken not to unintentionally mix sinks that want different types.
- The `write()` function is the main callback, where the sink logic takes place.  In a sink pipeline, this could alter, inspect, or log a packet, and then write it to a child sink.  `VoiceData` is a simple container class with attributes for the origin member, opus data, optionally pcm data, and raw audio packet.
- The `cleanup()` function is identical to `AudioSource.cleanup()`, a finalizer to cleanup any loose ends when the sink has finished its job.

In addition, sinks also have properties for their `voice_client`, as well as `parent` and `child`/`children` sinks.  Furthermore, sinks will be able to receive events in a similar manner to cogs, but this has not been implemented yet. (TODO)

This extension comes with several useful built in sinks, which I will briefly explain another time.  For now just [source dive](discord/ext/voice_recv/sinks.py).  (TODO)

## New events
```python
async def on_voice_member_speak(member: discord.Member, ssrc: int)
```
Called when a member first speaks (transmits audio) in a voice channel.  This event is only called once per their voice session (ssrc assignment).  Any packets received from this member before this event fires can (probably) be safely ignored since they are likely just silence packets.  The main purpose of this event is to reveal the ssrc of a member, to map packets to their originating member.

This is **NOT** a speaking indicator event.  The speaking indicator is determined by packet activity.  This functionality will be added in the future.

```python
async def on_voice_member_disconnect(member: discord.Member, ssrc: int)
```
Called when a member disconnects from a voice channel. The `ssrc` parameter is the unique id a member has to identify which packets belong to them.  This is useful when using custom sinks, particularly those that handle packets from multiple members.

```python
async def on_voice_member_video(member: discord.Member, data: voice_recv.VoiceVideoStreams)
```
Called when a member in voice channel toggles their webcam on or off, NOT screenshare.  Screenshare status is only indicated in the `self_video` attribute of `discord.VoiceState`.

```python
async def on_voice_member_flags(member: discord.Member, flags: Optional[int])
```
An undocumented event dispatched when a member joins a voice channel containing a flags bitfield.  Only values `0`, `2`, and `None` have been observed so far, but their meaning remains unknown.

```python
async def on_voice_member_platform(member: discord.Member, platform: Optional[int | str])
```
An undocumented event dispatched when a member joins a voice channel containing a platform key, presumably with what platform the member joined on.  However, this field has only ever been seen to contain `None`.

## Currently missing features
- Sink events (similar to cog event handlers)
- Silence generation (will be implemented as an included AudioSink)
- Member speaking state status/event (design not yet decided)
- Various internal impl details to maintain audio stability and consistency

## Future plans
- Muxer AudioSink (mixes multiple audio streams into a single stream)
- Rust implementations of some components for improved performance
- Alternative voice client implementation with a minimal interface intended for use with external data processing
