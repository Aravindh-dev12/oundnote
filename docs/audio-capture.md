# Recording a video call

In **New meeting**, enable **Microphone** and **System audio** together. Choose
your microphone in the first group and the output you listen to in the second.
Each input has its own level meter. Adding an input keeps the device selected in
the other group. You can also record either input on its own. **Media file**
switches to importing an existing recording.

Microphone + system produces one local mono WAV at 48 kHz and sends that same
mix to live transcription. Both inputs have equal gain with headroom for people
speaking simultaneously. Pause and resume control both devices. A silent system
output does not stop microphone recording. The recording metadata retains both
device names and their original audio formats.

Use headphones to avoid capturing the call again acoustically through your
microphone. The system input records all applications playing through the chosen
output, including notification sounds. It does not isolate Teams or Meet.

## Windows

Choose the headphones or speakers that Teams, Meet or the browser actually uses.
Oundnote exposes their WASAPI loopback inputs under **System audio**. No virtual
mixer is required. The application must have microphone access in Windows.

## macOS

Microphone recording uses CoreAudio. System audio currently requires a virtual
input such as [BlackHole](https://github.com/ExistentialAudio/BlackHole); Oundnote
does not yet implement ScreenCaptureKit or Core Audio Taps.

1. Install BlackHole separately.
2. In **Audio MIDI Setup**, create a **Multi-Output Device** containing your
   headphones/speakers and BlackHole. Follow BlackHole's
   [Multi-Output Device guide](https://github.com/ExistentialAudio/BlackHole/wiki/Multi-Output-Device)
   for clock and drift settings.
3. Send the call's output to that Multi-Output Device so you can hear it and
   BlackHole receives it.
4. Select your physical microphone and **BlackHole** in Oundnote.
5. Allow microphone access for Oundnote, or the terminal/Python host running it.

## Linux

Microphone recording uses the inputs exposed by PortAudio (ALSA/PulseAudio/
PipeWire). System audio needs an output monitor exposed as a PortAudio input.
PulseAudio provides a monitor for each sink, but the ALSA/PortAudio configuration
does not always list these monitors individually.

If a monitor already appears in **System audio**, select the one corresponding
to the call's output. Otherwise, on systems using the ALSA PulseAudio plugin:

1. Find the output monitor name with `pactl list short sources` (the PulseAudio
   compatibility server also supports this on PipeWire).
2. Add a named input to `~/.asoundrc`, preserving any existing configuration:

   ```text
   pcm.oundnote_monitor {
       type pulse
       device "YOUR_OUTPUT_MONITOR_NAME"
       hint {
           show on
           description "Oundnote system monitor"
       }
   }
   ```

3. Replace `YOUR_OUTPUT_MONITOR_NAME` with the actual monitor, restart Oundnote,
   and refresh the device list. Select your microphone plus **Oundnote system
   monitor**. Availability depends on the distribution's ALSA PulseAudio plugin;
   this is commonly provided by `libasound2-plugins` or `alsa-plugins-pulseaudio`.

See the [PulseAudio monitor documentation](https://wiki.freedesktop.org/www/Software/PulseAudio/FAQ/)
and the [ALSA PulseAudio plugin configuration](https://github.com/alsa-project/alsa-plugins/blob/master/doc/README-pulse).
An unavailable system input is shown with platform-specific setup guidance;
Oundnote does not silently record only the microphone when both were selected.

## Device changes and troubleshooting

Refresh the list after connecting or configuring a device. If a selected device
disappears, choose its replacement before starting. If one cannot be opened,
both inputs are closed and the failed recording is removed so you can retry.
During combined capture, a stopped stream or an input overflow is reported in
the live capture strip. Save the available recording, reconnect the device, and
start a new recording. Changing devices during a recording is not supported.

## API compatibility

`POST /api/capture/sessions` still accepts `source_id` for one input. New clients
can send `source_ids: ["microphone-id", "system-id"]` (one or two distinct IDs).
Two inputs must contain exactly one system source and one microphone/interface.
Do not send both request fields. Responses retain the primary `source` for older
clients and include `sources`, `source_levels`, and `capture_error`.

The native APIs and mixer are covered by simulated Windows, Linux and macOS
drivers in the test suite. Physical devices and permissions should also be
checked on each target OS before distributing a release.
