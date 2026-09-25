# Media draft generation (FR-27)

`mcp__ophira_media__generate_media` creates a draft file, validates its type and
signature, and registers it as an Artifact with the originating Run and any supplied
Evidence/Artifact source IDs. It does not publish the result. The main Agent remains
on Claude Agent SDK; the OpenAI integration below is only an image-producing MCP
adapter.

## Image adapter

Set these operator-owned backend values to enable direct image generation:

```dotenv
OPHIRA_MEDIA_IMAGE_PROVIDER=openai
OPHIRA_MEDIA_OPENAI_API_KEY=<pay-as-you-go OpenAI API key>
```

The adapter sends one request to the fixed HTTPS
`https://api.openai.com/v1/images/generations` endpoint using the dated
`gpt-image-2.5-flare-2026-09-08` model. It requests one 1024×1024 PNG at medium
quality. The tool filename must end in `.png`; it does not accept an arbitrary
OpenAI-compatible base URL or send this key to `OPHIRA_MEDIA_ENDPOINT`. The Image
API returns `data[0].b64_json`, which the adapter bounds before JSON/base64 decoding
and checks again as PNG bytes before writing. The generic media path remains the
default when `OPHIRA_MEDIA_IMAGE_PROVIDER=raw_binary`.

The endpoint, request parameters, response shape, and dated model ID follow the
[official OpenAI Image API guide](https://developers.openai.com/api/docs/guides/image-generation),
[image generation reference](https://developers.openai.com/api/reference/cli/resources/images/methods/generate),
and [Flare model page](https://developers.openai.com/api/docs/models/gpt-image-2.5-flare).
Account access may require API Organization Verification, as stated in the guide.

Image API charges are **separate from the Claude Agent SDK Run budget** and are
subject to the OpenAI account's billing and spend limits. The tool is not in the
Agent's preapproved tool list, so normal Run permission review applies. The key
stays in backend settings and is blanked from the SDK worker environment. No
automatic retry is attempted after an uncertain provider response.

## Video adapter

To opt into Runway Dev text-to-video generation, set:

```dotenv
OPHIRA_MEDIA_VIDEO_PROVIDER=runway
OPHIRA_MEDIA_RUNWAY_API_KEY=<Runway Dev API secret>
```

The adapter submits one `gen4.5` task to the fixed
`https://api.dev.runwayml.com/v1/image_to_video` endpoint with `promptText`, no
input image, portrait `720:1280` output, and a five-second duration. It returns
`MEDIA_TASK_PENDING` while the task runs. Calling `generate_media` later with the
same prompt, filename, and source IDs retrieves the existing task; it never
starts another paid generation for that request. A local task record is written
before the outbound request. If creation may have succeeded but no task ID was
saved, the adapter reports `MEDIA_TASK_CREATE_UNCERTAIN` and requires manual
reconciliation instead of silently paying again. Changing the filename starts
a different request.

After success, the adapter downloads the short-lived result URL without the
Runway key. Every URL and redirect is checked and pinned to a public HTTPS
address, the body is streamed with a 250 MB limit, and MP4 signature and
Artifact/source checks run before completion. A process interruption after the
task ID is saved can resume status polling; interrupted downloads can be
reclaimed after a ten-minute lease. The result URL itself is never returned
to the Agent or stored in the task record.

The endpoint and text-only Gen-4.5 request come from Runway's
[API getting started guide](https://docs.dev.runwayml.com/guides/using-the-api/).
Runway [documents asynchronous task polling](https://docs.dev.runwayml.com/api-details/sdks/)
and [short-lived output links](https://docs.dev.runwayml.com/assets/outputs/).
Its [input guide](https://docs.dev.runwayml.com/assets/inputs/) lists the
portrait ratio. Runway Dev requires its own API credits; those charges are
**outside the Claude Agent SDK Run budget**. This adapter has no live account,
credit, latency, or video quality acceptance yet.

## Generic binary adapter

The existing connector can still generate image or video drafts when an operator
sets both `OPHIRA_MEDIA_ENDPOINT` and `OPHIRA_MEDIA_API_KEY`. It sends a JSON POST
with `{ "prompt": "...", "kind": "image|video" }` and expects raw bytes with a
supported image or video `Content-Type`. It never follows redirects and applies
file size, signature, filename, workspace, and source-ID checks. Either media
type continues to use this connector when its provider setting is `raw_binary`.

This is a connector contract, not certification of a generic video provider. OpenAI's
[API deprecations](https://developers.openai.com/api/docs/deprecations) list the
Sora video API shutdown on 2026-09-24, so this project does not wire video to
that discontinued API.

## Verification boundary

The OpenAI and Runway request/response contracts, failure and recovery paths,
output validation, and Artifact/Run/Evidence links have offline tests with
`httpx.MockTransport`. No live account/key, generated image/video, provider
invoice, or human visual quality review has been validated here. Those remain
FR-27 acceptance work.
