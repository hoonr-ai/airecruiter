"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import { LayoutDashboard, Briefcase, Users, Settings, Megaphone, UsersRound, ShieldOff, FileClock } from "lucide-react";
import { cn } from "@/lib/utils";
import { AzureLoginButton } from "@/components/auth/AzureLoginButton";
import { useUserRole } from "@/hooks/use-user-role";

export function Sidebar() {
    const pathname = usePathname();
    const { isAdmin, isTeamLead, teamName, isLoading } = useUserRole();

    const navItems = [
        { label: "Jobs", href: "/", icon: Briefcase, disabled: false },
        { label: "Campaigns", href: "/campaigns", icon: Megaphone, disabled: false },
        { label: "Candidates", href: "/candidates", icon: Users, disabled: true },
        // Admins get the full analytics + team management; team leads get the
        // same analytics page auto-scoped to their team by the backend.
        ...(isAdmin
            ? [
                  { label: "Admin Analytics", href: "/admin/analytics", icon: LayoutDashboard, disabled: false },
                  { label: "Launch Report", href: "/admin/launch-report", icon: FileClock, disabled: false },
                  { label: "Teams", href: "/admin/teams", icon: UsersRound, disabled: false },
                  { label: "No Contact List", href: "/admin/no-contact", icon: ShieldOff, disabled: false },
              ]
            : []),
        // Team leads get both pages auto-scoped to their team by the backend.
        ...(!isAdmin && isTeamLead
            ? [
                  { label: "Team Analytics", href: "/admin/analytics", icon: LayoutDashboard, disabled: false },
                  { label: "Launch Report", href: "/admin/launch-report", icon: FileClock, disabled: false },
              ]
            : []),
        { label: "Settings", href: "/settings", icon: Settings, disabled: false },
    ];

    return (
        <div className="w-[260px] border-r border-slate-200 bg-white h-screen flex flex-col fixed left-0 top-0 p-6">
            {/* PAIR brand wordmark. Source: apps/web/public/pair-logo.png — 800×242
                (3.3:1), already trimmed to the artwork so there is no dead canvas to
                letterbox. width/height below must keep that ratio or the reserved box
                shifts on load. Plain <img> rather than next/image: it is a fixed-size
                brand asset, so the resize pipeline buys nothing. */}
            <div className="brand flex items-center justify-center mb-10">
                <img
                    src="/pair-logo.png"
                    alt="PAIR"
                    width={200}
                    height={61}
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

            {/* Role identity chip — team leads see "Team Lead" instead of
                recruiter/admin, per the team management spec. */}
            {!isLoading && (isAdmin || isTeamLead) && (
                <div className="mt-4 px-4">
                    <span
                        className={cn(
                            "inline-flex items-center gap-1.5 rounded-full px-2.5 py-1 text-[11px] font-semibold ring-1 ring-inset",
                            isAdmin
                                ? "bg-slate-100 text-slate-600 ring-slate-200"
                                : "bg-indigo-50 text-indigo-700 ring-indigo-200"
                        )}
                        title={!isAdmin && teamName ? `Team: ${teamName}` : undefined}
                    >
                        {isAdmin ? "Admin" : "Team Lead"}
                        {!isAdmin && teamName ? <span className="font-medium text-indigo-500">· {teamName}</span> : null}
                    </span>
                </div>
            )}

            <AzureLoginButton />
        </div>
    );
}
