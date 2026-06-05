import * as Sentry from "@sentry/nextjs";

// Sentry is enabled whenever NEXT_PUBLIC_SENTRY_DSN is set.
// This makes it work transparently in any environment (QA, staging, production)
// without changing NODE_ENV. Leave NEXT_PUBLIC_SENTRY_DSN unset to disable.
Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENVIRONMENT,

  // Capture 100 % of errors; set > 0 to enable performance tracing.
  tracesSampleRate: 0,

  // Tunnel browser events through the same-origin route defined in next.config.ts
  // to avoid ad-blocker interference.
  tunnel: "/sentry-tunnel",

  // Active when a DSN is provided — works for QA, staging, and production alike.
  enabled: !!process.env.NEXT_PUBLIC_SENTRY_DSN,
});
