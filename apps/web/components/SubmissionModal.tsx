'use client';

import { useEffect, useState } from 'react';
import { ArrowLeft, Briefcase, Building2, ExternalLink, FileText, Mail, Send, User, Zap } from 'lucide-react';
import { Button } from '@/components/ui/button';

export type SubmissionPayload = {
  submission_type: 'internal' | 'external';
  manager_email?: string;
  recruiter_notes?: string;
};

type SubmissionModalProps = {
  isOpen: boolean;
  onClose: () => void;
  candidateName?: string;
  jobTitle?: string;
  jobRef?: string;
  clientName?: string;
  onConfirmSubmit: (submissionData: SubmissionPayload) => Promise<void>;
  isSubmitting?: boolean;
  defaultMode?: 'choose' | 'internal' | 'external';
};

const isValidEmail = (value: string) => /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(value.trim());

export function SubmissionModal({
  isOpen,
  onClose,
  candidateName = 'Candidate',
  jobTitle = 'Job',
  jobRef = '',
  clientName = '-',
  onConfirmSubmit,
  isSubmitting = false,
  defaultMode = 'choose',
}: SubmissionModalProps) {
  const [mode, setMode] = useState<'choose' | 'internal' | 'external'>(defaultMode);
  const [managerEmail, setManagerEmail] = useState('');
  const [recruiterNotes, setRecruiterNotes] = useState('');
  const [emailError, setEmailError] = useState('');

  useEffect(() => {
    if (!isOpen) return;
    setMode(defaultMode);
    setManagerEmail('');
    setRecruiterNotes('');
    setEmailError('');
  }, [defaultMode, isOpen]);

  if (!isOpen) return null;

  const resetAndClose = () => {
    setMode(defaultMode);
    setManagerEmail('');
    setRecruiterNotes('');
    setEmailError('');
    onClose();
  };

  const submitInternal = async () => {
    const email = managerEmail.trim();
    if (!email) {
      setEmailError("Manager's email is required");
      return;
    }
    if (!isValidEmail(email)) {
      setEmailError('Enter a valid manager email');
      return;
    }
    setEmailError('');
    await onConfirmSubmit({
      submission_type: 'internal',
      manager_email: email,
      recruiter_notes: recruiterNotes.trim() || undefined,
    });
  };

  const submitExternal = async () => {
    await onConfirmSubmit({ submission_type: 'external' });
  };

  return (
    <div className="fixed inset-0 z-[100] flex items-center justify-center bg-slate-900/40 backdrop-blur-sm p-4 no-print">
      <div className="bg-white rounded-2xl shadow-xl w-full max-w-lg overflow-hidden border border-slate-200">
        <div className="px-6 py-4 border-b border-slate-100 flex items-center justify-between">
          <div className="flex items-center gap-2">
            {mode !== 'choose' && defaultMode === 'choose' && (
              <button
                type="button"
                onClick={() => setMode('choose')}
                className="p-1 -ml-1 text-slate-400 hover:text-slate-700 rounded-lg hover:bg-slate-100 transition-colors"
                aria-label="Back to submission options"
              >
                <ArrowLeft className="w-4 h-4" />
              </button>
            )}
            <h3 className="text-lg font-bold text-slate-900 flex items-center gap-2">
              <ExternalLink className="w-5 h-5 text-indigo-600" />
              {mode === 'choose' ? 'Submit Candidate' : mode === 'internal' ? 'Internal Submission to Manager' : 'External Submission to JobDiva'}
            </h3>
          </div>
          <button onClick={resetAndClose} className="text-slate-400 hover:text-slate-600" aria-label="Close">x</button>
        </div>

        <div className="p-6 space-y-4">
          {mode === 'choose' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">
                Choose how to submit <strong className="text-slate-900 font-semibold">{candidateName}</strong>.
              </p>
              <div className="grid gap-3">
                <button type="button" onClick={() => setMode('internal')} className="group flex items-start gap-4 p-4 rounded-xl border border-slate-200 bg-white hover:border-indigo-500 hover:bg-indigo-50/40 text-left transition-all shadow-sm hover:shadow">
                  <span className="w-10 h-10 rounded-lg bg-indigo-100 text-indigo-600 flex items-center justify-center shrink-0 group-hover:bg-indigo-600 group-hover:text-white transition-colors"><Send className="w-5 h-5" /></span>
                  <span>
                    <span className="block text-sm font-bold text-slate-900">Internal Submission</span>
                    <span className="block text-xs text-slate-500 mt-1 leading-relaxed">Send to your manager for review. This does not externally submit the candidate.</span>
                  </span>
                </button>
                <button type="button" onClick={() => setMode('external')} className="group flex items-start gap-4 p-4 rounded-xl border border-slate-200 bg-white hover:border-emerald-500 hover:bg-emerald-50/40 text-left transition-all shadow-sm hover:shadow">
                  <span className="w-10 h-10 rounded-lg bg-emerald-100 text-emerald-600 flex items-center justify-center shrink-0 group-hover:bg-emerald-600 group-hover:text-white transition-colors"><ExternalLink className="w-5 h-5" /></span>
                  <span>
                    <span className="block text-sm font-bold text-slate-900">External Submission</span>
                    <span className="block text-xs text-slate-500 mt-1 leading-relaxed">Submit externally through JobDiva and log PAIR External Submission.</span>
                  </span>
                </button>
              </div>
            </div>
          )}

          {mode === 'internal' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">Send this candidate to a manager for internal review.</p>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider">
                <span className="flex items-center gap-1.5 mb-1.5"><Mail className="w-3.5 h-3.5 text-slate-400" /> Manager's Email</span>
                <input
                  type="email"
                  value={managerEmail}
                  onChange={(event) => { setManagerEmail(event.target.value); setEmailError(''); }}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:border-indigo-500 focus:ring-indigo-500/20 normal-case font-normal tracking-normal"
                  placeholder="manager@pyramidci.com"
                />
                {emailError && <span className="block text-xs text-rose-500 mt-1 normal-case font-medium tracking-normal">{emailError}</span>}
              </label>
              <label className="block text-xs font-bold text-slate-700 uppercase tracking-wider">
                <span className="flex items-center gap-1.5 mb-1.5"><FileText className="w-3.5 h-3.5 text-slate-400" /> Notes / Comments for Manager</span>
                <textarea
                  rows={3}
                  value={recruiterNotes}
                  onChange={(event) => setRecruiterNotes(event.target.value)}
                  className="w-full px-3 py-2 text-sm border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:border-indigo-500 focus:ring-indigo-500/20 resize-none normal-case font-normal tracking-normal"
                  placeholder="Add context for the manager..."
                />
              </label>
              <Summary candidateName={candidateName} jobTitle={jobTitle} jobRef={jobRef} clientName={clientName} action="PAIR Internal Submission" />
            </div>
          )}

          {mode === 'external' && (
            <div className="space-y-4">
              <p className="text-sm text-slate-500">Confirm external submission to JobDiva.</p>
              <Summary candidateName={candidateName} jobTitle={jobTitle} jobRef={jobRef} clientName={clientName} action="PAIR External Submission" />
            </div>
          )}
        </div>

        <div className="px-6 py-4 border-t border-slate-100 bg-slate-50 flex justify-end gap-3">
          <Button variant="outline" onClick={resetAndClose} className="font-semibold text-slate-600" disabled={isSubmitting}>Cancel</Button>
          {mode === 'internal' && <Button onClick={submitInternal} disabled={isSubmitting} className="bg-indigo-600 hover:bg-indigo-700 text-white font-bold">{isSubmitting ? 'Sending...' : 'Send to Manager'}</Button>}
          {mode === 'external' && <Button onClick={submitExternal} disabled={isSubmitting} className="bg-emerald-600 hover:bg-emerald-700 text-white font-bold">{isSubmitting ? 'Syncing...' : 'Submit Externally'}</Button>}
        </div>
      </div>
    </div>
  );
}

function Summary({ candidateName, jobTitle, jobRef, clientName, action }: { candidateName: string; jobTitle: string; jobRef: string; clientName: string; action: string }) {
  return (
    <div className="bg-slate-50 p-4 rounded-xl border border-slate-100 space-y-3 text-sm text-slate-700">
      <div className="flex items-center gap-2.5"><User className="w-4 h-4 text-slate-400 shrink-0" /><p><strong className="text-slate-900">Candidate:</strong> {candidateName}</p></div>
      <div className="flex items-center gap-2.5"><Briefcase className="w-4 h-4 text-slate-400 shrink-0" /><p><strong className="text-slate-900">Job:</strong> {jobTitle} ({jobRef || '-'})</p></div>
      <div className="flex items-center gap-2.5"><Building2 className="w-4 h-4 text-slate-400 shrink-0" /><p><strong className="text-slate-900">Client:</strong> {clientName || '-'}</p></div>
      <div className="flex items-center gap-2.5"><Zap className="w-4 h-4 text-indigo-600 shrink-0" /><p><strong className="text-slate-900">Action:</strong> {action}</p></div>
    </div>
  );
}
