import type { NextConfig } from "next";
import { withSentryConfig } from "@sentry/nextjs";

const nextConfig: NextConfig = {
};

export default withSentryConfig(nextConfig, {
  // Suppresses source-map upload logs during build.
  silent: true,
  // Source maps are uploaded only when SENTRY_AUTH_TOKEN is present
  // (typically in CI / prod builds). Local dev builds skip the upload.
  org: process.env.SENTRY_ORG,
  authToken: process.env.SENTRY_AUTH_TOKEN,
  // Hide source maps from public bundles after upload.
  widenClientFileUpload: true,
  hideSourceMaps: true,
  disableLogger: true,
  // Tunnel browser events through a same-origin route to bypass ad-blockers.
  tunnelRoute: "/monitoring",
  // Don't fail the build if the Sentry release step errors out.
  errorHandler: () => {},
});
