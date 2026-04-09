"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { Search, ExternalLink, User, MapPin, Briefcase, Linkedin, ShieldCheck, Mail, ArrowLeft } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";

interface Candidate {
  id: number;
  job_id: string;
  job_title?: string;
  candidate_id: string;
  source: string;
  name: string;
  headline: string;
  location: string;
  profile_url: string;
  image_url: string;
  status: string;
  created_at: string;
}

export default function CandidatesPage() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [filteredCandidates, setFilteredCandidates] = useState<Candidate[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchCandidates();
  }, []);

  const fetchCandidates = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates`);
      const data = await response.json();
      if (data.status === "success") {
        setCandidates(data.candidates);
        setFilteredCandidates(data.candidates);
      }
    } catch (error) {
      console.error("Error fetching candidates:", error);
    } finally {
      setIsLoading(true);
      setTimeout(() => setIsLoading(false), 500); // Small delay for aesthetic
    }
  };

  const handleSearch = (query: string) => {
    setSearchQuery(query);
    const filtered = candidates.filter(c =>
      c.name.toLowerCase().includes(query.toLowerCase()) ||
      c.job_title?.toLowerCase().includes(query.toLowerCase()) ||
      c.headline.toLowerCase().includes(query.toLowerCase()) ||
      c.source.toLowerCase().includes(query.toLowerCase())
    );
    setFilteredCandidates(filtered);
  };

  const getSourceIcon = (source: string) => {
    const s = source.toLowerCase();
    if (s.includes('linkedin')) return <Linkedin className="w-3.5 h-3.5 text-[#0A66C2]" />;
    if (s.includes('jobdiva')) return <ShieldCheck className="w-3.5 h-3.5 text-[#6366f1]" />;
    return <User className="w-3.5 h-3.5 text-slate-400" />;
  };

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Header */}
      <div className="flex items-center gap-4 mt-2">
        <Button variant="ghost" size="icon" asChild className="rounded-full h-10 w-10">
          <Link href="/">
             <ArrowLeft className="h-5 w-5 text-slate-400" />
          </Link>
        </Button>
        <div>
          <h1 className="text-[28px] font-bold text-slate-900 tracking-tight">Master Candidate Pool</h1>
          <p className="text-slate-500 text-[14px]">All talent sourced across your jobs portfolio.</p>
        </div>
      </div>

      {/* Controls */}
      <div className="flex justify-between items-center gap-4 mt-4">
        <div className="relative w-[400px]">
          <Search className="absolute left-3.5 top-1/2 transform -translate-y-1/2 text-slate-400 h-[18px] w-[18px]" />
          <Input
            placeholder="Search candidates, jobs, or sources..."
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            className="pl-10 h-11 border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white rounded-xl text-[14px] shadow-sm"
          />
        </div>
        <div className="flex items-center gap-3">
          <Badge variant="outline" className="px-4 py-1.5 h-11 flex items-center gap-2 border-slate-200 bg-white text-slate-600 font-bold rounded-xl shadow-sm">
             <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
             {candidates.length} Total Sourced
          </Badge>
        </div>
      </div>

      {/* Candidates Table */}
      <div className="bg-white rounded-2xl shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] border border-slate-200 overflow-hidden mt-2">
        {isLoading ? (
          <div className="flex flex-col items-center justify-center py-40 gap-4">
            <div className="w-10 h-10 border-4 border-[#6366f1]/20 border-t-[#6366f1] rounded-full animate-spin" />
            <p className="text-slate-400 font-medium text-[14px]">Synchronizing talent pool...</p>
          </div>
        ) : filteredCandidates.length === 0 ? (
          <div className="flex flex-col items-center justify-center py-40 text-center gap-4">
            <div className="w-20 h-20 bg-slate-50 rounded-full flex items-center justify-center mb-2">
                  <User className="w-10 h-10 text-slate-200" />
            </div>
            <div>
              <h3 className="text-[18px] font-bold text-slate-900">No candidates found</h3>
              <p className="text-slate-500 text-[14px] mt-1">Try adjusting your search filters or source more talent.</p>
            </div>
            <Button asChild className="mt-2 bg-[#6366f1] hover:bg-[#4f46e5] text-white font-bold h-10 px-6 rounded-lg">
              <Link href="/jobs/new">Find Talent</Link>
            </Button>
          </div>
        ) : (
          <div className="overflow-x-auto">
            <Table>
              <TableHeader className="bg-[#fcfdfd]">
                <TableRow className="border-slate-100">
                  <TableHead className="w-[64px] pl-8"></TableHead>
                  <TableHead className="w-[200px] text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Candidate</TableHead>
                  <TableHead className="text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Applied For</TableHead>
                  <TableHead className="text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Sourcing Details</TableHead>
                  <TableHead className="text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Sourced On</TableHead>
                  <TableHead className="text-right pr-8 text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Action</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-slate-100">
                {filteredCandidates.map((candidate) => (
                  <TableRow key={candidate.id} className="hover:bg-slate-50/50 transition-colors group">
                    <TableCell className="pl-8 py-6">
                      <Avatar className="h-12 w-12 border border-slate-200 shadow-sm transition-transform group-hover:scale-105">
                        <AvatarImage src={candidate.image_url} />
                        <AvatarFallback className="bg-slate-100 text-slate-400 text-[14px] font-bold">
                          {candidate.name.split(' ').map(n => n[0]).join('')}
                        </AvatarFallback>
                      </Avatar>
                    </TableCell>
                    <TableCell className="py-6 max-w-[200px]">
                      <div className="space-y-1">
                        <p className="text-[15px] font-bold text-slate-900 leading-tight truncate">{candidate.name}</p>
                        <div className="flex items-center gap-1.5 opacity-70">
                          <Briefcase className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                          <p className="text-[13px] text-slate-600 font-medium line-clamp-1">{candidate.headline}</p>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-1">
                        <p className="text-[13.5px] font-bold text-[#4f46e5] hover:underline cursor-pointer">
                          {candidate.job_title || "Unknown Job"}
                        </p>
                        <p className="text-[11.5px] text-slate-400 font-medium">Ref: {candidate.job_id}</p>
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-2">
                        <div className="flex items-center gap-2 px-2.5 py-1 rounded-full bg-slate-100 w-fit">
                           {getSourceIcon(candidate.source)}
                           <span className="text-[11.5px] font-bold text-slate-600 uppercase tracking-tight">{candidate.source}</span>
                        </div>
                        {candidate.location && (
                           <div className="flex items-center gap-1.5 pl-1 opacity-60">
                             <MapPin className="w-3 h-3 text-slate-400" />
                             <p className="text-[12px] text-slate-500 font-medium">{candidate.location}</p>
                           </div>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="py-6 whitespace-nowrap">
                       <p className="text-[13.5px] font-medium text-slate-600">
                         {new Date(candidate.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                       </p>
                    </TableCell>
                    <TableCell className="text-right pr-8 py-6">
                       <div className="flex items-center justify-end gap-3">
                         {candidate.profile_url && (
                           <Button
                             variant="outline"
                             size="icon"
                             asChild
                             className="h-9 w-9 border-slate-200 hover:bg-slate-50 text-slate-400 hover:text-[#6366f1] rounded-xl shadow-none"
                           >
                             <a href={candidate.profile_url} target="_blank" rel="noopener noreferrer">
                               <ExternalLink className="h-4 w-4" />
                             </a>
                           </Button>
                         )}
                         <Button className="h-9 px-5 bg-white border border-[#6366f1]/30 text-[#6366f1] hover:bg-[#6366f1]/5 font-bold text-[13px] rounded-xl shadow-none">
                            Reach Out
                         </Button>
                       </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          </div>
        )}
      </div>
    </div>
  );
}