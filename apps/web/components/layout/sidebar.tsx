"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Briefcase, Users, Settings } from "lucide-react";
import { cn } from "@/lib/utils";
import { AzureLoginButton } from "@/components/auth/AzureLoginButton";

export function Sidebar() {
    const pathname = usePathname();

    const navItems = [
        { label: "Jobs", href: "/", icon: Briefcase, disabled: false },
        { label: "Candidates", href: "/candidates", icon: Users, disabled: true },
        { label: "Settings", href: "/settings", icon: Settings, disabled: false },
    ];

    return (
        <div className="w-[260px] border-r border-slate-200 bg-white h-screen flex flex-col fixed left-0 top-0 p-6">
            {/* Brand wordmark — stacked "Hoonr./Curate" with the arrow
                flourishes baked into the PNG. Source: apps/web/public/hoonr-curate-logo.png.
                `unoptimized` skips Next's image pipeline since the source PNG is oversized
                (2730×1536, 4MB) and we just want the raw asset rendered by the browser. */}
            <div className="brand flex items-center justify-center mb-10">
                <img
                    src="/hoonr-curate-logo.png"
                    alt="Hoonr.Curate"
                    width={200}
                    height={112}
                    className="object-contain"
                    style={{ width: 200, height: "auto" }}
                />
            </div>

            <nav>
                <ul className="space-y-2 list-none">
                    {navItems.map((item) => {
                        const isActive = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
                        const Icon = item.icon;

                        return (
                            <li key={item.label}>
                                {item.disabled ? (
                                    <div
                                        aria-disabled="true"
                                        title="Temporarily disabled"
                                        className="flex items-center px-4 py-3 text-[14px] font-medium rounded-lg transition-all duration-200 text-slate-400 bg-slate-50 cursor-not-allowed opacity-70"
                                    >
                                        <Icon className="mr-3 h-[20px] w-[20px] text-slate-300" />
                                        {item.label}
                                    </div>
                                ) : (
                                    <Link
                                        href={item.href}
                                        className={cn(
                                            "flex items-center px-4 py-3 text-[14px] font-medium rounded-lg transition-all duration-200 group",
                                            isActive
                                                ? "bg-primary text-white shadow-md shadow-primary/20"
                                                : "text-slate-500 hover:bg-slate-50 hover:text-slate-900"
                                        )}
                                    >
                                        <Icon className={cn(
                                            "mr-3 h-[20px] w-[20px] transition-colors duration-200",
                                            isActive ? "text-white" : "text-slate-400 group-hover:text-slate-600"
                                        )} />
                                        {item.label}
                                    </Link>
                                )}
                            </li>
                        );
                    })}
                </ul>
            </nav>

            <AzureLoginButton />
        </div>
    );
}
