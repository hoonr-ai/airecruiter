"use client";

import { Sheet, SheetContent, SheetHeader, SheetTitle, SheetDescription } from "@/components/ui/sheet";
import { ScrollArea } from "@/components/ui/scroll-area";
import { Input } from "@/components/ui/input";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import {
    Send,
    Sparkles,
    MessageSquare,
    FileSearch,
    Bug,
    Upload,
    Check,
    Loader2,
    AlertCircle,
    Search,
    Copy,
    Bot,
    X,
} from "lucide-react";
import { useAI } from "@/context/ai-context";
import { useEffect, useMemo, useRef, useState } from "react";
import { cn } from "@/lib/utils";
import { API_BASE, authFetch, isNetworkFetchError } from "@/lib/api";

type TiraMode = "chat" | "boolean" | "match" | "aicheck" | "bug";

interface AICheckResult {
    filename: string;
    candidate_name: string;
    word_count: number;
    ai_likelihood: number;
    verdict: "likely_human" | "uncertain" | "likely_ai";
    confidence: "low" | "medium" | "high";
    ai_signals: string[];
    human_signals: string[];
    summary: string;
}

interface AICheckReport {
    status: string;
    results: AICheckResult[];
    failed: Array<{ filename: string; reason: string }>;
    summary: {
        total: number;
        analyzed: number;
        failed: number;
        likely_ai: number;
        uncertain: number;
        likely_human: number;
        avg_ai_likelihood: number;
    };
}

interface BooleanResult {
    status: string;
    boolean_string: string;
    must_have_titles: string[];
    must_have_skills: string[];
    nice_to_have: string[];
    exclusions: string[];
    source?: string;
}

interface MonitoredJob {
    job_id?: string;
    jobdiva_id?: string;
    title?: string;
    customer_name?: string;
}

interface MatchResult {
    score: number;
    matched_skills: string[];
    missing_skills: string[];
    explainability: Array<string | Record<string, unknown>>;
    candidate: {
        name?: string;
        title?: string | null;
        location?: string | null;
        years_of_experience?: number | string | null;
    };
    job: {
        title?: string;
        jobdiva_id?: string;
    };
}

export function TiraChat() {
    const { isOpen, setIsOpen, messages, sendMessage, isLoading } = useAI();
    const [mode, setMode] = useState<TiraMode>("chat");

    return (
        <Sheet open={isOpen} onOpenChange={setIsOpen}>
            <SheetContent className="w-[400px] sm:w-[560px] flex flex-col p-0 gap-0 border-l border-border/50 shadow-2xl bg-background/80 backdrop-blur-xl">
                <SheetHeader className="p-4 border-b flex flex-row items-center justify-between bg-primary/5 space-y-0">
                    <div className="flex items-center gap-3">
                        <div className="w-10 h-10 rounded-full bg-gradient-to-br from-indigo-500 to-purple-600 flex items-center justify-center shadow-md">
                            <Sparkles className="text-white h-5 w-5" />
                        </div>
                        <div>
                            <SheetTitle className="font-semibold text-lg leading-none">Tira</SheetTitle>
                            <SheetDescription className="text-xs text-muted-foreground mt-1">Your recruiting sidekick</SheetDescription>
                        </div>
                    </div>
                </SheetHeader>

                <ModeSwitcher mode={mode} setMode={setMode} />

                {mode === "chat" && (
                    <ChatMode
                        messages={messages}
                        sendMessage={sendMessage}
                        isLoading={isLoading}
                    />
                )}
                {mode === "boolean" && <BooleanMode />}
                {mode === "match" && <MatchMode />}
                {mode === "aicheck" && <AICheckMode />}
                {mode === "bug" && <BugMode />}
            </SheetContent>
        </Sheet>
    );
}

// ---------------------------------------------------------------------------
// Mode switcher chips
// ---------------------------------------------------------------------------

function ModeSwitcher({ mode, setMode }: { mode: TiraMode; setMode: (m: TiraMode) => void }) {
    const tabs: Array<{ id: TiraMode; label: string; icon: React.ReactNode }> = [
        { id: "chat", label: "Chat", icon: <MessageSquare className="w-3.5 h-3.5" /> },
        { id: "boolean", label: "Boolean", icon: <Search className="w-3.5 h-3.5" /> },
        { id: "match", label: "Resume match", icon: <FileSearch className="w-3.5 h-3.5" /> },
        { id: "aicheck", label: "AI check", icon: <Bot className="w-3.5 h-3.5" /> },
        { id: "bug", label: "Report bug", icon: <Bug className="w-3.5 h-3.5" /> },
    ];
    return (
        <div className="flex flex-wrap gap-1.5 px-4 py-2.5 border-b bg-background/60">
            {tabs.map(t => (
                <button
                    key={t.id}
                    type="button"
                    onClick={() => setMode(t.id)}
                    className={cn(
                        "flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[12.5px] font-medium transition-colors border",
                        mode === t.id
                            ? "bg-indigo-600 text-white border-indigo-600 shadow-sm"
                            : "bg-white text-slate-600 border-slate-200 hover:bg-slate-50",
                    )}
                >
                    {t.icon}
                    {t.label}
                </button>
            ))}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Chat mode
// ---------------------------------------------------------------------------

function ChatMode({
    messages,
    sendMessage,
    isLoading,
}: {
    messages: Array<{ role: "user" | "assistant"; content: string }>;
    sendMessage: (content: string) => Promise<void>;
    isLoading: boolean;
}) {
    const [input, setInput] = useState("");
    // Anchor at the bottom of the message list — scrollIntoView on new messages
    // keeps the conversation pinned to the latest reply without needing a ref
    // into Radix's internal viewport.
    const endRef = useRef<HTMLDivElement | null>(null);
    useEffect(() => {
        endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
    }, [messages, isLoading]);
    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!input.trim() || isLoading) return;
        sendMessage(input);
        setInput("");
    };
    return (
        <>
            <ScrollArea className="flex-1 min-h-0 p-4">
                <div className="space-y-4">
                    {messages.map((m, i) => (
                        <div key={i} className={cn("flex gap-3", m.role === "user" ? "flex-row-reverse" : "flex-row")}>
                            <Avatar className="w-8 h-8 border">
                                {m.role === "assistant" ? (
                                    <AvatarFallback className="bg-primary/10 text-primary text-xs">T</AvatarFallback>
                                ) : (
                                    <AvatarFallback className="bg-muted text-xs">ME</AvatarFallback>
                                )}
                            </Avatar>
                            <div
                                className={cn(
                                    "p-3 rounded-2xl text-sm max-w-[80%] whitespace-pre-wrap",
                                    m.role === "user"
                                        ? "bg-primary text-primary-foreground rounded-br-none"
                                        : "bg-muted text-foreground rounded-bl-none border border-border/50",
                                )}
                            >
                                {m.content}
                            </div>
                        </div>
                    ))}
                    {isLoading && (
                        <div className="flex gap-3">
                            <Avatar className="w-8 h-8 border">
                                <AvatarFallback className="bg-primary text-primary-foreground text-xs">T</AvatarFallback>
                            </Avatar>
                            <div className="bg-muted p-3 rounded-2xl rounded-bl-none text-sm border border-border/50 flex items-center gap-1">
                                <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                                <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                                <div className="w-2 h-2 bg-primary/40 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                            </div>
                        </div>
                    )}
                    <div ref={endRef} />
                </div>
            </ScrollArea>
            <div className="p-4 border-t bg-background">
                <form onSubmit={handleSubmit} className="flex gap-2">
                    <Input
                        value={input}
                        onChange={e => setInput(e.target.value)}
                        placeholder="Ask Tira anything..."
                        className="flex-1 bg-muted/50 border-0 focus-visible:ring-1 focus-visible:ring-primary/20"
                    />
                    <Button type="submit" size="icon" disabled={isLoading || !input.trim()} className="bg-hoonr-gradient text-white shadow-md hover:opacity-90 transition-opacity">
                        <Send className="h-4 w-4" />
                    </Button>
                </form>
            </div>
        </>
    );
}

// ---------------------------------------------------------------------------
// Boolean mode
// ---------------------------------------------------------------------------

function BooleanMode() {
    const apiBase = API_BASE;

    const [jdText, setJdText] = useState("");
    const [file, setFile] = useState<File | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<BooleanResult | null>(null);
    const [copied, setCopied] = useState(false);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setResult(null);
        if (!jdText.trim() && !file) {
            setError("Paste a JD or upload a file.");
            return;
        }
        setSubmitting(true);
        try {
            const fd = new FormData();
            if (jdText.trim()) fd.append("jd_text", jdText.trim());
            if (file) fd.append("jd_file", file);
            const res = await authFetch(`${apiBase}/tira/boolean`, { method: "POST", body: fd });
            const data = await res.json();
            if (!res.ok) throw new Error(data?.detail || `Failed (${res.status})`);
            setResult(data as BooleanResult);
        } catch (e) {
            const msg = e instanceof Error ? e.message : "Couldn't build a boolean string.";
            setError(msg);
        } finally {
            setSubmitting(false);
        }
    };

    const handleCopy = async () => {
        if (!result?.boolean_string) return;
        try {
            await navigator.clipboard.writeText(result.boolean_string);
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
        } catch {
            /* clipboard not available */
        }
    };

    return (
        <ScrollArea className="flex-1">
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Job description</label>
                    <textarea
                        value={jdText}
                        onChange={e => setJdText(e.target.value)}
                        rows={6}
                        placeholder="Paste a JD here — or upload one below."
                        className="w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-[13.5px] focus:outline-none focus:ring-2 focus:ring-indigo-500/30 resize-y"
                    />
                </div>

                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Or upload a JD file</label>
                    <label className="flex items-center gap-2 h-10 px-3 border border-dashed border-slate-300 rounded-md bg-slate-50/60 text-[13px] text-slate-600 cursor-pointer hover:bg-slate-50">
                        <Upload className="w-4 h-4 text-slate-400" />
                        <span className="truncate">{file ? file.name : "PDF, DOCX, or TXT"}</span>
                        <input
                            type="file"
                            accept=".pdf,.docx,.txt,.md"
                            onChange={e => setFile(e.target.files?.[0] || null)}
                            className="hidden"
                        />
                    </label>
                </div>

                {error && (
                    <div className="text-[13px] text-rose-600 flex items-start gap-1.5">
                        <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> <span>{error}</span>
                    </div>
                )}

                <Button type="submit" disabled={submitting} className="w-full bg-hoonr-gradient text-white h-10">
                    {submitting ? (<><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Generating…</>) : "Generate boolean string"}
                </Button>

                {result && (
                    <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-4 shadow-sm">
                        <div>
                            <div className="flex items-center justify-between mb-1.5">
                                <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">Boolean string</div>
                                <button
                                    type="button"
                                    onClick={handleCopy}
                                    className="inline-flex items-center gap-1 text-[11.5px] text-indigo-600 hover:text-indigo-700 font-medium"
                                >
                                    {copied ? <Check className="w-3 h-3" /> : <Copy className="w-3 h-3" />}
                                    {copied ? "Copied" : "Copy"}
                                </button>
                            </div>
                            <div className="font-mono text-[12.5px] leading-relaxed bg-slate-50 border border-slate-200 rounded-md p-3 whitespace-pre-wrap break-words text-slate-800">
                                {result.boolean_string}
                            </div>
                        </div>

                        {result.must_have_titles?.length > 0 && (
                            <ChipGroup label="Must-have titles" tone="indigo" items={result.must_have_titles} />
                        )}
                        {result.must_have_skills?.length > 0 && (
                            <ChipGroup label="Must-have skills" tone="emerald" items={result.must_have_skills} />
                        )}
                        {result.nice_to_have?.length > 0 && (
                            <ChipGroup label="Nice to have" tone="slate" items={result.nice_to_have} />
                        )}
                        {result.exclusions?.length > 0 && (
                            <ChipGroup label="Exclusions" tone="rose" items={result.exclusions} />
                        )}
                    </div>
                )}
            </form>
        </ScrollArea>
    );
}

function ChipGroup({ label, items, tone }: { label: string; items: string[]; tone: "indigo" | "emerald" | "rose" | "slate" }) {
    const toneMap = {
        indigo: "bg-indigo-50 text-indigo-700 border-indigo-200",
        emerald: "bg-emerald-50 text-emerald-700 border-emerald-200",
        rose: "bg-rose-50 text-rose-700 border-rose-200",
        slate: "bg-slate-50 text-slate-700 border-slate-200",
    } as const;
    const headingTone = {
        indigo: "text-indigo-700",
        emerald: "text-emerald-700",
        rose: "text-rose-700",
        slate: "text-slate-500",
    } as const;
    return (
        <div>
            <div className={cn("text-[11px] uppercase tracking-wider font-semibold mb-1.5", headingTone[tone])}>{label}</div>
            <div className="flex flex-wrap gap-1.5">
                {items.map((s, i) => (
                    <span key={`${label}-${i}`} className={cn("px-2 py-0.5 rounded-full border text-[11.5px] font-medium", toneMap[tone])}>
                        {s}
                    </span>
                ))}
            </div>
        </div>
    );
}

// ---------------------------------------------------------------------------
// Resume match mode
// ---------------------------------------------------------------------------

function MatchMode() {
    const apiBase = API_BASE;

    const [jobs, setJobs] = useState<MonitoredJob[]>([]);
    const [jobsError, setJobsError] = useState<string | null>(null);
    const [jobsLoading, setJobsLoading] = useState(true);
    const [selectedJobId, setSelectedJobId] = useState<string>("");
    const [file, setFile] = useState<File | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [result, setResult] = useState<MatchResult | null>(null);

    useEffect(() => {
        let cancelled = false;
        const run = async () => {
            setJobsLoading(true);
            setJobsError(null);
            try {
                const res = await authFetch(`${apiBase}/jobs/monitored`);
                const data = await res.json();
                if (cancelled) return;
                const jobsDict = data?.jobs || {};
                const list: MonitoredJob[] = Object.values(jobsDict);
                list.sort((a, b) => (a.title || "").localeCompare(b.title || ""));
                setJobs(list);
            } catch (e) {
                if (!cancelled) setJobsError("Couldn't load jobs. Is the API running?");
            } finally {
                if (!cancelled) setJobsLoading(false);
            }
        };
        run();
        return () => {
            cancelled = true;
        };
    }, [apiBase]);

    const jobOptions = useMemo(() => {
        return jobs.map(j => {
            const id = String(j.job_id ?? j.jobdiva_id ?? "");
            const label = [j.title || "(untitled job)", j.jobdiva_id ? `(${j.jobdiva_id})` : ""].filter(Boolean).join(" ");
            return { id, label };
        }).filter(o => o.id);
    }, [jobs]);

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        setResult(null);
        if (!selectedJobId) {
            setError("Pick a job first.");
            return;
        }
        if (!file) {
            setError("Upload a resume (PDF, DOCX, or TXT).");
            return;
        }
        setSubmitting(true);
        try {
            const fd = new FormData();
            fd.append("job_id", selectedJobId);
            fd.append("resume_file", file);
            const res = await authFetch(`${apiBase}/tira/match`, { method: "POST", body: fd });
            const data = await res.json();
            if (!res.ok) throw new Error(data?.detail || `Failed (${res.status})`);
            setResult(data as MatchResult);
        } catch (e) {
            const msg = e instanceof Error ? e.message : "Something went wrong scoring the resume.";
            setError(msg);
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <ScrollArea className="flex-1">
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Job</label>
                    {jobsLoading ? (
                        <div className="h-10 rounded-md bg-slate-100 animate-pulse" />
                    ) : jobsError ? (
                        <div className="text-[13px] text-rose-600 flex items-center gap-1.5">
                            <AlertCircle className="w-3.5 h-3.5" /> {jobsError}
                        </div>
                    ) : (
                        <select
                            value={selectedJobId}
                            onChange={e => setSelectedJobId(e.target.value)}
                            className="w-full h-10 rounded-md border border-slate-200 bg-white px-3 text-[13.5px] focus:outline-none focus:ring-2 focus:ring-indigo-500/30"
                        >
                            <option value="">Select a job…</option>
                            {jobOptions.map(o => (
                                <option key={o.id} value={o.id}>{o.label}</option>
                            ))}
                        </select>
                    )}
                </div>

                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Resume</label>
                    <label className="flex items-center gap-2 h-10 px-3 border border-dashed border-slate-300 rounded-md bg-slate-50/60 text-[13px] text-slate-600 cursor-pointer hover:bg-slate-50">
                        <Upload className="w-4 h-4 text-slate-400" />
                        <span className="truncate">{file ? file.name : "Click to upload (PDF, DOCX, TXT)"}</span>
                        <input
                            type="file"
                            accept=".pdf,.docx,.txt,.md"
                            onChange={e => setFile(e.target.files?.[0] || null)}
                            className="hidden"
                        />
                    </label>
                </div>

                {error && (
                    <div className="text-[13px] text-rose-600 flex items-start gap-1.5">
                        <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> <span>{error}</span>
                    </div>
                )}

                <Button type="submit" disabled={submitting} className="w-full bg-hoonr-gradient text-white h-10">
                    {submitting ? (<><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Scoring…</>) : "Score resume"}
                </Button>

                {result && <MatchResultCard result={result} />}
            </form>
        </ScrollArea>
    );
}

function MatchResultCard({ result }: { result: MatchResult }) {
    const score = Math.round(result.score || 0);
    const scoreColor = score >= 70 ? "text-emerald-600" : score >= 40 ? "text-amber-500" : "text-rose-600";
    return (
        <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-4 shadow-sm">
            <div className="flex items-end justify-between">
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">Match score</div>
                    <div className={cn("text-4xl font-bold leading-none mt-1", scoreColor)}>{score}<span className="text-lg text-slate-400 font-semibold ml-0.5">/100</span></div>
                </div>
                <div className="text-right text-[12px] text-slate-500">
                    <div className="font-semibold text-slate-700">{result.candidate.name || "Candidate"}</div>
                    {result.candidate.title && <div className="text-[11.5px]">{result.candidate.title}</div>}
                    {result.candidate.years_of_experience != null && <div className="text-[11.5px]">{result.candidate.years_of_experience} yrs exp</div>}
                </div>
            </div>

            {result.matched_skills?.length > 0 && (
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-emerald-700 font-semibold mb-1.5">Matched</div>
                    <div className="flex flex-wrap gap-1.5">
                        {result.matched_skills.map((s, i) => (
                            <span key={`m-${i}`} className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-[11.5px] font-medium">
                                <Check className="w-3 h-3" /> {s}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {result.missing_skills?.length > 0 && (
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-rose-700 font-semibold mb-1.5">Missing</div>
                    <div className="flex flex-wrap gap-1.5">
                        {result.missing_skills.map((s, i) => (
                            <span key={`x-${i}`} className="px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200 text-[11.5px] font-medium">
                                {s}
                            </span>
                        ))}
                    </div>
                </div>
            )}

            {result.explainability?.length > 0 && (
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold mb-1.5">Why this score</div>
                    <ul className="space-y-1 text-[12.5px] text-slate-600 list-disc pl-4">
                        {result.explainability.slice(0, 8).map((e, i) => (
                            <li key={`e-${i}`}>{typeof e === "string" ? e : JSON.stringify(e)}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// AI check mode — bulk AI-plagiarism check on resumes
// ---------------------------------------------------------------------------

const AI_CHECK_MAX_FILES = 25;
const AI_CHECK_MAX_FILE_MB = 8;
// nginx's client_max_body_size is 25m server-wide — cap the whole batch below
// it (multipart overhead included) so oversized uploads fail here with a clear
// message instead of an HTML 413 from the proxy.
const AI_CHECK_MAX_TOTAL_MB = 20;

function AICheckMode() {
    const apiBase = API_BASE;

    const [files, setFiles] = useState<File[]>([]);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [report, setReport] = useState<AICheckReport | null>(null);

    const addFiles = (picked: FileList | null) => {
        if (!picked || picked.length === 0) return;
        setError(null);
        setFiles(prev => {
            const merged = [...prev];
            let totalBytes = merged.reduce((sum, f) => sum + f.size, 0);
            for (const f of Array.from(picked)) {
                if (f.size > AI_CHECK_MAX_FILE_MB * 1024 * 1024) {
                    setError(`${f.name} is larger than ${AI_CHECK_MAX_FILE_MB}MB and was skipped.`);
                    continue;
                }
                const dup = merged.some(m => m.name === f.name && m.size === f.size && m.lastModified === f.lastModified);
                if (dup) continue;
                if (totalBytes + f.size > AI_CHECK_MAX_TOTAL_MB * 1024 * 1024) {
                    setError(`Batch is limited to ${AI_CHECK_MAX_TOTAL_MB}MB total — ${f.name} was skipped.`);
                    continue;
                }
                merged.push(f);
                totalBytes += f.size;
            }
            if (merged.length > AI_CHECK_MAX_FILES) {
                setError(`You can check up to ${AI_CHECK_MAX_FILES} resumes at a time — extra files were skipped.`);
                return merged.slice(0, AI_CHECK_MAX_FILES);
            }
            return merged;
        });
    };

    const removeFile = (idx: number) => setFiles(prev => prev.filter((_, i) => i !== idx));

    const resetAll = () => {
        setFiles([]);
        setReport(null);
        setError(null);
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        if (files.length === 0) {
            setError("Add at least one resume (PDF, DOCX, or TXT).");
            return;
        }
        setSubmitting(true);
        setReport(null);
        try {
            const fd = new FormData();
            files.forEach(f => fd.append("files", f));
            const res = await authFetch(`${apiBase}/tira/ai-check`, { method: "POST", body: fd });
            // A proxy error (e.g. 413 for an oversized batch) returns an HTML
            // body — don't let the JSON parse failure mask the real cause.
            let data: any = null;
            try {
                data = await res.json();
            } catch {
                /* non-JSON body */
            }
            if (!res.ok || !data) {
                throw new Error(
                    data?.detail
                        || (res.status === 413
                            ? "Upload too large — remove some files and try again."
                            : `Failed (${res.status})`),
                );
            }
            setReport(data as AICheckReport);
        } catch (e) {
            const msg = isNetworkFetchError(e)
                ? "Network failure — check your connection and try again."
                : e instanceof Error
                    ? e.message
                    : "Couldn't run the AI check.";
            setError(msg);
        } finally {
            setSubmitting(false);
        }
    };

    return (
        <ScrollArea className="flex-1">
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
                <div className="text-[12.5px] text-slate-500 leading-relaxed">
                    Upload resumes in bulk and Tira estimates how likely each one was written by AI, with the signals behind each verdict.
                </div>

                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">
                        Resumes ({files.length}/{AI_CHECK_MAX_FILES})
                    </label>
                    <label className="flex items-center gap-2 h-10 px-3 border border-dashed border-slate-300 rounded-md bg-slate-50/60 text-[13px] text-slate-600 cursor-pointer hover:bg-slate-50">
                        <Upload className="w-4 h-4 text-slate-400" />
                        <span className="truncate">
                            {files.length > 0 ? "Add more resumes" : "Click to upload (PDF, DOCX, TXT) — multiple allowed"}
                        </span>
                        <input
                            type="file"
                            multiple
                            accept=".pdf,.docx,.txt,.md"
                            onChange={e => {
                                addFiles(e.target.files);
                                e.target.value = "";
                            }}
                            className="hidden"
                        />
                    </label>
                </div>

                {files.length > 0 && (
                    <ul className="space-y-1">
                        {files.map((f, i) => (
                            <li key={`${f.name}-${f.size}-${i}`} className="flex items-center gap-2 text-[12.5px] text-slate-700 bg-white border border-slate-200 rounded-md px-2.5 py-1.5">
                                <FileSearch className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                                <span className="truncate flex-1">{f.name}</span>
                                <span className="text-slate-400 text-[11px] shrink-0">{(f.size / 1024).toFixed(0)} KB</span>
                                <button type="button" onClick={() => removeFile(i)} className="text-slate-400 hover:text-rose-600 shrink-0" aria-label={`Remove ${f.name}`}>
                                    <X className="w-3.5 h-3.5" />
                                </button>
                            </li>
                        ))}
                    </ul>
                )}

                {error && (
                    <div className="text-[13px] text-rose-600 flex items-start gap-1.5">
                        <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> <span>{error}</span>
                    </div>
                )}

                <Button type="submit" disabled={submitting || files.length === 0} className="w-full bg-hoonr-gradient text-white h-10">
                    {submitting
                        ? (<><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Analyzing {files.length} resume{files.length === 1 ? "" : "s"}… this can take a minute</>)
                        : `Check ${files.length || ""} resume${files.length === 1 ? "" : "s"} for AI content`}
                </Button>

                {report && (
                    <div className="space-y-3">
                        <AICheckSummaryCard report={report} />
                        {report.results.map((r, i) => (
                            <AICheckResultCard key={`${r.filename}-${i}`} result={r} />
                        ))}
                        {report.failed.length > 0 && (
                            <div className="rounded-xl border border-amber-200 bg-amber-50/60 p-3 space-y-1">
                                <div className="text-[11px] uppercase tracking-wider text-amber-700 font-semibold">Not analyzed</div>
                                {report.failed.map((f, i) => (
                                    <div key={`f-${i}`} className="text-[12.5px] text-amber-800">
                                        <span className="font-medium">{f.filename}</span> — {f.reason}
                                    </div>
                                ))}
                            </div>
                        )}
                        <div className="text-[11.5px] text-slate-400 leading-relaxed">
                            Heuristic estimate — AI detection from text alone is probabilistic, not proof. Use it as a signal to probe in a screen, not to reject a candidate outright.
                        </div>
                        <Button type="button" variant="outline" onClick={resetAll} className="w-full h-9">
                            Check another batch
                        </Button>
                    </div>
                )}
            </form>
        </ScrollArea>
    );
}

function AICheckSummaryCard({ report }: { report: AICheckReport }) {
    const s = report.summary;
    return (
        <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm space-y-2">
            <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">Batch report</div>
            <div className="flex flex-wrap gap-1.5">
                <span className="px-2 py-0.5 rounded-full bg-rose-50 text-rose-700 border border-rose-200 text-[11.5px] font-medium">
                    {s.likely_ai} likely AI
                </span>
                <span className="px-2 py-0.5 rounded-full bg-amber-50 text-amber-700 border border-amber-200 text-[11.5px] font-medium">
                    {s.uncertain} uncertain
                </span>
                <span className="px-2 py-0.5 rounded-full bg-emerald-50 text-emerald-700 border border-emerald-200 text-[11.5px] font-medium">
                    {s.likely_human} likely human
                </span>
                {s.failed > 0 && (
                    <span className="px-2 py-0.5 rounded-full bg-slate-50 text-slate-600 border border-slate-200 text-[11.5px] font-medium">
                        {s.failed} not analyzed
                    </span>
                )}
            </div>
            <div className="text-[12.5px] text-slate-500">
                {s.analyzed} of {s.total} analyzed · average AI likelihood {s.avg_ai_likelihood}/100
            </div>
        </div>
    );
}

const AI_CHECK_VERDICT_LABEL: Record<AICheckResult["verdict"], string> = {
    likely_ai: "Likely AI-generated",
    uncertain: "Uncertain",
    likely_human: "Likely human-written",
};

function AICheckResultCard({ result }: { result: AICheckResult }) {
    const score = Math.round(result.ai_likelihood || 0);
    // Inverse of the match-score ramp — here a HIGH score is the bad outcome.
    const scoreColor = score >= 65 ? "text-rose-600" : score >= 30 ? "text-amber-500" : "text-emerald-600";
    const verdictTone =
        result.verdict === "likely_ai"
            ? "bg-rose-50 text-rose-700 border-rose-200"
            : result.verdict === "uncertain"
                ? "bg-amber-50 text-amber-700 border-amber-200"
                : "bg-emerald-50 text-emerald-700 border-emerald-200";
    return (
        <div className="rounded-xl border border-slate-200 bg-white p-4 space-y-3 shadow-sm">
            <div className="flex items-end justify-between gap-3">
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-slate-500 font-semibold">AI likelihood</div>
                    <div className={cn("text-4xl font-bold leading-none mt-1", scoreColor)}>
                        {score}<span className="text-lg text-slate-400 font-semibold ml-0.5">/100</span>
                    </div>
                </div>
                <div className="text-right text-[12px] text-slate-500 min-w-0">
                    <div className="font-semibold text-slate-700 truncate">{result.candidate_name || result.filename}</div>
                    <div className="text-[11.5px] truncate">{result.filename}</div>
                    <div className="text-[11.5px]">{result.word_count} words</div>
                </div>
            </div>

            <div className="flex flex-wrap items-center gap-1.5">
                <span className={cn("px-2 py-0.5 rounded-full border text-[11.5px] font-medium", verdictTone)}>
                    {AI_CHECK_VERDICT_LABEL[result.verdict] ?? result.verdict}
                </span>
                <span className="px-2 py-0.5 rounded-full bg-slate-50 text-slate-600 border border-slate-200 text-[11.5px] font-medium">
                    {result.confidence} confidence
                </span>
            </div>

            {result.summary && <div className="text-[12.5px] text-slate-600 leading-relaxed">{result.summary}</div>}

            {result.ai_signals?.length > 0 && (
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-rose-700 font-semibold mb-1.5">AI signals</div>
                    <ul className="space-y-1 text-[12.5px] text-slate-600 list-disc pl-4">
                        {result.ai_signals.slice(0, 6).map((sig, i) => (
                            <li key={`ai-${i}`}>{sig}</li>
                        ))}
                    </ul>
                </div>
            )}

            {result.human_signals?.length > 0 && (
                <div>
                    <div className="text-[11px] uppercase tracking-wider text-emerald-700 font-semibold mb-1.5">Human signals</div>
                    <ul className="space-y-1 text-[12.5px] text-slate-600 list-disc pl-4">
                        {result.human_signals.slice(0, 6).map((sig, i) => (
                            <li key={`h-${i}`}>{sig}</li>
                        ))}
                    </ul>
                </div>
            )}
        </div>
    );
}

// ---------------------------------------------------------------------------
// Bug report mode
// ---------------------------------------------------------------------------

function BugMode() {
    const apiBase = API_BASE;

    const [title, setTitle] = useState("");
    const [description, setDescription] = useState("");
    const [screenshot, setScreenshot] = useState<File | null>(null);
    const [submitting, setSubmitting] = useState(false);
    const [error, setError] = useState<string | null>(null);
    const [success, setSuccess] = useState<"sent" | "logged" | null>(null);

    const resetForm = () => {
        setTitle("");
        setDescription("");
        setScreenshot(null);
        setSuccess(null);
        setError(null);
    };

    const handleSubmit = async (e: React.FormEvent) => {
        e.preventDefault();
        setError(null);
        if (!title.trim()) {
            setError("Add a short title so we know what broke.");
            return;
        }
        setSubmitting(true);
        try {
            const fd = new FormData();
            fd.append("title", title.trim());
            fd.append("description", description.trim());
            fd.append("page_url", typeof window !== "undefined" ? window.location.href : "");
            fd.append("user_agent", typeof navigator !== "undefined" ? navigator.userAgent : "");
            if (screenshot) fd.append("screenshot", screenshot);
            const res = await authFetch(`${apiBase}/tira/bug-report`, { method: "POST", body: fd });
            const data = await res.json();
            if (!res.ok) throw new Error(data?.detail || `Failed (${res.status})`);
            setSuccess(data?.sent ? "sent" : "logged");
        } catch (e) {
            const msg = e instanceof Error ? e.message : "Couldn't submit the bug report.";
            setError(msg);
        } finally {
            setSubmitting(false);
        }
    };

    if (success) {
        return (
            <div className="flex-1 p-6 flex flex-col items-center justify-center text-center gap-3">
                <div className="w-12 h-12 rounded-full bg-emerald-50 border border-emerald-200 flex items-center justify-center">
                    <Check className="w-6 h-6 text-emerald-600" />
                </div>
                <div className="text-[15px] font-semibold text-slate-800">Thanks — we got it.</div>
                <div className="text-[13px] text-slate-500 max-w-xs">
                    {success === "sent"
                        ? "Your report is on its way to Akarsh."
                        : "We logged the report on the server. Email delivery isn't configured yet, but nothing was lost."}
                </div>
                <Button variant="outline" onClick={resetForm} className="mt-2">Send another</Button>
            </div>
        );
    }

    return (
        <ScrollArea className="flex-1">
            <form onSubmit={handleSubmit} className="p-4 space-y-4">
                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Title</label>
                    <Input
                        value={title}
                        onChange={e => setTitle(e.target.value)}
                        placeholder="Rankings page shows everyone as Fail"
                        maxLength={140}
                    />
                </div>

                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">What happened?</label>
                    <textarea
                        value={description}
                        onChange={e => setDescription(e.target.value)}
                        rows={6}
                        placeholder="Steps to reproduce, what you expected, what you saw instead…"
                        className="w-full rounded-md border border-slate-200 bg-white px-3 py-2 text-[13.5px] focus:outline-none focus:ring-2 focus:ring-indigo-500/30 resize-y"
                    />
                </div>

                <div>
                    <label className="text-[12px] font-semibold text-slate-500 uppercase tracking-wide block mb-1.5">Screenshot (optional)</label>
                    <label className="flex items-center gap-2 h-10 px-3 border border-dashed border-slate-300 rounded-md bg-slate-50/60 text-[13px] text-slate-600 cursor-pointer hover:bg-slate-50">
                        <Upload className="w-4 h-4 text-slate-400" />
                        <span className="truncate">{screenshot ? screenshot.name : "Attach an image"}</span>
                        <input
                            type="file"
                            accept="image/*"
                            onChange={e => setScreenshot(e.target.files?.[0] || null)}
                            className="hidden"
                        />
                    </label>
                </div>

                {error && (
                    <div className="text-[13px] text-rose-600 flex items-start gap-1.5">
                        <AlertCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" /> <span>{error}</span>
                    </div>
                )}

                <Button type="submit" disabled={submitting} className="w-full bg-hoonr-gradient text-white h-10">
                    {submitting ? (<><Loader2 className="w-4 h-4 mr-2 animate-spin" /> Sending…</>) : "Send bug report"}
                </Button>

                <div className="text-[11.5px] text-slate-400 text-center">
                    We send the current page URL and browser info with your report.
                </div>
            </form>
        </ScrollArea>
    );
}
