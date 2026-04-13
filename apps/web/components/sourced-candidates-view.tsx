"use client";

import { useState, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Checkbox } from "@/components/ui/checkbox";
import { 
  ArrowLeft,
  Linkedin,
  ShieldCheck,
  User,
  Star,
  Mail,
  MessageSquare,
  FileText
} from "lucide-react";

interface SourcedCandidate {
  id: string;
  candidate_id?: string;
  name: string;
  firstName?: string;
  lastName?: string;
  email?: string;
  title?: string;
  location?: string;
  source: string;
  skills?: string[];
  experience_years?: number;
  match_score?: number;
  selected?: boolean;
}

interface SourcedCandidatesViewProps {
  candidates: SourcedCandidate[];
  onBack?: () => void;
  onLaunchPair?: (selectedCandidates: SourcedCandidate[]) => void;
  onEmailCandidate?: (candidate: SourcedCandidate) => void;
  showFilters?: boolean;
}

export function SourcedCandidatesView({ 
  candidates: initialCandidates = [], 
  onBack,
  onLaunchPair,
  onEmailCandidate,
  showFilters = true
}: SourcedCandidatesViewProps) {
  const [candidates, setCandidates] = useState<SourcedCandidate[]>(
    initialCandidates.map(c => ({ ...c, selected: false }))
  );
  const [selectedCount, setSelectedCount] = useState(0);
  const [selectBest, setSelectBest] = useState<number>(150);

  useEffect(() => {
    setCandidates(initialCandidates.map(c => ({ ...c, selected: false })));
  }, [initialCandidates]);

  useEffect(() => {
    setSelectedCount(candidates.filter(c => c.selected).length);
  }, [candidates]);

  const handleCandidateSelect = (candidateId: string, selected: boolean) => {
    setCandidates(prev => 
      prev.map(c => 
        c.id === candidateId ? { ...c, selected } : c
      )
    );
  };

  const handleSelectAll = () => {
    const allSelected = selectedCount === candidates.length;
    setCandidates(prev => 
      prev.map(c => ({ ...c, selected: !allSelected }))
    );
  };

  const handleSelectBest = (count: number) => {
    // Select top N candidates (prioritize those with more skills)
    const prioritizedCandidates = [...candidates].sort((a, b) => {
      // Sort by skills count and experience
      const aScore = (a.skills?.length || 0) + (a.experience_years || 0) * 0.1;
      const bScore = (b.skills?.length || 0) + (b.experience_years || 0) * 0.1;
      return bScore - aScore;
    });
    const selectedIds = new Set(prioritizedCandidates.slice(0, count).map(c => c.id));
    
    setCandidates(prev => 
      prev.map(c => ({ ...c, selected: selectedIds.has(c.id) }))
    );
    setSelectBest(count);
  };

  const handleLaunchPair = () => {
    const selectedCandidates = candidates.filter(c => c.selected);
    if (onLaunchPair) {
      onLaunchPair(selectedCandidates);
    } else {
      // Default behavior: navigate to Master Candidate Pool
      window.location.href = '/candidates';
    }
  };

  const handleEmailCandidate = async (candidate: SourcedCandidate) => {
    // Save candidate to master pool first
    try {
      // Convert to proper format for API
      const candidateForSave = {
        candidate_id: candidate.candidate_id || candidate.id,
        name: candidate.name,
        email: candidate.email || "",
        skills: candidate.skills || [],
        experience_years: candidate.experience_years || 0,
        source: candidate.source,
        is_selected: false
      };
      
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL}/candidates/save`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          jobdiva_id: "GENERAL_SOURCING",
          candidates: [candidateForSave]
        })
      });
      
      if (response.ok && onEmailCandidate) {
        onEmailCandidate(candidate);
      } else {
        console.error("Failed to save candidate:", response.status, await response.text());
      }
    } catch (error) {
      console.error("Error saving candidate:", error);
    }
  };

  const handleEngageCandidate = (candidate: SourcedCandidate) => {
    // Coming in next phase
    console.log("Engage feature coming in next phase:", candidate.name);
  };

  const handleAssessCandidate = (candidate: SourcedCandidate) => {
    // Coming in next phase
    console.log("Assess feature coming in next phase:", candidate.name);
  };

  const getSourceIcon = (source: string) => {
    const s = source.toLowerCase();
    if (s.includes('linkedin')) return <Linkedin className="w-3 h-3 text-[#0A66C2]" />;
    if (s.includes('jobdiva')) return <ShieldCheck className="w-3 h-3 text-[#6366f1]" />;
    return <User className="w-3 h-3 text-slate-400" />;
  };

  return (
    <div className="p-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between mb-6">
        <div className="flex items-center space-x-4">
          {onBack && (
            <Button variant="ghost" size="sm" onClick={onBack}>
              <ArrowLeft className="w-4 h-4 mr-2" />
              Back
            </Button>
          )}
          <div>
            <h2 className="text-xl font-semibold">Sourced Candidates</h2>
            <p className="text-sm text-muted-foreground">
              {candidates.length} candidates found
            </p>
          </div>
        </div>
        
        <div className="flex items-center space-x-3">
          <Button 
            variant="outline"
            onClick={() => handleSelectBest(150)}
          >
            <Star className="w-4 h-4 mr-2" />
            Select Best 150
          </Button>
          <Button 
            variant="outline"
            onClick={handleSelectAll}
          >
            {selectedCount === candidates.length ? 'Deselect All' : 'Select All'}
          </Button>
          <Button 
            onClick={handleLaunchPair}
            disabled={selectedCount === 0}
            className="bg-blue-600 hover:bg-blue-700"
          >
            🚀 Launch PAIR
          </Button>
        </div>
      </div>

      {/* Selection Summary */}
      {selectedCount > 0 && (
        <div className="mb-4 p-3 bg-blue-50 border border-blue-200 rounded-lg">
          <p className="text-sm font-medium text-blue-800">
            {selectedCount} candidate{selectedCount !== 1 ? 's' : ''} selected
          </p>
        </div>
      )}

      {/* Candidates Grid */}
      <div className="space-y-4">
        {candidates.map((candidate) => (
          <div 
            key={candidate.id}
            className={`border rounded-lg p-4 transition-all hover:shadow-md ${
              candidate.selected ? 'border-blue-300 bg-blue-50/50' : 'border-gray-200'
            }`}
          >
            <div className="flex items-start space-x-4">
              {/* Selection Checkbox */}
              <Checkbox 
                checked={candidate.selected}
                onCheckedChange={(checked) => 
                  handleCandidateSelect(candidate.id, checked as boolean)
                }
                className="mt-1"
              />
              
              {/* Candidate Info */}
              <div className="flex-1 space-y-3">
                {/* Header Row */}
                <div className="flex items-start justify-between">
                  <div>
                    <h3 className="font-semibold text-lg">{candidate.name}</h3>
                    {candidate.title && (
                      <p className="text-sm text-muted-foreground">
                        {candidate.title}
                        {candidate.experience_years && (
                          <span> • {candidate.experience_years} yrs exp</span>
                        )}
                      </p>
                    )}
                  </div>
                  {candidate.match_score !== undefined && candidate.match_score > 0 && (
                    <span className={`px-2.5 py-1 rounded-full text-[12px] font-bold shadow-sm h-fit ${
                      candidate.match_score >= 80 ? 'bg-emerald-100 text-emerald-700 border border-emerald-200' : 
                      candidate.match_score >= 60 ? 'bg-amber-100 text-amber-700 border border-amber-200' : 
                      'bg-rose-100 text-rose-700 border border-rose-200'
                    }`}>
                      {candidate.match_score}% Match
                    </span>
                  )}
                </div>

                {/* Location & Source */}
                <div className="flex items-center space-x-4 text-sm text-muted-foreground">
                  {candidate.location && (
                    <span>{candidate.location}</span>
                  )}
                  <div className="flex items-center space-x-1">
                    {getSourceIcon(candidate.source)}
                    <span>{candidate.source}</span>
                  </div>
                </div>

                {/* Skills */}
                {candidate.skills && candidate.skills.length > 0 && (
                  <div className="flex flex-wrap gap-2">
                    {candidate.skills.slice(0, 8).map((skill, index) => (
                      <Badge 
                        key={index}
                        variant="secondary" 
                        className="text-xs px-2 py-1 bg-gray-100 text-gray-700"
                      >
                        {skill}
                      </Badge>
                    ))}
                    {candidate.skills.length > 8 && (
                      <Badge 
                        variant="secondary" 
                        className="text-xs px-2 py-1 bg-gray-100 text-gray-500"
                      >
                        +{candidate.skills.length - 8} more
                      </Badge>
                    )}
                  </div>
                )}

                {/* Action Buttons */}
                <div className="flex items-center gap-3 pt-2 border-t border-gray-100">
                  <Button 
                    size="sm"
                    onClick={() => handleEmailCandidate(candidate)}
                    className="h-8 px-3 bg-blue-600 hover:bg-blue-700 text-white"
                  >
                    <Mail className="w-3.5 h-3.5 mr-1.5" />
                    Email
                  </Button>
                  
                  <Button 
                    size="sm"
                    variant="outline"
                    onClick={() => handleEngageCandidate(candidate)}
                    disabled={true}
                    className="h-8 px-3 opacity-50 cursor-not-allowed"
                  >
                    <MessageSquare className="w-3.5 h-3.5 mr-1.5" />
                    Engage
                  </Button>
                  
                  <Button 
                    size="sm"
                    variant="outline"
                    onClick={() => handleAssessCandidate(candidate)}
                    disabled={true}
                    className="h-8 px-3 opacity-50 cursor-not-allowed"
                  >
                    <FileText className="w-3.5 h-3.5 mr-1.5" />
                    Assess
                  </Button>
                  
                  <span className="text-xs text-gray-400 ml-auto">
                    {candidate.source}
                  </span>
                </div>
              </div>
            </div>
          </div>
        ))}
      </div>

      {/* Empty State */}
      {candidates.length === 0 && (
        <div className="text-center py-12">
          <div className="w-16 h-16 mx-auto mb-4 bg-gray-100 rounded-full flex items-center justify-center">
            <User className="w-8 h-8 text-gray-400" />
          </div>
          <h3 className="text-lg font-medium text-gray-900 mb-2">No candidates found</h3>
          <p className="text-gray-500">Try adjusting your search criteria.</p>
        </div>
      )}
    </div>
  );
}