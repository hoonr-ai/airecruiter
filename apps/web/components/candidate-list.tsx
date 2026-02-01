"use client";

import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Badge } from "@/components/ui/badge";

interface Candidate {
    id: string;
    name: string;
    matchScore: number;
    skills: string[];
    missing: string[];
}

interface CandidateListProps {
    candidates: Candidate[];
}

export function CandidateList({ candidates }: CandidateListProps) {
    // Mock logic to categorize candidates
    const perfectFit = candidates.filter((c) => c.matchScore >= 90);
    const stretch = candidates.filter((c) => c.matchScore >= 70 && c.matchScore < 90);
    const pastApplicants = candidates.filter((c) => c.matchScore < 70); // Just for demo logic

    const CandidateCard = ({ candidate }: { candidate: Candidate }) => (
        <Card className="mb-4">
            <CardHeader className="pb-2">
                <div className="flex justify-between items-start">
                    <div>
                        <CardTitle>{candidate.name}</CardTitle>
                        <CardDescription>Match Score: {candidate.matchScore}%</CardDescription>
                    </div>
                    <Badge variant={candidate.matchScore >= 90 ? "default" : "secondary"}>
                        {candidate.matchScore}% Match
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
                    <div className="flex flex-wrap gap-2">
                        <span className="text-xs text-muted-foreground self-center">Missing:</span>
                        {candidate.missing.map((skill) => (
                            <Badge key={skill} variant="outline" className="bg-red-50 text-red-700 border-red-200">
                                {skill}
                            </Badge>
                        ))}
                    </div>
                )}
            </CardContent>
        </Card>
    );

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
