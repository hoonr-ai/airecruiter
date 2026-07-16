"use client";

import React from "react";

export function AIPostingJobDescription({ text }: { text: string }) {
  const renderInline = (content: string) => {
    // Parse [text](url), **bold** and *italic*
    const parts = content.split(/(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))/g);
    return parts.map((part, i) => {
      if (part.startsWith('[') && part.includes('](') && part.endsWith(')')) {
        const match = part.match(/\[(.*?)\]\((.*?)\)/);
        if (match) {
          return (
            <a key={i} href={match[2]} target="_blank" rel="noopener noreferrer" className="text-primary hover:underline">
              {match[1]}
            </a>
          );
        }
      } else if (part.startsWith('**') && part.endsWith('**')) {
        return <strong key={i} className="font-semibold text-slate-900">{part.slice(2, -2)}</strong>;
      } else if (part.startsWith('*') && part.endsWith('*')) {
        return <em key={i} className="italic text-slate-800">{part.slice(1, -1)}</em>;
      }
      return <span key={i}>{part}</span>;
    });
  };

  const formatLines = (rawText: string) => {
    if (!rawText) return null;
    return rawText.split('\n').map((line, index) => {
      const trimmedLine = line.trim();
      if (!trimmedLine) return <div key={index} className="h-2" />;

      // Header check: starts with bold all caps or any section title wrapped entirely in ** or is just an all caps line
      const isHeader = /^\*\*.+?\*\*$/.test(trimmedLine) || /^[A-Z\s]{3,25}$/.test(trimmedLine);
      if (isHeader) {
        const title = trimmedLine.replace(/\*\*/g, '').replace(/^\+\+/, '').trim();
        return (
          <div key={index} className="text-[15px] font-semibold text-slate-900 mt-5 mb-2 first:mt-0 uppercase tracking-tight">
            {title}
          </div>
        );
      }

      // Bullet points
      if (trimmedLine.startsWith('•') || trimmedLine.startsWith('-')) {
        const content = trimmedLine.replace(/^[•-]\s*/, '').trim();
        return (
          <div key={index} className="flex gap-2.5 ml-1 my-1.5 items-start">
            <span className="text-slate-400 mt-1">•</span>
            <div className="flex-1">{renderInline(content)}</div>
          </div>
        );
      }

      return (
        <div key={index} className="mb-2 text-slate-600 leading-relaxed">
          {renderInline(trimmedLine)}
        </div>
      );
    });
  };

  return <div className="space-y-1">{formatLines(text)}</div>;
}
