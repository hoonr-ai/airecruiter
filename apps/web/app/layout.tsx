import type { Metadata } from "next";
import { Inter, Outfit } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import { Sidebar } from "@/components/layout/sidebar";
import { AIProvider } from "@/context/ai-context";
import { NotificationsProvider } from "@/context/notifications-context";
import { MsalProviderWrapper } from "@/components/auth/MsalProviderWrapper";
import { AuthGuard } from "@/components/auth/AuthGuard";
import { TiraChat } from "@/components/ai/tira-chat";
import { TiraFab } from "@/components/ai/tira-fab";
import { LaunchTracker } from "@/components/layout/launch-tracker";
import { TelemetryBootstrap } from "@/components/TelemetryBootstrap";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });
const outfit = Outfit({ subsets: ["latin"], variable: "--font-outfit" });

export const metadata: Metadata = {
  title: "PAIR",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className={`${inter.variable} ${outfit.variable} antialiased min-h-screen bg-background text-foreground font-inter`}>
        <MsalProviderWrapper>
          <AuthGuard>
            <AIProvider>
              <NotificationsProvider>
                <TelemetryBootstrap />
                <div className="flex min-h-screen">
                  <Sidebar />
                  <main className="flex-1 ml-64 p-8 overflow-y-auto min-h-screen bg-[#f8fafc]">
                    {children}
                  </main>
                </div>
                <TiraChat />
                <TiraFab />
                <LaunchTracker />
              </NotificationsProvider>
            </AIProvider>
          </AuthGuard>
        </MsalProviderWrapper>
        {/* New Relic Browser agent: picks the PAIR Web app per host (PROD / QA;
            localhost only after ?nr=1) and is a no-op on any other host.
            beforeInteractive so it wraps fetch/XHR before app code (and
            lib/api.ts's fetch patch) runs. Table: lib/nr-config.ts. */}
        <Script src="/newrelic-browser-agent.js" strategy="beforeInteractive" />
      </body>
    </html>
  );
}
