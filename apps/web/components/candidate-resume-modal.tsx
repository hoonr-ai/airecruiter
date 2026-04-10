"use client";

import { useState, useEffect } from "react";
import { 
  Dialog, 
  DialogContent, 
  DialogHeader, 
  DialogTitle 
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Separator } from "@/components/ui/separator";
import { 
  Mail, 
  Phone, 
  MapPin, 
  Briefcase, 
  GraduationCap,
  FileText,
  Download,
  ExternalLink,
  Loader2
} from "lucide-react";

interface CandidateResumeData {
  id: string;
  name: string;
  email: string;
  phone: string;
  title: string;
  location: string;
  resume_text: string;
  skills: string[] | string;
  experience: string;
  education: string;
  source: string;
}

interface CandidateResumeModalProps {
  candidateId: string | null;
  isOpen: boolean;
  onClose: () => void;
}

export function CandidateResumeModal({ 
  candidateId, 
  isOpen, 
  onClose 
}: CandidateResumeModalProps) {
  const [resumeData, setResumeData] = useState<CandidateResumeData | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const fetchResumeData = async (id: string) => {
    setLoading(true);
    setError(null);
    
    try {
      console.log("Fetching resume for candidate ID:", id);
      const apiUrl = process.env.NEXT_PUBLIC_API_URL || "http://localhost:8001/api/v1";
      const response = await fetch(`${apiUrl}/candidates/resume/${id}`);
      const data = await response.json();
      
      console.log("Resume API response:", response.status, data);
      
      if (response.ok) {
        setResumeData(data.candidate);
      } else {
        setError(data.detail || "Failed to load resume");
      }
    } catch (err) {
      console.error("Resume fetch error:", err);
      setError("Network error loading resume");
    } finally {
      setLoading(false);
    }
  };

  // Fetch resume data when modal opens
  useEffect(() => {
    if (candidateId && isOpen) {
      console.log("Modal opened for candidate ID:", candidateId);
      setResumeData(null); // Reset previous data
      fetchResumeData(candidateId);
    } else if (!isOpen) {
      // Clear data when modal closes
      setResumeData(null);
      setError(null);
    }
  }, [candidateId, isOpen]);

  const handleClose = () => {
    setResumeData(null);
    setError(null);
    onClose();
  };

  const formatSkills = (skills: string[] | string | undefined) => {
    if (!skills) return [];
    if (typeof skills === 'string') {
      return skills.split(',').map(s => s.trim()).filter(Boolean);
    }
    return Array.isArray(skills) ? skills : [];
  };

  return (
    <Dialog open={isOpen} onOpenChange={handleClose}>
      <DialogContent className="max-w-4xl max-h-[90vh] overflow-y-auto">
        <DialogHeader>
          <DialogTitle className="flex items-center justify-between">
            <span>Candidate Resume</span>
            <div className="flex items-center space-x-2">
              <Button variant="outline" size="sm">
                <Download className="w-4 h-4 mr-2" />
                Export PDF
              </Button>
              <Button variant="outline" size="sm">
                <ExternalLink className="w-4 h-4 mr-2" />
                View in JobDiva
              </Button>
            </div>
          </DialogTitle>
        </DialogHeader>

        {loading && (
          <div className="flex items-center justify-center py-12 space-x-2">
            <Loader2 className="w-6 h-6 animate-spin" />
            <span>Loading resume...</span>
          </div>
        )}

        {error && (
          <div className="text-center py-12">
            <div className="text-red-500 mb-2">Error loading resume</div>
            <div className="text-sm text-muted-foreground mb-4">{error}</div>
            <Button 
              variant="outline" 
              onClick={() => candidateId && fetchResumeData(candidateId)}
            >
              Try Again
            </Button>
          </div>
        )}

        {resumeData && (
          <div className="space-y-6">
            {/* Header Section */}
            <div className="border rounded-lg p-6 bg-slate-50">
              <div className="flex items-start justify-between mb-4">
                <div>
                  <h2 className="text-2xl font-bold text-gray-900">{resumeData.name}</h2>
                  {resumeData.title && (
                    <p className="text-lg text-gray-600 mt-1">{resumeData.title}</p>
                  )}
                </div>
                <Badge variant="secondary" className="ml-4">
                  {resumeData.source}
                </Badge>
              </div>
              
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4 text-sm">
                {resumeData.email && (
                  <div className="flex items-center space-x-2">
                    <Mail className="w-4 h-4 text-gray-500" />
                    <span>{resumeData.email}</span>
                  </div>
                )}
                {resumeData.phone && (
                  <div className="flex items-center space-x-2">
                    <Phone className="w-4 h-4 text-gray-500" />
                    <span>{resumeData.phone}</span>
                  </div>
                )}
                {resumeData.location && (
                  <div className="flex items-center space-x-2">
                    <MapPin className="w-4 h-4 text-gray-500" />
                    <span>{resumeData.location}</span>
                  </div>
                )}
              </div>
            </div>

            {/* No Resume Available */}
            {(!resumeData.resume_text || resumeData.resume_text.trim() === '') ? (
              <div className="text-center py-12 border-2 border-dashed border-gray-200 rounded-lg bg-gray-50">
                <FileText className="w-16 h-16 text-gray-400 mx-auto mb-4" />
                <h3 className="text-lg font-semibold text-gray-700 mb-2">No Resume Available</h3>
                <p className="text-gray-500 mb-4">
                  This candidate hasn't uploaded a resume yet.
                </p>
                <div className="text-sm text-gray-400">
                  Contact information and basic details are available above.
                </div>
              </div>
            ) : (
              <>
                {/* Skills Section */}
                {formatSkills(resumeData.skills).length > 0 && (
                  <div>
                    <h3 className="text-lg font-semibold mb-3 flex items-center">
                      <Briefcase className="w-5 h-5 mr-2" />
                      Skills & Technologies
                    </h3>
                    <div className="flex flex-wrap gap-2">
                      {formatSkills(resumeData.skills).map((skill, index) => (
                        <Badge key={index} variant="outline" className="px-3 py-1">
                          {skill}
                        </Badge>
                      ))}
                    </div>
                  </div>
                )}

                <Separator />

                {/* Resume Content */}
                <div>
                  <h3 className="text-lg font-semibold mb-3 flex items-center">
                    <FileText className="w-5 h-5 mr-2" />
                    Resume Content
                  </h3>
                  <div className="bg-white border rounded-lg p-6">
                    <pre className="whitespace-pre-wrap text-sm leading-relaxed font-mono">
                      {resumeData.resume_text}
                    </pre>
                  </div>
                </div>
              </>
            )}

            {/* Resume Text Section */}
            {resumeData.resume_text && (
              <div>
                <h3 className="text-lg font-semibold mb-3 flex items-center">
                  <FileText className="w-5 h-5 mr-2" />
                  Resume Summary
                </h3>
                <div className="bg-white border rounded-lg p-4">
                  <p className="text-gray-700 whitespace-pre-wrap leading-relaxed">
                    {resumeData.resume_text}
                  </p>
                </div>
              </div>
            )}

            {/* Experience Section */}
            {resumeData.experience && (
              <div>
                <h3 className="text-lg font-semibold mb-3 flex items-center">
                  <Briefcase className="w-5 h-5 mr-2" />
                  Work Experience
                </h3>
                <div className="bg-white border rounded-lg p-4">
                  <p className="text-gray-700 whitespace-pre-wrap leading-relaxed">
                    {resumeData.experience}
                  </p>
                </div>
              </div>
            )}

            {/* Education Section */}
            {resumeData.education && (
              <div>
                <h3 className="text-lg font-semibold mb-3 flex items-center">
                  <GraduationCap className="w-5 h-5 mr-2" />
                  Education
                </h3>
                <div className="bg-white border rounded-lg p-4">
                  <p className="text-gray-700 whitespace-pre-wrap leading-relaxed">
                    {resumeData.education}
                  </p>
                </div>
              </div>
            )}

            {/* Action Buttons */}
            <div className="flex justify-end space-x-3 pt-4 border-t">
              <Button variant="outline" onClick={handleClose}>
                Close
              </Button>
              <Button onClick={() => resumeData.email && window.open(`mailto:${resumeData.email}`)}>
                <Mail className="w-4 h-4 mr-2" />
                Send Email
              </Button>
            </div>
          </div>
        )}
      </DialogContent>
    </Dialog>
  );
}