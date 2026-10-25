"use client";

// Recruiter warm handoff join page (Pair-bot docs/recruiter-handoff-pair-contract.md,
// docs/recruiter-copilot-design.md).
//
// Reached from the "Join now" notification, email or Teams card. Claims the
// handoff as the logged-in recruiter (the backend sends their login email,
// never anything typed here), joins the LiveKit room Pair-bot returns, plays
// the candidate, publishes the recruiter's mic/camera, and ends the
// interview with "End interview".
//
// Copilot: while waiting, the recruiter preps from the AI interview, resume
// and AI transcript. In the call, Pair-bot streams the live transcript and
// follow-up suggestions on the `handoff_copilot` data topic (to the recruiter
// only); the page backfills the transcript from /transcript on (re)connect,
// dedupes by seq, and tells Pair-bot whether suggestions are wanted.

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Loader2, Mic, MicOff, PhoneCall, PhoneOff, UserRound, Video, VideoOff } from "lucide-react";
import { Room, RoomEvent, Track, createLocalTracks } from "livekit-client";
import type { LocalTrack, RemoteParticipant, RemoteTrack, RemoteTrackPublication } from "livekit-client";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { Textarea } from "@/components/ui/textarea";
import { SuggestionsPanel, TranscriptPanel } from "@/components/handoff/CopilotPanels";
import { HandoffHeader } from "@/components/handoff/HandoffHeader";
import { HandoffInfoTabs, type Loadable } from "@/components/handoff/HandoffInfoTabs";
import { api, getActiveUserEmail, type HandoffContext, type HandoffRecording, type HandoffState } from "@/lib/api";
import {
  COPILOT_TOPIC,
  describeHandoffError,
  formatElapsed,
  formatSecondsLeft,
  isCandidateParticipant,
  isHandoffOver,
  joinViewFor,
  loadCopilotPrefs,
  mergeTranscript,
  parseCopilotPacket,
  saveCopilotPrefs,
  type CopilotLine,
  type CopilotPrefs,
  type CopilotSuggestionBatch,
} from "@/lib/handoff";

type Phase = "loading" | "ready" | "joining" | "in_call" | "ended";

const getStorage = (): Storage | null => {
  if (typeof window === "undefined") return null;
  try {
    return window.localStorage;
  } catch {
    return null;
  }
};

/** A copilot read that may not exist yet: 403/404 (not deployed, not yours,
 * no launch) is "not available", not an error for the recruiter. */
async function optional<T>(p: Promise<T>): Promise<T | null> {
  try {
    return await p;
  } catch (err) {
    const status = (err as { status?: number })?.status;
    if (status !== 403 && status !== 404) console.warn("handoff copilot read failed", err);
    return null;
  }
}

export default function HandoffJoinPage() {
  const params = useParams<{ interviewId: string }>();
  const router = useRouter();
  const interviewId = Number.parseInt(params?.interviewId ?? "", 10);
  const validId = Number.isInteger(interviewId) && interviewId > 0;

  const [phase, setPhase] = useState<Phase>(validId ? "loading" : "ready");
  const [state, setState] = useState<HandoffState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [confirmEnd, setConfirmEnd] = useState(false);
  const [now, setNow] = useState(() => Date.now());
  const [joinedAt, setJoinedAt] = useState<number | null>(null);
  const [candidateVideo, setCandidateVideo] = useState<RemoteTrack | null>(null);
  const [candidateHere, setCandidateHere] = useState(false);
  const [micOn, setMicOn] = useState(true);
  // Off until the recruiter turns it on: no surprise live camera on join.
  const [camOn, setCamOn] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);

  // Copilot
  const [context, setContext] = useState<Loadable<HandoffContext>>(undefined);
  const [resume, setResume] = useState<Loadable<string>>(undefined);
  const [lines, setLines] = useState<CopilotLine[]>([]);
  const [suggestions, setSuggestions] = useState<CopilotSuggestionBatch[]>([]);
  // The recruiter's remembered toggles (per login, browser-local). Read
  // lazily: the server render only ever shows the loading state.
  const [prefs, setPrefs] = useState<CopilotPrefs>(() => loadCopilotPrefs(getStorage(), getActiveUserEmail()));
  const [recording, setRecording] = useState<HandoffRecording | null>(null);

  const roomRef = useRef<Room | null>(null);
  const localTracks = useRef<LocalTrack[]>([]);
  const audioEls = useRef<HTMLMediaElement[]>([]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const selfRef = useRef<HTMLVideoElement>(null);
  // Room event handlers outlive renders; they read the latest toggles here.
  const prefsRef = useRef(prefs);

  const updatePrefs = (patch: Partial<CopilotPrefs>) => {
    const next = { ...prefsRef.current, ...patch };
    prefsRef.current = next;
    setPrefs(next);
    saveCopilotPrefs(getStorage(), getActiveUserEmail(), next);
    if (patch.suggestions !== undefined) void sendSettings(next.suggestions);
  };

  const sendSettings = async (on: boolean) => {
    const lp = roomRef.current?.localParticipant;
    if (!lp) return;
    try {
      const payload = new TextEncoder().encode(JSON.stringify({ type: "copilot_settings", suggestions: on }));
      await lp.publishData(payload, { reliable: true, topic: COPILOT_TOPIC });
    } catch (err) {
      console.warn("handoff: could not send copilot settings", err);
    }
  };

  const refresh = useCallback(async () => {
    if (!validId) return;
    try {
      setState(await api.handoff.state(interviewId));
      setError(null);
    } catch (err) {
      setError(describeHandoffError(err));
    }
  }, [interviewId, validId]);

  // First load. Promise callbacks, so no state is set synchronously in the
  // effect; `live` drops a response that lands after unmount or an id change.
  useEffect(() => {
    if (!validId) return;
    let live = true;
    api.handoff.state(interviewId).then(
      (s) => {
        if (!live) return;
        setState(s);
        setPhase("ready");
      },
      (err) => {
        if (!live) return;
        setError(describeHandoffError(err));
        setPhase("ready");
      },
    );
    return () => {
      live = false;
    };
  }, [interviewId, validId]);

  // Prep material, once we know the handoff isn't someone else's.
  const mine = state !== null && state.state !== null && state.is_your_handoff !== false;
  useEffect(() => {
    if (!mine) return;
    let live = true;
    void optional(api.handoff.context(interviewId)).then((c) => live && setContext(c));
    void optional(api.handoff.resume(interviewId)).then((r) => live && setResume(r?.resume_text || null));
    return () => {
      live = false;
    };
  }, [interviewId, mine]);

  // Countdown, plus a light poll so a page left open learns the handoff
  // timed out or was cancelled without the recruiter reloading.
  // Stops once the handoff is over: nothing can change after that.
  const over = isHandoffOver(state?.state);
  useEffect(() => {
    if (phase !== "ready" || over) return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    const poll = setInterval(() => void refresh(), 10000);
    return () => {
      clearInterval(tick);
      clearInterval(poll);
    };
  }, [phase, over, refresh]);

  // Call timer.
  useEffect(() => {
    if (phase !== "in_call") return;
    const tick = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(tick);
  }, [phase]);

  // After the handoff: the recording, once Pair-bot has it.
  useEffect(() => {
    if (!mine || !(phase === "ended" || over)) return;
    let live = true;
    void optional(api.handoff.recording(interviewId)).then((r) => live && setRecording(r?.url ? r : null));
    void optional(api.handoff.transcript(interviewId)).then((t) => live && t && setLines((cur) => mergeTranscript(cur, t.lines ?? [])));
    return () => {
      live = false;
    };
  }, [interviewId, mine, phase, over]);

  const teardown = useCallback(async () => {
    audioEls.current.forEach((el) => el.remove());
    audioEls.current = [];
    localTracks.current.forEach((t) => t.stop());
    localTracks.current = [];
    setCandidateVideo(null);
    setCandidateHere(false);
    const room = roomRef.current;
    roomRef.current = null;
    if (room) await room.disconnect();
  }, []);

  // Leaving the page must never leave the mic/camera live in the room.
  useEffect(() => () => void teardown(), [teardown]);

  useEffect(() => {
    const el = videoRef.current;
    if (!candidateVideo || !el) return;
    candidateVideo.attach(el);
    return () => {
      candidateVideo.detach(el);
    };
  }, [candidateVideo]);

  const syncCandidate = (room: Room) => {
    setCandidateHere(Array.from(room.remoteParticipants.values()).some((p) => isCandidateParticipant(p)));
  };

  const backfill = useCallback(async () => {
    const t = await optional(api.handoff.transcript(interviewId));
    if (t?.lines) setLines((cur) => mergeTranscript(cur, t.lines));
  }, [interviewId]);

  const join = async () => {
    setPhase("joining");
    setError(null);
    try {
      const claim = await api.handoff.claim(interviewId);
      const room = new Room({ adaptiveStream: true, dynacast: true });
      roomRef.current = room;
      room.on(RoomEvent.TrackSubscribed, (track: RemoteTrack, _pub: RemoteTrackPublication, p: RemoteParticipant) => {
        if (track.kind === Track.Kind.Audio) {
          // The candidate, and the bot while it speaks its closing line.
          const el = track.attach();
          el.hidden = true;
          document.body.appendChild(el);
          audioEls.current.push(el);
        } else if (track.kind === Track.Kind.Video && isCandidateParticipant(p)) {
          setCandidateVideo(track);
        }
      });
      room.on(RoomEvent.TrackUnsubscribed, (track: RemoteTrack) => {
        track.detach().forEach((el) => el.remove());
        setCandidateVideo((prev) => (prev === track ? null : prev));
      });
      room.on(RoomEvent.ParticipantConnected, () => syncCandidate(room));
      room.on(RoomEvent.ParticipantDisconnected, () => syncCandidate(room));
      room.on(RoomEvent.AudioPlaybackStatusChanged, () => setAudioBlocked(!room.canPlaybackAudio));
      room.on(RoomEvent.DataReceived, (payload: Uint8Array, _p, _kind, topic?: string) => {
        if (topic !== COPILOT_TOPIC) return;
        const packet = parseCopilotPacket(new TextDecoder().decode(payload));
        if (!packet) return;
        if (packet.type === "transcript") {
          setLines((cur) => mergeTranscript(cur, [packet]));
        } else if (packet.items.length > 0) {
          setSuggestions((cur) => (cur.some((b) => b.id && b.id === packet.id) ? cur : [...cur, packet].slice(-10)));
        }
      });
      room.on(RoomEvent.Reconnected, () => {
        // Packets sent while we were away are lost: catch up from the backlog,
        // and restate our settings in case the agent restarted.
        void backfill();
        void sendSettings(prefsRef.current.suggestions);
      });
      room.on(RoomEvent.Disconnected, () => {
        // The room is deleted when the interview ends (by us, the candidate
        // leaving, or Pair-bot's ceiling).
        void teardown();
        setPhase("ended");
        void refresh();
      });

      await room.connect(claim.livekit_url, claim.token);
      syncCandidate(room);
      setAudioBlocked(!room.canPlaybackAudio);
      void sendSettings(prefsRef.current.suggestions);
      void backfill();
      try {
        const tracks = await createLocalTracks({ audio: true, video: false });
        localTracks.current = tracks;
        for (const t of tracks) await room.localParticipant.publishTrack(t);
      } catch (mediaErr) {
        console.warn("handoff: joined without a microphone", mediaErr);
        setMicOn(false);
      }
      setJoinedAt(Date.now());
      setNow(Date.now());
      setPhase("in_call");
    } catch (err) {
      await teardown();
      setError(describeHandoffError(err));
      setPhase("ready");
      void refresh();
    }
  };

  const end = async () => {
    try {
      await api.handoff.close(interviewId, notes.trim() || undefined);
    } catch (err) {
      setError(describeHandoffError(err));
      return;
    }
    await teardown();
    setPhase("ended");
    void refresh();
  };

  const toggle = async (kind: "mic" | "cam") => {
    const lp = roomRef.current?.localParticipant;
    if (!lp) return;
    try {
      if (kind === "mic") {
        await lp.setMicrophoneEnabled(!micOn);
        setMicOn(!micOn);
      } else {
        const pub = await lp.setCameraEnabled(!camOn);
        if (!camOn && pub?.track && selfRef.current) pub.track.attach(selfRef.current);
        setCamOn(!camOn);
      }
    } catch (err) {
      setError(kind === "mic" ? "Couldn't use your microphone." : "Couldn't use your camera.");
      console.warn("handoff: device toggle failed", err);
    }
  };

  if (!validId) {
    return <Shell title="Invalid link" body="This handoff link is not valid." />;
  }
  if (phase === "loading") {
    return (
      <Shell title="Recruiter handoff">
        <Loader2 className="w-6 h-6 animate-spin text-slate-400" aria-label="Loading" />
      </Shell>
    );
  }

  const copilotColumn = (
    <div className="flex flex-col gap-4 min-h-0 lg:h-[calc(100vh-8rem)] lg:sticky lg:top-4">
      <TranscriptPanel
        lines={lines}
        visible={prefs.showTranscript}
        onToggle={() => updatePrefs({ showTranscript: !prefs.showTranscript })}
        className={prefs.showTranscript ? "flex-1 min-h-[240px]" : undefined}
      />
      <SuggestionsPanel
        batches={suggestions}
        enabled={prefs.suggestions}
        onToggle={() => updatePrefs({ suggestions: !prefs.suggestions })}
        className="flex-1 min-h-[200px]"
      />
    </div>
  );

  if (phase === "ended") {
    return (
      <div className="max-w-6xl mx-auto p-6 space-y-4">
        <HandoffHeader context={context}>
          <Button variant="outline" onClick={() => router.push("/notifications")}>Back to notifications</Button>
        </HandoffHeader>
        <Card className="p-6 gap-3">
          <h2 className="text-lg font-semibold text-slate-900">Interview ended</h2>
          <p className="text-slate-600">Thanks — the candidate&apos;s call has ended.</p>
          {recording && (
            <audio controls src={recording.url} className="w-full" aria-label="Handoff recording" />
          )}
        </Card>
        {lines.length > 0 && <TranscriptPanel lines={lines} visible onToggle={() => undefined} className="max-h-[50vh]" />}
      </div>
    );
  }

  if (phase === "in_call") {
    return (
      <div className="max-w-[1400px] mx-auto p-4 md:p-6 space-y-4">
        <HandoffHeader context={context} timerLabel="In call" timer={joinedAt ? formatElapsed(now - joinedAt) : null}>
          <span className="inline-flex items-center gap-2 text-sm font-medium text-emerald-700">
            <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" aria-hidden="true" />
            {candidateHere ? "Connected" : "Candidate reconnecting…"}
          </span>
          <Button variant="destructive" onClick={() => setConfirmEnd(true)}>
            <PhoneOff className="w-4 h-4" /> End interview
          </Button>
        </HandoffHeader>
        {confirmEnd && (
          <Card className="p-4 gap-3">
            <Textarea
              value={notes}
              onChange={(e) => setNotes(e.target.value)}
              placeholder="Notes for the record (optional)"
              aria-label="Notes"
              maxLength={5000}
              rows={3}
            />
            <div className="flex gap-2 justify-end">
              <Button variant="outline" onClick={() => setConfirmEnd(false)}>Keep talking</Button>
              <Button variant="destructive" onClick={() => void end()}>
                <PhoneOff className="w-4 h-4" /> End for everyone
              </Button>
            </div>
          </Card>
        )}
        {audioBlocked && (
          <Button onClick={() => void roomRef.current?.startAudio().then(() => setAudioBlocked(false))}>
            Click to turn on sound
          </Button>
        )}
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <div className="grid gap-4 lg:grid-cols-[minmax(0,1fr)_360px]">
          <div className="space-y-4 min-w-0">
            <Card className="relative overflow-hidden bg-slate-900 aspect-video flex items-center justify-center py-0">
              {candidateVideo ? (
                <video ref={videoRef} autoPlay playsInline muted aria-label="Candidate video" className="w-full h-full object-cover" />
              ) : (
                <div className="flex flex-col items-center gap-2 text-slate-300">
                  <UserRound className="w-16 h-16" aria-hidden="true" />
                  <span className="text-sm">Candidate is on audio</span>
                </div>
              )}
              <div className="absolute bottom-3 right-3 w-40 sm:w-48 aspect-video rounded-lg overflow-hidden bg-slate-800 ring-1 ring-white/20 shadow-lg">
                <video ref={selfRef} autoPlay playsInline muted aria-label="Your camera" className={camOn ? "w-full h-full object-cover" : "hidden"} />
                {!camOn && (
                  <div className="w-full h-full flex items-center justify-center text-xs text-slate-300">You</div>
                )}
                <div className="absolute bottom-1.5 left-1/2 -translate-x-1/2 flex gap-1.5">
                  <Button size="icon-sm" variant={micOn ? "secondary" : "destructive"} onClick={() => void toggle("mic")}
                    aria-pressed={!micOn} aria-label={micOn ? "Mute" : "Unmute"}>
                    {micOn ? <Mic /> : <MicOff />}
                  </Button>
                  <Button size="icon-sm" variant={camOn ? "secondary" : "destructive"} onClick={() => void toggle("cam")}
                    aria-pressed={!camOn} aria-label={camOn ? "Camera off" : "Camera on"}>
                    {camOn ? <Video /> : <VideoOff />}
                  </Button>
                </div>
              </div>
            </Card>
            <HandoffInfoTabs context={context} resume={resume} className="max-h-[45vh]" />
          </div>
          {copilotColumn}
        </div>
      </div>
    );
  }

  // ready / joining
  const view = state ? joinViewFor(state) : { kind: "none" as const };
  const left = state?.state === "requested" ? formatSecondsLeft(state.expires_at, now) : null;
  if (view.kind === "waiting") {
    // Pre-join: prep from the AI interview and resume while the candidate waits.
    return (
      <div className="max-w-6xl mx-auto p-4 md:p-6 space-y-4">
        <HandoffHeader context={context} timerLabel="Waits" timer={left && left !== "0:00" ? left : null}>
          <Button onClick={() => void join()} disabled={phase === "joining"}>
            {phase === "joining" ? <Loader2 className="w-4 h-4 animate-spin" /> : <PhoneCall className="w-4 h-4" />}
            {phase === "joining" ? "Joining…" : "Join now"}
          </Button>
        </HandoffHeader>
        <p className="text-slate-600">
          The candidate finished the PAIR interview and is waiting on the call for you.
          {left && left !== "0:00" && <> They&apos;ll wait about <strong>{left}</strong> more.</>}
        </p>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
        <HandoffInfoTabs context={context} resume={resume} />
      </div>
    );
  }
  return (
    <Shell title="Recruiter handoff">
      {view.kind === "over" && <p className="text-slate-600">{view.reason}</p>}
      {view.kind === "over" && recording && (
        <audio controls src={recording.url} className="w-full" aria-label="Handoff recording" />
      )}
      {view.kind === "not_yours" && (
        <p className="text-slate-600">Only the recruiter who launched this interview can join it.</p>
      )}
      {view.kind === "none" && !error && <p className="text-slate-600">This candidate isn&apos;t waiting for a recruiter.</p>}
      {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
    </Shell>
  );
}

function Shell({ title, body, children }: { title: string; body?: string; children?: React.ReactNode }) {
  return (
    <div className="max-w-xl mx-auto p-6">
      <Card className="p-8 flex flex-col items-start gap-4">
        <h1 className="text-xl font-bold text-slate-900">{title}</h1>
        {body && <p className="text-slate-600">{body}</p>}
        {children}
      </Card>
    </div>
  );
}
