"use client";

import React, { createContext, useContext, useState, ReactNode } from 'react';

interface Message {
    role: 'user' | 'assistant';
    content: string;
}

interface AIContextType {
    isOpen: boolean;
    setIsOpen: (open: boolean) => void;
    toggle: () => void;
    messages: Message[];
    addMessage: (role: 'user' | 'assistant', content: string) => void;
    sendMessage: (content: string) => Promise<void>;
    isLoading: boolean;
}

const AIContext = createContext<AIContextType | undefined>(undefined);

export function AIProvider({ children }: { children: ReactNode }) {
    const [isOpen, setIsOpen] = useState(false);
    const [messages, setMessages] = useState<Message[]>([
        { role: 'assistant', content: "Hi, I'm Aria! How can I help you today?" }
    ]);
    const [isLoading, setIsLoading] = useState(false);

    const toggle = () => setIsOpen(prev => !prev);

    const addMessage = (role: 'user' | 'assistant', content: string) => {
        setMessages(prev => [...prev, { role, content }]);
    };

    const sendMessage = async (content: string) => {
        setIsLoading(true);
        addMessage('user', content);

        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/chat`, {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ message: content, history: messages })
            });
            const data = await res.json();
            addMessage('assistant', data.response);
        } catch (e) {
            console.error(e);
            addMessage('assistant', "Sorry, I'm having trouble connecting right now.");
        }
        setIsLoading(false);
    };

    return (
        <AIContext.Provider value={{ isOpen, setIsOpen, toggle, messages, addMessage, sendMessage, isLoading }}>
            {children}
        </AIContext.Provider>
    );
}

export function useAI() {
    const context = useContext(AIContext);
    if (context === undefined) {
        throw new Error('useAI must be used within an AIProvider');
    }
    return context;
}
