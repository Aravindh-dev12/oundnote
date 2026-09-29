# Native desktop shell

Oundnote's web application remains the source of truth for the Hub, capture
workflow, meeting notes, settings, transcription, and Live Assistant. The
desktop shell wraps that UI in native windows instead of maintaining a second
application stack.

## Architecture

1. The launcher starts the existing `local_meeting_ai` server as a child process
   only when another Oundnote instance is not already listening on the selected
   host and port.
2. pywebview 6.2.1 opens the Hub in a native WebView window and a separate
   always-on-top frameless Flow Bar.
3. The Flow Bar calls the existing `/api/capture/sessions` endpoints, keeping
   one capture engine and one source of truth for meetings.
4. pynput registers Ctrl+Alt+Space by default and asks the Flow Bar to toggle
   listening.
5. Closing the main native window asks `/api/application/shutdown` to stop the
   child server and release local models.

## Install

Windows:

`install.ps1 -Desktop`

`start-desktop.bat`

macOS/Linux:

`./install.sh --desktop`

`./start-desktop.sh`

The browser launcher remains available when a native WebView backend is not
available.

## Security and privacy

The desktop shell talks to the same loopback-only Oundnote server. It does not
add a cloud relay, upload recordings, or create a second database. The global
shortcut only toggles the local capture session.

## Current boundary

The desktop shell covers the native window and Flow Bar interaction model. It
does not yet inject dictated text into arbitrary third-party applications;
that requires platform-specific accessibility/Input APIs and should be added
as a separate native integration rather than simulated in JavaScript.