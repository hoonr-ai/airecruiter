"use client";

import Link from "next/link";
import { cn } from "@/lib/utils";
import { PlusCircle, Search, Filter, MoreVertical, Briefcase, Users, Clock, ArrowUpRight, Zap, Target, Users2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Progress } from "@/components/ui/progress";
import { useState } from "react";

const MONITORED_JOBS = [
  {
    id: "26-05604",
    title: "Digital Marketing Specialist IV",
    customer: "Intuit",
    status: "Open",
    location: "San Francisco, CA",
    type: "Hybrid",
    matchRate: 65,
    candidates: 42
  },
  {
    id: "26-06182",
    title: "X-Ray Technician",
    customer: "Staffing Engine",
    status: "Open",
    location: "Rowlett, TX",
    type: "Onsite",
    matchRate: 88,
    candidates: 12
  },
  {
    id: "25-68140",
    title: "Java Full Stack Developer",
    customer: "American Boa Inc",
    status: "Open",
    location: "Remote",
    type: "Remote",
    matchRate: 40,
    candidates: 18
  },
  {
    id: "26-06183",
    title: "IT Quality Assur Anlyt Sr",
    customer: "Progressive",
    status: "Open",
    location: "Columbus, OH",
    type: "Remote",
    matchRate: 72,
    candidates: 24
  },
  {
    id: "16-06182",
    title: "Software Developer",
    customer: "Cox Media",
    status: "Closed",
    location: "Atlanta, GA",
    type: "Hybrid",
    matchRate: 0,
    candidates: 0
  },
  {
    id: "24-00123",
    title: "Sr. Software Engineer",
    customer: "Tech Mahindra",
    status: "Closed",
    location: "Remote",
    type: "Remote",
    matchRate: 0,
    candidates: 0
  },
  {
    id: "26-06118",
    title: "Field Service Technician II US",
    customer: "Compucom Systems",
    status: "Cancelled",
    location: "Lancaster, PA",
    type: "Onsite",
    matchRate: 0,
    candidates: 0
  }
];

export default function DashboardPage() {
  const [showActiveJobs, setShowActiveJobs] = useState(false);

  return (
    <div className="space-y-8 min-h-[80vh] flex flex-col justify-center">
      {/* Welcome Header */}
      {!showActiveJobs && (
        <div className="text-center space-y-4 mb-8">
          <h1 className="text-4xl font-bold tracking-tight text-hoonr-gradient inline-block pb-1">
            Welcome back, John
          </h1>
          <p className="text-xl text-muted-foreground">What would you like to do today?</p>
        </div>
      )}

      {/* Navigation Hub */}
      {!showActiveJobs ? (
        <div className="grid gap-6 md:grid-cols-3 max-w-5xl mx-auto w-full">
          {/* Choice 1: Active Jobs */}
          <Card
            className="group hover:border-primary/50 transition-all cursor-pointer hover:shadow-xl bg-card/50 backdrop-blur-sm border-white/5"
            onClick={() => setShowActiveJobs(true)}
          >
            <CardHeader className="text-center pt-10">
              <div className="mx-auto p-4 bg-primary/10 rounded-full mb-4 group-hover:scale-110 transition-transform shadow-sm shadow-primary/5">
                <Briefcase className="h-8 w-8 text-primary" />
              </div>
              <CardTitle className="text-xl">Look at Active Jobs</CardTitle>
              <CardDescription>View status of your {MONITORED_JOBS.filter(j => j.status === 'Open').length} ongoing searches</CardDescription>
            </CardHeader>
            <CardContent className="text-center pb-10">
              <p className="text-sm text-muted-foreground">Monitor pipeline, tribunal results, and interviews.</p>
            </CardContent>
          </Card>

          {/* Choice 2: Find Talent */}
          <Link href="/jobs/new" className="block h-full">
            <Card className="group hover:border-primary/50 transition-all cursor-pointer hover:shadow-lg bg-card/50 backdrop-blur-sm h-full border-white/5">
              <CardHeader className="text-center pt-10">
                <div className="mx-auto p-4 bg-primary/10 rounded-full mb-4 group-hover:scale-110 transition-transform shadow-sm shadow-primary/5">
                  <Target className="h-8 w-8 text-primary" />
                </div>
                <CardTitle className="text-xl">Find Talent</CardTitle>
                <CardDescription>Start a new search with AI Sourcing</CardDescription>
              </CardHeader>
              <CardContent className="text-center pb-10">
                <p className="text-sm text-muted-foreground">Create a job, set filters, and let AI match candidates.</p>
              </CardContent>
            </Card>
          </Link>

          {/* Choice 3: Create Team */}
          <Card className="group hover:border-primary/20 transition-all bg-card/50 backdrop-blur-sm opacity-50 border-white/5">
            <CardHeader className="text-center pt-10">
              <div className="mx-auto p-4 bg-primary/5 rounded-full mb-4">
                <Users2 className="h-8 w-8 text-primary/40" />
              </div>
              <CardTitle className="text-xl text-primary/40">Create a Team</CardTitle>
              <CardDescription>Coming Soon</CardDescription>
            </CardHeader>
            <CardContent className="text-center pb-10">
              <p className="text-sm text-muted-foreground/40">Collaborate with colleagues and hiring managers.</p>
            </CardContent>
          </Card>
        </div>
      ) : (
        /* Active Jobs Dashboard View (Revealed on Click) */
        <div className="animate-in fade-in slide-in-from-bottom-4 duration-500">
          <div className="flex items-center justify-between mb-6">
            <div>
              <h2 className="text-2xl font-bold tracking-tight">Active Jobs</h2>
              <p className="text-muted-foreground">Overview of your recruitment pipeline.</p>
            </div>
            <Button variant="ghost" onClick={() => setShowActiveJobs(false)}>Back to Hub</Button>
          </div>

          {/* Stats Row */}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4 mb-8">
            <Card className="bg-card/50 backdrop-blur-sm border-white/10">
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Active Jobs</CardTitle>
                <Briefcase className="h-4 w-4 text-primary" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{MONITORED_JOBS.filter(j => j.status === 'Open').length}</div>
                <p className="text-xs text-muted-foreground">+2 from last week</p>
              </CardContent>
            </Card>
            <Card className="bg-card/50 backdrop-blur-sm border-white/10">
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Total Tracked</CardTitle>
                <Clock className="h-4 w-4 text-primary" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">{MONITORED_JOBS.length}</div>
                <p className="text-xs text-muted-foreground">Across all stages</p>
              </CardContent>
            </Card>
          </div>

          {/* Job List (Unified Premium Design) */}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {MONITORED_JOBS.map((job) => (
              <Card key={job.id} className={cn(
                "bg-card/50 hover:bg-card/80 transition-all cursor-pointer group shadow-md hover:shadow-xl border-white/5 border-l-4",
                job.status === 'Open' ? "border-l-primary" : job.status === 'Closed' ? "border-l-muted-foreground/40 opacity-70" : "border-l-destructive/40 opacity-60"
              )}>
                <CardHeader className="pb-2">
                  <div className="flex justify-between items-start">
                    <div>
                      <CardTitle className="text-lg group-hover:text-primary transition-colors line-clamp-1">{job.title}</CardTitle>
                      <CardDescription>{job.customer} • {job.location} ({job.type})</CardDescription>
                    </div>
                    <Badge variant="secondary" className={cn(
                      "border-none text-[10px] font-semibold tracking-wide uppercase",
                      job.status === 'Open' ? "bg-primary/10 text-primary" : "bg-muted text-muted-foreground"
                    )}>
                      {job.status}
                    </Badge>
                  </div>
                </CardHeader>
                <CardContent>
                  <div className="space-y-4">
                    <div className="flex justify-between text-sm">
                      <span className="text-muted-foreground truncate mr-2">Job ID: {job.id}</span>
                      <span className="font-bold flex-shrink-0">{job.candidates || 0} Candidates</span>
                    </div>
                    {job.status === 'Open' && (
                      <div className="space-y-1">
                        <div className="flex justify-between text-[11px] text-muted-foreground font-medium uppercase tracking-tighter">
                          <span>Screening Progress</span>
                          <span className="text-primary font-bold">{job.matchRate}% Match</span>
                        </div>
                        <Progress value={job.matchRate} className="h-2 rounded-full bg-primary/5" />
                      </div>
                    )}
                    {job.status === 'Open' && (
                       <div className="pt-2 flex items-center -space-x-2">
                       {[1, 2, 3].map(i => (
                         <Avatar key={i} className="w-8 h-8 border-2 border-card shadow-sm">
                           <AvatarFallback className="bg-primary/5 text-primary text-[10px] font-bold">U{i}</AvatarFallback>
                         </Avatar>
                       ))}
                       <div className="w-8 h-8 rounded-full bg-muted/50 border-2 border-card flex items-center justify-center text-[10px] font-black text-muted-foreground shadow-sm">+{job.candidates - 3}</div>
                     </div>
                    )}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
