"use client";


import { cn } from "@/lib/utils";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from "@/components/ui/card";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Loader2, Wand2, Search, Download, Trash2, Plus, Eye, Gavel, CheckCircle, AlertTriangle, ShieldAlert, RefreshCw } from "lucide-react";
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


// Helper component to format job descriptions nicely
const FormattedJobDescription = ({ text }: { text: string }) => {
   const formatText = (rawText: string) => {
       if (!rawText) return '';
      
       // Split by lines and process each
       return rawText.split('\n').map((line, index) => {
           const trimmedLine = line.trim();
          
           // Check if this is a section header (starts with 📋)
           if (trimmedLine.startsWith('📋')) {
               const sectionTitle = trimmedLine.replace('📋', '').trim();
               return (
                   <div key={index} className="font-semibold text-primary mt-4 mb-2 first:mt-0">
                       <span className="text-sm tracking-wide">{sectionTitle}</span>
                   </div>
               );
           }
          
           // Check if this is a bullet point
           if (trimmedLine.startsWith('•')) {
               const bulletText = trimmedLine.substring(1).trim();
               // Split the bullet text on full stops to create multiple bullets, ensuring each ends with a period
               const sentences = bulletText.split('.').filter(s => s.trim().length > 0);
              
               return sentences.map((sentence, sentenceIndex) => {
                   const cleanSentence = sentence.trim();
                   const sentenceWithPeriod = cleanSentence.endsWith('.') ? cleanSentence : cleanSentence + '.';
                  
                   return (
                       <div key={`${index}-${sentenceIndex}`} className="flex items-start gap-2 ml-4 my-1">
                           <div className="h-1.5 w-1.5 bg-muted-foreground rounded-full mt-2 shrink-0"></div>
                           <span className="text-sm">{sentenceWithPeriod}</span>
                       </div>
                   );
               });
           }
          
           // Regular paragraph text - also split on full stops if it contains multiple sentences
           if (trimmedLine) {
               if (trimmedLine.includes('.')) {
                   const sentences = trimmedLine.split('.').filter(s => s.trim().length > 0);
                   return sentences.map((sentence, sentenceIndex) => {
                       const cleanSentence = sentence.trim();
                       const sentenceWithPeriod = cleanSentence.endsWith('.') ? cleanSentence : cleanSentence + '.';
                      
                       return (
                           <div key={`${index}-${sentenceIndex}`} className="flex items-start gap-2 ml-4 my-1">
                               <div className="h-1.5 w-1.5 bg-muted-foreground rounded-full mt-2 shrink-0"></div>
                               <span className="text-sm">{sentenceWithPeriod}</span>
                           </div>
                       );
                   });
               } else {
                   // For lines without periods, add one and create a bullet point
                   const sentenceWithPeriod = trimmedLine.endsWith('.') ? trimmedLine : trimmedLine + '.';
                   return (
                       <div key={index} className="flex items-start gap-2 ml-4 my-1">
                           <div className="h-1.5 w-1.5 bg-muted-foreground rounded-full mt-2 shrink-0"></div>
                           <span className="text-sm">{sentenceWithPeriod}</span>
                       </div>
                   );
               }
           }
          
           // Empty line for spacing
           return <div key={index} className="h-2"></div>;
       });
   };


   return (
       <div className="space-y-1">
           {formatText(text)}
       </div>
   );
};


export default function CreateJobPage() {
   const [step, setStep] = useState(1);
   const [loading, setLoading] = useState(false);
   const [jdText, setJdText] = useState("");
   const [jobId, setJobId] = useState("");
  
   // Imported Metadata (JobDiva)
   const [customerName, setCustomerName] = useState("");
   const [jobStatus, setJobStatus] = useState("");
   const [jobNotes, setJobNotes] = useState("");
   const [recruiterEmails, setRecruiterEmails] = useState("");


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
              
               if (data.city) setLocation(`${data.city}, ${data.state || ""}`);
              
               // Auto-parse logic could be added here if desired.
           } else {
               alert("Job not found");
           }
       } catch (e) {
           console.error(e);
           alert("Error fetching job");
       }
       setLoading(false);
   };


   const handleParse = async () => {
       setLoading(true);
       try {
           const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
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
           if (data.location) setLocation(data.location); // Assume parser might return this
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
                       <CardDescription>Paste the raw job description or import from JobDiva.</CardDescription>
                   </CardHeader>
                   <CardContent className="space-y-4">
                       <Tabs defaultValue="paste" className="w-full">
                           <TabsList className="grid w-full grid-cols-2 mb-4">
                               <TabsTrigger value="paste">Paste Text</TabsTrigger>
                               <TabsTrigger value="import">Import from JobDiva</TabsTrigger>
                           </TabsList>


                           <TabsContent value="paste">
                               <Textarea
                                   placeholder="Paste JD here..."
                                   rows={10}
                                   className="font-mono text-sm mb-4"
                                   value={jdText}
                                   onChange={(e) => setJdText(e.target.value)}
                               />
                           </TabsContent>


                           <TabsContent value="import">
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
                                                   onClick={async () => {
                                                       try {
                                                           const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8000';
                                                           const res = await fetch(`${apiUrl}/jobs/${jobId}/sync`);
                                                           if (!res.ok) {
                                                               throw new Error(`HTTP ${res.status}`);
                                                           }
                                                           const data = await res.json();
                                                           console.log('Sync response:', data);
                                                          
                                                           if (data.status && data.status !== "ERROR" && data.status !== "NOT_FOUND") {
                                                               setJobStatus(data.status);
                                                               alert(`Status synced: ${data.status}`);
                                                           } else {
                                                               alert(`Sync failed: ${data.error || data.status || 'Unknown error'}`);
                                                           }
                                                       } catch (e: any) {
                                                           console.error('Sync error:', e);
                                                           alert(`Failed to sync status: ${e?.message || 'Unknown error'}`);
                                                       }
                                                   }}
                                                   title="Refresh status from JobDiva"
                                               >
                                                   <RefreshCw className="h-4 w-4" />
                                               </Button>
                                           </div>
                                           <p className="text-xs text-muted-foreground">Status updates automatically from JobDiva every 5 minutes.</p>
                                        </div>
                                        <div className="space-y-2">
                                           <Label>Recruiter Emails</Label>
                                           <Input
                                               value={recruiterEmails}
                                               onChange={(e) => setRecruiterEmails(e.target.value)}
                                               placeholder="email1@company.com, email2@company.com"
                                               className=""
                                           />
                                           <p className="text-xs text-muted-foreground">Enter recruiter emails separated by commas for sending updates.</p>
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
                                   </div>
                               )}


                               {jdText && (
                                   <div className="space-y-3">
                                       <div className="flex items-center gap-2">
                                           <h3 className="font-semibold text-base">Job Description</h3>
                                       </div>
                                       <div className="rounded-md border bg-background p-4 max-h-64 overflow-y-auto">
                                           <FormattedJobDescription text={jdText} />
                                       </div>
                                   </div>
                               )}
                           </TabsContent>
                       </Tabs>


                       <Button onClick={handleParse} disabled={!jdText || loading} className="w-full bg-hoonr-gradient text-white">
                           {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Wand2 className="mr-2 h-4 w-4" />}
                           Update Job Posting Description
                       </Button>
                   </CardContent>
               </Card>
           )}


           {/* Step 2: Review */}
           {step === 2 && (
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




                       <div className="flex flex-col gap-3 pt-4">
                           <Button onClick={handleSearch} disabled={loading} className="w-full bg-hoonr-gradient text-white h-12 text-lg shadow-lg">
                               {loading ? <Loader2 className="mr-2 h-4 w-4 animate-spin" /> : <Search className="mr-2 h-4 w-4" />}
                               Find Candidates
                           </Button>
                           <Button variant="outline" onClick={() => setStep(1)} className="w-full">Back to Input</Button>
                       </div>
                   </div>
               </div>
           )}


           {/* Step 3: Candidates */}
           {step === 3 && (
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
           )}


       </div>
   );
}