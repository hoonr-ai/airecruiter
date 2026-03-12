"use client";


import { cn } from "@/lib/utils";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Download, Loader2, Sparkles, Wand2, Search, CheckCircle, AlertTriangle, ShieldAlert, Eye, Gavel, RefreshCw, RotateCcw, Copy, Plus, Trash2, ChevronRight, CheckCircle2, XCircle, Globe, LayoutGrid } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import { CandidateTable } from "@/components/candidate-table";
import { AnalysisCard } from "@/components/analysis/analysis-card";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import {
    Dialog,
    DialogContent,
    DialogDescription,
    DialogHeader,
    DialogTitle,
} from "@/components/ui/dialog";


// Helper to highlight text
const HighlightText = ({ text, keywords }: { text: string, keywords: string[] }) => {
    if (!keywords.length || !text) return <>{text}</>;


    // Create regex pattern from keywords (escape special chars)
    const pattern = new RegExp(`(${keywords.map(k => k.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`, 'gi');


    // Split text
    const parts = text.split(pattern);


    return (
        <>
            {parts.map((part, i) => {
                const isMatch = keywords.some(k => k.toLowerCase() === part.toLowerCase());
                return isMatch ? (
                    <mark key={i} className="bg-yellow-200 dark:bg-yellow-900/50 text-inherit font-semibold rounded-sm px-0.5">
                        {part}
                    </mark>
                ) : (
                    <span key={i}>{part}</span>
                );
            })}
        </>
    );
};




// Helper component to format AI-generated postings with rich text copying support
const AIPostingJobDescription = ({ text }: { text: string }) => {
    const renderInline = (content: string) => {
        // Parse [text](url), **bold** and *italic*
        const parts = content.split(/(\[.*?\]\(.*?\)+|\*\*.*?\*\*|\*(?!\*).*?\*(?!\*))/g);
        return parts.map((part, i) => {
            if (part.startsWith('[') && part.includes('](') && part.endsWith(')')) {
                const match = part.match(/\[(.*?)\]\((.*?)\)/);
                if (match) {
                    return (
                        <a
                            key={i}
                            href={match[2]}
                            target="_blank"
                            rel="noopener noreferrer"
                            className="text-blue-600 hover:underline font-medium"
                        >
                            {match[1]}
                        </a>
                    );
                }
            }
            if (part.startsWith('**') && part.endsWith('**')) {
                return <strong key={i} style={{ fontWeight: 600 }}>{part.slice(2, -2)}</strong>;
            } else if (part.startsWith('*') && part.endsWith('*')) {
                return <em key={i} style={{ fontStyle: 'italic' }}>{part.slice(1, -1)}</em>;
            }
            return <span key={i}>{part}</span>;
        });
    };

    const formatText = (rawText: string) => {
        if (!rawText) return null;

        // Split by lines and process each
        return rawText.split('\n').map((line, index) => {
            const trimmedLine = line.trim();
            if (!trimmedLine) return <br key={index} />;

            // Check if this is a section header (All caps line or starts with bold all caps)
            const isHeader = /^[A-Z\s]+$/.test(trimmedLine) || /^\*\*[A-Z\s]+\*\*$/.test(trimmedLine);
            if (isHeader) {
                const sectionTitle = trimmedLine.replace(/\*\*/g, '').trim();
                return (
                    <div key={index} style={{ fontWeight: 600, marginTop: '1rem', marginBottom: '0.5rem' }}>
                        {sectionTitle}
                    </div>
                );
            }

            // Check if this is a bullet point
            if (trimmedLine.startsWith('•') || trimmedLine.startsWith('-')) {
                const bulletText = trimmedLine.replace(/^[•-]\s*/, '').trim();
                return (
                    <div key={index} style={{ display: 'flex', gap: '0.5rem', marginLeft: '1rem', marginTop: '0.25rem', marginBottom: '0.25rem' }}>
                        <span>•</span>
                        <span>{renderInline(bulletText)}</span>
                    </div>
                );
            }

            // Regular text line
            return (
                <div key={index} style={{ marginBottom: '0.25rem' }}>
                    {renderInline(trimmedLine)}
                </div>
            );
        });
    };

    return (
        <div style={{ fontFamily: 'inherit', fontSize: '14px', lineHeight: 1.5 }}>
            {formatText(text)}
        </div>
    );
};

const JOB_BOARDS = [
    { id: "skip", name: "Skip External Posting" },
    { id: "careerbuilder", name: "CareerBuilder" },
    { id: "dice", name: "Dice" },
    { id: "monster", name: "Monster" },
    { id: "indeed", name: "Indeed" },
    { id: "linkedin", name: "LinkedIn" },
];


export default function CreateJobPage() {
    const [step, setStep] = useState(1);
    const [loading, setLoading] = useState(false);
    const [jdText, setJdText] = useState("");
    const [jobId, setJobId] = useState("");

    // Imported Metadata (JobDiva)
    const [customerName, setCustomerName] = useState("");
    const [jobStatus, setJobStatus] = useState("");
    const [jobNotes, setJobNotes] = useState("");
    const [workAuthorization, setWorkAuthorization] = useState("");
    const [recruiterEmails, setRecruiterEmails] = useState("");
    const [aiDescription, setAiDescription] = useState("");
    const [selectedBoards, setSelectedBoards] = useState<string[]>([]);
    const [isGenerating, setIsGenerating] = useState(false);


    // Parsed Data
    const [title, setTitle] = useState("");
    const [hardSkills, setHardSkills] = useState<any[]>([]); // Array of { name, seniority, priority }
    const [softSkills, setSoftSkills] = useState<string[]>([]);
    const [locationType, setLocationType] = useState("Onsite");
    const [location, setLocation] = useState(""); // Specific location like City, State
    const [sourceFilters, setSourceFilters] = useState({ vetted: true, jobdiva: true, linkedin: false }); // Source filters
    const [openToWork, setOpenToWork] = useState(false);
    const [candidates, setCandidates] = useState<any[]>([]);


    // Resume Viewing State
    const [viewingCandidate, setViewingCandidate] = useState<any>(null);
    const [resumeText, setResumeText] = useState("");
    const [resumeLoading, setResumeLoading] = useState(false);
    const [resumeOpen, setResumeOpen] = useState(false);


    // AI Analysis State
    const [selectedIds, setSelectedIds] = useState<string[]>([]);
    const [analysisLoading, setAnalysisLoading] = useState(false);
    const [analysisResults, setAnalysisResults] = useState<any[]>([]);
    const [showAnalysis, setShowAnalysis] = useState(false);
    const [viewingAnalysis, setViewingAnalysis] = useState<any>(null); // For Modal
    const [emailError, setEmailError] = useState(false);


    // Unified email validation helper with progressive feedback
    const getEmailValidationStatus = (input: string) => {
        if (!input || input.trim() === "") return { status: 'empty', message: 'Recruiter email field is required' };

        const emails = input.split(",").map(e => e.trim());
        // Robust regex: TLD must be 2-6 alpha chars, domain must exist
        const emailRegex = /^[^\s@]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,6}$/;

        // Progressive check for the first email to provide specific feedback
        const firstEmail = emails[0];
        if (!firstEmail.includes("@")) return { status: 'invalid', message: 'The @ symbol is missing.' };

        const atParts = firstEmail.split("@");
        const domain = atParts[1];
        if (!domain || domain.trim() === "") return { status: 'invalid', message: 'Domain name is missing.' };

        const domainParts = domain.split(".");
        const tld = domainParts[domainParts.length - 1];
        // Must have at least one dot, domain part (before last dot) must be non-empty, TLD must be 2+ alpha chars
        const domainBody = domainParts.slice(0, -1).join('.');
        if (domainParts.length < 2 || !domainBody || !/^[a-zA-Z]{2,6}$/.test(tld)) {
            return { status: 'invalid', message: 'Suffix is missing or invalid (e.g. .com, .org).' };
        }

        const allValid = emails.every(e => emailRegex.test(e));
        return allValid ? { status: 'valid', message: 'Valid' } : { status: 'invalid', message: 'Please enter a valid email address.' };
    };

    const validateEmails = (input: string) => {
        return getEmailValidationStatus(input).status === 'valid';
    };


    const handleAnalyze = async () => {
        if (selectedIds.length === 0) return;
        setAnalysisLoading(true);
        setShowAnalysis(true);


        // Filter candidates to just selected ones
        const selectedCandidates = candidates.filter(c => selectedIds.includes(c.id));


        // Construct Structured JD (TOON-ready)
        const structuredJd = {
            title: title,
            location: location,
            location_type: locationType,
            hard_skills: hardSkills,
            soft_skills: softSkills,
            summary: jdText
        };


        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/candidates/analyze`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    job_description: jdText || "Using Structured JD",
                    structured_jd: structuredJd,
                    candidates: selectedCandidates
                })
            });
            const data = await res.json();
            setAnalysisResults(data.results || []);
        } catch (e) {
            console.error("Analysis failed", e);
            alert("Analysis failed");
        }
        setAnalysisLoading(false);
    };


    const handleFetchJob = async () => {
        if (!jobId) return;
        setLoading(true);
        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/jobs/fetch`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ job_id: jobId })
            });
            if (res.ok) {
                const data = await res.json();
                setJdText(data.description);
                setTitle(data.title || "");
                setCustomerName(data.customer_name || data.company || "Unknown");
                setJobStatus(data.job_status || "OPEN");

                // RESTORE AI CONTENT FROM UDFs
                if (data.ai_description) setAiDescription(data.ai_description);
                if (data.job_notes) setJobNotes(data.job_notes);

                if (data.city) setLocation(`${data.city}, ${data.state || ""}`);

                // Auto-parse logic could be added here if desired.
            } else {
                alert("No job found");
            }
        } catch (e) {
            console.error(e);
            alert("Error fetching job");
        }
        setLoading(false);
    };


    const handleUpdateDescription = async () => {
        const validation = getEmailValidationStatus(recruiterEmails);
        if (validation.status !== 'valid') {
            setEmailError(true);
            const msg = validation.status === 'empty'
                ? "Recruiter Email field is required."
                : validation.message;
            alert(msg);
            return;
        }
        setEmailError(false);
        if (!jdText && !jobNotes) return;
        setIsGenerating(true);
        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/api/v1/gemini/jobs/${jobId || 'new'}/generate-description`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    jobTitle: title || "Job Opportunity",
                    jobNotes: jobNotes,
                    workAuthorization: workAuthorization,
                    jobDescription: jdText
                })
            });
            if (res.ok) {
                const data = await res.json();
                setAiDescription(data.description);
            } else {
                alert("Failed to generate description");
            }
        } catch (e) {
            console.error(e);
            alert("Error generating description");
        }
        setIsGenerating(false);
    };


    const handleParse = async () => {
        const validation = getEmailValidationStatus(recruiterEmails);
        if (validation.status !== 'valid') {
            setEmailError(true);
            const msg = validation.status === 'empty'
                ? "Recruiter Email field is required."
                : validation.message;
            alert(msg);
            return;
        }
        setEmailError(false);
        setLoading(true);
        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';

            // --- Sync AI JD + Notes to JobDiva UDF #230 / #231 and track locally ---
            if (jobId && (aiDescription || jobNotes)) {
                console.log("🔄 Syncing AI JD & Job Notes to JobDiva...");
                try {
                    const syncRes = await fetch(`${apiUrl}/api/v1/gemini/sync-jobdiva`, {
                        method: "POST",
                        headers: { "Content-Type": "application/json" },
                        body: JSON.stringify({
                            jobId,
                            aiDescription,
                            jobNotes,
                        }),
                    });
                    const syncData = await syncRes.json();
                    console.log("✅ Sync result:", syncData);
                } catch (syncErr) {
                    console.error("❌ Sync failed (non-blocking):", syncErr);
                }
            }

            // --- Parse JD for skills / structured data ---
            const res = await fetch(`${apiUrl}/jobs/parse`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ text: jdText })
            });
            const data = await res.json();
            setTitle(data.title);
            setHardSkills(data.hard_skills || []);
            setSoftSkills(data.soft_skills || []);
            if (data.location_type) setLocationType(data.location_type);
            if (data.location) setLocation(data.location);
            setStep(2);
        } catch (e) {
            console.error(e);
            alert("Failed to parse JD");
        }
        setLoading(false);
    };


    const handleSearch = async () => {
        setLoading(true);
        try {
            // Build sources array based on checkboxes
            const sources = [];
            if (sourceFilters.vetted) sources.push("VettedDB");
            if (sourceFilters.jobdiva) sources.push("JobDiva");
            if (sourceFilters.linkedin) sources.push("LinkedIn");


            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/candidates/search`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    skills: hardSkills,
                    location: location, // Send actual location field
                    location_type: locationType, // Send location type separately 
                    sources: sources, // Filter by source
                    open_to_work: openToWork, // Pass OpenToWork filter
                    page: 1,      // Added pagination
                    limit: 100    // Max 100 per page response
                })
            });
            const data = await res.json();
            if (Array.isArray(data)) {
                setCandidates(data);
            } else {
                setCandidates([]);
            }
            setStep(3);
        } catch (e) {
            console.error(e);
            alert("Search failed");
            setCandidates([]);
        }
        setLoading(false);
    };


    const handleViewResume = async (candidate: any) => {
        setViewingCandidate(candidate);
        setResumeOpen(true);
        setResumeLoading(true);
        setResumeText("");


        try {
            const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
            const res = await fetch(`${apiUrl}/candidates/${candidate.id}/resume`);
            if (res.ok) {
                const data = await res.json();
                setResumeText(data.resume_text);
            } else {
                setResumeText("Failed to load resume.");
            }
        } catch (e) {
            setResumeText("Error fetching resume.");
        }
        setResumeLoading(false);
    };


    // Helper to update a specific skill
    const updateSkill = (index: number, field: string, value: string) => {
        const updated = [...(hardSkills || [])];
        updated[index] = { ...updated[index], [field]: value };
        setHardSkills(updated);
    };


    const removeSkill = (index: number) => {
        const updated = (hardSkills || []).filter((_, i) => i !== index);
        setHardSkills(updated);
    };


    const addSkill = () => {
        setHardSkills([...(hardSkills || []), { name: "New Skill", seniority: "Mid", priority: "Must Have" }]);
    }


    return (
        <div className="container mx-auto py-10">
            <div>
                <h1 className="text-3xl font-bold tracking-tight bg-hoonr-gradient text-transparent bg-clip-text inline-block">Create New Job</h1>
                <p className="text-muted-foreground mt-1">AI-Assisted Job Creation & Candidate Matching</p>
            </div>


            {/* Progress */}
            <div className="flex items-center gap-4 text-sm">
                <div className={`px-4 py-2 rounded-full border ${step >= 1 ? 'bg-primary/10 border-primary text-primary' : 'bg-muted'}`}>1. Input JD</div>
                <div className="w-8 h-px bg-border"></div>
                <div className={`px-4 py-2 rounded-full border ${step >= 2 ? 'bg-primary/10 border-primary text-primary' : 'bg-muted'}`}>2. Review Criteria</div>
                <div className="w-8 h-px bg-border"></div>
                <div className={`px-4 py-2 rounded-full border ${step >= 3 ? 'bg-primary/10 border-primary text-primary' : 'bg-muted'}`}>3. Candidates</div>
            </div>


            {/* Step 1: Input */}
            {step === 1 && (
                <Card>
                    <CardHeader>
                        <CardTitle>Job Description</CardTitle>
                        <CardDescription>All jobs must be imported from JobDiva. Enter the Job ID below to begin.</CardDescription>
                    </CardHeader>
                    <CardContent className="space-y-4">
                        <div className="space-y-4">
                            <div className="flex gap-4 mb-4">
                                <Input
                                    placeholder="Enter Job ID (e.g. 26-12345)"
                                    value={jobId}
                                    onChange={(e) => setJobId(e.target.value)}
                                />
                                <Button onClick={handleFetchJob} disabled={!jobId || loading} variant="outline">
                                    {loading && <Loader2 className="mr-2 h-4 w-4 animate-spin" />}
                                    <Download className="mr-2 h-4 w-4" /> Import
                                </Button>
                            </div>

                            {title && (
                                <div className="grid grid-cols-2 gap-4 mb-4 p-4 border rounded-lg bg-muted/40">
                                    <div className="space-y-2">
                                        <Label>Job Title</Label>
                                        <Input value={title} readOnly className="bg-muted text-muted-foreground cursor-not-allowed" />
                                    </div>
                                    <div className="space-y-2">
                                        <Label>Customer Name</Label>
                                        <Input value={customerName} readOnly className="bg-muted text-muted-foreground cursor-not-allowed" />
                                    </div>
                                    <div className="space-y-2">
                                        <Label>Job Status</Label>
                                        <div className="flex items-center gap-2">
                                            <Input
                                                value={jobStatus.charAt(0).toUpperCase() + jobStatus.slice(1).toLowerCase()}
                                                readOnly
                                                className="bg-muted text-muted-foreground cursor-not-allowed flex-1"
                                            />
                                            <Button
                                                variant="outline"
                                                size="sm"
                                                onClick={handleFetchJob}
                                                title="Refresh status from JobDiva"
                                            >
                                                <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
                                            </Button>
                                        </div>
                                        <p className="text-xs text-muted-foreground">Status updates automatically from JobDiva every 5 minutes.</p>
                                    </div>
                                    <div className="space-y-2">
                                        <Label className="flex items-center justify-between">
                                            <span>Recruiter Email <span className="text-destructive">*</span></span>
                                            {recruiterEmails && (
                                                <span className="flex items-center gap-1 text-[10px] font-medium uppercase tracking-wider">
                                                    {getEmailValidationStatus(recruiterEmails).status === 'valid' ? (
                                                        <>
                                                            <CheckCircle2 className="w-3 h-3 text-green-500" />
                                                            <span className="text-green-600">Valid</span>
                                                        </>
                                                    ) : (
                                                        <>
                                                            <XCircle className="w-3 h-3 text-destructive" />
                                                            <span className="text-destructive">Invalid</span>
                                                        </>
                                                    )}
                                                </span>
                                            )}
                                        </Label>
                                        <div className="relative">
                                            <Input
                                                value={recruiterEmails}
                                                onChange={(e) => {
                                                    const newVal = e.target.value;
                                                    setRecruiterEmails(newVal);
                                                    // Immediate validation check
                                                    if (getEmailValidationStatus(newVal).status === 'valid') {
                                                        setEmailError(false);
                                                    }
                                                }}
                                                placeholder="recruiter@example.com"
                                                className={cn(
                                                    "transition-all duration-200",
                                                    recruiterEmails && getEmailValidationStatus(recruiterEmails).status === 'invalid' && "border-destructive focus-visible:ring-destructive bg-destructive/5",
                                                    recruiterEmails && getEmailValidationStatus(recruiterEmails).status === 'valid' && "border-green-500/50 focus-visible:ring-green-500 bg-green-50/30"
                                                )}
                                            />
                                        </div>
                                        {getEmailValidationStatus(recruiterEmails).status === 'invalid' && (
                                            <p className="text-[11px] font-medium text-destructive transition-colors">
                                                {getEmailValidationStatus(recruiterEmails).message}
                                            </p>
                                        )}
                                    </div>
                                    <div className="space-y-2 col-span-2">
                                        <Label>Job Notes</Label>
                                        <Textarea
                                            value={jobNotes}
                                            onChange={(e) => setJobNotes(e.target.value)}
                                            placeholder="Enter your notes here..."
                                            className="min-h-[80px]"
                                        />
                                        <p className="text-xs text-muted-foreground">Add hiring manager notes, intake call notes, or any important job-related information.</p>
                                    </div>
                                    <div className="space-y-2 col-span-2">
                                        <Label>Work Authorization</Label>
                                        <Input
                                            value={workAuthorization}
                                            onChange={(e) => setWorkAuthorization(e.target.value)}
                                            placeholder="e.g. US Citizen, Green Card, H1B, Any"
                                        />
                                        <p className="text-xs text-muted-foreground">Specify required work authorization for this role.</p>
                                    </div>
                                </div>
                            )}
                        </div>

                        {jdText && (
                            <div className="space-y-2 pt-4 border-t">
                                <Label>JobDiva Original Description</Label>
                                <div className="rounded-md border bg-muted/20 p-4 max-h-60 overflow-y-auto text-sm whitespace-pre-wrap">
                                    {jdText}
                                </div>
                                <p className="text-xs text-muted-foreground italic">Raw description imported from JobDiva.</p>
                            </div>
                        )}

                        {aiDescription && (
                            <div className="mt-8 space-y-4 animate-in fade-in slide-in-from-bottom-4 duration-700">
                                <div className="flex items-center justify-between">
                                    <div className="flex items-center gap-2">
                                        <Wand2 className="w-5 h-5 text-foreground" />
                                        <div>
                                            <h3 className="font-semibold text-base">AI Generated Posting Description</h3>
                                        </div>
                                    </div>
                                    <div className="flex gap-2">
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            className="h-9"
                                            onClick={handleUpdateDescription}
                                            disabled={isGenerating}
                                        >
                                            <RotateCcw className={`h-4 w-4 mr-2 ${isGenerating ? 'animate-spin' : ''}`} />
                                            Regenerate
                                        </Button>
                                        <Button
                                            variant="outline"
                                            size="sm"
                                            className="h-9"
                                            onClick={async () => {
                                                try {
                                                    const contentNode = document.getElementById('ai-jd-content');
                                                    if (contentNode && window.ClipboardItem) {
                                                        const htmlBlob = new Blob([contentNode.innerHTML], { type: 'text/html' });
                                                        const textBlob = new Blob([aiDescription], { type: 'text/plain' });
                                                        await navigator.clipboard.write([
                                                            new window.ClipboardItem({
                                                                'text/html': htmlBlob,
                                                                'text/plain': textBlob
                                                            })
                                                        ]);
                                                        alert("Copied to clipboard with formatting!");
                                                    } else {
                                                        throw new Error("Fallback");
                                                    }
                                                } catch (err) {
                                                    navigator.clipboard.writeText(aiDescription);
                                                    alert("Copied to clipboard as plain text!");
                                                }
                                            }}
                                        >
                                            <Copy className="h-4 w-4 mr-2" />
                                            Copy
                                        </Button>
                                    </div>
                                </div>

                                <div className="rounded-xl border border-border bg-background shadow-sm overflow-hidden" id="ai-jd-content">
                                    <div className="px-6 py-6 text-sm text-foreground">
                                        <AIPostingJobDescription text={aiDescription} />
                                    </div>
                                </div>

                                <div className="mt-8 space-y-5 pt-8 border-t border-dashed">
                                    <div className="flex items-center gap-2">
                                        <Globe className="w-5 h-5 text-muted-foreground mr-1" />
                                        <div>
                                            <h4 className="font-bold text-base text-foreground">External Job Board Selection</h4>
                                            <p className="text-xs text-muted-foreground italic">Select where you'd like to post this job.</p>
                                        </div>
                                    </div>
                                    <div className="grid grid-cols-2 lg:grid-cols-3 gap-3">
                                        {JOB_BOARDS.map((board) => {
                                            const isSkip = board.id === "skip";
                                            const skipSelected = selectedBoards.includes("skip");
                                            const isSelected = selectedBoards.includes(board.id);
                                            const isDisabled = !isSkip && skipSelected;

                                            return (
                                                <div 
                                                    key={board.id} 
                                                    className={cn(
                                                        "group flex items-center space-x-3 rounded-xl border p-4 transition-all duration-200 select-none",
                                                        isSelected 
                                                            ? "border-primary bg-primary/5 shadow-sm" 
                                                            : "border-border hover:border-primary/40 hover:bg-muted/30",
                                                        isDisabled && "opacity-40 grayscale cursor-not-allowed border-dashed bg-muted/10"
                                                    )}
                                                    onClick={() => {
                                                        if (isDisabled) return;
                                                        if (isSkip) {
                                                            if (isSelected) {
                                                                setSelectedBoards([]);
                                                            } else {
                                                                setSelectedBoards(["skip"]);
                                                            }
                                                        } else {
                                                            if (isSelected) {
                                                                setSelectedBoards(selectedBoards.filter(b => b !== board.id));
                                                            } else {
                                                                setSelectedBoards([...selectedBoards, board.id]);
                                                            }
                                                        }
                                                    }}
                                                >
                                                    <div className={cn(
                                                        "flex h-5 w-5 items-center justify-center rounded border transition-colors",
                                                        isSelected ? "bg-primary border-primary" : "border-input group-hover:border-primary/50"
                                                    )}>
                                                        {isSelected && <CheckCircle2 className="h-3.5 w-3.5 text-primary-foreground" />}
                                                    </div>
                                                    <Label 
                                                        className={cn(
                                                            "text-sm font-semibold cursor-pointer transition-colors",
                                                            isSelected ? "text-primary" : "text-muted-foreground group-hover:text-foreground",
                                                            isDisabled && "cursor-not-allowed"
                                                        )}
                                                    >
                                                        {board.name}
                                                    </Label>
                                                </div>
                                            );
                                        })}
                                    </div>
                                    {selectedBoards.includes("skip") && (
                                        <div className="bg-amber-500/5 border border-amber-500/20 rounded-lg p-3 flex items-start gap-3 animate-in zoom-in-95 duration-200">
                                            <ShieldAlert className="w-4 h-4 text-amber-500 mt-0.5" />
                                            <div className="text-xs text-amber-700 font-medium">
                                                External job board posting is skipped. No outreach will be sent to the posting team.
                                            </div>
                                        </div>
                                    )}
                                </div>
                            </div>
                        )}

                        {!aiDescription ? (
                            <Button
                                onClick={handleUpdateDescription}
                                disabled={isGenerating || (!jdText && !jobNotes)}
                                className="w-full bg-purple-600 hover:bg-purple-700 text-white mt-8 h-12 shadow-lg shadow-purple-500/20 group relative overflow-hidden transition-all active:scale-[0.98]"
                            >
                                <div className="absolute inset-0 bg-gradient-to-r from-transparent via-white/10 to-transparent -translate-x-full group-hover:translate-x-full transition-transform duration-1000" />
                                {isGenerating ? (
                                    <Loader2 className="mr-2 h-5 w-5 animate-spin" />
                                ) : (
                                    <Wand2 className="mr-2 h-5 w-5 group-hover:rotate-12 transition-transform" />
                                )}
                                <span className="font-semibold text-base">Update Posting Description</span>
                            </Button>
                        ) : (
                            <Button
                                onClick={handleParse}
                                disabled={loading}
                                className="w-full bg-hoonr-gradient text-white mt-8 h-12 shadow-lg shadow-primary/20 transition-all active:scale-[0.98]"
                            >
                                {loading ? <Loader2 className="mr-2 h-5 w-5 animate-spin" /> : <Plus className="mr-2 h-5 w-5" />}
                                <span className="font-semibold text-base">Analyze & Find Candidates</span>
                            </Button>
                        )}
                    </CardContent>
                </Card >
            )
            }


            {/* Step 2: Review */}
            {
                step === 2 && (
                    <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
                        <div className="col-span-2 space-y-6">
                            <Card>
                                <CardHeader className="flex flex-row items-center justify-between">
                                    <div>
                                        <CardTitle>Skills & Requirements</CardTitle>
                                        <CardDescription>Refine the extraction.</CardDescription>
                                    </div>
                                    <Button size="sm" variant="outline" onClick={addSkill}><Plus className="h-4 w-4 mr-2" /> Add Skill</Button>
                                </CardHeader>
                                <CardContent>
                                    <Table>
                                        <TableHeader>
                                            <TableRow>
                                                <TableHead>Skill Name</TableHead>
                                                <TableHead className="w-32">Seniority</TableHead>
                                                <TableHead className="w-32">Priority</TableHead>
                                                <TableHead className="w-12"></TableHead>
                                            </TableRow>
                                        </TableHeader>
                                        <TableBody>
                                            {(hardSkills || []).map((skill, i) => (
                                                <TableRow key={i}>
                                                    <TableCell>
                                                        <Input
                                                            value={skill.name || ""}
                                                            onChange={(e) => updateSkill(i, 'name', e.target.value)}
                                                            className="h-8"
                                                        />
                                                    </TableCell>
                                                    <TableCell>
                                                        <Select value={skill.seniority || "Mid"} onValueChange={(v) => updateSkill(i, 'seniority', v)}>
                                                            <SelectTrigger className="h-8 text-xs">
                                                                <SelectValue />
                                                            </SelectTrigger>
                                                            <SelectContent>
                                                                <SelectItem value="Junior">Junior</SelectItem>
                                                                <SelectItem value="Mid">Mid</SelectItem>
                                                                <SelectItem value="Senior">Senior</SelectItem>
                                                            </SelectContent>
                                                        </Select>
                                                    </TableCell>
                                                    <TableCell>
                                                        <Select value={skill.priority || "Must Have"} onValueChange={(v) => updateSkill(i, 'priority', v)}>
                                                            <SelectTrigger className="h-8 text-xs">
                                                                <SelectValue />
                                                            </SelectTrigger>
                                                            <SelectContent>
                                                                <SelectItem value="Must Have">Must Have</SelectItem>
                                                                <SelectItem value="Flexible">Flexible</SelectItem>
                                                            </SelectContent>
                                                        </Select>
                                                    </TableCell>
                                                    <TableCell>
                                                        <Button variant="ghost" size="icon" className="h-8 w-8 text-muted-foreground hover:text-destructive" onClick={() => removeSkill(i)}>
                                                            <Trash2 className="h-4 w-4" />
                                                        </Button>
                                                    </TableCell>
                                                </TableRow>
                                            ))}
                                        </TableBody>
                                    </Table>
                                </CardContent>
                            </Card>
                        </div>


                        <div className="space-y-6">
                            <Card>
                                <CardHeader>
                                    <CardTitle>Job Details</CardTitle>
                                </CardHeader>
                                <CardContent className="space-y-4">
                                    <div className="space-y-2">
                                        <Label>Job Title</Label>
                                        <Input value={title} onChange={(e) => setTitle(e.target.value)} />
                                    </div>
                                    <div className="space-y-2">
                                        <Label>Location Type</Label>
                                        <Select value={locationType} onValueChange={setLocationType}>
                                            <SelectTrigger>
                                                <SelectValue />
                                            </SelectTrigger>
                                            <SelectContent>
                                                <SelectItem value="Onsite">Onsite</SelectItem>
                                                <SelectItem value="Hybrid">Hybrid</SelectItem>
                                                <SelectItem value="Remote">Remote</SelectItem>
                                            </SelectContent>
                                        </Select>
                                    </div>


                                    {/* New Location Input */}
                                    <div className="space-y-2">
                                        <Label>Location (City/State)</Label>
                                        <Input
                                            placeholder="e.g. New York, NY"
                                            value={location}
                                            onChange={(e) => setLocation(e.target.value)}
                                        />
                                    </div>


                                    {/* Source Filters */}
                                    <div className="space-y-4">
                                        <div className="space-y-2">
                                            <Label>Candidate Sources</Label>
                                            <div className="flex flex-col gap-2">
                                                <div className="flex gap-4">
                                                    <label className="flex items-center gap-2 cursor-pointer">
                                                        <input
                                                            type="checkbox"
                                                            checked={sourceFilters.vetted}
                                                            onChange={(e) => setSourceFilters({ ...sourceFilters, vetted: e.target.checked })}
                                                            className="w-4 h-4 accent-purple-600"
                                                        />
                                                        <span>Vetted DB</span>
                                                    </label>
                                                    <label className="flex items-center gap-2 cursor-pointer">
                                                        <input
                                                            type="checkbox"
                                                            checked={sourceFilters.jobdiva}
                                                            onChange={(e) => setSourceFilters({ ...sourceFilters, jobdiva: e.target.checked })}
                                                            className="w-4 h-4 accent-purple-600"
                                                        />
                                                        <span>JobDiva</span>
                                                    </label>
                                                    <label className="flex items-center gap-2 cursor-pointer">
                                                        <input
                                                            type="checkbox"
                                                            checked={sourceFilters.linkedin}
                                                            onChange={(e) => setSourceFilters({ ...sourceFilters, linkedin: e.target.checked })}
                                                            className="w-4 h-4 accent-purple-600"
                                                        />
                                                        <span>LinkedIn (Unipile)</span>
                                                    </label>
                                                </div>
                                            </div>

                                            {/* LinkedIn Config */}
                                            {sourceFilters.linkedin && (
                                                <div className="p-3 bg-blue-50 dark:bg-blue-900/10 rounded-md border border-blue-100 dark:border-blue-900/20">
                                                    <label className="flex items-center gap-2 cursor-pointer">
                                                        <input
                                                            type="checkbox"
                                                            checked={openToWork}
                                                            onChange={(e) => setOpenToWork(e.target.checked)}
                                                            className="w-4 h-4 accent-blue-600"
                                                        />
                                                        <span className="text-sm font-medium text-blue-700 dark:text-blue-300">
                                                            "Open to Work" Only
                                                        </span>
                                                    </label>
                                                    <p className="text-xs text-muted-foreground mt-1 ml-6">
                                                        Restricts search to candidates signaling they are open to new opportunities.
                                                    </p>
                                                </div>
                                            )}
                                        </div>
                                    </div>
                                </CardContent>
                            </Card>


                            <Card>
                                <CardHeader>
                                    <CardTitle>Soft Skills</CardTitle>
                                </CardHeader>
                                <CardContent>
                                    <div className="flex flex-wrap gap-2">
                                        {(softSkills || []).map((skill, i) => (
                                            <Badge key={i} variant="outline" className="text-xs">
                                                {skill}
                                            </Badge>
                                        ))}
                                    </div>
                                </CardContent>
                            </Card>




                        </div>
                    </div>
                )
            }


            {/* Step 3: Candidates */}
            {
                step === 3 && (
                    <div className="space-y-6">
                        <Card>
                            <CardHeader>
                                <CardTitle>Matched Candidates</CardTitle>
                                <CardDescription>Based on <strong>{title}</strong> requirements ({locationType}).</CardDescription>
                            </CardHeader>
                            <CardContent>
                                <CandidateTable
                                    candidates={candidates}
                                    onView={handleViewResume}
                                    selectedIds={selectedIds}
                                    onSelectionChange={setSelectedIds}
                                />
                            </CardContent>
                        </Card>
                        <div className="flex justify-between items-center">
                            <div className="text-sm text-muted-foreground">
                                {selectedIds.length} candidates selected
                            </div>
                            <div className="flex gap-2">
                                <Button variant="outline" onClick={() => setStep(2)}>Adjust Filters</Button>
                                <Button
                                    onClick={handleAnalyze}
                                    disabled={selectedIds.length === 0 || analysisLoading}
                                    className="bg-purple-600 hover:bg-purple-700 text-white"
                                >
                                    {analysisLoading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
                                    Analyze Selected with AI
                                </Button>
                            </div>
                        </div>


                        {/* Analysis Results Dashboard */}
                        {showAnalysis && (
                            <Card className="border-purple-200 bg-purple-50/10 dark:bg-purple-900/10 overflow-hidden">
                                <CardHeader className="bg-purple-100/5 dark:bg-purple-900/20 border-b border-purple-200/10">
                                    <div className="flex items-center gap-3">
                                        <div className="p-2 bg-purple-500/20 rounded-lg">
                                            <Gavel className="w-5 h-5 text-purple-400" />
                                        </div>
                                        <div>
                                            <CardTitle>Tribunal Analysis</CardTitle>
                                            <CardDescription>AI-driven evaluation of candidate fit and career narrative.</CardDescription>
                                        </div>
                                    </div>
                                </CardHeader>
                                <CardContent className="p-0">
                                    {analysisLoading ? (
                                        <div className="flex flex-col items-center justify-center py-20">
                                            <Loader2 className="h-10 w-10 animate-spin text-purple-600 mb-4" />
                                            <p className="text-muted-foreground animate-pulse">Convening the Tribunal...</p>
                                        </div>
                                    ) : (
                                        <Table>
                                            <TableHeader className="bg-muted/50">
                                                <TableRow>
                                                    <TableHead className="w-20 text-center">Rank</TableHead>
                                                    <TableHead>Candidate</TableHead>
                                                    <TableHead className="text-center">Score</TableHead>
                                                    <TableHead className="w-44">Tribunal Verdict</TableHead>
                                                    <TableHead className="text-right">Actions</TableHead>
                                                </TableRow>
                                            </TableHeader>
                                            <TableBody>
                                                {analysisResults.sort((a, b) => b.score - a.score).map((res, i) => {
                                                    const candidate = candidates.find(c => c.id === res.candidate_id);
                                                    return (
                                                        <TableRow key={i} className="group hover:bg-muted/50 cursor-pointer transition-colors" onClick={() => setViewingAnalysis(res)}>
                                                            <TableCell className="text-center font-mono text-muted-foreground">#{i + 1}</TableCell>
                                                            <TableCell>
                                                                <div className="font-semibold">{candidate?.firstName} {candidate?.lastName}</div>
                                                                <div className="text-xs text-muted-foreground truncate max-w-48">{candidate?.email}</div>
                                                            </TableCell>
                                                            <TableCell className="text-center">
                                                                <div className="inline-flex items-center justify-center w-12 h-12 rounded-full border-4 border-muted/30 relative">
                                                                    <span className={cn("text-sm font-bold",
                                                                        res.score >= 80 ? "text-emerald-500" : res.score >= 50 ? "text-amber-500" : "text-rose-500"
                                                                    )}>{res.score}</span>
                                                                    <svg className="absolute w-full h-full -rotate-90" viewBox="0 0 36 36">
                                                                        <path className={cn("fill-none stroke-current stroke-2",
                                                                            res.score >= 80 ? "text-emerald-500" : res.score >= 50 ? "text-amber-500" : "text-rose-500"
                                                                        )}
                                                                            strokeDasharray={`${res.score}, 100`}
                                                                            d="M18 2.0845 a 15.9155 15.9155 0 0 1 0 31.831 a 15.9155 15.9155 0 0 1 0 -31.831" />
                                                                    </svg>
                                                                </div>
                                                            </TableCell>
                                                            <TableCell>
                                                                {res.tribunal_status ? (
                                                                    <Badge variant="outline" className={cn("gap-1.5 py-1 pr-3",
                                                                        res.tribunal_status === "Green" ? "bg-emerald-500/10 text-emerald-600 border-emerald-200" :
                                                                            res.tribunal_status === "Red" ? "bg-rose-500/10 text-rose-600 border-rose-200" :
                                                                                "bg-amber-500/10 text-amber-600 border-amber-200"
                                                                    )}>
                                                                        {res.tribunal_status === "Green" && <CheckCircle className="w-3.5 h-3.5" />}
                                                                        {res.tribunal_status === "Red" && <ShieldAlert className="w-3.5 h-3.5" />}
                                                                        {res.tribunal_status === "Yellow" && <AlertTriangle className="w-3.5 h-3.5" />}
                                                                        {res.tribunal_status === "Green" ? "Top Potential" : res.tribunal_status === "Red" ? "High Risk" : "Solid"}
                                                                    </Badge>
                                                                ) : (
                                                                    <span className="text-muted-foreground text-xs italic">Pending...</span>
                                                                )}
                                                            </TableCell>
                                                            <TableCell className="text-right">
                                                                <Button variant="ghost" size="sm" onClick={(e) => { e.stopPropagation(); setViewingAnalysis(res); }}>
                                                                    View Report <Eye className="ml-2 w-4 h-4" />
                                                                </Button>
                                                            </TableCell>
                                                        </TableRow>
                                                    )
                                                })}
                                            </TableBody>
                                        </Table>
                                    )}
                                </CardContent>
                            </Card>
                        )}


                        {/* Resume Viewer Dialog */}
                        <Dialog open={resumeOpen} onOpenChange={setResumeOpen}>
                            <DialogContent className="max-w-4xl max-h-[90vh] flex flex-col p-0 overflow-hidden bg-zinc-50 dark:bg-zinc-900">
                                <DialogHeader className="p-6 border-b bg-background">
                                    <DialogTitle className="text-xl">Resume: {viewingCandidate?.firstName} {viewingCandidate?.lastName}</DialogTitle>
                                    <DialogDescription className="mt-2 text-sm text-muted-foreground">
                                        {viewingCandidate?.email} • {viewingCandidate?.city}, {viewingCandidate?.state}
                                    </DialogDescription>
                                </DialogHeader>
                                <div className="flex-1 overflow-y-auto p-8">
                                    <div className="max-w-3xl mx-auto bg-white dark:bg-zinc-950 shadow-sm border p-10 min-h-full rounded-sm">
                                        <div className="whitespace-pre-wrap text-sm font-serif leading-relaxed text-zinc-800 dark:text-zinc-300">
                                            {resumeLoading ? (
                                                <div className="flex items-center justify-center py-20 text-muted-foreground">
                                                    <Loader2 className="h-6 w-6 animate-spin mr-2" />
                                                    Loading resume content...
                                                </div>
                                            ) : (
                                                resumeText ? (
                                                    <HighlightText
                                                        text={resumeText}
                                                        keywords={hardSkills.map(s => s.name)}
                                                    />
                                                ) : (
                                                    <div className="text-center italic text-muted-foreground py-20">No resume content available.</div>
                                                )
                                            )}
                                        </div>
                                    </div>
                                </div>
                            </DialogContent>
                        </Dialog>


                        {/* Analysis Detail Modal */}
                        <Dialog open={!!viewingAnalysis} onOpenChange={(open) => !open && setViewingAnalysis(null)}>
                            <DialogContent className="max-w-5xl h-[85vh] p-0 overflow-y-auto bg-background border-border">
                                <div className="sr-only">
                                    <DialogTitle>Analysis Report: {viewingAnalysis?.candidate_name}</DialogTitle>
                                </div>
                                <AnalysisCard result={viewingAnalysis} />
                            </DialogContent>
                        </Dialog>
                    </div>
                )
            }
        </div >
    );
}