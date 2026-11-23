# Analytics

`analytics.tsx` exposes the report page consumed by Settings. The same module
owns report loading, cancellation, filters, user rows and CSV links.
`performance.tsx` and `performance-trend.tsx` render the recorded timing view.
`analytics-shared.tsx` owns charts, the date picker and report controls.

Arithmetic and normalization live in `analytics-data.ts`, `analytics-dates.ts`,
`analytics-format.ts`, `analytics-render.js` and `performance-labels.ts`.
They can import each other and generated contracts, but cannot import renderers,
Settings or transport. The JavaScript helper keeps its TypeScript declaration.
Callers import individual files so the existing Settings lazy load remains
intact.

`tests/analytics` checks these import rules and existing report interfaces.
Dates, attribution, missing-cost handling, request counts and report arithmetic
retain their existing semantics.
