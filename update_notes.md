# Update notes
Notably, not a changelog, just notes.

## 0.5.4 (idoldange fork)
- Adds receive support for Discord's DAVE end-to-end encrypted voice calls (requires discord.py with DAVE support and `davey`)
  - After transport decryption every voice frame is MLS ciphertext, so opus used to fail with `corrupted stream`.  Frames are now decrypted with the `DaveSession` discord.py already keeps for sending.
  - Frames that cannot be decrypted (session not ready, ssrc not mapped to a user yet, mid epoch transition) are dropped instead of being fed to opus.
  - Silence frames and non-E2EE calls (protocol version 0) pass through untouched.
  - The FEC path peeks at the next, still encrypted, packet; it is now decrypted exactly once before use.
- Opus decode errors no longer escape: they fall back to packet loss concealment, then to a silent frame.
- A decoder raising inside the packet router no longer stops listening for every user; the frame is logged and dropped.
- Adds `tests/test_dave.py`
- Credit: the idea of decrypting through discord.py's `DaveSession` comes from upstream PRs #54 and #58

## 0.5.2
- Adds `extras.localplayback` module
- Adds info about the extras modules to the readme
- Adds `WavSink` as an alias to `WaveSink`
- Fixed a member cleanup error in SpeechRecognitionSink
- Changes the optional dependency format
  - Previously it was a single optional dep, `extras`.  Now there is a dependency per module, with `extras` installing all of them.  See the readme for details.

## 0.5.1
- Fixes a build process related error
- Changes `voice_recv.extras` import semantics
  - The `__all__` contents of the extras modules are no longer `*` imported into `voice_recv.extras` (this was only `extras.SpeechRecognitionSink`).  You will have to access them directly, or import that specific extra module.  Example:
    ```py
    from discord.ext.voice_recv.extras.speechrecognition import SpeechRecognitionSink
    # or
    from discord.ext.voice_recv.extras import speechrecognition
    sink = speechrecognition.SpeechRecognitionSink(...)
    ```
