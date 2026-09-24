# Voice input (FR-40)

The workspace composer and approved phone companion use the browser's
`SpeechRecognition` or `webkitSpeechRecognition` interface when available on a
secure origin. Phone users can dictate a new Run prompt or an interjection to an
active Run. English and Chinese can be selected before recording on the phone;
the workspace uses its selected interface language by default.

The microphone starts only after the user presses **Dictate message** and grants
the browser's permission. The user presses **Stop dictation**, reviews or edits
the text, then presses **Use reviewed text**. This inserts text into the ordinary
message field; it never starts a Run or sends an interjection by itself. The
user still presses the existing submit button. Cancelling, hiding the page, or
unmounting the input aborts recognition and discards its draft. The application
does not upload or persist the raw audio. The browser may use an external speech
service, as described in the [Web Speech API guide](https://developer.mozilla.org/en-US/docs/Web/API/Web_Speech_API/Using_the_Web_Speech_API).

When the browser lacks speech recognition or the origin is insecure, the voice
button is unavailable and typed input remains usable. Browser support is
limited; see the [SpeechRecognition API reference](https://developer.mozilla.org/en-US/docs/Web/API/SpeechRecognition/start).

Local tests cover confirmation before insertion, cancellation, microphone denial,
language choice, and phone Run submission. Local Chromium used a simulated
recognition result and companion API to inspect 390px and desktop layouts.
Actual microphone permission, speech quality, mobile Safari and Android browser
behavior, and HTTPS deployment remain to be verified on devices.
