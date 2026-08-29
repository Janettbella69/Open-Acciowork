# TradeMind · Brand Spec
> Updated: 2026-08-29
> Sources: repository `README.md`, live frontend, https://youmind.com/overview

## Brand idea
- Name: TradeMind.
- Meaning: “Trade” states the cross-border commerce category; “Mind” signals research, judgment, and coordinated action.
- Homepage line: “Go beyond borders.”
- Supporting line: “From product idea to global sales.”
- Audience line: “AI global commerce intelligence for cross-border teams.”

## Core assets

### Mark
- Primary: `public/trademind-mark.svg`.
- Treatment: near-black circular mark with the TradeMind wordmark; coral is reserved for product accents and action states.
- Intended uses: navigation, application rail, favicon, and empty product state.
- The former `public/ophira-mark.svg` remains only as a compatibility asset and is not referenced by the current UI.

### Editorial imagery
- Story triptych: `public/images/global-commerce-triptych.png`, representing demand discovery, the supply/route layer, and buyer connection.
- Recommendation series: the four `public/images/recommendations/*-classical.jpg` assets use bright, historically inspired editorial painting to represent product discovery, sourcing, localization, and growth without copying a specific artwork.
- Introduction film: `public/images/trademind-introduction.png`, a separate cinematic signal-to-sale composition.
- Each adjacent section uses a different source or a code-native graphic treatment; YouMind artwork is never reused.

### Product interface
- The landing page introduces the product through an original cinematic path visual, avoiding a heavy browser or device mockup.
- The functional React workspace remains available at `/app`; the marketing visual does not pretend to be a live product screenshot.

## Color palette
- Canvas: `#ffffff`.
- Ink: `#1d1d1b`.
- Muted text: `#62625e` / `#969691`.
- Hairline: `#e7e7e2`.
- Brand accent: `#e8442d`.
- Deep route blue: `#173e9f`.
- Evidence green: `#2f6a52`.
- Warm editorial paper: `#efe8d9`.
- Avoid saturated purple AI gradients, glossy effects, and heavy device frames.

## Typography
- Marketing display: self-hosted Libre Baskerville.
- Interface and body copy: self-hosted Inter variable with platform fallbacks.
- Data and indexes: `SFMono-Regular`, Consolas, `Liberation Mono`, monospace.

## Layout system
- Navigation: quiet 76px sticky bar with centered links and a right-side workspace pill.
- Hero: one large serif proposition, one supporting line, a composer at roughly two-thirds of the viewport, prompt-lens controls, and four opportunity recommendations.
- Story rhythm: three visual chapters → cinematic product introduction → decision gallery → panoramic brand line → use cases → product architecture → CTA.
- Corners: 15–24px for cards and large surfaces; pills use full rounding.
- Motion: 160–620ms, limited to restrained rise, hover lift, and saturation changes.
- Breakpoints: desktop, 1180px, 860px, and 600px; horizontal content becomes swipeable where useful.

## Voice
- Use short, common English words.
- Start with the buyer or market need, then explain the commercial path.
- Prefer “market,” “product,” “supply,” “route,” “buyer,” and “sale” over abstract AI language.
- Never fabricate customer counts, transaction volume, revenue, testimonials, or performance claims.

## Reference boundary
- YouMind informs page rhythm, whitespace, editorial type, and input-led product storytelling only.
- TradeMind uses original copy, global-commerce scenarios, UI, mark, colors, and generated imagery.
