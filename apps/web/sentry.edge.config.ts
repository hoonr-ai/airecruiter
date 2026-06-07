import * as Sentry from "@sentry/nextjs";

// Active when NEXT_PUBLIC_SENTRY_DSN is set — works for QA, staging, and production.
Sentry.init({
  dsn: process.env.NEXT_PUBLIC_SENTRY_DSN,
  environment: process.env.NEXT_PUBLIC_ENVIRONMENT,

  tracesSampleRate: 0,

  enabled: !!process.env.NEXT_PUBLIC_SENTRY_DSN,
});
