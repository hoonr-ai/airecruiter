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
} from "lucide-react";
import { useAI } from "@/context/ai-context";
import { useEffect, useMemo, useState } from "react";
import { cn } from "@/lib/utils";

type TiraMode = "chat" | "boolean" | "match" | "bug";

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
        { id: "bug", label: "Report bug", icon: <Bug className="w-3.5 h-3.5" /> },
    ];
    return (
        <div className="flex gap-1.5 px-4 py-2.5 border-b bg-background/60">
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
    const handleSubmit = (e: React.FormEvent) => {
        e.preventDefault();
        if (!input.trim() || isLoading) return;
        sendMessage(input);
        setInput("");
    };
    return (
        <>
            <ScrollArea className="flex-1 p-4">
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
    const apiBase = process.env.NEXT_PUBLIC_API_URL;

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
            const res = await fetch(`${apiBase}/tira/boolean`, { method: "POST", body: fd });
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
    const apiBase = process.env.NEXT_PUBLIC_API_URL;

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
                const res = await fetch(`${apiBase}/jobs/monitored`);
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
            const res = await fetch(`${apiBase}/tira/match`, { method: "POST", body: fd });
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
// Bug report mode
// ---------------------------------------------------------------------------

function BugMode() {
    const apiBase = process.env.NEXT_PUBLIC_API_URL;

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
            const res = await fetch(`${apiBase}/tira/bug-report`, { method: "POST", body: fd });
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
