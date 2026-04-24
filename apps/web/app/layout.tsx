import type { Metadata } from "next";
import { Inter, Outfit } from "next/font/google";
import "./globals.css";
import { Sidebar } from "@/components/layout/sidebar";
import { AIProvider } from "@/context/ai-context";
import { MsalProviderWrapper } from "@/components/auth/MsalProviderWrapper";
import { TiraChat } from "@/components/ai/tira-chat";
import { TiraFab } from "@/components/ai/tira-fab";
import { AppGuard } from "@/components/auth/AppGuard";

const inter = Inter({ subsets: ["latin"], variable: "--font-inter" });
const outfit = Outfit({ subsets: ["latin"], variable: "--font-outfit" });

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
      <body className={`${inter.variable} ${outfit.variable} antialiased min-h-screen bg-background text-foreground font-inter`}>
        <MsalProviderWrapper>
          <AppGuard>
            <AIProvider>
              <div className="flex min-h-screen">
                <Sidebar />
                <main className="flex-1 ml-64 p-8 overflow-y-auto min-h-screen bg-[#f8fafc]">
                  {children}
                </main>
              </div>
              <TiraChat />
              <TiraFab />
            </AIProvider>
          </AppGuard>
        </MsalProviderWrapper>
      </body>
    </html>
  );
}
