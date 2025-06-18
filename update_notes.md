# Update notes
Notably, not a changelog, just notes.

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
