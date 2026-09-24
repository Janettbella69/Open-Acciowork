# Voice input (FR-40)

The workspace composer and an approved phone companion's new-task form use the
browser's `SpeechRecognition` or `webkitSpeechRecognition` interface when it is
available on a secure origin. The microphone starts only after the user presses
**Dictate message**. The user stops recording, reviews or edits the transcript,
and presses **Use reviewed text** to place it in the ordinary message field.
The user still presses **Send** or **Start run**. Recognition never submits a
message on its own.

The phone form offers English and Chinese recognition language choices. The
workspace defaults to its current interface language. Cancelling, hiding the
page, disabling the form, or unmounting the component aborts recognition and
discards its draft. The application does not upload or persist raw audio;
the browser may use its own speech service. Unsupported browsers or insecure
origins keep typed input available.

Local tests cover review before insertion, cancellation, denied permission,
language choice, insecure origin, and phone Run submission. Voice input for
phone interjections depends on the FR-31 interjection entry point and remains
to be integrated. Actual microphone permission, speech quality, mobile Safari
and Android behavior, and HTTPS deployment still need device acceptance.
