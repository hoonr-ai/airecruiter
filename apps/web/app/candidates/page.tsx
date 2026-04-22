"use client";

import { useState, useEffect, useMemo, useRef } from "react";
import Link from "next/link";
import { Search, ExternalLink, User, MapPin, Briefcase, Linkedin, ShieldCheck, Mail, ArrowLeft, Eye, Zap, Filter, ChevronDown, X, ArrowUp, ArrowDown, ChevronsUpDown } from "lucide-react";
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Sentinel value for "All X" options. Radix Select forbids "" as an item
// value, so we use this constant and map it to "no filter" in the pipeline.
const ALL = "__all__";

// Match-band filter options. Values encode the score range so the filter
// pipeline can decode without a switch.
const MATCH_BANDS: { value: string; label: string }[] = [
  { value: ALL, label: "Any match" },
  { value: "strong", label: "Strong (≥80%)" },
  { value: "good", label: "Good (60–79%)" },
  { value: "low", label: "Low (<60%)" },
  { value: "unscored", label: "Unscored" },
];

function pickMatchScore(c: any): number | null {
  const s = c?.match_score ?? c?.data?.match_score ?? c?.resume_match_percentage;
  return typeof s === "number" && !Number.isNaN(s) ? s : null;
}

function matchBandMatches(c: any, band: string): boolean {
  if (band === ALL) return true;
  const s = pickMatchScore(c);
  if (band === "unscored") return s === null;
  if (s === null) return false;
  if (band === "strong") return s >= 80;
  if (band === "good") return s >= 60 && s < 80;
  if (band === "low") return s < 60;
  return true;
}

// Excel-style sorting: 1st header click = asc, 2nd = desc, 3rd = clear.
type SortDir = "asc" | "desc" | null;
type SortKey =
  | "name"
  | "match"
  | "job_title"
  | "location"
  | "source"
  | "created_at";

// Accessor for each sortable column. Returns a comparable value per
// candidate — numbers for match/date (so we don't parse strings on every
// compare) and lowercased strings for text columns (so sort is
// case-insensitive like Excel).
const SORT_ACCESSORS: Record<SortKey, (c: any) => number | string> = {
  name: (c) => (c.name || "").toLowerCase(),
  match: (c) => {
    const s = pickMatchScore(c);
    return s ?? -Infinity; // unscored sinks to the bottom on asc
  },
  job_title: (c) => (c.job_title || `#${c.jobdiva_id || ""}`).toLowerCase(),
  location: (c) => (c.location || "").toLowerCase(),
  source: (c) => (c.source || "").toLowerCase(),
  created_at: (c) => {
    const t = c.created_at ? new Date(c.created_at).getTime() : 0;
    return Number.isFinite(t) ? t : 0;
  },
};

function compareCandidates(a: any, b: any, key: SortKey, dir: SortDir): number {
  if (!dir) return 0;
  const av = SORT_ACCESSORS[key](a);
  const bv = SORT_ACCESSORS[key](b);
  let cmp = 0;
  if (typeof av === "number" && typeof bv === "number") cmp = av - bv;
  else cmp = String(av).localeCompare(String(bv));
  return dir === "asc" ? cmp : -cmp;
}

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
  const [searchQuery, setSearchQuery] = useState("");
  const [jobFilter, setJobFilter] = useState<string>(ALL);
  const [matchFilter, setMatchFilter] = useState<string>(ALL);
  const [sourceFilter, setSourceFilter] = useState<string>(ALL);
  const [locationFilter, setLocationFilter] = useState<string>(ALL);
  const [sortKey, setSortKey] = useState<SortKey | null>(null);
  const [sortDir, setSortDir] = useState<SortDir>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [messageModalOpen, setMessageModalOpen] = useState(false);
  const [selectedCandidateForEmail, setSelectedCandidateForEmail] = useState<any>(null);
  const [resumeModalOpen, setResumeModalOpen] = useState(false);
  const [selectedCandidateForResume, setSelectedCandidateForResume] = useState<any>(null);
  const [detailsModalOpen, setDetailsModalOpen] = useState(false);
  const [selectedCandidateForDetails, setSelectedCandidateForDetails] = useState<any>(null);

  useEffect(() => {
    fetchCandidates(false);

    // Enable "live streaming" via background polling
    const intervalId = setInterval(() => {
      fetchCandidates(true);
    }, 5000);

    return () => clearInterval(intervalId);
  }, []);

  const fetchCandidates = async (isBackground = false) => {
    if (!isBackground) setIsLoading(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates`);
      const data = await response.json();
      if (!isBackground) console.log("📊 Fetched candidates data:", data);

      if (data.status === "success" && Array.isArray(data.candidates)) {
        const seen = new Set();
        const uniqueCandidates = data.candidates.filter((c: any) => {
          const id = c.candidate_id || c.id;
          if (!id) return true;
          if (seen.has(id)) return false;
          seen.add(id);
          return true;
        });

        const getSourcePriority = (source: string) => {
          const s = source.toLowerCase();
          if (s.includes('applicants')) return 1;
          if (s.includes('linkedin')) return 2;
          if (s.includes('talentsearch') || s.includes('talent_search')) return 3;
          return 4;
        };

        const sortedUnique = uniqueCandidates.sort((a: any, b: any) => {
          const prioA = getSourcePriority(a.source);
          const prioB = getSourcePriority(b.source);
          if (prioA !== prioB) return prioA - prioB;

          const scoreA = a.match_score || (a as any).resume_match_percentage || 0;
          const scoreB = b.match_score || (b as any).resume_match_percentage || 0;
          return scoreB - scoreA;
        });

        if (!isBackground) console.log(`✅ Found ${data.candidates.length} tracking records, deduplicated and sorted to ${sortedUnique.length} unique candidates`);
        setCandidates(sortedUnique);
      }
    } catch (error) {
      console.error("Error fetching candidates:", error);
    } finally {
      if (!isBackground) {
        setIsLoading(true);
        setTimeout(() => setIsLoading(false), 500); // Small delay for aesthetic
      }
    }
  };

  // Build dropdown option lists from the current candidate set. We derive
  // these from `candidates` (not `filteredCandidates`) so toggling one
  // filter doesn't empty out the other dropdowns.
  const jobOptions = useMemo(() => {
    const byId = new Map<string, string>();
    for (const c of candidates) {
      if (!c.jobdiva_id) continue;
      const label = c.job_title ? `${c.job_title} — #${c.jobdiva_id}` : `#${c.jobdiva_id}`;
      if (!byId.has(c.jobdiva_id)) byId.set(c.jobdiva_id, label);
    }
    return Array.from(byId.entries())
      .map(([id, label]) => ({ id, label }))
      .sort((a, b) => a.label.localeCompare(b.label));
  }, [candidates]);

  const sourceOptions = useMemo(() => {
    const set = new Set<string>();
    for (const c of candidates) if (c.source) set.add(c.source);
    return Array.from(set).sort();
  }, [candidates]);

  const locationOptions = useMemo(() => {
    const set = new Set<string>();
    for (const c of candidates) if (c.location) set.add(c.location);
    return Array.from(set).sort();
  }, [candidates]);

  // Single derived list applying search + all active filters + optional
  // column sort. Runs on every state change — no manual re-derivation
  // needed in fetchCandidates.
  const filteredCandidates = useMemo(() => {
    const q = searchQuery.trim().toLowerCase();
    const filtered = candidates.filter((c: any) => {
      if (jobFilter !== ALL && c.jobdiva_id !== jobFilter) return false;
      if (sourceFilter !== ALL && c.source !== sourceFilter) return false;
      if (locationFilter !== ALL && c.location !== locationFilter) return false;
      if (!matchBandMatches(c, matchFilter)) return false;
      if (q) {
        const hay = [
          c.name,
          c.job_title,
          c.headline,
          c.source,
          c.jobdiva_id,
          c.location,
        ]
          .filter(Boolean)
          .join(" ")
          .toLowerCase();
        if (!hay.includes(q)) return false;
      }
      return true;
    });
    // When no column sort is active, the default order from fetchCandidates
    // (source priority + match desc) is preserved. When active, apply the
    // user-selected sort on a stable copy.
    if (sortKey && sortDir) {
      return [...filtered].sort((a, b) => compareCandidates(a, b, sortKey, sortDir));
    }
    return filtered;
  }, [candidates, searchQuery, jobFilter, matchFilter, sourceFilter, locationFilter, sortKey, sortDir]);

  const activeFilterCount =
    (jobFilter !== ALL ? 1 : 0) +
    (matchFilter !== ALL ? 1 : 0) +
    (sourceFilter !== ALL ? 1 : 0) +
    (locationFilter !== ALL ? 1 : 0);

  const clearAllFilters = () => {
    setJobFilter(ALL);
    setMatchFilter(ALL);
    setSourceFilter(ALL);
    setLocationFilter(ALL);
    setSearchQuery("");
  };

  // Excel-style three-state toggle on a column header: off -> asc -> desc -> off.
  const toggleSort = (key: SortKey) => {
    if (sortKey !== key) {
      setSortKey(key);
      setSortDir("asc");
      return;
    }
    if (sortDir === "asc") {
      setSortDir("desc");
      return;
    }
    // dir === "desc" -> clear
    setSortKey(null);
    setSortDir(null);
  };

  const renderSortIcon = (key: SortKey) => {
    if (sortKey !== key) {
      return <ChevronsUpDown className="w-3.5 h-3.5 text-slate-300 group-hover/sortable:text-slate-400 transition-colors" />;
    }
    return sortDir === "asc"
      ? <ArrowUp className="w-3.5 h-3.5 text-[#6366f1]" />
      : <ArrowDown className="w-3.5 h-3.5 text-[#6366f1]" />;
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
      <div className="space-y-3 mt-4">
        <div className="flex justify-between items-center gap-4">
          <div className="relative w-[400px]">
            <Search className="absolute left-3.5 top-1/2 transform -translate-y-1/2 text-slate-400 h-[18px] w-[18px]" />
            <Input
              placeholder="Search candidates, jobs, or sources..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              className="pl-10 h-11 border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white rounded-xl text-[14px] shadow-sm"
            />
          </div>
          <div className="flex items-center gap-3">
            <Badge variant="outline" className="px-4 py-1.5 h-11 flex items-center gap-2 border-slate-200 bg-white text-slate-600 font-bold rounded-xl shadow-sm">
              <span className="w-2 h-2 rounded-full bg-green-500 animate-pulse" />
              {filteredCandidates.length} of {candidates.length} shown
            </Badge>
          </div>
        </div>

        {/* Filter dropdowns. Values derived from `candidates` so toggling
            one filter never empties out the choices in the others. */}
        <div className="flex flex-wrap items-center gap-2.5">
          <div className="flex items-center gap-1.5 text-[12.5px] font-semibold text-slate-500 mr-1">
            <Filter className="w-4 h-4 text-slate-400" />
            Filters
          </div>

          <Select value={jobFilter} onValueChange={setJobFilter}>
            <SelectTrigger className="h-10 min-w-[220px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="All jobs" />
            </SelectTrigger>
            <SelectContent className="max-h-[320px]">
              <SelectItem value={ALL}>All jobs</SelectItem>
              {jobOptions.map((j) => (
                <SelectItem key={j.id} value={j.id}>
                  {j.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={matchFilter} onValueChange={setMatchFilter}>
            <SelectTrigger className="h-10 min-w-[160px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any match" />
            </SelectTrigger>
            <SelectContent>
              {MATCH_BANDS.map((b) => (
                <SelectItem key={b.value} value={b.value}>
                  {b.label}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={sourceFilter} onValueChange={setSourceFilter}>
            <SelectTrigger className="h-10 min-w-[170px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any source" />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value={ALL}>Any source</SelectItem>
              {sourceOptions.map((s) => (
                <SelectItem key={s} value={s}>
                  {s}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          <Select value={locationFilter} onValueChange={setLocationFilter}>
            <SelectTrigger className="h-10 min-w-[170px] bg-white border-slate-200 rounded-xl shadow-sm text-[13px] font-medium">
              <SelectValue placeholder="Any location" />
            </SelectTrigger>
            <SelectContent className="max-h-[320px]">
              <SelectItem value={ALL}>Any location</SelectItem>
              {locationOptions.map((l) => (
                <SelectItem key={l} value={l}>
                  {l}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>

          {(activeFilterCount > 0 || searchQuery || sortKey) && (
            <Button
              variant="ghost"
              size="sm"
              onClick={() => {
                clearAllFilters();
                setSortKey(null);
                setSortDir(null);
              }}
              className="h-10 px-3 text-[12.5px] font-semibold text-slate-500 hover:text-slate-900 hover:bg-slate-100 rounded-xl flex items-center gap-1.5"
            >
              <X className="w-3.5 h-3.5" />
              Clear
              {activeFilterCount > 0 && (
                <span className="ml-1 px-1.5 py-0.5 bg-slate-100 text-slate-600 rounded-md text-[11px] font-bold">
                  {activeFilterCount}
                </span>
              )}
            </Button>
          )}
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
          <Table 
            className="relative" 
            containerClassName="overflow-x-auto overflow-y-auto max-h-[calc(100vh-220px)] border-b border-slate-100"
          >
              <TableHeader className="bg-[#fcfdfd]">
                <TableRow className="border-slate-100">
                  <TableHead className="pl-10 sticky top-0 left-0 bg-[#fcfdfd] z-50 w-[110px] min-w-[110px] h-14 border-b border-slate-100"></TableHead>
                  <TableHead
                    onClick={() => toggleSort("name")}
                    className="group/sortable cursor-pointer select-none w-[300px] min-w-[300px] text-left text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 left-[110px] bg-[#fcfdfd] z-50 border-r border-b border-slate-100 shadow-[4px_0_8px_-4px_rgba(0,0,0,0.1)] hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Candidate Name {renderSortIcon("name")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("match")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Match {renderSortIcon("match")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("job_title")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Applied For {renderSortIcon("job_title")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("location")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Location {renderSortIcon("location")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("source")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Sourcing Details {renderSortIcon("source")}</span>
                  </TableHead>
                  <TableHead
                    onClick={() => toggleSort("created_at")}
                    className="group/sortable cursor-pointer select-none text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100 hover:text-slate-900 hover:bg-slate-50"
                  >
                    <span className="inline-flex items-center gap-1.5">Sourced On {renderSortIcon("created_at")}</span>
                  </TableHead>
                  <TableHead className="text-center pr-10 text-[12.5px] font-bold text-slate-500 uppercase tracking-wide h-14 sticky top-0 bg-[#fcfdfd] z-40 border-b border-slate-100">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody className="divide-y divide-slate-100">
                {filteredCandidates.map((candidate, idx) => (
                  <TableRow key={`${candidate.id || candidate.candidate_id}-${idx}`} className="hover:bg-slate-50/50 transition-colors group">
                    <TableCell className="pl-10 py-6 sticky left-0 bg-white z-30 w-[110px] min-w-[110px] group-hover:bg-slate-50 transition-colors">
                      <Avatar className="h-12 w-12 border border-slate-200 shadow-sm transition-transform group-hover:scale-105">
                        <AvatarImage src={candidate.image_url} />
                        <AvatarFallback className="bg-slate-100 text-slate-400 text-[14px] font-bold">
                          {candidate.name.split(' ').map(n => n[0]).join('')}
                        </AvatarFallback>
                      </Avatar>
                    </TableCell>
                    <TableCell className="py-6 w-[300px] min-w-[300px] max-w-[300px] sticky left-[110px] bg-white z-30 border-r border-slate-100 shadow-[4px_0_8px_-4px_rgba(0,0,0,0.1)] group-hover:bg-slate-50 transition-colors overflow-hidden">
                      <div className="space-y-1 relative z-10">
                        <a
                          href={candidate.source?.startsWith('LinkedIn') ? candidate.profile_url || '#' : '#'}
                          target={candidate.source?.startsWith('LinkedIn') ? "_blank" : undefined}
                          rel={candidate.source?.startsWith('LinkedIn') ? "noopener noreferrer" : undefined}
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
                               candidate.source?.startsWith('LinkedIn') ? 'group-hover/name:text-[#1d4ed8]' : 
                               candidate.source === 'JobDiva-TalentSearch' ? 'group-hover/name:text-[#c2410c]' : 
                               'group-hover/name:text-[#6366f1]'
                             }`}>
                               {candidate.name}
                             </span>
                             <span 
                               className={`shrink-0 h-6 w-6 flex items-center justify-center border border-slate-200 bg-white text-slate-400 rounded-lg shadow-sm transition-all ${
                                 candidate.source?.startsWith('LinkedIn') 
                                   ? 'group-hover/name:border-[#bfdbfe] group-hover/name:bg-[#eff6ff] group-hover/name:text-[#1d4ed8]' : 
                                 candidate.source === 'JobDiva-TalentSearch' 
                                   ? 'group-hover/name:border-[#D2B48C] group-hover/name:bg-[#FDF8F5] group-hover/name:text-[#8B5A2B]' : 
                                 'group-hover/name:border-[#c7d2fe] group-hover/name:bg-[#f5f3ff] group-hover/name:text-[#6366f1]'
                               }`}
                               title={candidate.source?.startsWith('LinkedIn') ? "View LinkedIn Profile" : "Click to view resume"}
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
                        <span className={`px-2.5 w-fit py-0.5 rounded-lg text-[10.5px] font-extrabold uppercase tracking-wider flex items-center gap-1.5 shadow-sm h-fit border ${candidate.source?.startsWith('LinkedIn')
                            ? 'bg-[#eff6ff] text-[#1d4ed8] border-[#bfdbfe]'
                            : candidate.source === 'JobDiva-TalentSearch'
                              ? 'bg-[#FDF8F5] text-[#8B5A2B] border-[#D2B48C]'
                              : 'bg-[#f5f3ff] text-[#6366f1] border-[#ddd6fe]'
                          }`}>
                          {candidate.source?.startsWith('LinkedIn') ? <Linkedin className="w-3 h-3 fill-current" /> : candidate.source === 'JobDiva-TalentSearch' ? <Zap className="w-3 h-3 fill-current" /> : <ShieldCheck className="w-3 h-3" />}
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