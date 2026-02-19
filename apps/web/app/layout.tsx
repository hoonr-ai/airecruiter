import type { Metadata } from "next";
import "./globals.css";
import { Sidebar } from "@/components/layout/sidebar";
import { AIProvider } from "@/context/ai-context";
import { AriaChat } from "@/components/ai/aria-chat";
import { AriaFab } from "@/components/ai/aria-fab";

export const metadata: Metadata = {
  title: "Hoonr.ai | Deployment OS",
  description: "Advanced Talent Operating System",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html lang="en">
      <body className="antialiased min-h-screen bg-background text-foreground">
        <AIProvider>
          <div className="flex min-h-screen">
            <Sidebar />
            <main className="flex-1 ml-64 p-8 overflow-y-auto min-h-screen">
              {children}
            </main>
          </div>
          <AriaChat />
          <AriaFab />
        </AIProvider>
      </body>
    </html>
  );
}
