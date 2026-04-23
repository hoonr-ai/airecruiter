# Implementation Prompt

> **Prompt:**
> "Implement a full-stack Engage and Assess button workflow as described in this document. The Engage button should allow preview/edit/send of a JSON payload for a candidate, trigger a backend API to generate and send the payload, and log the response in an audit table. The Assess button should fetch the latest interview for a candidate and display assessment data using the interview_id. Use the provided frontend React/Next.js code and backend FastAPI endpoint patterns. Ensure all required fields are handled, fallbacks are present for missing data, and audit logging is implemented."

# Engage & Assess Button Logic (Full Stack)

---

## Engage Button Logic

### 1. Frontend (React/Next.js)

#### a. State Management
```tsx
const [isEngageModalOpen, setIsEngageModalOpen] = useState(false);
const [engagePayload, setEngagePayload] = useState<string>('');
const [engageLoading, setEngageLoading] = useState(false);
const [engageError, setEngageError] = useState<string | null>(null);
const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([]);
const [apiResponse, setApiResponse] = useState<any>(null);
const [showApiStatus, setShowApiStatus] = useState(false);
const [candidateInterviewData, setCandidateInterviewData] = useState<{[key: string]: any}>({});
```

#### b. Engage Button Handler
```tsx
const handleEngageClick = async (candidate: Candidate) => {
  setEngageLoading(true);
  setEngageError(null);
  try {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001';
    const response = await fetch(`${apiUrl}/api/v1/engagement/engage/generate-payload`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        candidate_ids: [candidate.candidate_id],
        job_id: candidate.jobdiva_id
      })
    });
    if (!response.ok) throw new Error('Failed to generate payload');
    const data = await response.json();
    setEngagePayload(data.payload);
    setSelectedCandidateIds([candidate.candidate_id]);
    setIsEngageModalOpen(true);
  } catch (err: any) {
    setEngageError(err.message || 'Failed to generate payload');
  } finally {
    setEngageLoading(false);
  }
};
```

#### c. Modal & Send Handler
```tsx
const handleScheduleCall = async () => {
  setEngageLoading(true);
  setEngageError(null);
  setApiResponse(null);
  try {
    const apiUrl = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001';
    let payloadObj;
    try {
      payloadObj = JSON.parse(engagePayload);
    } catch (e) {
      throw new Error('Invalid JSON format in payload');
    }
    const response = await fetch(`${apiUrl}/api/v1/engagement/engage/send-bulk-interview`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ 
        payload: engagePayload,
        real_candidate_ids: selectedCandidateIds
      })
    });
    const data = await response.json();
    setApiResponse(data);
    if (response.ok && data.success) {
      setShowApiStatus(true);
      // Store interview data for each candidate for later Assess use
      if (data.data && Array.isArray(data.data)) {
        const interviewDataMap = {...candidateInterviewData};
        data.data.forEach((interviewInfo: any) => {
          const candidateId = selectedCandidateIds[0] || interviewInfo.candidate_email;
          interviewDataMap[candidateId] = {
            interview_id: interviewInfo.interview_id,
            candidate_name: interviewInfo.candidate_name,
            candidate_email: interviewInfo.candidate_email,
            links: interviewInfo.links,
            session_token: interviewInfo.session_token,
            created_at: interviewInfo.created_at
          };
        });
        setCandidateInterviewData(interviewDataMap);
      }
    } else {
      setEngageError(data.message || 'API returned error status');
    }
  } catch (err: any) {
    setEngageError(err.message || 'Unknown error');
  } finally {
    setEngageLoading(false);
  }
};
```

#### d. Modal UI
```tsx
<Dialog open={isEngageModalOpen} onOpenChange={setIsEngageModalOpen}>
  <DialogContent>
    <DialogHeader>
      <DialogTitle>Preview & Edit Engage Payload</DialogTitle>
    </DialogHeader>
    <Textarea
      value={engagePayload}
      onChange={(e) => setEngagePayload(e.target.value)}
      rows={12}
    />
    {engageError && <div className="text-red-500">{engageError}</div>}
    <DialogFooter>
      <Button onClick={handleScheduleCall} disabled={engageLoading}>
        {engageLoading ? 'Sending...' : 'Send'}
      </Button>
      <Button variant="secondary" onClick={() => setIsEngageModalOpen(false)}>
        Cancel
      </Button>
    </DialogFooter>
  </DialogContent>
</Dialog>
```

---

### 2. Backend (FastAPI)

- **`POST /api/v1/engagement/engage/generate-payload`**
  - Accepts candidate_ids and job_id, returns a JSON payload for the interview.
- **`POST /api/v1/engagement/engage/send-bulk-interview`**
  - Accepts the payload and real_candidate_ids, sends to external API, saves audit log, returns interview info.
- **Database:**
  - Table: `engage_interview_audit` stores candidate_id, interview_id, payload, and response for audit.

---

## Assess Button Logic

### 1. Frontend (React/Next.js)

#### a. Assess Button Handler
```tsx
<Button
  size="sm"
  onClick={async () => {
    setSelectedAssessCandidate(candidate);
    try {
      const url = `${process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8001'}/api/v1/engagement/latest-interview/by-id/${candidate.candidate_id}`;
      const res = await fetch(url);
      if (res.ok) {
        const data = await res.json();
        setSelectedAssessInterviewId(data.interview_id);
        setIsAssessModalOpen(true);
      } else {
        setSelectedAssessInterviewId(null);
        setIsAssessModalOpen(true);
      }
    } catch (e) {
      setSelectedAssessInterviewId(null);
      setIsAssessModalOpen(true);
    }
  }}
>
  Assess
</Button>
```

#### b. Assess Modal
```tsx
{isAssessModalOpen && selectedAssessCandidate && selectedAssessInterviewId && (
  <AssessModal
    open={isAssessModalOpen}
    onClose={() => setIsAssessModalOpen(false)}
    interviewId={selectedAssessInterviewId}
    candidateName={selectedAssessCandidate.name}
  />
)}
```

---

### 2. Backend (FastAPI)

- **`GET /api/v1/engagement/latest-interview/by-id/{candidate_id}`**
  - Looks up the latest interview_id for the candidate from `engage_interview_audit`.
  - Returns `{ interview_id, candidate_name }`.
- **External API:**
  - The frontend then uses `interview_id` to fetch assessment data from the external API (e.g., outreach status, evaluation, transcripts).

---

## Notes
- All API URLs are constructed using `NEXT_PUBLIC_API_URL` for environment flexibility.
- Fallbacks are used in the UI for missing fields to prevent blank rendering.
- The backend must return all required fields for candidates and interview info.
- The audit table ensures all engage/assess actions are logged for traceability.
