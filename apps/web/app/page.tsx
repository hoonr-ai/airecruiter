"use client";

import Link from "next/link";
import { PlusCircle, Search, Filter, MoreVertical, Briefcase, Users, Clock, ArrowUpRight, Zap, Target, Users2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Badge } from "@/components/ui/badge";
import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar";
import { Progress } from "@/components/ui/progress";
import { useState } from "react";

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
              <CardDescription>View status of your 12 ongoing searches</CardDescription>
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

          {/* Stats Row (Moved here) */}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-4 mb-8">
            <Card className="bg-card/50 backdrop-blur-sm border-white/10">
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Active Jobs</CardTitle>
                <Briefcase className="h-4 w-4 text-primary" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">12</div>
                <p className="text-xs text-muted-foreground">+2 from last week</p>
              </CardContent>
            </Card>
            <Card className="bg-card/50 backdrop-blur-sm border-white/10">
              <CardHeader className="flex flex-row items-center justify-between space-y-0 pb-2">
                <CardTitle className="text-sm font-medium">Candidates in Pipeline</CardTitle>
                <Users className="h-4 w-4 text-primary" />
              </CardHeader>
              <CardContent>
                <div className="text-2xl font-bold">245</div>
                <p className="text-xs text-muted-foreground">+18 new today</p>
              </CardContent>
            </Card>
            {/* ... other stats omitted for brevity/simplicity as user asked to remove clutter, but keeping core summary ... */}
          </div>

          {/* Job List (Unified Premium Design) */}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {/* Job Card 1 */}
            <Card className="bg-card/50 hover:bg-card/80 transition-all cursor-pointer border-l-4 border-l-primary group shadow-md hover:shadow-xl border-white/5">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg group-hover:text-primary transition-colors">Senior Frontend Engineer</CardTitle>
                    <CardDescription>Engineering • Remote</CardDescription>
                  </div>
                  <Badge variant="secondary" className="bg-primary/10 text-primary border-none text-[10px] font-semibold tracking-wide">ACTIVE</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">Candidates</span>
                    <span className="font-bold">42</span>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-[11px] text-muted-foreground font-medium uppercase tracking-tighter">
                      <span>Screening Progress</span>
                      <span className="text-primary font-bold">65% Match</span>
                    </div>
                    <Progress value={65} className="h-2 rounded-full bg-primary/5" />
                  </div>
                  <div className="pt-2 flex items-center -space-x-2">
                    {[1, 2, 3].map(i => (
                      <Avatar key={i} className="w-8 h-8 border-2 border-card shadow-sm">
                        <AvatarFallback className="bg-primary/5 text-primary text-[10px] font-bold">U{i}</AvatarFallback>
                      </Avatar>
                    ))}
                    <div className="w-8 h-8 rounded-full bg-muted/50 border-2 border-card flex items-center justify-center text-[10px] font-black text-muted-foreground shadow-sm">+39</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Job Card 2 */}
            <Card className="bg-card/50 hover:bg-card/80 transition-all cursor-pointer border-l-4 border-l-primary/40 group shadow-md hover:shadow-xl border-white/5">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg group-hover:text-primary transition-colors font-semibold tracking-tight">Product Designer</CardTitle>
                    <CardDescription>Design • New York</CardDescription>
                  </div>
                  <Badge variant="secondary" className="bg-primary/5 text-primary/60 border-none text-[10px] font-semibold tracking-wide uppercase">Urgent</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">Candidates</span>
                    <span className="font-bold">18</span>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-[11px] text-muted-foreground font-medium uppercase tracking-tighter">
                      <span>Screening Progress</span>
                      <span className="text-primary/60 font-bold">40% Match</span>
                    </div>
                    <Progress value={40} className="h-2 rounded-full bg-primary/5" />
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Job Card 3 (Draft) */}
            <Card className="bg-card/20 hover:bg-card/40 transition-all cursor-pointer border-l-4 border-l-transparent border-dashed border-muted-foreground/20 opacity-70 shadow-sm border-white/5">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg text-muted-foreground font-medium">Marketing Manager</CardTitle>
                    <CardDescription>Marketing • London</CardDescription>
                  </div>
                  <Badge variant="outline" className="text-[10px] text-muted-foreground/60 border-muted-foreground/20 font-bold uppercase tracking-widest">DRAFT</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="flex items-center justify-center h-12 bg-muted/10 rounded-xl border border-dashed border-muted-foreground/10 text-[10px] text-muted-foreground/60 font-bold uppercase tracking-widest">
                  Finish drafting to publish
                </div>
              </CardContent>
            </Card>
          </div>
        </div>
      )}

    </div>
  );
}
