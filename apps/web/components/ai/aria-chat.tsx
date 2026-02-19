"use client";

import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription, SheetFooter } from "@/components/ui/sheet";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Send, Sparkles, X } from "lucide-react";
import { useAI } from "@/context/ai-context";
import { useState } from "react";
import { cn } from "@/lib/utils";

export function AriaChat() {
    const { isOpen, setIsOpen, messages, sendMessage, isLoading } = useAI();
    const [input, setInput] = useState("");

    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!input.trim() || isLoading) return;
        sendMessage(input);
        setInput("");
    };

    return (
        <Sheet open={isOpen} onOpenChange={setIsOpen}>
            <SheetContent className="w-[400px] sm:w-[540px] flex flex-col p-0 gap-0 border-l border-border/50 shadow-2xl bg-background/80 backdrop-blur-xl">
                {/* Header */}
                {/* Header */}
                <SheetHeader className="p-4 border-b flex flex-row items-center justify-between bg-primary/5 space-y-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-md">
                            <Sparkles className="text-white h-5 w-5" />
                        </div>
                        <div>
                            <SheetTitle className="font-semibold text-lg leading-none">Aria</SheetTitle>
                            <SheetDescription className="text-xs text-muted-foreground mt-1">AI Recruiting Companion</SheetDescription>
                        </div>
                    </div>
                </SheetHeader>

                {/* Messages */}
                <ScrollArea className="flex-1 p-4">
                    <div className="space-y-4">
                        {messages.map((m, i) => (
                            <div key={i} className={cn("flex gap-3", m.role === 'user' ? "flex-row-reverse" : "flex-row")}>
                                <Avatar className="w-8 h-8 border">
                                    {m.role === 'assistant' ? (
                                        <AvatarFallback className="bg-primary/10 text-primary text-xs">AI</AvatarFallback>
                                    ) : (
                                        <AvatarFallback className="bg-muted text-xs">ME</AvatarFallback>
                                    )}
                                </Avatar>
                                <div className={cn(
                                    "p-3 rounded-2xl text-sm max-w-[80%]",
                                    m.role === 'user'
                                        ? "bg-primary text-primary-foreground rounded-br-none"
                                        : "bg-muted text-foreground rounded-bl-none border border-border/50"
                                )}>
                                    {m.content}
                                </div>
                            </div>
                        ))}
                        {isLoading && (
                            <div className="flex gap-3">
                                <Avatar className="w-8 h-8 border"><AvatarFallback className="bg-primary text-primary-foreground text-xs">AI</AvatarFallback></Avatar>
                                <div className="bg-muted p-3 rounded-2xl rounded-bl-none text-sm border border-border/50 flex items-center gap-1">
                                    <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: '0ms' }} />
                                    <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: '150ms' }} />
                                    <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: '300ms' }} />
                                </div>
                            </div>
                        )}
                    </div>
                </ScrollArea>

                {/* Input */}
                <div className="p-4 border-t bg-background">
                    <form onSubmit={handleSubmit} className="flex gap-2">
                        <Input
                            value={input}
                            onChange={e => setInput(e.target.value)}
                            placeholder="Ask Aria anything..."
                            className="flex-1 bg-muted/50 border-0 focus-visible:ring-1 focus-visible:ring-primary/20"
                        />
                        <Button type="submit" size="icon" disabled={isLoading || !input.trim()} className="bg-hoonr-gradient text-white shadow-md hover:opacity-90 transition-opacity">
                            <Send className="h-4 w-4" />
                        </Button>
                    </form>
                </div>
            </SheetContent>
        </Sheet>
    );
}
