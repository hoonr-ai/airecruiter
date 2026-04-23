# PAIR Dashboard API Endpoints Documentation

This document provides detailed information about all API endpoints used by the PAIR (Phone AI Interview Rapid) outreach dashboard. These endpoints enable monitoring, analytics, and management of the automated candidate outreach system.

## Overview

The PAIR dashboard provides comprehensive visibility into the outreach process, including candidate status tracking, communication analytics, and performance metrics. The system uses a state machine approach with three phases:

- **Phase 1**: Initial contact (30-minute delay)
- **Phase 2**: Reminder/follow-up (16-hour delay)  
- **Phase 3**: Immediate processing

The dashboard uses **17 API endpoints** covering outreach management, candidate details, evaluations, transcripts, and analytics.

## Recent Updates (April 2026)

### Transcript Storage Improvements
- **Complete Message Storage**: All conversation messages are now stored, including partial responses, hesitations, and real-time utterances
- **No Filtering**: Removed previous filtering that excluded short or incomplete messages
- **Real-time Persistence**: Messages are stored immediately as they occur during interviews

### Evaluation System Enhancements
- **One Question at a Time**: Fixed bot behavior to ask exactly one question per response (no more multiple questions)
- **Question Reset Logic**: Evaluations now reset all questions to "unanswered" before processing to prevent accumulation
- **Improved Scoring**: Better detection of substantive responses with selective question completion marking
- **Response Validation**: Added safeguards to prevent multiple questions in single responses

### Technical Improvements
- **Lower Temperature**: Reduced LLM temperature to 0.01 for more deterministic responses
- **Token Limits**: Added max_tokens=150 to prevent overly long responses
- **Enhanced Prompts**: Strengthened instructions with explicit forbidden patterns and correct examples

## Authentication

All endpoints require proper authentication. The API uses standard HTTP authentication methods configured in the FastAPI application.

## PAIR Candidate Filtering

**IMPORTANT**: All endpoints used in the PAIR dashboard must only retrieve PAIR-tagged candidates. The PAIR-specific endpoints (`/api/dashboard/pair-*`) automatically filter for `pair_tag = 'pair'`. When using general interview endpoints for PAIR dashboard functionality, ensure client-side filtering or additional query parameters are used to restrict results to PAIR candidates only.

## Base URL

All endpoints are prefixed with the API base URL (typically `http://localhost:8000/api/`).

## Response Format

All endpoints return responses in the following JSON format:

```json
{
  "success": boolean,
  "message": "string",
  "data": object
}
```

---

## 1. GET /api/dashboard/pair-outreach

**Purpose**: Retrieve all PAIR candidates grouped by job description for dashboard display.

**Method**: GET

**Query Parameters**:
- `status` (optional): Filter by outreach status (e.g., "pending", "completed")
- `phase` (optional): Filter by outreach phase (e.g., "phase1", "phase2", "phase3")
- `date_from` (optional): Filter from date (YYYY-MM-DD format)
- `date_to` (optional): Filter to date (YYYY-MM-DD format)
- `jd_id` (optional): Filter by job description ID
- `search` (optional): Search by candidate name or email

**Response Data**:
```json
{
  "candidates_by_jd": {
    "Job Title 1": [
      {
        "id": 123,
        "person_name": "John Doe",
        "person_email": "john@example.com",
        "person_phone": "+1234567890",
        "role_position": "Software Engineer",
        "outreach_phase": "phase1",
        "outreach_status": "pending",
        "overall_score": null,
        "created_at": "2024-01-15T10:30:00Z",
        "bulk_jd_id": "jd_001",
        "pair_tag": "pair",
        "next_outreach_at": "2024-01-15T11:00:00Z",
        "last_outreach_at": null,
        "retry_count": 0,
        "jd_name": "Senior Software Engineer",
        "last_email_at": null,
        "last_sms_at": null,
        "last_call_at": null,
        "email_delivered": 0,
        "sms_delivered": 0,
        "call_completed": 0,
        "email_failed": 0,
        "sms_failed": 0,
        "call_failed": 0,
        "reachability": "pending"
      }
    ]
  },
  "total_jds": 5,
  "total_candidates": 25,
  "filters_applied": {
    "status": "pending",
    "phase": "phase1"
  }
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/dashboard/pair-outreach?status=pending&phase=phase1"
```

**Dashboard Usage**: This is the primary endpoint for displaying the main dashboard view, showing candidates organized by job descriptions with filtering capabilities. **Automatically filters for PAIR-tagged candidates only.**

---

## 2. GET /api/dashboard/pair-outreach/{jd_id}

**Purpose**: Get detailed PAIR outreach data for a specific job description.

**Method**: GET

**Path Parameters**:
- `jd_id`: Job description identifier (string)

**Response Data**:
```json
{
  "jd_details": {
    "id": "jd_001",
    "title": "Senior Software Engineer",
    "description": "Job description text...",
    "created_at": "2024-01-10T09:00:00Z"
  },
  "candidates": [
    {
      "id": 123,
      "person_name": "John Doe",
      "person_email": "john@example.com",
      "person_phone": "+1234567890",
      "role_position": "Senior Software Engineer",
      "outreach_phase": "phase1",
      "outreach_status": "pending",
      "overall_score": null,
      "created_at": "2024-01-15T10:30:00Z",
      "bulk_jd_id": "jd_001",
      "pair_tag": "pair",
      "next_outreach_at": "2024-01-15T11:00:00Z",
      "last_outreach_at": null,
      "retry_count": 0,
      "communication_timeline": [
        {
          "id": 456,
          "phase": "phase1",
          "step": "initial_email",
          "channel": "email",
          "sent_at": "2024-01-15T10:30:00Z",
          "delivered_at": "2024-01-15T10:31:00Z",
          "response_at": null,
          "status": "delivered",
          "error_message": null,
          "retry_count": 0
        }
      ]
    }
  ],
  "candidate_count": 1
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/dashboard/pair-outreach/jd_001"
```

**Dashboard Usage**: Used for drill-down views when users click on a specific job description to see detailed candidate information and communication history. **Automatically filters for PAIR-tagged candidates only.**

---

## 3. GET /api/dashboard/pair-metrics

**Purpose**: Retrieve comprehensive PAIR outreach metrics and statistics.

**Method**: GET

**Query Parameters**: None

**Response Data**:
```json
{
  "total_candidates": 150,
  "phase_distribution": {
    "phase1": 45,
    "phase2": 30,
    "phase3": 25,
    "completed": 50
  },
  "communication_rates": {
    "email": {
      "rate": 85.5,
      "successful": 128,
      "total": 150
    },
    "sms": {
      "rate": 92.0,
      "successful": 138,
      "total": 150
    },
    "call": {
      "rate": 78.0,
      "successful": 117,
      "total": 150
    }
  },
  "completion_rate": 33.3,
  "pass_rate": 65.0,
  "average_score": 78.5,
  "reachability": {
    "engaged": 50,
    "reached_no_response": 25,
    "unreachable": 15
  },
  "passed_candidates": 32
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/dashboard/pair-metrics"
```

**Dashboard Usage**: Provides key performance indicators and metrics displayed in charts and summary cards on the dashboard. **Based on PAIR-tagged candidates only.**

---

## 4. GET /api/dashboard/pair-passed

**Purpose**: Get candidates who passed the interview grouped by job description.

**Method**: GET

**Query Parameters**:
- `score_threshold` (optional): Minimum score threshold for passed candidates (default: 75)

**Response Data**:
```json
{
  "passed_candidates_by_jd": {
    "Senior Software Engineer": [
      {
        "id": 123,
        "person_name": "John Doe",
        "person_email": "john@example.com",
        "person_phone": "+1234567890",
        "role_position": "Senior Software Engineer",
        "overall_score": 85,
        "outreach_phase": "completed",
        "created_at": "2024-01-15T10:30:00Z",
        "bulk_jd_id": "jd_001",
        "pair_tag": "pair",
        "completed_at": "2024-01-16T14:30:00Z"
      }
    ]
  },
  "total_passed": 32,
  "score_threshold": 75,
  "jd_count": 5
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/dashboard/pair-passed?score_threshold=80"
```

**Dashboard Usage**: Displays successful candidates and hiring pipeline metrics. **Filters for PAIR-tagged candidates only.**

---

## 5. GET /api/interviews/{interview_id}/outreach-status

**Purpose**: Get current outreach status and communication history for a specific interview.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
{
  "interview_id": 123,
  "outreach": {
    "outreach_phase": "phase2",
    "outreach_status": "pending",
    "initiated_at": "2024-01-15T10:30:00Z",
    "completed_at": null,
    "last_outreach_at": "2024-01-15T10:30:00Z",
    "next_outreach_at": "2024-01-16T02:30:00Z",
    "created_by_bulk_id": "bulk_001"
  },
  "communications": [
    {
      "phase": "phase1",
      "step": "initial_email",
      "channel": "email",
      "status": "delivered",
      "sent_at": "2024-01-15T10:30:00Z",
      "delivered_at": "2024-01-15T10:31:00Z",
      "response_at": null,
      "external_id": "msg_12345",
      "error_message": null
    }
  ],
  "scheduled_jobs": [
    {
      "job_type": "send_sms",
      "scheduled_at": "2024-01-16T02:30:00Z",
      "status": "pending",
      "attempts": 0,
      "max_attempts": 3,
      "payload": "{\"message\": \"Interview reminder...\"}"
    }
  ]
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123/outreach-status"
```

**Dashboard Usage**: Used for detailed candidate views and troubleshooting outreach issues.

---

## 6. POST /api/outreach/start-scheduler

**Purpose**: Start the PAIR outreach job scheduler service.

**Method**: POST

**Request Body**: None

**Response Data**:
```json
{
  "scheduler_id": "api-scheduler",
  "started_at": "2024-01-15T10:30:00Z"
}
```

**Usage Example**:
```bash
curl -X POST "http://localhost:8000/api/outreach/start-scheduler"
```

**Dashboard Usage**: Administrative endpoint to ensure the background scheduler is running for processing outreach jobs.

---

## 7. POST /api/interviews/{interview_id}/trigger-phase2

**Purpose**: Manually trigger Phase 2 outreach for testing or administrative purposes.

**Method**: POST

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Request Body**: None

**Response Data**:
```json
{
  "interview_id": 123,
  "old_state": "phase1",
  "new_state": "phase2",
  "actions_scheduled": [
    "send_reminder_email",
    "schedule_followup_sms"
  ],
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Usage Example**:
```bash
curl -X POST "http://localhost:8000/api/interviews/123/trigger-phase2"
```

**Dashboard Usage**: Administrative tool for testing outreach flows or manually advancing candidates through phases.

---

## 8. POST /api/interviews/{interview_id}/initiate

**Purpose**: Mark an interview as initiated by the candidate (when they click the interview link).

**Method**: POST

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Request Body**: None

**Response Data**:
```json
{
  "interview_id": 123,
  "action": "initiated",
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Usage Example**:
```bash
curl -X POST "http://localhost:8000/api/interviews/123/initiate"
```

**Dashboard Usage**: Called when candidates click interview links, updating their status in the dashboard.

---

## 9. POST /api/interviews/{interview_id}/complete

**Purpose**: Mark an interview as completed with pass/fail status.

**Method**: POST

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Request Body**:
```json
{
  "passed": true,
  "score": 85,
  "notes": "Strong technical skills, good communication"
}
```

**Response Data**:
```json
{
  "interview_id": 123,
  "action": "completed",
  "passed": true,
  "score": 85,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

**Usage Example**:
```bash
curl -X POST "http://localhost:8000/api/interviews/123/complete" \
  -H "Content-Type: application/json" \
  -d '{"passed": true, "score": 85, "notes": "Excellent candidate"}'
```

**Dashboard Usage**: Updates candidate status when interviews are completed, affecting metrics and pass rates.

---

## 10. GET /api/interviews/{interview_id}

**Purpose**: Get basic interview information including candidate details, status, and scores.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
{
  "id": 123,
  "person_name": "Ronak Jain",
  "person_email": "ronakpahariya@gmail.com",
  "person_phone": "+1234567890",
  "role_position": "IT Quality Assur Anlyt Sr",
  "status": "completed",
  "overall_score": 9.0,
  "questions_completed": 7,
  "total_questions": 7,
  "created_at": "2024-01-15T10:30:00Z",
  "completed_at": "2024-01-17T14:30:00Z",
  "pair_tag": "pair",
  "bulk_jd_id": "jd_001"
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123"
```

**Dashboard Usage**: Provides candidate overview information displayed in candidate lists and detail views. **⚠️ WARNING: This endpoint returns ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are displayed by checking `pair_tag = 'pair'`.**

---

## 11. GET /api/interviews/{interview_id}/detail

**Purpose**: Get complete interview details including sessions, Q&A, call attempts, and assigned questions.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
{
  "interview": {
    "id": 123,
    "person_name": "Ronak Jain",
    "person_email": "ronakpahariya@gmail.com",
    "person_phone": "+1234567890",
    "role_position": "IT Quality Assur Anlyt Sr",
    "status": "completed",
    "overall_score": 9.0,
    "questions_completed": 7,
    "total_questions": 7,
    "created_at": "2024-01-15T10:30:00Z",
    "completed_at": "2024-01-17T14:30:00Z"
  },
  "sessions": [
    {
      "id": 456,
      "room_name": "interview_123",
      "status": "completed",
      "started_at": "2024-01-17T10:00:00Z",
      "ended_at": "2024-01-17T10:45:00Z",
      "duration_minutes": 45
    }
  ],
  "questions_answers": [
    {
      "id": 789,
      "question": "Tell me about your experience with quality assurance...",
      "answer": "I have 5 years of experience...",
      "score": 9.0,
      "feedback": "Excellent response showing deep understanding"
    }
  ],
  "call_attempts": [
    {
      "id": 101,
      "attempt_date": "2024-01-15T11:00:00Z",
      "status": "completed",
      "duration_seconds": 2700,
      "notes": "Successful interview completion"
    }
  ],
  "assigned_questions": [
    {
      "id": 202,
      "question_text": "Describe your QA methodology...",
      "question_type": "technical",
      "difficulty_level": "senior"
    }
  ]
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123/detail"
```

**Dashboard Usage**: Provides comprehensive candidate details for detailed views and analysis. **⚠️ WARNING: This endpoint returns ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are displayed by checking `pair_tag = 'pair'`.**

---

## 12. GET /api/interviews/{interview_id}/evaluation

**Purpose**: Get evaluated questions and answers with individual scores and overall assessment.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
{
  "questions": [
    {
      "question_id": 224,
      "question_text": "What is your experience with automated testing?",
      "question_order": 1,
      "answer_text": "I have extensive experience with Selenium, JUnit, and TestNG...",
      "score": 9.5,
      "answered_at": "2024-01-17T10:15:30Z"
    },
    {
      "question_id": 225,
      "question_text": "How do you handle test case prioritization?",
      "question_order": 2,
      "answer_text": "I prioritize based on risk assessment and business impact...",
      "score": 8.5,
      "answered_at": "2024-01-17T10:18:45Z"
    }
  ],
  "summary": {
    "total_questions": 14,
    "questions_completed": 14,
    "overall_score": 9.0,
    "average_score": 9.0
  }
}
```

**Notes**:
- **Evaluation Logic**: The system now evaluates interviews by analyzing conversation transcripts and mapping candidate answers to specific questions. It saves ALL evaluations (including unanswered questions scored as 0.0) and calculates overall scores as averages.
- **Question Reset**: Before each evaluation run, all questions are reset to "unanswered" state to prevent accumulation of completion marks from previous evaluations.
- **Scoring**: Individual question scores range from 0.0 to 10.0. Overall score is the average of all evaluated questions.
- **Completion Tracking**: Questions are marked as "answered" only when candidates provide substantive responses (score > 0 OR explicitly marked as PASS).

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123/evaluation"
```

**Dashboard Usage**: Shows detailed evaluation results and scoring breakdown for completed interviews. **⚠️ WARNING: This endpoint returns evaluations for ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are displayed.**

---

## 12.5. POST /api/interviews/{interview_id}/evaluate

**Purpose**: Manually trigger evaluation of an interview using stored transcripts.

**Method**: POST

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Request Body**: None

**Response Data**:
```json
{
  "success": true,
  "message": "Evaluation completed successfully",
  "data": {
    "interview_id": 123,
    "evaluation_status": "completed",
    "questions_evaluated": 14,
    "overall_score": 8.5,
    "progress": {
      "total": 14,
      "completed": 12,
      "overall_score": 8.5
    }
  }
}
```

**Notes**:
- **Automatic Evaluation Reset**: Before evaluation, all questions are reset to "unanswered" state to ensure clean evaluation
- **Transcript Analysis**: Uses the latest interview session transcripts to evaluate candidate responses
- **Question Mapping**: AI analyzes conversation flow to map candidate answers to specific interview questions
- **Selective Completion**: Only marks questions as "answered" when substantive responses are detected (score > 0 OR explicitly PASS)
- **Scoring**: Evaluates each question on 0.0-10.0 scale based on technical accuracy, relevance, and completeness

**Usage Example**:
```bash
curl -X POST "http://localhost:8000/api/interviews/123/evaluate"
```

**Dashboard Usage**: Allows manual triggering of interview evaluation for completed interviews. **⚠️ WARNING: This endpoint evaluates ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are processed.**

---

## 13. GET /api/interviews/{interview_id}/transcriptions

**Purpose**: Get all conversation transcripts for an interview session.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
[
  {
    "id": 1001,
    "interview_id": 123,
    "session_id": 456,
    "speaker_type": "bot",
    "message_text": "Hello Pragati, I'm John here to assess your fit for the IT Quality Assur Anlyt Sr position. Are you ready to begin?",
    "timestamp": "2024-01-17T10:00:15Z",
    "message_order": 1,
    "audio_duration": 0.0,
    "confidence_score": null
  },
  {
    "id": 1002,
    "interview_id": 123,
    "session_id": 456,
    "speaker_type": "candidate",
    "message_text": "Yes. I'm ready to begin this.",
    "timestamp": "2024-01-17T10:00:20Z",
    "message_order": 2,
    "audio_duration": 0.0,
    "confidence_score": null
  },
  {
    "id": 1003,
    "interview_id": 123,
    "session_id": 456,
    "speaker_type": "candidate",
    "message_text": "Uh, right now, yes, I'm open to explore.",
    "timestamp": "2024-01-17T10:01:05Z",
    "message_order": 3,
    "audio_duration": 0.0,
    "confidence_score": null
  }
]
```

**Notes**:
- **Complete Transcript Storage**: The system now stores ALL conversation messages, including partial responses, hesitations, and incomplete utterances. No filtering is applied - every message from both bot and candidate is preserved.
- **Real-time Storage**: Messages are stored immediately as they occur during the interview, ensuring no loss of conversation data.
- **Speaker Types**: 
  - `"bot"`: Interviewer/AI messages
  - `"candidate"`: Candidate responses
- **Message Order**: Messages are ordered chronologically by `message_order` field
- **Latest Session**: Returns transcripts from the most recent interview session that contains conversation data

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123/transcriptions"
```

**Dashboard Usage**: Provides conversation transcripts for review and analysis of interview interactions. **⚠️ WARNING: This endpoint returns transcripts for ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are displayed.**

---

## 14. GET /api/interviews/{interview_id}/transcriptions/download

**Purpose**: Download formatted interview transcripts as a text file.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response**: Text file download with formatted transcript content including ALL messages.

**Notes**:
- **Complete Conversation**: Downloads the full conversation including all partial messages, hesitations, and real-time exchanges
- **Formatted Output**: Presents transcripts in a readable format with timestamps and speaker labels
- **All Sessions**: Includes transcripts from all sessions if multiple interview sessions exist

**Usage Example**:
```bash
curl -O "http://localhost:8000/api/interviews/123/transcriptions/download"
```

**Dashboard Usage**: Allows downloading transcripts for offline review or documentation. **⚠️ WARNING: This endpoint returns transcripts for ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are accessed.**

---

## 15. GET /api/interviews/{interview_id}/progress

**Purpose**: Get comprehensive interview progress information.

**Method**: GET

**Path Parameters**:
- `interview_id`: Interview identifier (integer)

**Response Data**:
```json
{
  "interview_id": 123,
  "status": "completed",
  "progress_percentage": 100,
  "questions_completed": 7,
  "total_questions": 7,
  "current_question": null,
  "time_elapsed": 2700,
  "estimated_time_remaining": 0,
  "last_activity": "2024-01-17T10:45:00Z",
  "completion_summary": {
    "overall_score": 9.0,
    "passed": true,
    "feedback_summary": "Strong technical candidate with excellent QA expertise"
  }
}
```

**Usage Example**:
```bash
curl "http://localhost:8000/api/interviews/123/progress"
```

**Dashboard Usage**: Shows real-time progress tracking and completion status for active interviews. **⚠️ WARNING: This endpoint returns progress for ALL interviews. For PAIR dashboard usage, ensure only PAIR-tagged candidates are displayed.**

---

## 16. GET /api/candidates/{candidate_name}/transcripts

**Purpose**: Get all transcripts for a candidate across all their interviews.

**Method**: GET

**Path Parameters**:
- `candidate_name`: Candidate name (string, URL-encoded)

**Response Data**:
```json
{
  "candidate_name": "Pragati",
  "total_interviews": 2,
  "interviews": {
    "123_IT Quality Assur Anlyt Sr": {
      "interview_id": 123,
      "person_name": "Pragati",
      "role_position": "IT Quality Assur Anlyt Sr",
      "interview_status": "completed",
      "interview_date": "2024-01-17T10:00:00Z",
      "sessions": {
        "456": {
          "session_id": 456,
          "session_start": "2024-01-17T10:00:00Z",
          "session_end": "2024-01-17T10:45:00Z",
          "room_id": "interview_123",
          "messages": [
            {
              "id": 1001,
              "speaker_type": "bot",
              "message_text": "Hello Pragati, I'm John here to assess your fit...",
              "timestamp": "2024-01-17T10:00:15Z",
              "message_order": 1
            },
            {
              "id": 1002,
              "speaker_type": "candidate",
              "message_text": "Yes. I'm ready to begin this.",
              "timestamp": "2024-01-17T10:00:20Z",
              "message_order": 2
            }
          ]
        }
      }
    }
  }
}
```

**Notes**:
- **Complete History**: Returns ALL conversation messages across all interviews for the candidate
- **Multi-Session Support**: Handles candidates with multiple interview sessions
- **Real-time Data**: Includes all stored messages, including partial responses and real-time conversation flow

**Usage Example**:
```bash
curl "http://localhost:8000/api/candidates/Pragati/transcripts"
```

**Dashboard Usage**: Provides comprehensive transcript history for candidates across multiple interviews. **⚠️ WARNING: This endpoint returns transcripts for ALL candidates. For PAIR dashboard usage, ensure only PAIR-tagged candidates are accessed.**

---

## Implementation Notes for PAIR Filtering

### PAIR-Specific Endpoints (Automatic Filtering)
The following endpoints automatically filter for PAIR-tagged candidates (`pair_tag = 'pair'`):
- `GET /api/dashboard/pair-outreach`
- `GET /api/dashboard/pair-outreach/{jd_id}`
- `GET /api/dashboard/pair-metrics`
- `GET /api/dashboard/pair-passed`

### General Interview Endpoints (Manual Filtering Required)
When using general interview endpoints in the PAIR dashboard, implement client-side filtering:

```javascript
// Example: Filter for PAIR candidates only
const interviews = await fetch('/api/interviews/');
const pairInterviews = interviews.data.filter(interview => interview.pair_tag === 'pair');
```

### Database-Level Filtering
For enhanced security, consider adding query parameters to filter at the database level:

```javascript
// Frontend request
const response = await fetch('/api/interviews/?pair_only=true');

// Backend implementation (if added)
@app.get("/api/interviews/")
async def get_interviews(pair_only: bool = Query(False)):
    if pair_only:
        # Add WHERE pair_tag = 'pair' to query
        pass
```

### Security Considerations
- Always validate that users can only access PAIR-tagged candidates
- Implement role-based access control for PAIR dashboard features
- Log access attempts to non-PAIR candidates for audit purposes

---

## Error Handling

All endpoints follow consistent error handling:

- **400 Bad Request**: Invalid parameters or request format
- **404 Not Found**: Resource not found (interview, JD, etc.)
- **500 Internal Server Error**: Server-side errors

Error responses include:
```json
{
  "success": false,
  "message": "Error description",
  "data": null
}
```

## Rate Limiting

Endpoints may have rate limiting applied. Check server logs for rate limit headers in responses.

## Monitoring

All endpoints are logged for monitoring and debugging purposes. Check application logs for detailed request/response information.

## Frontend Integration

The dashboard frontend (React/TypeScript) consumes these endpoints using standard HTTP requests with proper error handling and loading states.</content>
<parameter name="filePath">c:\dev\New folder\New folder (2)\livekit-airecruiter\PAIR_DASHBOARD_API_ENDPOINTS.md