"use client";

import { useState } from "react";
import { DropZone } from "@/components/drop-zone";
import { CandidateList } from "@/components/candidate-list";

export default function DashboardPage() {
  const [candidates, setCandidates] = useState<any[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);

  const handleUpload = async (file: File) => {
    setIsProcessing(true);
    console.log("Uploading:", file.name);

    // Mock API delay and response
    setTimeout(() => {
      const mockCandidates = [
        {
          id: "1",
          name: "Alice Johnson",
          matchScore: 95,
          skills: ["React", "TypeScript", "Next.js", "Tailwind"],
          missing: [],
        },
        {
          id: "2",
          name: "Bob Smith",
          matchScore: 85,
          skills: ["React", "JavaScript", "CSS"],
          missing: ["TypeScript", "Next.js"],
        },
        {
          id: "3",
          name: "Charlie Brown",
          matchScore: 60,
          skills: ["Python", "Django"],
          missing: ["React", "TypeScript"], // Completely different stack
        },
      ];
      setCandidates(mockCandidates);
      setIsProcessing(false);
    }, 1500);
  };

  return (
    <div className="container mx-auto py-10 px-4 max-w-5xl">
      <div className="mb-8 text-center">
        <h1 className="text-3xl font-bold tracking-tight mb-2">Hoonr.ai Smart Screen</h1>
        <p className="text-muted-foreground">
          Upload a Job Description to instantly match against our talent pool.
        </p>
      </div>

      <div className="grid gap-8 md:grid-cols-[1fr_2fr]">
        <div>
          <h2 className="text-xl font-semibold mb-4">Ingestion</h2>
          <DropZone onUpload={handleUpload} />
          {isProcessing && (
            <div className="mt-4 text-center text-sm text-muted-foreground animate-pulse">
              Parsing JD and Matching Candidates...
            </div>
          )}
        </div>

        <div>
          <h2 className="text-xl font-semibold mb-4">Matched Candidates</h2>
          {candidates.length > 0 ? (
            <CandidateList candidates={candidates} />
          ) : (
            <div className="border border-dashed rounded-lg h-64 flex items-center justify-center text-muted-foreground">
              {isProcessing ? "Crunching numbers..." : "Upload a JD to see matches"}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
