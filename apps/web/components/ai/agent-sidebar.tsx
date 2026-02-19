"use client";

import { useState } from "react";
import { MessageSquare, Send, X, Bot, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";

export function AIAgentSidebar() {
    const [isOpen, setIsOpen] = useState(true);
    const [messages, setMessages] = useState([
        { role: "agent", content: "Hello! I'm Aria, your recruiting assistant. I can help you parse JDs, screen candidates, or draft emails. How can I help today?" }
    ]);
    const [input, setInput] = useState("");

    const handleSend = () => {
        if (!input.trim()) return;
        setMessages([...messages, { role: "user", content: input }]);

        // Mock response
        setTimeout(() => {
            setMessages(prev => [...prev, { role: "agent", content: "I'm analyzing that request for you. (Mock: Connected into Skills Engine)" }]);
        }, 1000);

        setInput("");
    };

    if (!isOpen) {
        return (
            <Button
                className="fixed bottom-6 right-6 h-14 w-14 rounded-full bg-hoonr-gradient shadow-lg z-50 flex items-center justify-center hover:scale-105 transition-transform"
                onClick={() => setIsOpen(true)}
            >
                <Bot className="h-6 w-6 text-white" />
            </Button>
        );
    }

    return (
        <div className="fixed bottom-6 right-6 w-96 h-[600px] z-50 flex flex-col shadow-2xl rounded-xl overflow-hidden border border-border bg-card/95 backdrop-blur-sm">
            <div className="bg-hoonr-gradient p-4 flex items-center justify-between">
                <div className="flex items-center gap-2 text-white">
                    <Bot className="h-5 w-5" />
                    <span className="font-semibold">Aria (AI Agent)</span>
                </div>
                <Button variant="ghost" size="icon" className="text-white hover:bg-white/20" onClick={() => setIsOpen(false)}>
                    <X className="h-4 w-4" />
                </Button>
            </div>

            <div className="flex-1 overflow-y-auto p-4 space-y-4">
                {messages.map((msg, i) => (
                    <div key={i} className={`flex ${msg.role === "user" ? "justify-end" : "justify-start"}`}>
                        <div className={`max-w-[85%] rounded-lg p-3 text-sm ${msg.role === "user"
                                ? "bg-primary text-white"
                                : "bg-muted text-foreground"
                            }`}>
                            {msg.content}
                        </div>
                    </div>
                ))}
            </div>

            <div className="p-4 border-t border-border bg-card">
                <div className="flex gap-2">
                    <Input
                        placeholder="Ask Aria..."
                        value={input}
                        onChange={(e) => setInput(e.target.value)}
                        onKeyDown={(e) => e.key === "Enter" && handleSend()}
                        className="flex-1 bg-background"
                    />
                    <Button size="icon" onClick={handleSend} className="bg-hoonr-gradient">
                        <Send className="h-4 w-4" />
                    </Button>
                </div>
            </div>
        </div>
    );
}
