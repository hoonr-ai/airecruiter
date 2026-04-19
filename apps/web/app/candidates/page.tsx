"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { Search, ExternalLink, User, MapPin, Briefcase, Linkedin, ShieldCheck, Mail, ArrowLeft, Eye, Zap, Filter, ChevronDown } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { CandidateMessageModal } from "@/components/candidate-message-modal";
import { ResumeModal } from "@/components/ResumeModal";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";
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
  jobdiva_id: string;
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
  resume_text?: string;
  data?: any;
}

export default function CandidatesPage() {
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [filteredCandidates, setFilteredCandidates] = useState<Candidate[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);
  const [messageModalOpen, setMessageModalOpen] = useState(false);
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<any>(null);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [selectedCandidateForResume, setSelectedCandidateForResume] = useState<any>(null);
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidateForDetails, setSelectedCandidateForDetails] = useState<any>(null);

  useEffect(() => {
    fetchCandidates();
  }, []);

  const fetchCandidates = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates`);
      const data = await response.json();
      console.log("📊 Fetched candidates data:", data);
      if (data.status === "success") {
        console.log(`✅ Found ${data.candidates.length} candidates in master pool`);
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

  const handleEmailCandidate = (candidate: any) => {
    setSelectedCandidateForEmail(candidate);
    setMessageModalOpen(true);
  };

  const fetchCandidateResume = async (candidateId: string) => {
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates/${candidateId}/resume`);
      const data = await response.json();
      return data.resume_text || "Resume content is not available for this candidate.";
    } catch (error) {
      console.error("Error fetching resume:", error);
      return "Resume content is not available for this candidate.";
    }
  };

  const handleViewResume = async (candidate: Candidate) => {
    let resumeText = candidate.resume_text || candidate.data?.resume_text;
    
    // If no resume text available, try to fetch it
    if (!resumeText || resumeText.trim() === "") {
      console.log(`📄 Fetching resume for candidate: ${candidate.name}`);
      resumeText = await fetchCandidateResume(candidate.candidate_id);
    }
    
    setSelectedCandidateForResume({
      name: candidate.name,
      resumeText: resumeText || "Resume content is not available for this candidate."
    });
    setResumeModalOpen(true);
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
          <Button variant="outline" className="h-11 px-4 border-slate-200 bg-white text-slate-600 font-bold rounded-xl shadow-sm hover:bg-slate-50 flex items-center gap-2">
            <Filter className="w-4 h-4 text-slate-400" />
            Filters
            <ChevronDown className="w-3.5 h-3.5 text-slate-400 ml-1" />
          </Button>
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
                  <TableHead className="pl-10 w-[80px] h-14"></TableHead>
                  <TableHead className="w-[240px] text-left text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Candidate Name</TableHead>
                  <TableHead className="text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Match</TableHead>
                  <TableHead className="text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Applied For</TableHead>
                  <TableHead className="text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Location</TableHead>
                  <TableHead className="text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Sourcing Details</TableHead>
                  <TableHead className="text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Sourced On</TableHead>
                  <TableHead className="text-center pr-10 text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-slate-100">
                {filteredCandidates.map((candidate) => (
                  <TableRow key={candidate.id} className="hover:bg-slate-50/50 transition-colors group">
                    <TableCell className="pl-10 py-6">
                      <Avatar className="h-12 w-12 border border-slate-200 shadow-sm transition-transform group-hover:scale-105">
                        <AvatarImage src={candidate.image_url} />
                        <AvatarFallback className="bg-slate-100 text-slate-400 text-[14px] font-bold">
                          {candidate.name.split(' ').map(n => n[0]).join('')}
                        </AvatarFallback>
                      </Avatar>
                    </TableCell>
                    <TableCell className="py-6 w-[240px] max-w-[240px]">
                      <div className="space-y-1">
                        <a
                          href={candidate.source === 'LinkedIn' ? candidate.profile_url || '#' : '#'}
                          target={candidate.source === 'LinkedIn' ? "_blank" : undefined}
                          rel={candidate.source === 'LinkedIn' ? "noopener noreferrer" : undefined}
                          className="group/name flex items-center gap-3"
                          onClick={(e) => {
                            if (candidate.source !== 'LinkedIn') {
                              e.preventDefault();
                              handleViewResume(candidate);
                            }
                          }}
                        >
                           <span className="flex items-center gap-2 max-w-full">
                             <span className={`text-[15px] font-bold text-slate-900 transition-colors whitespace-normal break-words ${
                               candidate.source === 'LinkedIn' ? 'group-hover/name:text-[#1d4ed8]' : 
                               candidate.source === 'JobDiva-TalentSearch' ? 'group-hover/name:text-[#c2410c]' : 
                               'group-hover/name:text-[#6366f1]'
                             }`}>
                               {candidate.name}
                             </span>
                             <span 
                               className={`shrink-0 h-6 w-6 flex items-center justify-center border border-slate-200 bg-white text-slate-400 rounded-lg shadow-sm transition-all ${
                                 candidate.source === 'LinkedIn' 
                                   ? 'group-hover/name:border-[#bfdbfe] group-hover/name:bg-[#eff6ff] group-hover/name:text-[#1d4ed8]' : 
                                 candidate.source === 'JobDiva-TalentSearch' 
                                   ? 'group-hover/name:border-[#fed7aa] group-hover/name:bg-[#fff7ed] group-hover/name:text-[#c2410c]' : 
                                 'group-hover/name:border-[#c7d2fe] group-hover/name:bg-[#f5f3ff] group-hover/name:text-[#6366f1]'
                               }`}
                               title={candidate.source === 'LinkedIn' ? "View LinkedIn Profile" : "Click to view resume"}
                             >
                               <ExternalLink className="w-3 h-3" />
                             </span>
                           </span>
                        </a>
                        <div className="flex items-center gap-1.5 opacity-70 mt-1" title={candidate.headline || ""}>
                          <Briefcase className="w-3.5 h-3.5 text-slate-500 shrink-0" />
                          <p className="text-[13px] text-slate-600 font-medium truncate">{candidate.headline}</p>
                        </div>
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="flex items-center justify-center">
                        {(candidate as any).match_score || candidate.data?.match_score ? (
                          <span className={`px-2.5 py-1 rounded-full text-[12px] font-bold shadow-sm ${
                            ((candidate as any).match_score || candidate.data?.match_score) >= 80 ? 'bg-emerald-100 text-emerald-700 border border-emerald-200' : 
                            ((candidate as any).match_score || candidate.data?.match_score) >= 60 ? 'bg-amber-100 text-amber-700 border border-amber-200' : 
                            'bg-rose-100 text-rose-700 border border-rose-200'
                          }`}>
                            {((candidate as any).match_score || candidate.data?.match_score)}% Match
                          </span>
                        ) : (
                          <span className="text-[12px] text-slate-400 font-medium px-2 py-1 bg-slate-50 rounded-md border border-slate-100">N/A</span>
                        )}
                      </div>
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-1 text-center">
                        <p className="text-[14px] font-medium text-slate-900">
                          {candidate.job_title || "Unknown Job"}
                        </p>
                        <p className="text-[11.5px] text-slate-400 font-medium">Ref: {candidate.jobdiva_id}</p>
                      </div>
                    </TableCell>
                    <TableCell className="py-6 min-w-[140px]">
                      {candidate.location ? (
                         <div className="flex items-center justify-center gap-1.5 opacity-80">
                           <MapPin className="w-3.5 h-3.5 text-slate-400" />
                           <p className="text-[12px] text-slate-600 font-medium whitespace-nowrap leading-tight">{candidate.location}</p>
                         </div>
                      ) : (
                         <div className="flex justify-center"><span className="text-[12px] text-slate-400 font-medium">N/A</span></div>
                      )}
                    </TableCell>
                    <TableCell className="py-6">
                      <div className="space-y-2 flex justify-center">
                        <span className={`px-2.5 w-fit py-0.5 rounded-lg text-[10.5px] font-extrabold uppercase tracking-wider flex items-center gap-1.5 shadow-sm h-fit border ${candidate.source === 'LinkedIn'
                            ? 'bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]'
                            : candidate.source === 'JobDiva-TalentSearch'
                              ? 'bg-[#fff7ed] text-[#c2410c] border-[#fed7aa]'
                              : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]'
                          }`}>
                          {candidate.source === 'LinkedIn' ? <Linkedin className="w-3 h-3 fill-current" /> : candidate.source === 'JobDiva-TalentSearch' ? <Zap className="w-3 h-3 fill-current" /> : <ShieldCheck className="w-3 h-3" />}
                          {candidate.source || "JobDiva"}
                        </span>
                      </div>
                    </TableCell>
                    <TableCell className="py-6 whitespace-nowrap text-center">
                       <p className="text-[13.5px] font-medium text-slate-600">
                         {new Date(candidate.created_at).toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' })}
                       </p>
                    </TableCell>
                    <TableCell className="py-6 pr-10">
                       <div className="flex items-center justify-center gap-2 shrink-0">
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                           onClick={() => handleEmailCandidate(candidate)}
                         >
                           <Mail className="w-3.5 h-3.5" />
                           {/* Add explicit span for text alignment if flex struggles */}
                           <span>Email</span>
                         </Button>
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                         >
                           Engage
                         </Button>
                         <Button
                           size="sm"
                           className="h-8 px-3.5 bg-white border border-[#6366f1]/20 text-[#6366f1] hover:bg-[#6366f1] hover:text-white font-bold text-[12px] rounded-lg shadow-sm transition-all flex items-center justify-center gap-2 min-w-[70px]"
                         >
                           Assess
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
      
      {/* Email Modal */}
      {selectedCandidateForEmail && (
        <CandidateMessageModal
          candidateName={selectedCandidateForEmail.name || `${selectedCandidateForEmail.firstName} ${selectedCandidateForEmail.lastName}`}
          candidateEmail={selectedCandidateForEmail.email || selectedCandidateForEmail.data?.email || "Email not available"}
          isOpen={messageModalOpen}
          onClose={() => {
            setMessageModalOpen(false);
            setSelectedCandidateForEmail(null);
          }}
        />
      )}

      {/* Resume Modal */}
      {selectedCandidateForResume && (
        <ResumeModal
          candidateName={selectedCandidateForResume.name}
          resumeText={selectedCandidateForResume.resumeText}
          isOpen={resumeModalOpen}
          onClose={() => {
            setResumeModalOpen(false);
            setSelectedCandidateForResume(null);
          }}
        />
      )}

      {/* Details Modal */}
      {selectedCandidateForDetails && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          onClose={() => {
            setDetailsModalOpen(false);
            setSelectedCandidateForDetails(null);
          }}
          candidateName={selectedCandidateForDetails.name}
          profileUrl={selectedCandidateForDetails.profileUrl}
          imageUrl={selectedCandidateForDetails.imageUrl}
          jobTitle={selectedCandidateForDetails.jobTitle}
          location={selectedCandidateForDetails.location}
          experienceYears={selectedCandidateForDetails.experienceYears}
          tags={selectedCandidateForDetails.tags}
          matchScore={selectedCandidateForDetails.matchScore}
          missingSkills={selectedCandidateForDetails.missingSkills}
          explainability={selectedCandidateForDetails.explainability}
          matchScoreDetails={selectedCandidateForDetails.matchScoreDetails}
          matchedSkills={selectedCandidateForDetails.matchedSkills}
        />
      )}
    </div>
  );
}