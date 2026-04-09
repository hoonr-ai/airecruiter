"use client";

import { useState, useEffect } from "react";
import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { ExternalLink, User, MapPin, Briefcase, Linkedin, ShieldCheck, Mail } from "lucide-react";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { ScrollArea } from "@/components/ui/scroll-area";

interface Candidate {
  id: number;
  job_id: string;
  candidate_id: string;
  source: string;
  name: string;
  headline: string;
  location: string;
  profile_url: string;
  image_url: string;
  status: string;
  created_at: string;
  data: any;
}

interface JobCandidatesDialogProps {
  jobId: string | null;
  jobTitle: string;
  isOpen: boolean;
  onClose: () => void;
}

export function JobCandidatesDialog({ jobId, jobTitle, isOpen, onClose }: JobCandidatesDialogProps) {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [isLoading, setIsLoading] = useState(false);

  useEffect(() => {
    if (isOpen && jobId) {
      fetchCandidates();
    }
  }, [isOpen, jobId]);

  const fetchCandidates = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/jobs/${jobId}/candidates`);
      const data = await response.json();
      if (data.status === "success") {
        setCandidates(data.candidates);
      }
    } catch (error) {
      console.error("Error fetching job candidates:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const getSourceIcon = (source: string) => {
    const s = source.toLowerCase();
    if (s.includes('linkedin')) return <Linkedin className="w-3.5 h-3.5 text-[#0A66C2]" />;
    if (s.includes('jobdiva')) return <ShieldCheck className="w-3.5 h-3.5 text-[#6366f1]" />;
    return <User className="w-3.5 h-3.5 text-slate-400" />;
  };

  return (
    <Dialog open={isOpen} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-[700px] w-full p-0 overflow-hidden border-none shadow-2xl rounded-2xl">
        <DialogHeader className="px-8 py-6 border-b border-slate-100" style={{ background: "linear-gradient(135deg, #f8f7ff 0%, #ffffff 60%)" }}>
          <div className="flex items-center justify-between">
            <div>
              <DialogTitle className="text-[20px] font-bold text-slate-900 tracking-tight">Sourced Candidates</DialogTitle>
              <p className="text-slate-500 text-[13px] mt-1 font-medium">{jobTitle} (ID: {jobId})</p>
            </div>
            {candidates.length > 0 && (
              <Badge variant="outline" className="bg-[#6366f1]/10 text-[#6366f1] border-[#6366f1]/20 font-bold px-3 py-1 text-[12px] h-fit">
                {candidates.length} Found
              </Badge>
            )}
          </div>
        </DialogHeader>

        <div className="p-0">
          <ScrollArea className="h-[500px] w-full">
            {isLoading ? (
              <div className="flex flex-col items-center justify-center p-20 gap-3">
                <div className="w-8 h-8 border-3 border-[#6366f1]/30 border-t-[#6366f1] rounded-full animate-spin" />
                <p className="text-slate-400 text-[13px] font-medium animate-pulse">Retrieving talent pool...</p>
              </div>
            ) : candidates.length === 0 ? (
              <div className="flex flex-col items-center justify-center p-20 text-center gap-4">
                <div className="w-16 h-16 bg-slate-50 rounded-full flex items-center justify-center">
                  <User className="w-8 h-8 text-slate-200" />
                </div>
                <div>
                   <p className="text-slate-900 font-bold text-[16px]">No candidates saved yet</p>
                   <p className="text-slate-500 text-[13px] mt-1 max-w-[240px]">Once you run a search in the job wizard, results will appear here automatically.</p>
                </div>
              </div>
            ) : (
              <div className="divide-y divide-slate-100">
                {candidates.map((candidate) => (
                  <div key={candidate.id} className="p-6 transition-colors hover:bg-slate-50/70 group flex items-start gap-4">
                    <Avatar className="h-12 w-12 border border-slate-200 mt-1 shrink-0 shadow-sm">
                      <AvatarImage src={candidate.image_url} />
                      <AvatarFallback className="bg-slate-100 text-slate-400 text-[14px] font-bold">
                        {candidate.name.split(' ').map(n => n[0]).join('')}
                      </AvatarFallback>
                    </Avatar>

                    <div className="flex-1 min-w-0">
                      <div className="flex items-start justify-between gap-4">
                        <div className="min-w-0">
                          <p className="text-[16px] font-bold text-slate-900 leading-none mb-2">{candidate.name}</p>
                          <div className="flex items-start gap-1.5 pt-0.5">
                            <Briefcase className="w-3.5 h-3.5 text-slate-400 mt-1 shrink-0" />
                            <p className="text-[13px] text-slate-600 font-medium leading-relaxed break-words">{candidate.headline}</p>
                          </div>
                          {candidate.location && (
                             <div className="flex items-center gap-1.5 mt-1.5 opacity-60">
                               <MapPin className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                               <p className="text-[12px] text-slate-500 font-medium">{candidate.location}</p>
                             </div>
                          )}
                        </div>

                        <div className="flex flex-col items-end gap-3 shrink-0">
                          <div className="flex items-center gap-2 px-3 py-1 rounded-full bg-slate-100 border border-slate-200/50">
                            {getSourceIcon(candidate.source)}
                            <span className="text-[11px] font-bold text-slate-600 uppercase tracking-tight">{candidate.source}</span>
                          </div>
                          <div className="flex items-center gap-2">
                             {candidate.profile_url && (
                                <Button
                                  variant="ghost"
                                  size="icon"
                                  asChild
                                  className="h-9 w-9 border border-slate-200 rounded-xl hover:bg-[#6366f1]/10 hover:text-[#6366f1] transition-all"
                                >
                                  <a href={candidate.profile_url} target="_blank" rel="noopener noreferrer">
                                    <ExternalLink className="h-4 w-4" />
                                  </a>
                                </Button>
                             )}
                             <Button 
                                variant="outline" 
                                size="sm"
                                className="h-9 px-4 text-[12px] font-bold border-slate-200 hover:bg-slate-50 transition-all rounded-xl shadow-none"
                             >
                                Reach Out
                             </Button>
                          </div>
                        </div>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </ScrollArea>
        </div>
      </DialogContent>
    </Dialog>
  );
}
