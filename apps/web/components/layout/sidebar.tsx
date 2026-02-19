"use client";

import Link from "next/link";
import Image from "next/image";
import { LayoutDashboard, Briefcase, Users, Settings, PlusCircle } from "lucide-react";
import { Button } from "@/components/ui/button";

export function Sidebar() {
    return (
        <div className="w-64 border-r border-border bg-card/50 backdrop-blur-xl h-screen flex flex-col fixed left-0 top-0">
            <div className="p-6">
                <Link href="/" className="flex items-center gap-2 mb-8 cursor-pointer hover:opacity-80 transition-opacity">
                    <div className="relative w-32 h-10">
                        <Image
                            src="/hoonr-logo.png"
                            alt="Hoonr.ai"
                            fill
                            className="object-contain object-left"
                            priority
                        />
                    </div>
                </Link>

                <Link href="/jobs/new">
                    <Button className="w-full bg-hoonr-gradient hover:opacity-90 transition-opacity mb-6">
                        <PlusCircle className="mr-2 h-4 w-4" /> New Job
                    </Button>
                </Link>

                <nav className="space-y-2">
                    <Link href="/" className="flex items-center px-4 py-2.5 text-sm font-medium rounded-lg bg-primary/10 text-primary">
                        <LayoutDashboard className="mr-3 h-4 w-4" />
                        Dashboard
                    </Link>
                    <Link href="#" className="flex items-center px-4 py-2.5 text-sm font-medium rounded-lg text-muted-foreground hover:bg-accent hover:text-white transition-colors">
                        <Briefcase className="mr-3 h-4 w-4" />
                        Jobs
                    </Link>
                    <Link href="#" className="flex items-center px-4 py-2.5 text-sm font-medium rounded-lg text-muted-foreground hover:bg-accent hover:text-white transition-colors">
                        <Users className="mr-3 h-4 w-4" />
                        Candidates
                    </Link>
                    <Link href="#" className="flex items-center px-4 py-2.5 text-sm font-medium rounded-lg text-muted-foreground hover:bg-accent hover:text-white transition-colors">
                        <Settings className="mr-3 h-4 w-4" />
                        Settings
                    </Link>
                </nav>
            </div>

            <div className="mt-auto p-6 border-t border-border">
                <div className="flex items-center gap-3">
                    <div className="w-10 h-10 rounded-full bg-gradient-to-tr from-blue-500 to-cyan-500" />
                    <div className="flex-1 overflow-hidden">
                        <p className="text-sm font-medium truncate">John Smith</p>
                        <p className="text-xs text-muted-foreground truncate">Recruiter Admin</p>
                    </div>
                </div>
            </div>
        </div>
    );
}
