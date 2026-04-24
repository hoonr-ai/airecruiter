"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { MessageSquare, ClipboardCheck, CheckCircle2, Loader2 } from "lucide-react";
import { API_BASE } from "@/lib/api";

interface Candidate {
    id: string;
    name: string;
    skills: string[];
    missing: string[];
}

interface CandidateListProps {
    candidates: Candidate[];
}

export function CandidateList({ candidates }: CandidateListProps) {
    // Categorize candidates by skills match
    const perfectFit = candidates.filter((c) => c.missing.length === 0);
    const stretch = candidates.filter((c) => c.missing.length > 0 && c.missing.length <= 2);
    const pastApplicants = candidates.filter((c) => c.missing.length > 2);

    const CandidateCard = ({ candidate }: { candidate: Candidate }) => {
        const [engaging, setEngaging] = useState(false);
        const [assessing, setAssessing] = useState(false);
        const [engaged, setEngaged] = useState(false);
        const [assessed, setAssessed] = useState(false);

        const handleEngage = async () => {
            setEngaging(true);
            try {
                const res = await fetch(`${API_BASE}/candidates/${candidate.id}/engage`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ candidate_id: candidate.id })
                });
                if (res.ok) setEngaged(true);
            } catch (e) {
                console.error("Engage failed", e);
            } finally {
                setEngaging(false);
            }
        };

        const handleAssess = async () => {
            setAssessing(true);
            try {
                const res = await fetch(`${API_BASE}/candidates/${candidate.id}/assess`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ candidate_id: candidate.id })
                });
                if (res.ok) setAssessed(true);
            } catch (e) {
                console.error("Assess failed", e);
            } finally {
                setAssessing(false);
            }
        };

        return (
            <Card className="mb-4">
                <CardHeader className="pb-2">
                    <div className="flex justify-between items-start">
                        <div>
                            <CardTitle>{candidate.name}</CardTitle>
                            <CardDescription>{candidate.missing.length === 0 ? 'Perfect Match' : `${candidate.missing.length} skills missing`}</CardDescription>
                        </div>
                        <Badge variant={candidate.missing.length === 0 ? "default" : "secondary"}>
                            {candidate.missing.length === 0 ? 'Perfect Fit' : candidate.missing.length <= 2 ? 'Good Match' : 'Stretch'}
                        </Badge>
                    </div>
                </CardHeader>
                <CardContent>
                    <div className="flex flex-wrap gap-2 mb-2">
                        {candidate.skills.map((skill) => (
                            <Badge key={skill} variant="outline" className="bg-green-50 text-green-700 border-green-200">
                                {skill}
                            </Badge>
                        ))}
                    </div>
                    {candidate.missing.length > 0 && (
                        <div className="flex flex-wrap gap-2 mb-4">
                            <span className="text-xs text-muted-foreground self-center">Missing:</span>
                            {candidate.missing.map((skill) => (
                                <Badge key={skill} variant="outline" className="bg-red-50 text-red-700 border-red-200">
                                    {skill}
                                </Badge>
                            ))}
                        </div>
                    )}

                    {/* Actions Footer */}
                    <div className="flex gap-3 pt-2 mt-2 border-t border-border/40">
                        <Button
                            size="sm"
                            variant={engaged ? "outline" : "default"}
                            className={`flex-1 ${engaged ? "text-green-600 border-green-200 bg-green-50" : "bg-blue-600 hover:bg-blue-700"}`}
                            onClick={handleEngage}
                            disabled={engaging || engaged}
                        >
                            {engaging ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> :
                                engaged ? <CheckCircle2 className="mr-2 h-4 w-4" /> :
                                    <MessageSquare className="mr-2 h-4 w-4" />}
                            {engaged ? "Reachout Sent" : "Engage"}
                        </Button>

                        <Button
                            size="sm"
                            variant={assessed ? "outline" : "secondary"}
                            className={`flex-1 ${assessed ? "text-purple-600 border-purple-200 bg-purple-50" : ""}`}
                            onClick={handleAssess}
                            disabled={assessing || assessed}
                        >
                            {assessing ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> :
                                assessed ? <CheckCircle2 className="mr-2 h-4 w-4" /> :
                                    <ClipboardCheck className="mr-2 h-4 w-4" />}
                            {assessed ? "Assessed" : "Assess"}
                        </Button>
                    </div>
                </CardContent>
            </Card>
        );
    };

    return (
        <Tabs defaultValue="perfect" className="w-full">
            <TabsList className="grid w-full grid-cols-3">
                <TabsTrigger value="perfect">Perfect Fit ({perfectFit.length})</TabsTrigger>
                <TabsTrigger value="stretch">Stretch ({stretch.length})</TabsTrigger>
                <TabsTrigger value="past">Past Applicants ({pastApplicants.length})</TabsTrigger>
            </TabsList>
            <TabsContent value="perfect" className="mt-4">
                {perfectFit.map((c) => (
                    <CandidateCard key={c.id} candidate={c} />
                ))}
                {perfectFit.length === 0 && <p className="text-muted-foreground text-center py-8">No perfect matches yet.</p>}
            </TabsContent>
            <TabsContent value="stretch" className="mt-4">
                {stretch.map((c) => (
                    <CandidateCard key={c.id} candidate={c} />
                ))}
                {stretch.length === 0 && <p className="text-muted-foreground text-center py-8">No stretch candidates found.</p>}
            </TabsContent>
            <TabsContent value="past" className="mt-4">
                {pastApplicants.map((c) => (
                    <CandidateCard key={c.id} candidate={c} />
                ))}
                {pastApplicants.length === 0 && <p className="text-muted-foreground text-center py-8">No past applicants relevant.</p>}
            </TabsContent>
        </Tabs>
    );
}
