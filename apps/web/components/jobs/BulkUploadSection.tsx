"use client";

import type { RefObject } from "react";
import { FileInput, CloudDownload } from "lucide-react";
import { Button } from "@/components/ui/button";

interface BulkProgress {
  processed: number;
  failed: number;
  total: number;
}

interface BulkUploadSectionProps {
  jobRef: string;
  bulkFiles: File[];
  onBulkFilesChange: (files: File[]) => void;
  onClearProgress: () => void;
  isUploadingBulk: boolean;
  bulkProgress: BulkProgress | null;
  bulkFileInputRef: RefObject<HTMLInputElement | null>;
  onUpload: () => void;
}

export function BulkUploadSection({
  jobRef,
  bulkFiles,
  onBulkFilesChange,
  onClearProgress,
  isUploadingBulk,
  bulkProgress,
  bulkFileInputRef,
  onUpload,
}: BulkUploadSectionProps) {
  if (!jobRef) return null;

  return (
    <div className="mt-6 p-5 border border-slate-200 rounded-2xl bg-gradient-to-br from-[#f5f3ff]/40 to-white shadow-sm">
      <div className="flex items-center gap-2 mb-3">
        <FileInput className="w-4 h-4 text-[#6366f1]" />
        <h3 className="text-[13.5px] font-bold text-slate-800">Upload Resumes</h3>
        <span className="text-[11px] text-slate-500 font-medium">PDF, DOCX, or TXT — scored against this job's rubric</span>
      </div>
      <div className="flex flex-wrap items-center gap-3">
        <input
          ref={bulkFileInputRef}
          type="file"
          multiple
          accept=".pdf,.docx,.txt,.md"
          onChange={(e) => {
            const list = e.target.files ? Array.from(e.target.files) : [];
            onBulkFilesChange(list);
            onClearProgress();
          }}
          className="text-[12.5px] text-slate-700 file:mr-3 file:py-1.5 file:px-3 file:rounded-lg file:border-0 file:bg-[#ede9fe] file:text-[#6366f1] file:font-bold hover:file:bg-[#ddd6fe] cursor-pointer"
          disabled={isUploadingBulk}
        />
        <Button
          onClick={onUpload}
          disabled={isUploadingBulk || bulkFiles.length === 0}
          className="bg-[#6366f1] hover:bg-[#4f46e5] text-white font-bold h-9 px-4 rounded-lg flex items-center gap-2 shadow-sm text-[13px]"
        >
          {isUploadingBulk ? (
            <>
              <span className="w-3.5 h-3.5 border-2 border-white/30 border-t-white rounded-full animate-spin" />
              Scoring {bulkFiles.length}...
            </>
          ) : (
            <>
              <CloudDownload className="w-4 h-4 rotate-180" />
              Upload & Score ({bulkFiles.length || 0})
            </>
          )}
        </Button>
        {bulkProgress && !isUploadingBulk && (
          <span className="text-[12px] font-medium text-slate-600">
            Last batch: <span className="font-bold text-emerald-700">{bulkProgress.processed} processed</span>
            {bulkProgress.failed > 0 && <span className="font-bold text-rose-600 ml-1.5">{bulkProgress.failed} failed</span>}
          </span>
        )}
      </div>
    </div>
  );
}
