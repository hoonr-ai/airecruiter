"use client";

import { useState, useEffect } from "react";
import { useParams, useRouter } from "next/navigation";
import Link from "next/link";
import { ArrowLeft, Save, Edit3, Building2, MapPin, Calendar, DollarSign, User } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Badge } from "@/components/ui/badge";
const getStatusColor = (status: string) => {
  switch (status?.toLowerCase()) {
    case 'open': return 'bg-green-100 text-green-800';
    case 'closed': return 'bg-red-100 text-red-800'; 
    case 'on hold': return 'bg-yellow-100 text-yellow-800';
    default: return 'bg-gray-100 text-gray-800';
  }
};
import { Card, CardHeader, CardTitle, CardContent } from "@/components/ui/card";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";

// Employment Type Options
const employmentTypeOptions = [
  { value: "Full-Time", label: "Full-Time" },
  { value: "Part-Time", label: "Part-Time" },
  { value: "Contract", label: "Contract" },
  { value: "Contract-to-Hire", label: "Contract-to-Hire" },
  { value: "Temporary", label: "Temporary" },
  { value: "Freelance", label: "Freelance" },
  { value: "Internship", label: "Internship" },
];

// Work Authorization Options
const workAuthOptions = [
  { value: "", label: "Not specified" },
  { value: "US Citizen", label: "US Citizen" },
  { value: "Green Card", label: "Green Card" },
  { value: "H1B", label: "H1B" },
  { value: "H1B Transfer", label: "H1B Transfer" },
  { value: "TN Visa", label: "TN Visa" },
  { value: "L1 Visa", label: "L1 Visa" },
  { value: "EAD", label: "EAD" },
  { value: "OPT", label: "OPT" },
  { value: "CPT", label: "CPT" },
  { value: "F1 Visa", label: "F1 Visa" },
  { value: "Work Authorization Required", label: "Work Authorization Required" },
  { value: "No Sponsorship", label: "No Sponsorship" },
  { value: "Sponsorship Available", label: "Sponsorship Available" },
  { value: "Any Work Authorization", label: "Any Work Authorization" },
];

interface JobDetailData {
  id: string;
  title: string;
  status: string;
  customer_name: string;
  employment_type?: string;
  recruiter_notes?: string;
  jobdiva_description?: string;
  city?: string;
  state?: string;
  pay_rate?: string;
  posted_date?: string;
  start_date?: string;
  work_authorization?: string;
  location_type?: string;
  recruiter_emails?: string[]; // JSONB array from backend
}

export default function JobDetailPage() {
  const params = useParams();
  const router = useRouter();
  const jobId = params.jobId as string;

  const [jobData, setJobData] = useState<JobDetailData | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [isEditing, setIsEditing] = useState(false);
  const [isSaving, setSaving] = useState(false);
  const [formData, setFormData] = useState({
    employment_type: "",
    recruiter_notes: "",
    work_authorization: "",
    recruiter_emails: [] as string[],
  });
  const [recruiterEmailInput, setRecruiterEmailInput] = useState("");
  const [toast, setToast] = useState<{ message: string; type: "success" | "error" } | null>(null);

  const employmentTypeOptions = [
    { value: "Full-Time", label: "Full-Time" },
    { value: "Contract", label: "Contract" },
    { value: "W2", label: "W2" },
    { value: "1099", label: "1099" },
    { value: "C2C", label: "C2C" },
    { value: "Part-Time", label: "Part-Time" },
  ];

  useEffect(() => {
    fetchJobDetail();
  }, [jobId]);

  useEffect(() => {
    if (toast) {
      const timer = setTimeout(() => setToast(null), 3000);
      return () => clearTimeout(timer);
    }
  }, [toast]);

  const fetchJobDetail = async () => {
    setIsLoading(true);
    try {
      const response = await fetch(`http://localhost:8001/jobs/monitored`);
      const data = await response.json();
      const job = data.jobs[jobId];
      
      if (job) {
        const jobDetail = {
          id: jobId,
          ...job
        };
        setJobData(jobDetail);
        setFormData({
          employment_type: job.employment_type || "",
          recruiter_notes: job.recruiter_notes || "",
          work_authorization: job.work_authorization || "",
          recruiter_emails: Array.isArray(job.recruiter_emails) ? job.recruiter_emails : [],
        });
      } else {
        setToast({ message: "Job not found", type: "error" });
      }
    } catch (error) {
      console.error("Error fetching job:", error);
      setToast({ message: "Failed to load job details", type: "error" });
    } finally {
      setIsLoading(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const response = await fetch(`http://localhost:8001/jobs/${jobId}/basic-info`, {
        method: "PUT",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(formData),
      });

      if (response.ok) {
        setToast({ message: "Job updated successfully", type: "success" });
        setIsEditing(false);
        await fetchJobDetail(); // Refresh data
      } else {
        setToast({ message: "Failed to update job", type: "error" });
      }
    } catch (error) {
      console.error("Error updating job:", error);
      setToast({ message: "Failed to update job", type: "error" });
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = () => {
    setFormData({
      employment_type: jobData?.employment_type || "",
      recruiter_notes: jobData?.recruiter_notes || "",
      work_authorization: jobData?.work_authorization || "",
      recruiter_emails: Array.isArray(jobData?.recruiter_emails) ? jobData.recruiter_emails : [],
    });
    setRecruiterEmailInput("");
    setIsEditing(false);
  };

  const addRecruiterEmail = () => {
    const email = recruiterEmailInput.trim();
    if (email && !formData.recruiter_emails.includes(email)) {
      setFormData({
        ...formData,
        recruiter_emails: [...formData.recruiter_emails, email]
      });
      setRecruiterEmailInput("");
    }
  };

  const removeRecruiterEmail = (emailToRemove: string) => {
    setFormData({
      ...formData,
      recruiter_emails: formData.recruiter_emails.filter(email => email !== emailToRemove)
    });
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addRecruiterEmail();
    }
  };

  const getStatusColor = (status: string) => {
    const s = status?.toLowerCase() || "";
    if (s === 'open') return 'bg-[#dcfce7] text-[#166534]';
    if (s === 'completed') return 'bg-[#ffedd5] text-[#c2410c]';
    if (s === 'cancelled' || s === 'closed') return 'bg-[#fee2e2] text-[#b91c1c]';
    return 'bg-slate-100 text-slate-700';
  };

  if (isLoading) {
    return (
      <div className="max-w-6xl mx-auto p-6">
        <div className="animate-pulse">
          <div className="h-8 bg-slate-200 rounded w-1/4 mb-6"></div>
          <div className="h-32 bg-slate-200 rounded mb-4"></div>
          <div className="h-64 bg-slate-200 rounded"></div>
        </div>
      </div>
    );
  }

  if (!jobData) {
    return (
      <div className="max-w-6xl mx-auto p-6">
        <div className="text-center py-16">
          <h1 className="text-2xl font-bold text-slate-900 mb-4">Job Not Found</h1>
          <p className="text-slate-600 mb-6">The job you're looking for doesn't exist or has been removed.</p>
          <Button asChild>
            <Link href="/jobs">Back to Jobs</Link>
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="max-w-6xl mx-auto p-6 space-y-6">
      {/* Toast notification */}
      {toast && (
        <div className={`fixed top-4 right-4 z-50 rounded-lg p-4 text-white ${
          toast.type === 'success' ? 'bg-green-500' : 'bg-red-500'
        }`}>
          {toast.message}
        </div>
      )}

      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-4">
          <Button variant="ghost" asChild className="p-2">
            <Link href="/jobs">
              <ArrowLeft className="h-5 w-5" />
            </Link>
          </Button>
          <div>
            <h1 className="text-2xl font-bold text-slate-900">{jobData.title}</h1>
            <p className="text-slate-600">Job ID: {jobData.id}</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          <Badge variant="secondary" className={`${getStatusColor(jobData.status)} border-0`}>
            {jobData.status}
          </Badge>
          {!isEditing ? (
            <Button onClick={() => setIsEditing(true)} className="flex items-center gap-2">
              <Edit3 className="h-4 w-4" />
              Edit Details
            </Button>
          ) : (
            <div className="flex items-center gap-2">
              <Button variant="outline" onClick={handleCancel} disabled={isSaving}>
                Cancel
              </Button>
              <Button onClick={handleSave} disabled={isSaving} className="flex items-center gap-2">
                <Save className="h-4 w-4" />
                {isSaving ? "Saving..." : "Save"}
              </Button>
            </div>
          )}
        </div>
      </div>

      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Main Job Details */}
        <div className="lg:col-span-2 space-y-6">
          {/* Basic Information Card */}
          <Card>
            <CardHeader>
              <CardTitle>Basic Information</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
                <div>
                  <Label htmlFor="employment_type">Employment Type</Label>
                  {isEditing ? (
                    <Select value={formData.employment_type} onValueChange={(value) => setFormData({...formData, employment_type: value})}>
                      <SelectTrigger>
                        <SelectValue placeholder="Select employment type" />
                      </SelectTrigger>
                      <SelectContent>
                        {employmentTypeOptions.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <p className="text-sm text-slate-700 mt-1">{jobData.employment_type || "Not specified"}</p>
                  )}
                </div>

                <div>
                  <Label htmlFor="work_authorization">Work Authorization</Label>
                  {isEditing ? (
                    <Select value={formData.work_authorization} onValueChange={(value) => setFormData({...formData, work_authorization: value})}>
                      <SelectTrigger>
                        <SelectValue placeholder="Select work authorization" />
                      </SelectTrigger>
                      <SelectContent>
                        {workAuthOptions.map((option) => (
                          <SelectItem key={option.value} value={option.value}>
                            {option.label}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  ) : (
                    <p className="text-sm text-slate-700 mt-1">{jobData.work_authorization || "Not specified"}</p>
                  )}
                </div>
              </div>

              <div>
                <Label>Customer</Label>
                <div className="flex items-center gap-2 mt-1">
                  <Building2 className="h-4 w-4 text-slate-500" />
                  <span className="text-sm text-slate-700">{jobData.customer_name}</span>
                </div>
              </div>

              {/* Recruiter Emails Section */}
              <div>
                <Label htmlFor="recruiter_emails">Recruiter Emails</Label>
                {isEditing ? (
                  <div className="space-y-2 mt-1">
                    <div className="flex gap-2">
                      <Input
                        type="email"
                        placeholder="Enter recruiter email..."
                        value={recruiterEmailInput}
                        onChange={(e) => setRecruiterEmailInput(e.target.value)}
                        onKeyPress={handleKeyPress}
                        className="flex-1"
                      />
                      <Button type="button" onClick={addRecruiterEmail} size="sm">
                        Add
                      </Button>
                    </div>
                    <div className="flex flex-wrap gap-2">
                      {formData.recruiter_emails.map((email, index) => (
                        <Badge key={index} variant="secondary" className="flex items-center gap-1">
                          {email}
                          <button
                            type="button"
                            onClick={() => removeRecruiterEmail(email)}
                            className="ml-1 text-slate-500 hover:text-slate-700"
                          >
                            ×
                          </button>
                        </Badge>
                      ))}
                    </div>
                  </div>
                ) : (
                  <div className="mt-1">
                    {Array.isArray(jobData.recruiter_emails) && jobData.recruiter_emails.length > 0 ? (
                      <div className="flex flex-wrap gap-2">
                        {jobData.recruiter_emails.map((email, index) => (
                          <Badge key={index} variant="outline" className="text-sm">
                            {email}
                          </Badge>
                        ))}
                      </div>
                    ) : (
                      <p className="text-sm text-slate-500">No recruiter emails</p>
                    )}
                  </div>
                )}
              </div>
              
              <div>
                <Label htmlFor="recruiter_notes">Recruiter Notes</Label>
                {isEditing ? (
                  <Textarea
                    id="recruiter_notes"
                    value={formData.recruiter_notes}
                    onChange={(e) => setFormData({...formData, recruiter_notes: e.target.value})}
                    placeholder="Add notes about this job..."
                    rows={4}
                    className="mt-1"
                  />
                ) : (
                  <p className="text-sm text-slate-700 mt-1 p-2 bg-slate-50 rounded border min-h-[100px]">
                    {jobData.recruiter_notes || "No notes added"}
                  </p>
                )}
              </div>
            </CardContent>
          </Card>

          {/* Job Description Card */}
          <Card>
            <CardHeader>
              <CardTitle>Job Description</CardTitle>
            </CardHeader>
            <CardContent>
              <div className="prose prose-sm max-w-none">
                <pre className="whitespace-pre-wrap text-sm text-slate-700 leading-relaxed">
                  {jobData.jobdiva_description || "No description available"}
                </pre>
              </div>
            </CardContent>
          </Card>
        </div>

        {/* Sidebar Info */}
        <div className="space-y-6">
          <Card>
            <CardHeader>
              <CardTitle>Job Details</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              {jobData.city && jobData.state && (
                <div className="flex items-center gap-2">
                  <MapPin className="h-4 w-4 text-slate-500" />
                  <span className="text-sm">{jobData.city}, {jobData.state}</span>
                </div>
              )}
              
              {jobData.pay_rate && (
                <div className="flex items-center gap-2">
                  <DollarSign className="h-4 w-4 text-slate-500" />
                  <span className="text-sm">{jobData.pay_rate}</span>
                </div>
              )}
              
              {jobData.posted_date && (
                <div className="flex items-center gap-2">
                  <Calendar className="h-4 w-4 text-slate-500" />
                  <span className="text-sm">Posted: {jobData.posted_date}</span>
                </div>
              )}
              
              {jobData.start_date && (
                <div className="flex items-center gap-2">
                  <Calendar className="h-4 w-4 text-slate-500" />
                  <span className="text-sm">Start: {jobData.start_date}</span>
                </div>
              )}
              
              {jobData.work_authorization && (
                <div className="flex items-center gap-2">
                  <User className="h-4 w-4 text-slate-500" />
                  <span className="text-sm">Auth: {jobData.work_authorization}</span>
                </div>
              )}
            </CardContent>
          </Card>
          
          <Card>
            <CardHeader>
              <CardTitle>Actions</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              <Button variant="outline" className="w-full">View Candidates</Button>
              <Button variant="outline" className="w-full">Export Job Details</Button>
              <Button variant="outline" className="w-full text-red-600 hover:text-red-700">
                Archive Job
              </Button>
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
}