"use client";

import { useState, useEffect } from "react";
import Link from "next/link";
import { Search, Plus, FileText, ArrowUpDown, MoreVertical, Link as LinkIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Users } from "lucide-react";

interface Job {
  id: string;
  jobdiva_id?: string;
  title: string;
  customer_name: string;
  status: string;
  location: string;
  priority: string;
  programDuration: string;
  maxAllowedSubmittals: string;
  pairStatus: string;
  candidatesSourced: number;
  resumesShortlisted: number;
  completeSubmissions: number;
  passSubmissions: number;
  pairExternalSubs: number;
  feedbackCompleted: number;
  timeToFirstPass: number;
}

type SortField = keyof Job;
type SortDirection = "asc" | "desc";

export default function DashboardPage() {
  const [searchQuery, setSearchQuery] = useState("");
  const [sortField, setSortField] = useState<keyof Job>("id");
  const [sortDirection, setSortDirection] = useState<SortDirection>("asc");
  const [allJobs, setAllJobs] = useState<Job[]>([]);
  const [filteredJobs, setFilteredJobs] = useState<Job[]>([]);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    fetchJobs();
  }, []);

  const fetchJobs = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/jobs/monitored`);
      const data = await response.json();

      const jobs: Job[] = Object.entries(data.jobs).map(([id, details]: [string, any]) => {
        const status = details.status || "Open";
        const procStatus = details.processing_status || "pending";

        let pairStatus = "Unpublished";

        // PAIR Status mapping based on internal processing_status
        if (procStatus === "monitoring_added" || procStatus === "manual_created") {
          // Setup is finished. Check JobDiva status for Active/Inactive
          if (status.toLowerCase() === "closed" || status.toLowerCase() === "cancelled") {
            pairStatus = "Inactive";
          } else {
            pairStatus = "Active";
          }
        } else {
          // If pending, step_X_complete, or any other state, wizard is not finished
          pairStatus = "Unpublished";
        }

        return {
          id,
          jobdiva_id: details.jobdiva_id || "",
          title: details.title || "—",
          customer_name: details.customer_name || "—",
          status: status || "—",
          location: [
            details.city ? `${details.city}, ${details.state || ""}`.trim() : "",
            details.zip_code || ""
          ].filter(Boolean).join(" ") || "—",
          priority: (!details.priority || details.priority === "[null]") ? "—" : details.priority,
          programDuration: (!details.program_duration && !details.duration) || details.program_duration === "[null]" || details.duration === "[null]"
            ? "—" 
            : details.program_duration || details.duration,
          maxAllowedSubmittals: (!details.max_allowed_submittals || details.max_allowed_submittals === "[null]" || Number.isNaN(Number.parseInt(details.max_allowed_submittals, 10)))
            ? "—"
            : Number.parseInt(details.max_allowed_submittals, 10).toString(),
          pairStatus: pairStatus,
          candidatesSourced: details.candidates_sourced || 0,
          resumesShortlisted: details.resumes_shortlisted || 0,
          completeSubmissions: details.complete_submissions || 0,
          passSubmissions: details.pass_submissions || 0,
          pairExternalSubs: details.pair_external_subs || 0,
          feedbackCompleted: details.feedback_completed || 0,
          timeToFirstPass: details.time_to_first_pass || 0,
        };
      });

      setAllJobs(jobs);
      setFilteredJobs(jobs);
    } catch (error) {
      console.error("Error fetching jobs:", error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleSort = (field: SortField) => {
    const newDirection = sortField === field && sortDirection === "asc" ? "desc" : "asc";
    setSortField(field);
    setSortDirection(newDirection);

    const sorted = [...filteredJobs].sort((a, b) => {
      const aVal = a[field as keyof Job];
      const bVal = b[field as keyof Job];

      if (typeof aVal === "string" && typeof bVal === "string") {
        return newDirection === "asc" ? aVal.localeCompare(bVal) : bVal.localeCompare(aVal);
      }

      if (typeof aVal === "number" && typeof bVal === "number") {
        return newDirection === "asc" ? aVal - bVal : bVal - aVal;
      }

      return 0;
    });

    setFilteredJobs(sorted);
  };

  const handleSearch = (query: string) => {
    setSearchQuery(query);
    const filtered = allJobs.filter(job =>
      Object.values(job).some(value =>
        (value?.toString() || "").toLowerCase().includes(query.toLowerCase())
      )
    );
    setFilteredJobs(filtered);
  };

  const getStatusColor = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'open') return 'bg-[#dcfce7] text-[#166534]'; // Custom soft green
    if (s === 'completed') return 'bg-[#ffedd5] text-[#c2410c]'; // Custom soft orange
    if (s === 'cancelled' || s === 'closed') return 'bg-[#fee2e2] text-[#b91c1c]'; // Custom soft red
    return 'bg-slate-100 text-slate-700';
  };

  const getPairStatusColor = (status: string) => {
    const s = status.toLowerCase();
    if (s === 'active') return 'bg-[#dcfce7] text-[#166534]';
    if (s === 'inactive' || s === 'paused') return 'bg-[#fee2e2] text-[#b91c1c]';
    if (s === 'unpublished') return 'bg-[#f1f5f9] text-[#475569]'; // Custom gray
    return 'bg-slate-100 text-slate-700';
  };

  const SortableHeader = ({ field, children, className = "" }: { field: keyof Job; children: React.ReactNode; className?: string }) => (
    <th className={`px-6 py-4 text-left text-[12.5px] font-bold text-slate-500 uppercase tracking-wide border-b border-slate-100 whitespace-nowrap ${className}`}>
      <div className="flex items-center gap-1.5 cursor-pointer hover:text-slate-800 transition-colors" onClick={() => handleSort(field)}>
        {children}
        <ArrowUpDown className="h-3.5 w-3.5 text-slate-400" />
      </div>
    </th>
  );

  return (
    <div className="space-y-6 max-w-[1240px] mx-auto pb-10">
      {/* Page Header */}
      <h1 className="text-[28px] font-bold text-slate-900 tracking-tight mt-2">Jobs Portfolio</h1>

      {/* Controls Bar */}
      <div className="flex justify-between items-center gap-4 mt-4">
        <div className="relative w-[360px]">
          <Search className="absolute left-3.5 top-1/2 transform -translate-y-1/2 text-slate-400 h-[18px] w-[18px]" />
          <Input
            placeholder="Search across all fields..."
            value={searchQuery}
            onChange={(e) => handleSearch(e.target.value)}
            className="pl-10 h-11 border-slate-200 focus:border-primary/50 focus:ring-primary/20 bg-white rounded-xl text-[14px] shadow-sm"
          />
        </div>
        <div className="flex items-center gap-3">
          <Button variant="outline" className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all">
            <FileText className="h-4 w-4" />
            Export to Excel
          </Button>
          <Button asChild variant="outline" className="flex items-center gap-2 h-10 px-4 border-slate-200 text-slate-700 font-semibold text-[13px] rounded-lg bg-white shadow-sm hover:bg-slate-50 transition-all">
            <Link href="/candidates">
              <Users className="h-4 w-4" />
              All Candidates
            </Link>
          </Button>
          <Button asChild className="flex items-center gap-2 h-10 px-5 bg-[#4f46e5] hover:bg-[#4338ca] text-white font-semibold text-[13px] rounded-lg shadow-sm transition-all active:scale-95 border-none">
            <Link href="/jobs/new">
              <Plus className="h-4 w-4" />
              New Job
            </Link>
          </Button>
        </div>
      </div>

      {/* Jobs Table */}
      <div className="bg-white rounded-2xl shadow-[0_2px_10px_-4px_rgba(0,0,0,0.1)] border border-slate-200 overflow-hidden mt-2">
        <div className="overflow-x-auto">
          <table className="min-w-full divide-y divide-slate-100">
            <thead className="bg-[#fcfdfd]">
              <tr>
                <SortableHeader field="id">JOBDIVA ID</SortableHeader>
                <SortableHeader field="title" className="sticky left-0 bg-[#fcfdfd] z-10 shadow-[5px_0_15px_-5px_rgba(0,0,0,0.03)] border-r border-slate-100/50">JOB TITLE</SortableHeader>
                <SortableHeader field="customer_name">CUSTOMER NAME</SortableHeader>
                <SortableHeader field="location">LOCATION / ZIP</SortableHeader>
                <SortableHeader field="priority">PRIORITY</SortableHeader>
                <SortableHeader field="programDuration">PROGRAM DURATION</SortableHeader>
                <SortableHeader field="maxAllowedSubmittals">MAX ALLOWED SUBMITTALS</SortableHeader>
                <SortableHeader field="status">JOB STATUS</SortableHeader>
                <SortableHeader field="pairStatus">PAIR STATUS</SortableHeader>
                <SortableHeader field="candidatesSourced">CANDIDATES SOURCED</SortableHeader>
                <SortableHeader field="resumesShortlisted">RESUMES SHORTLISTED</SortableHeader>
                <SortableHeader field="completeSubmissions">COMPLETE SUBMISSIONS</SortableHeader>
                <SortableHeader field="passSubmissions">PASS SUBMISSIONS</SortableHeader>
                <SortableHeader field="pairExternalSubs">PAIR EXTERNAL SUBS</SortableHeader>
                <SortableHeader field="feedbackCompleted">FEEDBACK COMPLETED</SortableHeader>
                <SortableHeader field="timeToFirstPass">TIME TO FIRST PASS</SortableHeader>
                <th className="px-6 py-4 text-center text-[12.5px] font-bold text-slate-500 uppercase tracking-wide border-b border-l border-slate-100/50 sticky right-0 bg-[#fcfdfd] z-10 shadow-[-10px_0_15px_-5px_rgba(0,0,0,0.03)] whitespace-nowrap">
                  ACTIONS
                </th>
              </tr>
            </thead>
            <tbody className="bg-white divide-y divide-slate-100">
              {filteredJobs.length > 0 ? filteredJobs.map((job) => (
                <tr key={job.id} className="hover:bg-slate-50/70 transition-colors group">
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-[#4f46e5]">
                    <div className="flex items-center gap-1.5">
                      {job.jobdiva_id || job.id}
                      {job.pairStatus !== 'Unpublished' && <LinkIcon className="h-3 w-3 text-[#4f46e5]/70" />}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap sticky left-0 bg-white group-hover:bg-[#f6f8fb] transition-colors border-r border-slate-100/50 z-10 shadow-[5px_0_15px_-5px_rgba(0,0,0,0.03)]">
                    <div className="flex items-center gap-2">
                      <span className="text-[13.5px] font-semibold text-slate-900">{job.title}</span>
                      {job.pairStatus === 'Unpublished' && (
                        <span className="text-[11px] text-slate-400 font-medium">(draft)</span>
                      )}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.customer_name}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.location}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.priority}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.programDuration}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.maxAllowedSubmittals}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11.5px] font-bold tracking-wide ${getStatusColor(job.status)}`}>
                      {job.status}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap">
                    <div className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11.5px] font-bold tracking-wide ${getPairStatusColor(job.pairStatus)}`}>
                      {job.pairStatus}
                    </div>
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.candidatesSourced}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.resumesShortlisted}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.completeSubmissions}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.passSubmissions}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.pairExternalSubs}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.feedbackCompleted}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-[13.5px] font-medium text-slate-700">
                    {job.timeToFirstPass ? `${job.timeToFirstPass} mins` : "—"}
                  </td>
                  <td className="px-6 py-4 whitespace-nowrap text-center text-slate-400 sticky right-0 bg-white group-hover:bg-[#f6f8fb] transition-colors border-l border-slate-100/50 z-10 shadow-[-10px_0_15px_-5px_rgba(0,0,0,0.03)]">
                    <DropdownMenu>
                      <DropdownMenuTrigger asChild>
                        <Button variant="ghost" className="h-8 w-8 p-0 rounded-full hover:bg-slate-200 transition-colors">
                          <MoreVertical className="h-4 w-4 text-slate-500" />
                        </Button>
                      </DropdownMenuTrigger>
                      <DropdownMenuContent align="end" className="rounded-xl border-slate-200 font-medium text-[13px] shadow-lg">
                        {job.pairStatus === 'Unpublished' ? (
                          <DropdownMenuItem className="cursor-pointer bg-primary/5 text-primary font-bold">
                            <Link href={`/jobs/new?jobId=${job.jobdiva_id || job.id}`} className="w-full">
                              Resume Setup
                            </Link>
                          </DropdownMenuItem>
                        ) : (
                          <DropdownMenuItem className="cursor-pointer">
                            <Link href={`/jobs/${job.jobdiva_id || job.id}`} className="w-full">
                              View Details
                            </Link>
                          </DropdownMenuItem>
                        )}
                        <DropdownMenuItem className="cursor-pointer">Edit Job</DropdownMenuItem>
                        <DropdownMenuItem className="text-red-600 focus:text-red-700 cursor-pointer">
                          Archive Job
                        </DropdownMenuItem>
                      </DropdownMenuContent>
                    </DropdownMenu>
                  </td>
                </tr>
              )) : (
                <tr>
                  <td colSpan={16} className="text-center py-10 px-6">
                    <p className="text-[14px] font-medium text-slate-400 italic">No job results to display.</p>
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
