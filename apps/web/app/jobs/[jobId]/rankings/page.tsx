"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import {
  ArrowLeft,
  Search,
  RefreshCw,
  Linkedin,
  Mail,
  Phone,
  MapPin,
  Clock,
  ExternalLink,
  Medal,
  ChevronDown,
  ChevronsUpDown,
  Filter,
  Calendar,
  Check,
  X,
  Lightbulb,
  LinkIcon
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from "@/components/ui/table";
import { Skeleton } from "@/components/ui/skeleton";
import { CandidateDetailsModal } from "@/components/CandidateDetailsModal";

interface JobDetails {
  job_id: string;
  title: string;
  customer_name?: string;
  openings?: number;
  max_allowed_submittals?: number;
}

interface Candidate {
  id: number;
  name: string;
  email: string;
  phone?: string;
  location?: string;
  headline?: string;
  image_url?: string;
  profile_url?: string;
  source: string;
  match_score: number;
  engage_score?: number;
  engage_status?: string;
  engage_completed_at?: string;
  availability?: string;
  created_at: string;
  data?: any;
}

export default function CandidateRankingsPage() {
  const { jobId } = useParams();
  const router = useRouter();

  const [job, setJob] = useState<JobDetails | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [filteredCandidates, setFilteredCandidates] = useState<Candidate[]>([]);
  const [searchQuery, setSearchQuery] = useState("");
  const [isLoading, setIsLoading] = useState(true);

  // Modal states
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidate, setSelectedCandidate] = useState<Candidate | null>(null);

  // Integration Action states
  const [feedbacks, setFeedbacks] = useState<Record<number, string>>({});
  const [integrationModalOpen, setIntegrationModalOpen] = useState<'submit' | 'reject' | null>(null);
  const [actionCandidateId, setActionCandidateId] = useState<number | null>(null);
  const [rejectReason, setRejectReason] = useState("");

  const handleConfirmSubmit = () => {
    if (actionCandidateId) {
      setFeedbacks(prev => ({ ...prev, [actionCandidateId]: 'Submit' }));
      setIntegrationModalOpen(null);
      setActionCandidateId(null);
    }
  };

  const handleConfirmReject = () => {
    if (actionCandidateId && rejectReason) {
      setFeedbacks(prev => ({ ...prev, [actionCandidateId]: `Reject: ${rejectReason}` }));
      setIntegrationModalOpen(null);
      setActionCandidateId(null);
    }
  };

  useEffect(() => {
    if (jobId) {
      fetchData();
    }
  }, [jobId]);

  const fetchData = async () => {
    setIsLoading(true);
    try {
      const apiBase = process.env.NEXT_PUBLIC_API_URL;

      // Fetch job details
      const jobRes = await fetch(`${apiBase}/jobs/${jobId}/monitored-data`);
      const jobData = await jobRes.json();
      
      // Handle both { data: { ... } } and flat { ... } structures
      const data = jobData.data || jobData;
      
      if (data) {
        setJob({
          job_id: jobId as string,
          jobdiva_id: data.jobdiva_id,
          title: data.title || data.enhanced_title || `Job ${jobId}`,
          customer_name: data.customer_name,
          openings: data.openings,
          max_allowed_submittals: data.max_allowed_submittals
        });
      }

      // Fetch candidates
      const candRes = await fetch(`${apiBase}/jobs/${jobId}/candidates`);
      const candData = await candRes.json();
      if (candData.status === "success" && candData.candidates) {
        // Sort by total fit score (match_score + engage_score) descending
        const sorted = candData.candidates.sort((a: any, b: any) => {
          const totalA = (a.match_score || 0) + (a.engage_score || 0);
          const totalB = (b.match_score || 0) + (b.engage_score || 0);
          return totalB - totalA;
        });
        setCandidates(sorted);
        setFilteredCandidates(sorted);

        // EXTRA FALLBACK: If job title is still Unknown, borrow from candidates
        setJob(prev => {
          if (!prev || prev.title === `Job ${jobId}`) {
            const firstCand = sorted[0];
            const recoveredTitle = firstCand?.headline || firstCand?.job_title || `Job ${jobId}`;
            return {
              ...(prev || {}),
              job_id: jobId as string,
              title: recoveredTitle,
            };
          }
          return prev;
        });
      }
    } catch (error) {
      console.error("Error fetching ranking data:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSearch = (query: string) => {
    setSearchQuery(query);
    const filtered = candidates.filter(c =>
      c.name.toLowerCase().includes(query.toLowerCase()) ||
      c.email?.toLowerCase().includes(query.toLowerCase()) ||
      c.headline?.toLowerCase().includes(query.toLowerCase())
    );
    setFilteredCandidates(filtered);
  };

  const openDetails = (candidate: Candidate) => {
    setSelectedCandidate(candidate);
    setDetailsModalOpen(true);
  };

  const getStatusBadge = (statusOrScore?: string | number) => {
    if (statusOrScore === undefined || statusOrScore === null || statusOrScore === "") {
      return <Badge variant="outline" className="bg-slate-50 text-slate-500 border border-slate-200 shadow-sm px-3 py-0.5 font-semibold italic">Pending</Badge>;
    }
    
    // If it's a score (number), use 70 as threshold
    if (typeof statusOrScore === 'number') {
      if (statusOrScore >= 70) {
        return <Badge className="bg-emerald-50 text-emerald-600 hover:bg-emerald-100 border border-emerald-200 shadow-sm px-3 py-0.5 font-bold tracking-wide">Pass</Badge>;
      }
      return <Badge variant="destructive" className="bg-rose-50 text-rose-600 hover:bg-rose-100 border border-rose-200 shadow-sm px-3 py-0.5 font-bold tracking-wide">Fail</Badge>;
    }

    const s = statusOrScore.toLowerCase();
    if (s.includes("pass") || s.includes("completed") || s.includes("sourced")) {
      return <Badge className="bg-emerald-50 text-emerald-600 hover:bg-emerald-100 border border-emerald-200 shadow-sm px-3 py-0.5 font-bold tracking-wide">Pass</Badge>;
    }
    if (s.includes("fail") || s.includes("reject")) {
      return <Badge variant="destructive" className="bg-rose-50 text-rose-600 hover:bg-rose-100 border border-rose-200 shadow-sm px-3 py-0.5 font-bold tracking-wide">Fail</Badge>;
    }
    return <Badge variant="outline" className="bg-slate-50 text-slate-600 border border-slate-200 shadow-sm px-3 py-0.5 font-semibold">Pending</Badge>;
  };

  const isInitialLoading = isLoading && !job && candidates.length === 0;
  const isRefreshing = isLoading && !isInitialLoading;

  return (
    <div className="max-w-[1400px] mx-auto space-y-6 pb-20">
      {/* Top Navigation */}
      <div className="pt-2 mb-4">
        <Button
          variant="ghost"
          onClick={() => router.back()}
          className="text-slate-400 hover:text-slate-600 p-0 h-auto font-medium flex items-center gap-1.5 text-[14px]"
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          Back to Jobs Page
        </Button>
      </div>

      {/* Rankings Page Header matching the exact HTML vibe */}
      <div className="bg-white rounded-[16px] border border-slate-200 p-6 flex flex-row items-center justify-between shadow-[0_2px_10px_rgba(0,0,0,0.02)]">
        <div className="flex flex-col gap-1">
          <div className="flex items-center gap-3">
            {isInitialLoading ? (
              <Skeleton className="h-7 w-64 rounded bg-slate-100" />
            ) : (
              <>
                <Medal className="w-[24px] h-[24px] text-indigo-600" />
                <h2 className="text-[24px] font-bold text-slate-900 m-0 leading-none flex items-center gap-1.5">
                  {job?.title} 
                  <span className="text-slate-500 font-medium text-[16px]">
                    ({job?.jobdiva_id || job?.job_id || jobId}) <span className="text-indigo-600 text-[14px] ml-1">🔗</span>
                  </span>
                </h2>
              </>
            )}
          </div>
          <div className="text-[14px] text-slate-500 font-medium mt-0.5">Candidate Rank List</div>
        </div>
        <div className="flex items-center gap-6">
          <div className="flex items-center gap-8 py-1 px-4 border-r border-slate-100">
            <div className="space-y-1.5 text-[14px] text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-4 w-40 bg-slate-100" />
                  <Skeleton className="h-4 w-48 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Total Candidates Sourced: <strong className="text-slate-900 ml-1">{candidates.length}</strong>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Resume Shortlisted Candidates: <strong className="text-slate-900 ml-1">{candidates.filter(c => c.match_score >= 70).length}</strong>
                  </div>
                </>
              )}
            </div>
            <div className="space-y-1.5 text-[14px] text-slate-600">
              {isInitialLoading ? (
                <>
                  <Skeleton className="h-4 w-40 bg-slate-100" />
                  <Skeleton className="h-4 w-32 bg-slate-100" />
                </>
              ) : (
                <>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Max. Allowed Submittals: <strong className="text-slate-900 ml-1">{job?.max_allowed_submittals ?? 0}</strong>
                  </div>
                  <div className="flex items-center gap-2">
                    <div className="w-1.5 h-1.5 rounded-full bg-slate-300"></div> Openings: <strong className="text-slate-900 ml-1">{job?.openings ?? 0}</strong>
                  </div>
                </>
              )}
            </div>
          </div>
          <Button variant="outline" className="w-[40px] h-[40px] p-0 flex items-center justify-center text-slate-500 hover:text-slate-800" onClick={fetchData} disabled={isLoading}>
            <RefreshCw className={`w-[16px] h-[16px] ${isRefreshing ? "animate-spin" : ""}`} />
          </Button>
        </div>
      </div>

      {/* Table Interface */}
      <div className="space-y-4">
        {/* Full-width Search Bar */}
        <div className="relative w-full h-[40px]">
          <div className="absolute inset-y-0 left-3 flex items-center pointer-events-none">
            <Search className="h-4 w-4 text-slate-400" />
          </div>
          <Input
            placeholder="Search..."
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            className="h-full pl-10 pr-6 bg-white border-slate-200 rounded-[8px] shadow-sm text-[14px] focus:ring-indigo-500/20 focus:border-indigo-500/50"
          />
        </div>

        {/* HTML Exact Replica Table */}
        <div className="bg-white rounded-[12px] border border-slate-200 shadow-sm overflow-hidden relative">
          <div className="overflow-x-auto">
            <Table>
              <TableHeader>
                <TableRow className="bg-white border-b border-slate-200 hover:bg-white h-[50px]">
                  <TableHead className="w-[60px] text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider border-r border-[#e2e8f0]">#</TableHead>
                  <TableHead className="w-[300px] font-bold text-slate-900 text-[12px] uppercase tracking-wider border-r border-slate-200 py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" /> {/* Spacer */}
                      <span className="whitespace-nowrap flex-1 text-center">CANDIDATE NAME</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[240px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">RESUME SCREENING STATUS</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[220px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">RESUME SCREENING SCORE</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[180px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">ENGAGE STATUS</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[170px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">ENGAGE SCORE</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[240px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">ENGAGE COMPLETED AT</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[160px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">TOTAL FIT SCORE</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider min-w-[140px] py-0">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">JOB CONFIG</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <Lightbulb className="w-3.5 h-3.5 text-slate-500" />
                      </div>
                    </div>
                  </TableHead>
                  <TableHead className="sticky right-0 bg-white z-50 text-center font-bold text-slate-900 text-[12px] uppercase tracking-wider border-l border-slate-200 py-0 min-w-[160px]">
                    <div className="flex items-center justify-between w-full h-full">
                      <div className="w-[40px]" />
                      <span className="whitespace-nowrap flex-1 text-center">FEEDBACK</span>
                      <div className="w-[40px] flex items-center justify-end gap-1 px-2 opacity-40">
                        <ChevronsUpDown className="w-3.5 h-3.5" />
                        <Filter className="w-3.5 h-3.5" />
                      </div>
                    </div>
                  </TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className={isRefreshing ? "opacity-60 transition-opacity duration-300 pointer-events-none" : ""}>
                {isInitialLoading ? (
                  Array.from({ length: 5 }).map((_, i) => (
                    <TableRow key={i} className="h-32">
                      <TableCell className="pl-4"><Skeleton className="h-4 w-4 mx-auto" /></TableCell>
                      <TableCell className="sticky left-0 bg-white z-20 border-r border-slate-200/50"><Skeleton className="h-12 w-64" /></TableCell>
                      <TableCell className="pl-6"><Skeleton className="h-8 w-24 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-8 w-20 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-4 w-32 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-6 w-12 mx-auto" /></TableCell>
                      <TableCell><Skeleton className="h-6 w-16 mx-auto" /></TableCell>
                      <TableCell className="sticky right-0 bg-white z-20 border-l border-slate-200/50 pr-10"><Skeleton className="h-9 w-32 mx-auto" /></TableCell>
                    </TableRow>
                  ))
                ) : (
                  filteredCandidates.map((candidate, idx) => {
                    const screeningScore = candidate.match_score || 0;
                    const engageScore = candidate.engage_score || 0;
                    const totalScore = screeningScore + engageScore;
                    const initials = candidate.name.split(' ').map(n => n[0]).join('');

                    return (
                      <TableRow key={candidate.id} className="border-b border-[#e2e8f0] hover:bg-slate-50/80 transition-colors h-auto group text-center">
                        <TableCell className="text-center font-semibold text-slate-500 text-[13px] border-r border-[#e2e8f0] w-[60px] py-4 align-top">
                          {idx + 1}
                        </TableCell>
                        <TableCell className="border-r border-[#e2e8f0] min-w-[300px] py-4 px-5 align-middle text-center">
                          <button
                            onClick={() => openDetails(candidate)}
                            className="text-[15px] font-bold text-indigo-600 hover:underline text-center w-full block mb-1.5"
                          >
                            {candidate.name}
                          </button>
                          <span className="text-[13px] text-[#64748b] block mb-1 text-center">
                            <Mail className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.email || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[13px] text-[#64748b] block mb-1 text-center">
                            <Phone className="w-3.5 h-3.5 inline mr-1 opacity-70" /> {candidate.phone || <span className="font-normal opacity-50">—</span>}
                          </span>
                          <span className="text-[13px] text-[#64748b] block text-center">
                            <Calendar className="w-3.5 h-3.5 inline mr-1 opacity-70" /> Available: {candidate.availability || <span className="font-normal opacity-50">—</span>}
                          </span>
                        </TableCell>

                        <TableCell className="text-center align-middle py-4">
                          <div className="flex items-center justify-center gap-1.5">
                            <span className="font-medium text-[13px]" style={{ color: screeningScore >= 70 ? '#059669' : '#e11d48' }}>
                              {screeningScore >= 70 ? "Pass" : "Fail"}
                            </span>
                            <Lightbulb className="w-3.5 h-3.5 text-amber-500 opacity-80 cursor-help" />
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-top py-4 font-medium text-[#0f172a] text-[14px]">
                          <div className="flex items-center justify-center gap-1.5 w-full text-center">
                            {screeningScore}
                            <Lightbulb className="w-3.5 h-3.5 text-amber-500 opacity-80 cursor-help" />
                          </div>
                        </TableCell>

                        <TableCell className="text-center align-middle py-4">
                          <span className="font-medium text-[13px]" style={{ color: candidate.engage_status?.toLowerCase().includes("pass") ? '#059669' : '#64748b' }}>
                            {candidate.engage_status || "Pending"}
                          </span>
                        </TableCell>

                        <TableCell className="text-center align-middle py-4 font-medium text-slate-700 text-[14px]">
                          {engageScore > 0 ? (
                            <div className="flex items-center justify-center gap-1.5 w-full text-center">
                              {engageScore}
                              <Lightbulb className="w-3.5 h-3.5 text-amber-500 opacity-80 cursor-help" />
                            </div>
                          ) : (
                            <span className="font-normal opacity-50">—</span>
                          )}
                        </TableCell>

                        <TableCell className="text-center font-medium text-slate-700 text-[14px] align-middle py-4">
                          {candidate.data?.engage_completed_at ? formatDate(candidate.data.engage_completed_at) : <span className="font-normal opacity-50">—</span>}
                        </TableCell>

                        <TableCell className="text-center font-medium text-slate-700 text-[14px] align-middle py-4">
                          {totalScore || <span className="font-normal opacity-50">—</span>}
                        </TableCell>

                        <TableCell className="text-center align-middle py-4 font-medium text-slate-700 text-[13px]">
                          <div className="flex items-center justify-center gap-1.5 w-full text-center">
                            {candidate.data?.config_version || <span className="font-normal opacity-50">—</span>}
                            <Lightbulb className="w-3.5 h-3.5 text-amber-500 opacity-80 cursor-help" />
                          </div>
                        </TableCell>

                        <TableCell className="sticky right-0 bg-white z-40 text-center pr-5 pl-5 border-l border-[#e2e8f0] py-4 align-middle transition-colors group-hover:bg-slate-50/80">
                          <div className="flex flex-col items-center">
                            <select 
                              className="w-full text-[13px] font-medium text-[#334155] bg-white border border-[#cbd5e1] rounded h-9 px-2 focus:outline-none focus:ring-1 focus:ring-indigo-500 mb-2"
                              value={feedbacks[candidate.id]?.startsWith("Reject") ? "Reject" : feedbacks[candidate.id] || ""}
                              onChange={(e) => {
                                const val = e.target.value;
                                if (val === "Reject") {
                                  setActionCandidateId(candidate.id);
                                  setRejectReason("");
                                  setIntegrationModalOpen('reject');
                                } else if (val === "Submit") {
                                  setActionCandidateId(candidate.id);
                                  setIntegrationModalOpen('submit');
                                }
                              }}
                            >
                              <option value="" disabled>Select Action...</option>
                              <option value="Submit">Submit</option>
                              <option value="Reject">Reject</option>
                            </select>
                            {feedbacks[candidate.id] && (
                              <div className={`text-[11px] font-bold flex items-center justify-center gap-1.5 whitespace-nowrap ${feedbacks[candidate.id] === 'Submit' ? 'text-indigo-600' : 'text-rose-600'}`}>
                                {feedbacks[candidate.id] === 'Submit' ? <><Check className="w-3.5 h-3.5" /> Submitted</> : <><X className="w-3.5 h-3.5" /> Rejected</>}
                              </div>
                            )}
                          </div>
                        </TableCell>
                      </TableRow>
                    );
                  })
                )}
              </TableBody>
            </Table>
          </div>
        </div>
      </div>

      {/* Modals */}
      {selectedCandidate && (
        <CandidateDetailsModal
          isOpen={detailsModalOpen}
          onClose={() => setDetailsModalOpen(false)}
          candidateName={selectedCandidate.name}
          profileUrl={selectedCandidate.profile_url}
          imageUrl={selectedCandidate.image_url}
          jobTitle={selectedCandidate.headline}
          location={selectedCandidate.location}
          experienceYears={selectedCandidate.data?.experience_years}
          matchScore={selectedCandidate.match_score}
          matchScoreDetails={selectedCandidate.data?.match_score_details}
          matchedSkills={selectedCandidate.data?.matched_skills}
          missingSkills={selectedCandidate.data?.missing_skills}
          explainability={selectedCandidate.data?.explainability}
        />
      )}

      {/* Integration Modals */}
      {integrationModalOpen && actionCandidateId && (
        <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4">
          <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden border border-slate-200">
            {integrationModalOpen === 'submit' ? (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <ExternalLink className="w-5 h-5 text-indigo-600" /> 
                    Submit to JobDiva
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600"><Search className="w-4 h-4 hidden" />×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    This action will initiate an <strong className="text-slate-900 font-semibold">external submission in JobDiva</strong> for:
                  </p>
                  <div className="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-2 text-sm text-slate-700">
                    <p><strong>Candidate:</strong> {candidates.find(c => c.id === actionCandidateId)?.name}</p>
                    <p><strong>Job:</strong> {job?.title} ({job?.jobdiva_id || job?.job_id || jobId})</p>
                    <p><strong>Client:</strong> {job?.customer_name || "—"}</p>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <Button variant="outline" onClick={() => setIntegrationModalOpen(null)} className="font-semibold text-slate-600">Cancel</Button>
                  <Button className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold" onClick={handleConfirmSubmit}>Confirm & Submit</Button>
                </div>
              </>
            ) : (
              <>
                <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
                  <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
                    <span className="w-5 h-5 rounded-full bg-rose-100 text-rose-600 flex items-center justify-center font-bold text-[11px]">✕</span>
                    Reject Candidate
                  </h3>
                  <button onClick={() => setIntegrationModalOpen(null)} className="text-slate-400 hover:text-slate-600">×</button>
                </div>
                <div className="p-6 space-y-4">
                  <p className="text-sm text-slate-500">
                    Please provide a reason for rejecting <strong className="text-slate-900 font-semibold">{candidates.find(c => c.id === actionCandidateId)?.name}</strong>.
                  </p>
                  <div className="space-y-2">
                    <label className="text-xs font-bold text-slate-500 uppercase tracking-widest">Rejection Reason</label>
                    <select 
                      className="w-full h-11 px-3 text-sm border border-slate-200 rounded-lg focus:ring-2 focus:ring-rose-500/20 focus:border-rose-500/50"
                      value={rejectReason}
                      onChange={e => setRejectReason(e.target.value)}
                    >
                      <option value="" disabled>Select a reason...</option>
                      <option value="Skills do not meet requirements">Skills do not meet requirements</option>
                      <option value="Communication skills">Communication skills</option>
                      <option value="Compensation expectations exceed budget">Compensation expectations exceed budget</option>
                      <option value="Candidate withdrew interest">Candidate withdrew interest</option>
                      <option value="Other">Other</option>
                    </select>
                  </div>
                </div>
                <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
                  <Button variant="outline" onClick={() => setIntegrationModalOpen(null)} className="font-semibold text-slate-600">Cancel</Button>
                  <Button className="bg-rose-600 hover:bg-rose-700 text-white font-bold" onClick={handleConfirmReject} disabled={!rejectReason}>Confirm Reject</Button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
