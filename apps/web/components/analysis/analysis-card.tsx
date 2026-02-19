import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { ScrollArea } from "@/components/ui/scroll-area";
import { AlertTriangle, CheckCircle2, XCircle, BrainCircuit, ShieldAlert, BadgeCheck } from "lucide-react";
import { SkillRow } from "./skill-row";
import { cn } from "@/lib/utils";

interface AnalysisCardProps {
    result: any; // Using any for flexibility with backend payload
}

export function AnalysisCard({ result }: AnalysisCardProps) {
    if (!result) return null;

    const { score, tribunal_verdict, technical_trace, analysis, tribunal_status } = result;

    // Derive Trace Lists
    const requiredSkills = technical_trace?.filter((t: any) => t.priority === "required") || [];
    const preferredSkills = technical_trace?.filter((t: any) => t.priority === "preferred") || [];

    return (
        <div className="h-full flex flex-col bg-background text-foreground">
            {/* Header Summary */}
            <div className="flex items-center justify-between p-6 border-b border-border bg-card">
                <div>
                    <h2 className="text-2xl font-bold flex items-center gap-3">
                        {result.candidate_name || "Unknown Candidate"}
                        <Badge variant="outline" className={cn("text-sm",
                            score >= 80 ? "border-emerald-500 text-emerald-600 dark:text-emerald-400" :
                                score >= 50 ? "border-amber-500 text-amber-600 dark:text-amber-400" : "border-rose-500 text-rose-600 dark:text-rose-400"
                        )}>
                            {score} Match
                        </Badge>
                    </h2>
                    <p className="text-muted-foreground text-sm mt-1">ID: {result.candidate_id}</p>
                </div>

                {tribunal_status && (
                    <div className={cn("px-4 py-2 rounded-lg border flex items-center gap-2",
                        tribunal_status === "Green" ? "bg-emerald-500/10 border-emerald-500/20 text-emerald-600 dark:text-emerald-400" :
                            tribunal_status === "Red" ? "bg-rose-500/10 border-rose-500/20 text-rose-600 dark:text-rose-400" :
                                "bg-amber-500/10 border-amber-500/20 text-amber-600 dark:text-amber-400"
                    )}>
                        {tribunal_status === "Green" && <BadgeCheck className="w-5 h-5" />}
                        {tribunal_status === "Red" && <ShieldAlert className="w-5 h-5" />}
                        {tribunal_status === "Yellow" && <AlertTriangle className="w-5 h-5" />}
                        <span className="font-bold uppercase tracking-wider text-xs">
                            {tribunal_verdict?.narrative_tag?.replace(/_/g, " ") || "Analyzed"}
                        </span>
                    </div>
                )}
            </div>

            {/* Tabs */}
            <Tabs defaultValue="tribunal" className="flex-1 flex flex-col min-h-0">
                <div className="px-6 pt-4 bg-muted/30">
                    <TabsList className="bg-muted border border-border text-muted-foreground">
                        <TabsTrigger value="tribunal" className="data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm">
                            <BrainCircuit className="w-4 h-4 mr-2" />
                            Tribunal
                        </TabsTrigger>
                        <TabsTrigger value="skills" className="data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm">
                            Technical Trace
                        </TabsTrigger>
                        <TabsTrigger value="factors" className="data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow-sm">
                            Core Factors
                        </TabsTrigger>
                    </TabsList>
                </div>

                <ScrollArea className="flex-1 bg-muted/10">
                    <div className="p-6">

                        {/* TRIBUNAL TAB */}
                        <TabsContent value="tribunal" className="mt-0 space-y-6">
                            {!tribunal_verdict ? (
                                <div className="text-center py-12 text-muted-foreground">
                                    No narrative analysis available for this candidate.
                                </div>
                            ) : (
                                <>
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
                                        {/* SKEPTIC */}
                                        <Card className="bg-rose-50/50 dark:bg-rose-950/10 border-rose-200 dark:border-rose-500/20">
                                            <CardHeader>
                                                <CardTitle className="text-rose-600 dark:text-rose-400 flex items-center gap-2">
                                                    <XCircle className="w-5 h-5" /> The Skeptic
                                                </CardTitle>
                                            </CardHeader>
                                            <CardContent className="text-rose-700/90 dark:text-rose-200/90 leading-relaxed text-sm">
                                                {tribunal_verdict.skeptic_summary}
                                            </CardContent>
                                        </Card>

                                        {/* ADVOCATE */}
                                        <Card className="bg-emerald-50/50 dark:bg-emerald-950/10 border-emerald-200 dark:border-emerald-500/20">
                                            <CardHeader>
                                                <CardTitle className="text-emerald-600 dark:text-emerald-400 flex items-center gap-2">
                                                    <CheckCircle2 className="w-5 h-5" /> The Advocate
                                                </CardTitle>
                                            </CardHeader>
                                            <CardContent className="text-emerald-700/90 dark:text-emerald-200/90 leading-relaxed text-sm">
                                                {tribunal_verdict.advocate_summary}
                                            </CardContent>
                                        </Card>
                                    </div>

                                    {/* TRAJECTORY */}
                                    <Card className="bg-card border-border">
                                        <CardHeader>
                                            <CardTitle className="text-muted-foreground text-sm uppercase tracking-wider">Career Trajectory</CardTitle>
                                        </CardHeader>
                                        <CardContent>
                                            <div className="flex items-center gap-4">
                                                <Badge variant="secondary" className="bg-primary/10 text-primary border-primary/20 capitalize text-lg px-4 py-1">
                                                    {tribunal_verdict.trajectory_analysis?.direction}
                                                </Badge>
                                                <p className="text-muted-foreground text-sm italic">
                                                    "{tribunal_verdict.trajectory_analysis?.reasoning}"
                                                </p>
                                            </div>
                                        </CardContent>
                                    </Card>

                                    {/* FLAGS & STRENGTHS */}
                                    <div className="grid grid-cols-1 md:grid-cols-2 gap-6 text-sm">
                                        <div className="space-y-3">
                                            <h4 className="text-xs font-bold text-muted-foreground uppercase">Risk Signals</h4>
                                            {tribunal_verdict.consensus_flags?.length === 0 && <span className="text-muted-foreground block italic">None detected</span>}
                                            {tribunal_verdict.consensus_flags?.map((flag: any, i: number) => (
                                                <div key={i} className="bg-rose-50 dark:bg-rose-950/20 border border-rose-200 dark:border-rose-500/10 p-3 rounded-md flex gap-3 text-rose-700 dark:text-rose-300">
                                                    <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
                                                    <div>
                                                        <div className="font-semibold capitalize mb-1">{flag.type.replace(/_/g, " ")}</div>
                                                        <div className="opacity-80 text-xs">"{flag.evidence_snippet}"</div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>

                                        <div className="space-y-3">
                                            <h4 className="text-xs font-bold text-muted-foreground uppercase">Growth Signals</h4>
                                            {tribunal_verdict.consensus_strengths?.length === 0 && <span className="text-muted-foreground block italic">None detected</span>}
                                            {tribunal_verdict.consensus_strengths?.map((strength: any, i: number) => (
                                                <div key={i} className="bg-emerald-50 dark:bg-emerald-950/20 border border-emerald-200 dark:border-emerald-500/10 p-3 rounded-md flex gap-3 text-emerald-700 dark:text-emerald-300">
                                                    <BadgeCheck className="w-4 h-4 shrink-0 mt-0.5" />
                                                    <div>
                                                        <div className="font-semibold capitalize mb-1">{strength.type.replace(/_/g, " ")}</div>
                                                        <div className="opacity-80 text-xs">"{strength.evidence_snippet}"</div>
                                                    </div>
                                                </div>
                                            ))}
                                        </div>
                                    </div>
                                </>
                            )}
                        </TabsContent>

                        {/* SKILLS TAB */}
                        <TabsContent value="skills" className="mt-0 grid grid-cols-1 lg:grid-cols-2 gap-8">
                            <div className="space-y-4">
                                <h3 className="text-sm font-bold text-muted-foreground uppercase tracking-wider mb-4 border-b border-border pb-2">
                                    Required Skills
                                </h3>
                                {requiredSkills.length === 0 && <p className="text-muted-foreground italic">No required skills listed.</p>}
                                {requiredSkills.map((trace: any) => (
                                    <SkillRow
                                        key={trace.skill_slug}
                                        slug={trace.skill_slug}
                                        score={trace.score}
                                        status={trace.status}
                                        priority="required"
                                        level={trace.seniority_level}
                                    />
                                ))}
                            </div>

                            <div className="space-y-4">
                                <h3 className="text-sm font-bold text-muted-foreground uppercase tracking-wider mb-4 border-b border-border pb-2">
                                    Preferred Skills
                                </h3>
                                {preferredSkills.length === 0 && <p className="text-muted-foreground italic">No preferred skills listed.</p>}
                                {preferredSkills.map((trace: any) => (
                                    <SkillRow
                                        key={trace.skill_slug}
                                        slug={trace.skill_slug}
                                        score={trace.score}
                                        status={trace.status}
                                        priority="preferred"
                                        level={trace.seniority_level}
                                    />
                                ))}
                            </div>
                        </TabsContent>

                        {/* FACTORS TAB */}
                        <TabsContent value="factors" className="mt-0 space-y-4">
                            {analysis?.map((sec: any, i: number) => (
                                <Card key={i} className="bg-card border-border">
                                    <CardHeader className="py-4">
                                        <div className="flex items-center justify-between">
                                            <CardTitle className="text-base text-card-foreground">{sec.title}</CardTitle>
                                            <Badge variant="outline" className={cn("capitalize",
                                                sec.status === "met" ? "border-emerald-500 text-emerald-600 dark:text-emerald-500" :
                                                    sec.status === "not_met" ? "border-rose-500 text-rose-600 dark:text-rose-500" :
                                                        "border-amber-500 text-amber-600 dark:text-amber-500"
                                            )}>
                                                {sec.status.replace(/_/g, " ")}
                                            </Badge>
                                        </div>
                                        <CardDescription className="text-muted-foreground">{sec.summary}</CardDescription>
                                    </CardHeader>
                                    {sec.details?.length > 0 && (
                                        <CardContent className="pt-0 pb-4">
                                            <ul className="list-disc list-inside text-sm text-muted-foreground space-y-1">
                                                {sec.details.map((d: string, idx: number) => (
                                                    <li key={idx}>{d}</li>
                                                ))}
                                            </ul>
                                        </CardContent>
                                    )}
                                </Card>
                            ))}

                            {!analysis && <p className="text-muted-foreground italic">No structured factor analysis available.</p>}
                        </TabsContent>
                    </div>
                </ScrollArea>
            </Tabs>
        </div>
    );
}
