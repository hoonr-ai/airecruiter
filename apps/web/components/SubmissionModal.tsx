'use client';

import React, { useState } from 'react';
import { ExternalLink, User, Briefcase, Building2, Zap, Send, ArrowLeft, Mail, FileText, CheckCircle2 } from 'lucide-react';
import { Button } from '@/components/ui/button';

export interface SubmissionModalProps {
  isOpen: boolean;
  onClose: () => void;
  candidateName?: string;
  candidateId?: string | number;
  jobTitle?: string;
  jobRef?: string;
  clientName?: string;
  onConfirmSubmit: (submissionData: {
    submission_type: 'internal' | 'external';
    manager_email?: string;
    recruiter_notes?: string;
  }) => Promise<void>;
  isSubmitting?: boolean;
}

export function SubmissionModal({
  isOpen,
  onClose,
  candidateName,
  jobTitle,
  jobRef,
  clientName,
  onConfirmSubmit,
  isSubmitting = false,
}: SubmissionModalProps) {
  // Mode: 'choose' | 'internal' | 'external'
  const [selectedMode, setSelectedMode] = useState<'choose' | 'internal' | 'external'>('choose');
  const [managerEmail, setManagerEmail] = useState('');
  const [recruiterNotes, setRecruiterNotes] = useState('');
  const [emailError, setEmailError] = useState('');

  if (!isOpen) return null;

  const handleClose = () => {
    setSelectedMode('choose');
    setManagerEmail('');
    setRecruiterNotes('');
    setEmailError('');
    onClose();
  };

  const validateEmail = (email: string) => {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email.trim());
  };

  const handleInternalSubmit = async () => {
    if (!managerEmail.trim()) {
      setEmailError("Manager's email is required");
      return;
    }
    if (!validateEmail(managerEmail)) {
      setEmailError('Please enter a valid email address');
      return;
    }
    setEmailError('');
    await onConfirmSubmit({
      submission_type: 'internal',
      manager_email: managerEmail.trim(),
      recruiter_notes: recruiterNotes.trim() || undefined,
    });
  };

  const handleExternalSubmit = async () => {
    await onConfirmSubmit({
      submission_type: 'external',
    });
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4 no-print animate-in fade-in duration-150">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden border border-slate-200">
        
        {/* Modal Header */}
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {selectedMode !== 'choose' && (
              <button
                type="button"
                onClick={() => setSelectedMode('choose')}
                className="p-1 -ml-1 text-slate-400 hover:text-slate-700 rounded-lg hover:bg-slate-100 transition-colors"
                title="Back to options"
              >
                <ArrowLeft className="w-4 h-4" />
              </button>
            )}
            <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
              <ExternalLink className="w-5 h-5 text-indigo-600" />
              {selectedMode === 'choose' && 'Submit Candidate'}
              {selectedMode === 'internal' && 'Internal Submission to Manager'}
              {selectedMode === 'external' && 'External Submission to JobDiva'}
            </h3>
          </div>
          <button
            onClick={handleClose}
            className="text-slate-400 hover:text-slate-600 text-lg font-bold p-1 rounded-md"
          >
            ×
          </button>
        </div>

        {/* Modal Body */}
        <div className="p-6 space-y-4">
          
          {/* STEP 1: Choose Submission Type */}
          {selectedMode === 'choose' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">
                Choose how you would like to submit <strong className="text-slate-900 font-semibold">{candidateName || 'Candidate'}</strong>:
              </p>

              <div className="grid grid-cols-1 gap-3 pt-1">
                {/* Internal Option */}
                <button
                  type="button"
                  onClick={() => setSelectedMode('internal')}
                  className="group flex items-start gap-4 p-4 rounded-xl border border-slate-200 bg-white hover:border-indigo-500 hover:bg-indigo-50/40 text-left transition-all shadow-sm hover:shadow"
                >
                  <div className="w-10 h-10 rounded-lg bg-indigo-100 text-indigo-600 flex items-center justify-center shrink-0 group-hover:bg-indigo-600 group-hover:text-white transition-colors">
                    <Send className="w-5 h-5" />
                  </div>
                  <div className="flex-1">
                    <h4 className="text-sm font-bold text-slate-900 group-hover:text-indigo-700">
                      Internal Submission
                    </h4>
                    <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                      Submit to your manager for internal review. Your manager receives an email with the candidate report to approve and submit externally.
                    </p>
                  </div>
                </button>

                {/* External Option */}
                <button
                  type="button"
                  onClick={() => setSelectedMode('external')}
                  className="group flex items-start gap-4 p-4 rounded-xl border border-slate-200 bg-white hover:border-indigo-500 hover:bg-indigo-50/40 text-left transition-all shadow-sm hover:shadow"
                >
                  <div className="w-10 h-10 rounded-lg bg-emerald-100 text-emerald-600 flex items-center justify-center shrink-0 group-hover:bg-emerald-600 group-hover:text-white transition-colors">
                    <ExternalLink className="w-5 h-5" />
                  </div>
                  <div className="flex-1">
                    <h4 className="text-sm font-bold text-slate-900 group-hover:text-indigo-700">
                      External Submission (JobDiva)
                    </h4>
                    <p className="text-xs text-slate-500 mt-1 leading-relaxed">
                      Direct external submission to JobDiva logged as <span className="font-semibold text-slate-700">PAIR External Submission</span>.
                    </p>
                  </div>
                </button>
              </div>

              {/* Summary Card */}
              <div className="bg-slate-50 p-3.5 rounded-xl border border-slate-100 space-y-2 text-xs text-slate-600 mt-3">
                <div className="flex items-center gap-2">
                  <User className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="truncate"><strong className="text-slate-800">Candidate:</strong> {candidateName || '—'}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Briefcase className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="truncate"><strong className="text-slate-800">Job:</strong> {jobTitle} ({jobRef})</span>
                </div>
                <div className="flex items-center gap-2">
                  <Building2 className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="truncate"><strong className="text-slate-800">Client:</strong> {clientName || '—'}</span>
                </div>
              </div>
            </div>
          )}

          {/* STEP 2A: Internal Submission Form */}
          {selectedMode === 'internal' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">
                Send candidate for internal review. The manager will receive an email linking directly to the candidate's report.
              </p>

              <div className="space-y-3">
                <div>
                  <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5 flex items-center gap-1.5">
                    <Mail className="w-3.5 h-3.5 text-slate-400" />
                    Manager's Email <span className="text-rose-500">*</span>
                  </label>
                  <input
                    type="email"
                    placeholder="manager@pyramidci.com"
                    value={managerEmail}
                    onChange={(e) => {
                      setManagerEmail(e.target.value);
                      if (emailError) setEmailError('');
                    }}
                    className={`w-full px-3 py-2 text-sm border rounded-lg focus:outline-none focus:ring-2 transition-all ${
                      emailError
                        ? 'border-rose-400 focus:ring-rose-500/20'
                        : 'border-slate-200 focus:border-indigo-500 focus:ring-indigo-500/20'
                    }`}
                  />
                  {emailError && <p className="text-xs text-rose-500 mt-1">{emailError}</p>}
                </div>

                <div>
                  <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider mb-1.5 flex items-center gap-1.5">
                    <FileText className="w-3.5 h-3.5 text-slate-400" />
                    Notes / Comments for Manager <span className="text-slate-400 font-normal lowercase">(optional)</span>
                  </label>
                  <textarea
                    rows={3}
                    placeholder="E.g., Great technical match, strong in Python & React, available immediately..."
                    value={recruiterNotes}
                    onChange={(e) => setRecruiterNotes(e.target.value)}
                    className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:border-indigo-500 focus:ring-indigo-500/20 resize-none transition-all"
                  />
                </div>
              </div>

              {/* Summary Card */}
              <div className="bg-indigo-50/50 p-3.5 rounded-xl border border-indigo-100/70 space-y-2 text-xs text-slate-700">
                <div className="flex items-center gap-2">
                  <CheckCircle2 className="w-3.5 h-3.5 text-indigo-600 shrink-0" />
                  <span>Will log as <strong className="text-indigo-900">PAIR Internal Submission</strong> in JobDiva</span>
                </div>
                <div className="flex items-center gap-2">
                  <User className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="truncate"><strong className="text-slate-800">Candidate:</strong> {candidateName}</span>
                </div>
                <div className="flex items-center gap-2">
                  <Briefcase className="w-3.5 h-3.5 text-slate-400 shrink-0" />
                  <span className="truncate"><strong className="text-slate-800">Job:</strong> {jobTitle} ({jobRef})</span>
                </div>
              </div>
            </div>
          )}

          {/* STEP 2B: External Submission Confirmation */}
          {selectedMode === 'external' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">
                This action will initiate an <strong className="text-slate-900 font-semibold">external submission in JobDiva</strong> for:
              </p>

              <div className="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-3 text-sm text-slate-700">
                <div className="flex items-center gap-2.5">
                  <User className="w-4 h-4 text-slate-400 shrink-0" />
                  <p><strong className="text-slate-900">Candidate:</strong> {candidateName || '—'}</p>
                </div>
                <div className="flex items-center gap-2.5">
                  <Briefcase className="w-4 h-4 text-slate-400 shrink-0" />
                  <p><strong className="text-slate-900">Job:</strong> {jobTitle} ({jobRef})</p>
                </div>
                <div className="flex items-center gap-2.5">
                  <Building2 className="w-4 h-4 text-slate-400 shrink-0" />
                  <p><strong className="text-slate-900">Client:</strong> {clientName || '—'}</p>
                </div>
                <div className="flex items-center gap-2.5">
                  <Zap className="w-4 h-4 text-indigo-600 shrink-0" />
                  <p><strong className="text-slate-900">Action:</strong> Log <span className="font-semibold text-indigo-700">PAIR External Submission</span> in JobDiva</p>
                </div>
              </div>
            </div>
          )}

        </div>

        {/* Modal Footer */}
        <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex items-center justify-between">
          <div>
            {selectedMode !== 'choose' ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => setSelectedMode('choose')}
                className="text-slate-600 hover:text-slate-900 font-medium text-xs flex items-center gap-1 px-2"
                disabled={isSubmitting}
              >
                <ArrowLeft className="w-3.5 h-3.5" /> Back
              </Button>
            ) : (
              <span className="text-xs text-slate-400">Select an option to proceed</span>
            )}
          </div>

          <div className="flex items-center gap-2.5">
            <Button
              variant="outline"
              size="sm"
              onClick={handleClose}
              disabled={isSubmitting}
              className="font-semibold text-slate-600"
            >
              Cancel
            </Button>

            {selectedMode === 'internal' && (
              <Button
                size="sm"
                className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold flex items-center gap-1.5"
                onClick={handleInternalSubmit}
                disabled={isSubmitting}
              >
                {isSubmitting ? (
                  'Sending...'
                ) : (
                  <>
                    <Send className="w-3.5 h-3.5" /> Send to Manager
                  </>
                )}
              </Button>
            )}

            {selectedMode === 'external' && (
              <Button
                size="sm"
                className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold"
                onClick={handleExternalSubmit}
                disabled={isSubmitting}
              >
                {isSubmitting ? 'Syncing...' : 'Confirm & Submit to JobDiva'}
              </Button>
            )}
          </div>
        </div>

      </div>
    </div>
  );
}
