'use client';

import React, { useEffect, useState } from 'react';
import { useParams, useRouter } from 'next/navigation';
import { 
  Card, CardContent, CardHeader, CardTitle, CardDescription 
} from "@/components/ui/card";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Loader2, Plus, ChevronLeft, ChevronRight, Wand2, X } from "lucide-react";

interface Criterion {
  id?: string;
  name: string;
  is_required: boolean;
  is_ai_generated: boolean;
}

export default function SetCriteriaPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.jobId as string;
  
  const [loading, setLoading] = useState(true);
  const [syncing, setSyncing] = useState(false);
  const [criteria, setCriteria] = useState<Criterion[]>([]);

  useEffect(() => {
    fetchCriteria();
  }, [jobId]);

  const fetchCriteria = async () => {
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'}/api/jobs/${jobId}/criteria`);
      if (response.ok) {
        const data = await response.json();
        const sortedCriteria = (data.criteria || []).sort((a: any, b: any) => (b.weight || 0) - (a.weight || 0));
        setCriteria(sortedCriteria);
        // If empty, auto-sync once
        if (sortedCriteria.length === 0) {
          handleSync();
        }
      }
    } catch (error) {
      console.error("Error fetching criteria:", error);
    } finally {
      setLoading(false);
    }
  };

  const handleSync = async () => {
    setSyncing(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'}/api/jobs/${jobId}/criteria/sync`, {
        method: 'POST'
      });
      if (response.ok) {
        const data = await response.json();
        setCriteria(data.criteria);
        console.log("Criteria pre-populated by AI");
      }
    } catch (error) {
      console.error("Failed to sync criteria");
    } finally {
      setSyncing(false);
    }
  };

  const toggleRequired = (index: number) => {
    const newCriteria = [...criteria];
    newCriteria[index].is_required = !newCriteria[index].is_required;
    setCriteria(newCriteria);
  };

  const addCriterion = () => {
    setCriteria([...criteria, { name: "", is_required: false, is_ai_generated: false }]);
  };

  const removeCriterion = (index: number) => {
    setCriteria(criteria.filter((_, i) => i !== index));
  };

  const updateCriterionName = (index: number, name: string) => {
    const newCriteria = [...criteria];
    newCriteria[index].name = name;
    setCriteria(newCriteria);
  };

  const handleSave = async () => {
    setSyncing(true);
    try {
      const response = await fetch(`${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'}/api/jobs/${jobId}/criteria`, {
        method: 'PUT',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({ criteria }),
      });
      
      if (response.ok) {
        console.log("Criteria saved successfully");
        // Navigate to next step - assuming Create Filters or Candidates
        router.push(`/jobs/${jobId}/candidates`);
      } else {
        console.error("Failed to save criteria");
      }
    } catch (error) {
      console.error("Error saving criteria:", error);
    } finally {
      setSyncing(false);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center justify-center min-h-screen">
        <Loader2 className="h-8 w-8 animate-spin text-primary" />
      </div>
    );
  }

  return (
    <div className="container max-w-5xl py-8 space-y-8 animate-in fade-in duration-500">
      <div className="flex items-center justify-center space-x-4 mb-8">
         {/* Stepper Mockup */}
         <div className="flex items-center space-x-2">
            <div className="w-8 h-8 rounded-full bg-green-500 text-white flex items-center justify-center text-sm font-bold">✓</div>
            <span className="text-sm font-medium text-muted-foreground">Intake</span>
         </div>
         <div className="h-[2px] w-12 bg-green-500" />
         <div className="flex items-center space-x-2">
            <div className="w-8 h-8 rounded-full bg-green-500 text-white flex items-center justify-center text-sm font-bold">✓</div>
            <span className="text-sm font-medium text-muted-foreground">Publish</span>
         </div>
         <div className="h-[2px] w-12 bg-primary" />
         <div className="flex items-center space-x-2">
            <div className="w-8 h-8 rounded-full bg-primary text-white flex items-center justify-center text-sm font-bold">3</div>
            <span className="text-sm font-medium">Set Criteria</span>
         </div>
         <div className="h-[2px] w-12 bg-muted" />
         <div className="flex items-center space-x-2">
            <div className="w-8 h-8 rounded-full bg-muted text-muted-foreground flex items-center justify-center text-sm font-bold">4</div>
            <span className="text-sm font-medium text-muted-foreground">Create Filters</span>
         </div>
      </div>

      <Card className="border-none shadow-xl bg-background/60 backdrop-blur-md">
        <CardHeader className="pb-4">
          <div className="flex justify-between items-start">
            <div className="space-y-1">
              <CardTitle className="text-xl font-bold flex items-center gap-2">
                <Wand2 className="h-6 w-6 text-primary" />
                Set Criteria
              </CardTitle>
              <CardDescription className="text-base">
                Key requirements extracted from the job description. Edit, reorder, and set each as Required or Preferred.
              </CardDescription>
            </div>
            {(syncing) && <Loader2 className="h-5 w-5 animate-spin text-primary" />}
          </div>
          <div className="pt-4 flex items-center gap-4">
             <Badge variant="secondary" className="bg-blue-50 text-blue-700 border-blue-100 py-1">
                AI pre-populated
             </Badge>
             <span className="text-sm text-muted-foreground">
                — edit freely. Changes here apply to new candidates only after sourcing begins.
             </span>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="space-y-3">
            {criteria.map((item, index) => (
              <div 
                key={index} 
                className="group flex items-center gap-4 p-4 rounded-xl border border-transparent hover:border-primary/20 hover:bg-primary/5 transition-all duration-200"
              >
                <div className="flex-1">
                  <Textarea 
                    className="w-full bg-transparent border-none focus-visible:ring-0 text-base font-medium text-foreground/90 placeholder:text-muted-foreground/50 resize-none py-1 px-0 min-h-0 shadow-none border-0"
                    value={item.name}
                    onChange={(e) => updateCriterionName(index, e.target.value)}
                    placeholder="Enter criterion..."
                    rows={1}
                  />
                </div>
                
                <div className="flex items-center gap-3">
                  {item.is_ai_generated && (
                    <span className="text-[10px] font-bold text-blue-400 bg-blue-50 px-1.5 py-0.5 rounded border border-blue-100 uppercase tracking-tighter">AI</span>
                  )}
                  
                  <div className="flex p-1 bg-muted rounded-lg space-x-1">
                    <button
                      onClick={() => toggleRequired(index)}
                      className={`px-3 py-1 text-xs font-semibold rounded-md transition-all ${item.is_required ? 'bg-background text-primary shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
                    >
                      Required
                    </button>
                    <button
                      onClick={() => toggleRequired(index)}
                      className={`px-3 py-1 text-xs font-semibold rounded-md transition-all ${!item.is_required ? 'bg-background text-primary shadow-sm' : 'text-muted-foreground hover:text-foreground'}`}
                    >
                      Preferred
                    </button>
                  </div>
                  
                  <Button 
                    variant="ghost" 
                    size="icon" 
                    className="opacity-0 group-hover:opacity-100 h-8 w-8 text-destructive hover:text-destructive hover:bg-destructive/10 transition-all"
                    onClick={() => removeCriterion(index)}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                </div>
              </div>
            ))}
          </div>

          <button 
            onClick={addCriterion}
            className="flex items-center gap-2 text-sm font-medium text-primary hover:text-primary/80 transition-colors p-2 mt-4"
          >
            <Plus className="h-4 w-4" />
            Add Criterion
          </button>
        </CardContent>
      </Card>

      <div className="flex justify-between items-center pt-8">
        <Button 
          variant="outline" 
          onClick={() => router.back()}
          className="px-8 h-12 rounded-xl font-semibold border-2"
        >
          <ChevronLeft className="mr-2 h-5 w-5" />
          Back
        </Button>
        <Button 
          onClick={handleSave}
          disabled={syncing}
          className="px-10 h-12 rounded-xl font-bold bg-hoonr-gradient text-white shadow-lg shadow-primary/20 hover:scale-[1.02] active:scale-[0.98] transition-all disabled:opacity-50"
        >
          {syncing ? 'Saving...' : 'Save & Continue'}
          <ChevronRight className="ml-2 h-5 w-5" />
        </Button>
      </div>
    </div>
  );
}
