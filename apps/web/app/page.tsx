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
            className="group hover:border-primary/50 transition-all cursor-pointer hover:shadow-lg bg-card/50 backdrop-blur-sm"
            onClick={() => setShowActiveJobs(true)}
          >
            <CardHeader className="text-center pt-10">
              <div className="mx-auto p-4 bg-blue-500/10 rounded-full mb-4 group-hover:scale-110 transition-transform">
                <Briefcase className="h-8 w-8 text-blue-500" />
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
            <Card className="group hover:border-purple-500/50 transition-all cursor-pointer hover:shadow-lg bg-card/50 backdrop-blur-sm h-full border-purple-200/20">
              <CardHeader className="text-center pt-10">
                <div className="mx-auto p-4 bg-purple-500/10 rounded-full mb-4 group-hover:scale-110 transition-transform">
                  <Target className="h-8 w-8 text-purple-500" />
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
          <Card className="group hover:border-emerald-500/50 transition-all cursor-pointer hover:shadow-lg bg-card/50 backdrop-blur-sm opacity-60">
            <CardHeader className="text-center pt-10">
              <div className="mx-auto p-4 bg-emerald-500/10 rounded-full mb-4 group-hover:scale-110 transition-transform">
                <Users2 className="h-8 w-8 text-emerald-500" />
              </div>
              <CardTitle className="text-xl">Create a Team</CardTitle>
              <CardDescription>Coming Soon</CardDescription>
            </CardHeader>
            <CardContent className="text-center pb-10">
              <p className="text-sm text-muted-foreground">Collaborate with colleagues and hiring managers.</p>
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

          {/* Job List (Existing Cards) */}
          <div className="grid gap-4 md:grid-cols-2 lg:grid-cols-3">
            {/* Job Card 1 */}
            <Card className="bg-card hover:bg-card/80 transition-colors cursor-pointer border-l-4 border-l-primary">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg">Senior Frontend Engineer</CardTitle>
                    <CardDescription>Engineering • Remote</CardDescription>
                  </div>
                  <Badge variant="secondary" className="bg-blue-500/10 text-blue-400 border-blue-500/20">Active</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">Candidates</span>
                    <span className="font-medium">42</span>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs text-muted-foreground">
                      <span>Screening Progress</span>
                      <span>65% Match Rate</span>
                    </div>
                    <Progress value={65} className="h-1.5" />
                  </div>
                  <div className="pt-2 flex items-center gap-[-8px]">
                    {[1, 2, 3].map(i => (
                      <Avatar key={i} className="w-8 h-8 border-2 border-background -ml-2 first:ml-0">
                        <AvatarFallback className="bg-primary/20 text-xs">U{i}</AvatarFallback>
                      </Avatar>
                    ))}
                    <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center text-xs -ml-2 border-2 border-background">+39</div>
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Job Card 2 */}
            <Card className="bg-card hover:bg-card/80 transition-colors cursor-pointer border-l-4 border-l-purple-500">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg">Product Designer</CardTitle>
                    <CardDescription>Design • New York</CardDescription>
                  </div>
                  <Badge variant="secondary" className="bg-purple-500/10 text-purple-400 border-purple-500/20">Urgent</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="space-y-4">
                  <div className="flex justify-between text-sm">
                    <span className="text-muted-foreground">Candidates</span>
                    <span className="font-medium">18</span>
                  </div>
                  <div className="space-y-1">
                    <div className="flex justify-between text-xs text-muted-foreground">
                      <span>Screening Progress</span>
                      <span>40% Match Rate</span>
                    </div>
                    <Progress value={40} className="h-1.5" />
                  </div>
                </div>
              </CardContent>
            </Card>

            {/* Job Card 3 (Draft) */}
            <Card className="bg-card hover:bg-card/80 transition-colors cursor-pointer border-l-4 border-l-gray-500">
              <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                  <div>
                    <CardTitle className="text-lg">Marketing Manager</CardTitle>
                    <CardDescription>Marketing • London</CardDescription>
                  </div>
                  <Badge variant="outline">Draft</Badge>
                </div>
              </CardHeader>
              <CardContent>
                <div className="flex items-center justify-center h-12 bg-muted/20 rounded-lg border border-dashed text-xs text-muted-foreground">
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
