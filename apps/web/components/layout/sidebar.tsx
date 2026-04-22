"use client";

import Link from "next/link";
import Image from "next/image";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Briefcase, Users, Settings } from "lucide-react";
import { cn } from "@/lib/utils";

export function Sidebar() {
    const pathname = usePathname();

    const navItems = [
        { label: "Jobs", href: "/", icon: Briefcase },
        { label: "Candidates", href: "/candidates", icon: Users },
        { label: "Settings", href: "/settings", icon: Settings },
    ];

    return (
        <div className="w-[260px] border-r border-slate-200 bg-white h-screen flex flex-col fixed left-0 top-0 p-6">
            <div className="brand flex items-center gap-3 mb-10">
                <Image
                    src="/hoonr-logo.png"
                    alt="Hoonr.Curate"
                    width={160}
                    height={36}
                    priority
                    className="object-contain"
                />
            </div>

            <nav>
                <ul className="space-y-2 list-none">
                    {navItems.map((item) => {
                        const isActive = item.href === "/" ? pathname === "/" : pathname.startsWith(item.href);
                        const Icon = item.icon;

                        return (
                            <li key={item.label}>
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
                            </li>
                        );
                    })}
                </ul>
            </nav>
        </div>
    );
}
