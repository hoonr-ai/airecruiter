"use client";

import { Button } from "@/components/ui/button";
import { Sparkles } from "lucide-react";
import { useAI } from "@/context/ai-context";

export function AriaFab() {
    const { toggle, isOpen } = useAI();

    if (isOpen) return null; // Hide when open

    return (
        <Button
            onClick={toggle}
            className="fixed bottom-6 right-6 h-14 w-14 rounded-full bg-hoonr-gradient text-white shadow-2xl hover:scale-105 transition-transform flex items-center justify-center p-0 z-50 animate-in fade-in zoom-in duration-300"
        >
            <Sparkles className="h-6 w-6" />
        </Button>
    );
}
