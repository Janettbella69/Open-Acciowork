# Frontend requirement inventory

Updated: 2026-08-05

| Requirement | Status | Evidence / note |
| --- | --- | --- |
| Build an original OPHIRA marketing site inspired by YouMind principles | Delivered | `/` uses an input-led hero, editorial scale, generous whitespace, and first-party OPHIRA content |
| Build a usable frontend product surface | Delivered | `/app` supports research briefs, project selection, and Demand / Supply / Capital / Sales views |
| Match the OPHIRA opportunity-team architecture | Delivered | Opportunity Lead owns one project truth and identifies the weakest validation dimension |
| Distinguish facts, user statements, community signals, and inference | Delivered | Demand evidence ledger exposes all four evidence kinds without promoting mock content to fact |
| Avoid fabricated commercial precision | Delivered | Sample workspace is explicitly illustrative; capital values remain uncalculated until inputs exist |
| Responsive marketing and product UI | Delivered | Desktop, tablet, and mobile breakpoints; mobile controls retain 44px minimum targets |
| Automated frontend verification | Delivered | Component flow tests, ESLint, TypeScript, and Vite production build |
| Connect the live backend API and SSE event stream | Delivered | A typed client creates projects and runs, renders durable project truth, consumes protocol-discovered SSE event names, exposes tool/subagent/task activity, handles approvals and cancellation, and refreshes the ledger |
| Authentication and external side effects | Omitted | Not requested; the first frontend is a local product prototype |
| Production deployment | Deferred | `VITE_OPHIRA_API_URL` supports a deployed API origin, but no hosting target, domain, or deployment workflow has been selected |
