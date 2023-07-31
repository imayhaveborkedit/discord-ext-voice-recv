# discord-ext-voice-recv
Voice receive extension package for discord.py

## Warning
**This extension should be more or less functional, but the code is not yet feature complete.  No guarantees are given for stability or random breaking changes.**

## Installing
**Python 3.8 or higher is required**, preferably 3.11 or whatever is latest

```
python -m pip install discord-ext-voice-recv
```

To install directly from github:
```
python -m pip install git+https://github.com/imayhaveborkedit/discord-ext-voice-recv
```

Naturally, this extension depends on `discord.py` being installed with voice support (`pynacl`).

## Example
See the [example script](examples/recv.py).

## Feature overview
### Custom VoiceProtocol client
No monkey patching or bizarre hacks required.  Simply use the library feature to use `VoiceRecvClient` as the voice client class.  See [Usage](#usage).

### Six new events
This extension adds the unimplemented voice websocket events and one virtual event.  See [New Events](#new-events).

### Speaking state
It is now possible to determine if a member is speaking or not, using `VoiceRecvClient.get_speaking()`.  An event for changes is on the todo list as a maybe.

### Simple and familiar API
The overall API is designed to mirror the discord.py voice send API, with `AudioSink` being the counterpart to the existing `AudioSource`.  See [Sinks](#sinks).

### Convenient included utilities
Batteries included in the form of useful built in `AudioSinks`.  Some to match their `AudioSource` counterpart, some I merely considered useful.  See... uh... TODO.

### More or less typed
Its probably fine.

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

```python
def get_speaking(member: discord.Member | discord.User) -> bool | None
```
Gets the speaking state (voice activity, the green circle) of a member.  User is typed in for convenience.  Returns None if the member was not found.

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

    def write(self, user: User | Member | None, data: VoiceData):
        ...

    def cleanup(self):
        ...
```

These are the main functions of a sink, names and purpose reflecting that of their source counterparts.  It is important to note that `super().__init__()` must be called when inheriting from `AudioSink`, in contrast to `AudioSource` which does not have a default `__init__` function.

- The `wants_opus()` function determines if the sink should receive opus packets or decoded PCM packets.  Care should be taken not to unintentionally mix sinks that want different types.
- The `write()` function is the main callback, where the sink logic takes place.  In a sink pipeline, this could alter, inspect, or log a packet, and then write it to a child sink.  `VoiceData` is a simple container class with attributes for the origin member, opus data, optionally pcm data, and raw audio packet.
- The `cleanup()` function is identical to `AudioSource.cleanup()`, a finalizer to cleanup any loose ends when the sink has finished its job.

Additionally, sinks also have properties for their `voice_client`, as well as `parent` and `child`/`children` sinks.

This extension comes with several useful built in sinks, which I will briefly explain another time.  For now just [source dive](discord/ext/voice_recv/sinks.py).  (TODO)

### Sink event listeners
With AudioSinks being potentially more complex and stateful than AudioSources and the addition of new events, it is sometimes necessary to handle events in the context of a sink.  It would be rather awkward to have to register a sink function with `commands.Bot.add_listener()` while dealing with thread safety, and even moreso using `discord.Client`.  To remedy this, listeners can be defined within sinks, similarly to how they work in Cogs.

```python
class MySink(AudioSink):
    @AudioSink.listener()
    def on_voice_member_disconnect(self, member: discord.Member, ssrc: int | None):
        print(f"{member} has disconnected")
        self.do_something_like_handle_disconnect(ssrc)
```

Note that these functions must be sync functions, as they are dispatched from a thread.  Trying to use a coroutine will result in an error.  This restriction only applies to sink listeners, and normal async event listeners will function as per usual.  The event listener dispatch thread is different from the one used to dispatch the `write()` callback so potential threadsafety issues should be considered.  A decorator argument to run the event callback in the other thread may be added later.

## New events
```python
async def on_voice_member_speaking_state(member: discord.Member, ssrc: int, state: SpeakingState | int)
```
First and foremost, this event does **NOT** refer to the speaking indicator in discord (the green circle).  For voice activity, see `...TODO...`.
This event is fired when the speaking state of a member changes.  This happens when:
- A member first speaks (transmits audio) in a voice, but only once
- A member activates or deactivates priority speaker mode

This event is fired once initially to reveal the ssrc of a member, an identifier to map packets to their originating member.  Any packets received from this member before this event fires can (probably) be safely ignored since they are likely just silence packets.

```python
async def on_voice_member_disconnect(member: discord.Member, ssrc: int | None)
```
Called when a member disconnects from a voice channel. The `ssrc` parameter is the unique id a member has to identify which packets belong to them.  This is useful when using custom sinks, particularly those that handle packets from multiple members.

```python
async def on_voice_member_video(member: discord.Member, data: voice_recv.VoiceVideoStreams)
```
Called when a member in voice channel toggles their webcam on or off, NOT screenshare.  Screenshare status is only indicated in the `self_video` attribute of `discord.VoiceState`.

```python
async def on_voice_member_flags(member: discord.Member, flags: int | None)
```
An undocumented event dispatched when a member joins a voice channel containing a flags bitfield.  Only values `0`, `2`, and `None` have been observed so far, but their meaning remains unknown.

```python
async def on_voice_member_platform(member: discord.Member, platform: int | str | None)
```
An undocumented event dispatched when a member joins a voice channel containing a platform key, presumably with what platform the member joined on.  However, this field has only ever been seen to contain `None`.

```python
def on_rtcp_packet(packet: RTCPPacket, guild: discord.Guild)
```
A virtual event for when an RTCP packet is received.  This event only works inside of sinks, so it cannot be async.

## Currently missing features
- Silence generation (will be implemented as an included AudioSink)
- Member speaking state status/event (design not yet decided)
- Various internal impl details to maintain audio stability and consistency

## Future plans
- Muxer AudioSink (mixes multiple audio streams into a single stream)
- Rust implementations of some components for improved performance
- Alternative voice client implementation with a minimal interface intended for use with external data processing
