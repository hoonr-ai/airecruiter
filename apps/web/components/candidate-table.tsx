"use client";

import {
    Table,
    TableBody,
    TableCell,
    TableHead,
    TableHeader,
    TableRow,
} from "@/components/ui/table";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Avatar, AvatarFallback } from "@/components/ui/avatar";
import { decryptField } from "@/lib/crypto";
import { useState, useEffect } from "react";
import { MessageCircle, Mail, ChevronRight, User, MapPin } from "lucide-react";
import { Checkbox } from "@/components/ui/checkbox";
import {
    Dialog,
    DialogContent,
    DialogHeader,
    DialogTitle,
    DialogFooter,
    DialogDescription,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { Label } from "@/components/ui/label";
import { API_BASE } from "@/lib/api";

interface Candidate {
    id: string;
    provider_id?: string;
    firstName: string;
    lastName: string;
    email: string;
    city: string;
    state: string;
    source?: string;
    open_to_work?: boolean;
    profile_url?: string;
}

interface Props {
    candidates: Candidate[];
    onView?: (candidate: Candidate) => void;
    onSelectionChange?: (selectedIds: string[]) => void;
    selectedIds?: string[];
}

export function CandidateTable({ candidates, onView, onSelectionChange, selectedIds = [] }: Props) {
    const [processedCandidates, setProcessedCandidates] = useState<Candidate[]>([]);
    const [messagedIds, setMessagedIds] = useState<Set<string>>(new Set());

    // Dialog State
    const [isDialogOpen, setIsDialogOpen] = useState(false);
    const [currentCandidate, setCurrentCandidate] = useState<Candidate | null>(null);
    const [messageText, setMessageText] = useState("");
    const [sending, setSending] = useState(false);

    // Decrypt Vetted DB candidates asynchronously
    useEffect(() => {
        const decryptCandidates = async () => {
            const decrypted = await Promise.all(
                candidates.map(async (candidate) => {
                    if (candidate.source === "VettedDB") {
                        try {
                            const decryptedName = await decryptField(candidate.firstName);
                            const nameParts = decryptedName.split(" ", 2);
                            return {
                                ...candidate,
                                firstName: nameParts[0] || "Unknown",
                                lastName: nameParts[1] || "",
                                email: await decryptField(candidate.email)
                            };
                        } catch (error) {
                            console.error("Failed to decrypt candidate:", error);
                            return candidate;
                        }
                    }
                    return candidate;
                })
            );
            setProcessedCandidates(decrypted);
        };
        decryptCandidates();
    }, [candidates]);

    // Handle opening the modal
    const openMessageModal = (candidate: Candidate) => {
        setCurrentCandidate(candidate);
        // Default Template
        if (candidate.source === "LinkedIn") {
            setMessageText(`Hi ${candidate.firstName},\n\nI came across your profile and was impressed by your experience. I'm hiring for a position that seems like a great fit.\n\nWould you be open to a quick chat?\n\nBest,\n[Your Name]`);
        } else {
            // Email Template
            setMessageText(`Hi ${candidate.firstName},\n\nI hope this email finds you well.\n\nI reviewed your profile in our database and thought you'd be a great fit for a new opportunity we have.\n\nAre you available for a brief call this week?\n\nBest,\n[Your Name]`);
        }
        setIsDialogOpen(true);
    };

    // Handle Sending Message
    const handleSendMessage = async () => {
        if (!currentCandidate) return;
        setSending(true);
        try {
            const res = await fetch(`${API_BASE}/candidates/message`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    candidate_provider_id: currentCandidate.provider_id || currentCandidate.id,
                    message: messageText,
                    source: currentCandidate.source || "JobDiva"
                })
            });

            if (res.ok) {
                // Mark as messaged
                setMessagedIds(prev => new Set(prev).add(currentCandidate.id));
                setIsDialogOpen(false);
            } else {
                alert("Failed to send message: " + res.statusText);
            }
        } catch (err) {
            console.error("Message failed", err);
            alert("Error sending message");
        } finally {
            setSending(false);
        }
    };

    if (!candidates || candidates.length === 0) {
        return <div className="p-8 text-center text-muted-foreground border rounded-md">No candidates found.</div>;
    }

    const toggleSelection = (id: string, checked: boolean) => {
        if (!onSelectionChange) return;
        if (checked) {
            onSelectionChange([...selectedIds, id]);
        } else {
            onSelectionChange(selectedIds.filter(i => i !== id));
        }
    };

    return (
        <>
            <Table>
                <TableHeader>
                    <TableRow>
                        <TableHead className="w-10"></TableHead>
                        <TableHead className="w-50">Name</TableHead>
                        <TableHead>Email</TableHead>
                        <TableHead>Match</TableHead>
                        <TableHead>Location</TableHead>
                        <TableHead className="text-right">Action</TableHead>
                    </TableRow>
                </TableHeader>
                <TableBody>
                    {processedCandidates.map((candidate, i) => (
                        <CandidateRow
                            key={candidate.id || i}
                            candidate={candidate}
                            selectedIds={selectedIds}
                            toggleSelection={toggleSelection}
                            onView={onView}
                            onMessageClick={() => openMessageModal(candidate)}
                            isMessaged={messagedIds.has(candidate.id)}
                        />
                    ))}
                </TableBody>
            </Table>

            <Dialog open={isDialogOpen} onOpenChange={setIsDialogOpen}>
                <DialogContent className="sm:max-w-125">
                    <DialogHeader>
                        <DialogTitle>Message {currentCandidate?.firstName} {currentCandidate?.lastName}</DialogTitle>
                        <DialogDescription>
                            Customize your invitation message below.
                        </DialogDescription>
                    </DialogHeader>
                    <div className="grid gap-4 py-4">
                        <div className="grid gap-2">
                            <Label htmlFor="message">Message</Label>
                            <Textarea
                                id="message"
                                value={messageText}
                                onChange={(e) => setMessageText(e.target.value)}
                                className="h-37.5"
                            />
                        </div>
                    </div>
                    <DialogFooter>
                        <Button variant="outline" onClick={() => setIsDialogOpen(false)} disabled={sending}>
                            Cancel
                        </Button>
                        <Button onClick={handleSendMessage} disabled={sending || !messageText.trim()}>
                            {sending ? "Sending..." : "Send Message"}
                        </Button>
                    </DialogFooter>
                </DialogContent>
            </Dialog>
        </>
    );
}

function CandidateRow({
    candidate,
    selectedIds,
    toggleSelection,
    onView,
    onMessageClick,
    isMessaged
}: {
    candidate: Candidate,
    selectedIds: string[],
    toggleSelection: (id: string, checked: boolean) => void,
    onView?: (candidate: Candidate) => void,
    onMessageClick: () => void,
    isMessaged: boolean
}) {
    const [engaging, setEngaging] = useState(false);
    const [assessing, setAssessing] = useState(false);
    const [engaged, setEngaged] = useState(false);
    const [assessed, setAssessed] = useState(false);

    const handleEngage = async (e: React.MouseEvent) => {
        e.stopPropagation();
        setEngaging(true);
        try {
            const res = await fetch(`${API_BASE}/candidates/${candidate.id}/engage`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ candidate_id: candidate.id })
            });
            if (res.ok) setEngaged(true);
        } catch (err) {
            console.error("Engage failed", err);
        } finally {
            setEngaging(false);
        }
    };

    const handleAssess = async (e: React.MouseEvent) => {
        e.stopPropagation();
        setAssessing(true);
        try {
            const res = await fetch(`${API_BASE}/candidates/${candidate.id}/assess`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ candidate_id: candidate.id })
            });
            if (res.ok) setAssessed(true);
        } catch (err) {
            console.error("Assess failed", err);
        } finally {
            setAssessing(false);
        }
    };

    // Fallback logic for candidate name
    const displayName = `${candidate.firstName || ''} ${candidate.lastName || ''}`.trim() || 'Unknown';
    return (
        <TableRow>
            <TableCell>
                <Checkbox
                    checked={selectedIds.includes(candidate.id)}
                    onCheckedChange={(c: boolean) => toggleSelection(candidate.id, c === true)}
                />
            </TableCell>
            <TableCell className="font-medium">
                <div className="flex items-center gap-3">
                    <Avatar className="h-8 w-8">
                        <AvatarFallback>{displayName[0] || '?'}</AvatarFallback>
                    </Avatar>
                    <div>
                        <div className="flex items-center gap-2">
                            {candidate.profile_url ? (
                                <a href={candidate.profile_url} target="_blank" rel="noopener noreferrer" className="hover:underline font-semibold text-primary">
                                    {displayName}
                                </a>
                            ) : (
                                <span>{displayName}</span>
                            )}
                            {candidate.open_to_work && (
                                <span className="flex h-2 w-2 rounded-full bg-green-500" title="Open to Work" />
                            )}
                        </div>
                        <div className="text-xs text-muted-foreground">{candidate.email}</div>
                    </div>
                </div>
            </TableCell>
            <TableCell>
                <div className="flex flex-col gap-1 items-start">
                    {candidate.profile_url ? (
                        <a href={candidate.profile_url} target="_blank" rel="noopener noreferrer">
                            <Badge variant={candidate.source === "VettedDB" ? "default" : "outline"}
                                className={
                                    candidate.source === "VettedDB" ? "bg-primary/10 text-primary hover:bg-primary/20 border-primary/20" :
                                        candidate.source === "LinkedIn" ? "bg-sky-100 text-sky-700 hover:bg-sky-200 border-sky-200 cursor-pointer" : "bg-slate-100 text-slate-700 border-slate-200"
                                }>
                                {candidate.source || "JobDiva"}
                            </Badge>
                        </a>
                    ) : (
                        <Badge variant={candidate.source === "VettedDB" ? "default" : "outline"}
                            className={
                                candidate.source === "VettedDB" ? "bg-primary/10 text-primary hover:bg-primary/20 border-primary/20" :
                                    candidate.source === "LinkedIn" ? "bg-sky-100 text-sky-700 hover:bg-sky-200 border-sky-200" : "bg-slate-100 text-slate-700 border-slate-200"
                            }>
                            {candidate.source || "JobDiva"}
                        </Badge>
                    )}
                    {candidate.open_to_work && candidate.source === "LinkedIn" && (
                        <span className="text-[10px] text-green-600 font-medium">#OpenToWork</span>
                    )}
                </div>
            </TableCell>
            <TableCell>{candidate.city}, {candidate.state}</TableCell>
            <TableCell className="text-right">
                <div className="flex justify-end gap-2">
                    <Button
                        size="sm"
                        variant="outline"
                        className={isMessaged ? "bg-emerald-50 text-emerald-600 border-emerald-200" :
                            candidate.source === "LinkedIn" ? "hover:bg-sky-50 hover:text-sky-600 hover:border-sky-200 shadow-sm" :
                                "hover:bg-indigo-50 hover:text-indigo-600 hover:border-indigo-200 shadow-sm"}
                        onClick={(e) => { e.stopPropagation(); onMessageClick(); }}
                        disabled={isMessaged}
                    >
                        {candidate.source === "LinkedIn" ? (
                            <MessageCircle className="w-3.5 h-3.5 mr-1" />
                        ) : (
                            <Mail className="w-3.5 h-3.5 mr-1" />
                        )}
                        {isMessaged ? "Sent" : (candidate.source === "LinkedIn" ? "Message" : "Email")}
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        className={engaged ? "bg-amber-50 text-amber-600 border-amber-200" : "hover:bg-amber-50 hover:text-amber-600 hover:border-amber-200 transition-colors"}
                        onClick={handleEngage}
                        disabled={engaging || engaged}
                    >
                        {engaging ? "..." : engaged ? "Engaged" : "Engage"}
                    </Button>
                    <Button
                        size="sm"
                        variant="outline"
                        className={assessed ? "bg-violet-50 text-violet-600 border-violet-200" : "hover:bg-violet-50 hover:text-violet-600 hover:border-violet-200 transition-colors"}
                        onClick={handleAssess}
                        disabled={assessing || assessed}
                    >
                        {assessing ? "..." : assessed ? "Assessed" : "Assess"}
                    </Button>
                    <Button 
                        variant="ghost" 
                        size="icon" 
                        className="h-8 w-8 hover:bg-slate-100 text-slate-400 hover:text-slate-900 transition-all"
                        onClick={() => onView && onView(candidate)}
                    >
                        <ChevronRight className="h-5 w-5" />
                    </Button>
                </div>
            </TableCell>
        </TableRow>
    );
}
