"use client";

import { Button } from "@/components/ui/button";
import { ExternalLink, Linkedin } from "lucide-react";
import { 
  Dialog, 
  DialogContent, 
  DialogHeader, 
  DialogTitle,
  DialogDescription
} from "@/components/ui/dialog";

import { memo } from "react";

interface CandidateDetailsModalProps {
  isOpen: boolean;
  onClose: () => void;
  candidateName: string;
  profileUrl?: string;
  imageUrl?: string;
  details: string;
  tags: string[];
  matchScore?: number;
  missingSkills?: string[];
  explainability?: string[];
}

function CandidateDetailsModalBase({ isOpen, onClose, candidateName, profileUrl, imageUrl, details, tags, matchScore, missingSkills, explainability }: CandidateDetailsModalProps) {
  return (
    <Dialog open={isOpen} onOpenChange={onClose}>
      <DialogContent 
        className="max-w-md bg-white rounded-3xl shadow-2xl border-none animate-in fade-in zoom-in-95 duration-200 ease-out p-0 overflow-hidden"
        onOpenAutoFocus={(e) => {
          (e.currentTarget as HTMLElement)?.focus();
        }}
      >
        <div className="sr-only">
          <DialogTitle>{candidateName} Details</DialogTitle>
          <DialogDescription>
            Detailed view for candidate {candidateName} including summary and skills.
          </DialogDescription>
        </div>

        <div className="relative">
          {/* Header Section with Image and Name */}
          <div className="bg-gradient-to-br from-[#f8faff] to-[#f1f5f9] px-7 py-9 flex items-center gap-6">
            <div className="shrink-0 relative group">
              {imageUrl ? (
                <div className="relative">
                  <img 
                    src={imageUrl} 
                    alt={candidateName} 
                    className="w-24 h-24 rounded-[28px] object-cover border-4 border-white shadow-xl transition-transform duration-300 group-hover:scale-105"
                  />
                  <div className="absolute -bottom-1 -right-1 w-8 h-8 bg-white rounded-full flex items-center justify-center shadow-lg border-2 border-slate-50">
                    <Linkedin className="w-4 h-4 text-[#0077b5] fill-current" />
                  </div>
                </div>
              ) : (
                <div className="w-24 h-24 rounded-[28px] bg-gradient-to-br from-[#6366f1] to-[#4f46e5] text-white flex items-center justify-center text-3xl font-black shadow-xl border-4 border-white">
                  {candidateName.split(' ').map(n => n[0]).join('')}
                </div>
              )}
            </div>
            
            <div className="min-w-0">
              <h2 className="text-2xl font-black text-slate-900 tracking-tight leading-tight mb-2.5">
                {candidateName}
              </h2>
              
              {matchScore !== undefined && (
                <div className="flex items-center">
                  <div className={`px-3 py-1.5 rounded-xl border flex items-center gap-2 shadow-sm ${
                    matchScore >= 80 ? 'bg-emerald-50 border-emerald-200 text-emerald-700' :
                    matchScore >= 60 ? 'bg-amber-50 border-amber-200 text-amber-700' :
                    'bg-rose-50 border-rose-200 text-rose-700'
                  }`}>
                    <span className="text-[16px] font-black tracking-tight">{matchScore}%</span>
                    <span className="text-[10px] font-bold uppercase tracking-wider opacity-80 mt-0.5">Match</span>
                  </div>
                </div>
              )}
            </div>
          </div>

          {/* Details Content */}
          <div className="p-7 space-y-6 bg-white max-h-[60vh] overflow-y-auto">
            <div className="bg-slate-50/50 p-6 rounded-3xl border border-slate-100 shadow-inner">
              <h3 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-4">Professional Overview</h3>
              <p className="text-[16px] text-slate-700 font-bold leading-relaxed">
                {details}
              </p>
            </div>
            
            {explainability && explainability.length > 0 && (
              <div className="bg-slate-50/50 p-6 rounded-3xl border border-slate-100 shadow-inner">
                <h3 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em] mb-4">Match Analysis</h3>
                <ul className="space-y-3">
                  {explainability.map((exp, i) => (
                    <li key={i} className="flex items-start gap-3 text-[14px] text-slate-700 font-bold leading-relaxed">
                      <div className="w-1.5 h-1.5 rounded-full bg-[#6366f1] mt-2 shrink-0" />
                      {exp}
                    </li>
                  ))}
                </ul>
                
                {missingSkills && missingSkills.length > 0 && (
                  <div className="mt-5 pt-5 border-t border-slate-200/60">
                    <h4 className="text-[11px] font-bold text-slate-500 mb-3 uppercase tracking-wider">Missing Requirements</h4>
                    <div className="flex flex-wrap gap-2">
                      {missingSkills.map((skill, i) => (
                        <span key={i} className="bg-rose-50 border border-rose-100 text-rose-600 rounded-lg px-2.5 py-1 text-[12px] font-bold shadow-sm">
                          {skill}
                        </span>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}

            <div>
              <div className="flex items-center justify-between mb-5">
                <h3 className="text-[10px] font-black text-slate-400 uppercase tracking-[0.2em]">Skills</h3>
              </div>
              <div className="flex flex-wrap gap-2.5">
                {tags.map((tag, i) => (
                  <span key={i} className="bg-white text-slate-700 border border-slate-200 rounded-xl px-4 py-2 text-[13px] font-bold shadow-sm hover:border-[#6366f1]/30 hover:text-[#6366f1] transition-colors whitespace-nowrap">
                    {tag}
                  </span>
                ))}
              </div>
            </div>
          </div>
          
          {/* Footer Modal Buttons */}
          {profileUrl && (
            <div className="px-7 py-5 bg-slate-50 border-t border-slate-100 flex justify-end rounded-b-3xl">
              <a 
                href={profileUrl} 
                target="_blank" 
                rel="noopener noreferrer" 
                className="inline-flex items-center justify-center gap-2 text-white text-[14px] font-bold px-6 py-2.5 bg-[#6366f1] rounded-xl shadow-md hover:bg-[#4f46e5] transition-all w-full"
              >
                View External Profile
                <ExternalLink className="w-4 h-4" />
              </a>
            </div>
          )}

        </div>
      </DialogContent>
    </Dialog>
  );
}

export const CandidateDetailsModal = memo(CandidateDetailsModalBase);
