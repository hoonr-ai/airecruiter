"use client";

import { useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";

interface ResumeModalProps {
  isOpen: boolean;
  onClose: () => void;
  candidateName: string;
  resumeText: string;
}

export function ResumeModal({ isOpen, onClose, candidateName, resumeText }: ResumeModalProps) {
  const formatResumeText = (text: string) => {
    // Check for empty, null, or placeholder text
    if (!text || 
        text.trim() === "" || 
        text === "Resume content unavailable." ||
        text === "null" ||
        text.toLowerCase().includes("resume not available") ||
        text.toLowerCase().includes("content unavailable")) {
      return (
        <div className="text-slate-500 italic text-center py-8">
          <p className="mb-4">Resume content is not available for this candidate.</p>
          <p className="text-sm">This may occur if:</p>
          <ul className="text-sm mt-2 space-y-1">
            <li>• The candidate hasn't uploaded a resume</li>
            <li>• Resume access is restricted</li>
            <li>• Data sync is still in progress</li>
          </ul>
        </div>
      );
    }

    // Split text into sections and format
    const lines = text.split('\n');
    const formattedContent = [];
    
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i].trim();
      if (!line) continue;

      // Check if line is a section header (all caps or contains common section words)
      const isHeader = /^[A-Z\s]{3,}$/.test(line) || 
                      /^(SUMMARY|EXPERIENCE|EDUCATION|SKILLS|OBJECTIVE|QUALIFICATION|WORK|EMPLOYMENT|PROJECTS|CERTIFICATIONS|ACHIEVEMENTS)/i.test(line);

      if (isHeader) {
        formattedContent.push(
          <h3 key={i} className="text-lg font-bold text-slate-900 mt-6 mb-3 border-b border-slate-200 pb-2">
            {line}
          </h3>
        );
      } else if (line.includes('@') && line.includes('.')) {
        // Email detection
        formattedContent.push(
          <p key={i} className="text-sm text-blue-600 mb-2">
            {line}
          </p>
        );
      } else if (/^\(\d{3}\)|\d{3}-\d{3}-\d{4}/.test(line)) {
        // Phone number detection
        formattedContent.push(
          <p key={i} className="text-sm text-slate-600 mb-2 font-medium">
            {line}
          </p>
        );
      } else if (line.startsWith('•') || line.startsWith('-') || line.startsWith('*')) {
        // Bullet points
        formattedContent.push(
          <p key={i} className="text-sm text-slate-700 mb-1 ml-4">
            {line}
          </p>
        );
      } else {
        // Regular content
        formattedContent.push(
          <p key={i} className="text-sm text-slate-700 mb-2 leading-relaxed">
            {line}
          </p>
        );
      }
    }

    return <div className="space-y-1">{formattedContent}</div>;
  };

  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-hidden">
        <DialogHeader className="border-b border-slate-200 pb-4 mb-4">
          <DialogTitle className="text-xl font-bold text-slate-900">
            Resume: {candidateName}
          </DialogTitle>
        </DialogHeader>
        
        <div className="overflow-y-auto max-h-[70vh] pr-2">
          <div className="bg-white rounded-lg p-6 border border-slate-200">
            {formatResumeText(resumeText)}
          </div>
        </div>

        <div className="border-t border-slate-200 pt-4 mt-4">
          <div className="flex justify-end">
            <Button onClick={onClose} variant="outline">
              Close
            </Button>
          </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}