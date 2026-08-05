# Frontend requirement inventory

Updated: 2026-08-05

| Requirement | Status | Evidence / note |
| --- | --- | --- |
| Build an original OPHIRA marketing site inspired by YouMind principles | Delivered | `/` uses an input-led hero, editorial scale, generous whitespace, and first-party OPHIRA content |
| Build a usable frontend product surface | Delivered | `/app` keeps the empty, running, completed, failed, and saved-project states inside one conversation workspace |
| Keep website briefs on the formal workspace | Delivered | `/app?brief=...` seeds the primary composer, then normalizes to `/app` after the brief is consumed; starting a run never switches to the retired dashboard |
| Provide an account-to-settings navigation flow | Delivered | Clicking the workspace identity opens an accessible account menu; Settings routes to `/app/settings`, while outside press and Escape close the menu |
| Expose external capability policies | Delivered | Settings includes Plugins, Browser, and Computer control permission preferences; high-risk computer control defaults to disabled |
| Prepare recurring Agent work | Delivered | `/app/schedules` provides categorized templates and a functional local task-draft creator; automatic execution is explicitly deferred until the scheduler API is connected |
| Prepare trusted companion-device control | Delivered | Computer control settings include discovery/control and keep-awake preferences plus an honest pairing placeholder; no fake QR code or mobile account is shown before secure pairing services exist |
| Match the OPHIRA opportunity-team architecture | Delivered | Opportunity Lead owns one project truth and identifies the weakest validation dimension |
| Distinguish facts, user statements, community signals, and inference | Delivered | The live evidence ledger renders the backend evidence kind for every claim without promoting it to fact |
| Avoid fabricated commercial precision | Delivered | Sample workspace is explicitly illustrative; capital values remain uncalculated until inputs exist |
| Responsive marketing and product UI | Delivered | Desktop, tablet, and mobile breakpoints; mobile controls retain 44px minimum targets |
| Automated frontend verification | Delivered | Component flow tests, ESLint, TypeScript, and Vite production build |
| Connect the live backend API and SSE event stream | Delivered | The persistent conversation shell creates projects and runs, renders durable project truth, consumes protocol-discovered SSE event names, exposes Trace and Agent activity, handles approvals and cancellation, and refreshes the ledger |
| Authentication and external side effects | Omitted | Not requested; the first frontend is a local product prototype |
| Production deployment | Deferred | `VITE_OPHIRA_API_URL` supports a deployed API origin, but no hosting target, domain, or deployment workflow has been selected |
