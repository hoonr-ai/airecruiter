"use client";

// Recruiter warm handoff join page (Pair-bot docs/recruiter-handoff-pair-contract.md).
//
// Reached from the "Join now" notification, email or Teams card. Claims the
// handoff as the logged-in recruiter (the backend sends their login email,
// never anything typed here), joins the LiveKit room Pair-bot returns, plays
// the candidate, publishes the recruiter's mic/camera, and ends the
// interview with "End interview".

import { useCallback, useEffect, useRef, useState } from "react";
import { useParams, useRouter } from "next/navigation";
import { Loader2, Mic, MicOff, PhoneCall, PhoneOff, UserRound, Video, VideoOff } from "lucide-react";
import { Room, RoomEvent, Track, createLocalTracks } from "livekit-client";
import type { LocalTrack, RemoteParticipant, RemoteTrack, RemoteTrackPublication } from "livekit-client";
import { Button } from "@/components/ui/button";
import { Card } from "@/components/ui/card";
import { api, type HandoffState } from "@/lib/api";
import { describeHandoffError, formatSecondsLeft, isCandidateParticipant, isHandoffOver, joinViewFor } from "@/lib/handoff";

type Phase = "loading" | "ready" | "joining" | "in_call" | "ended";

export default function HandoffJoinPage() {
  const params = useParams<{ interviewId: string }>();
  const router = useRouter();
  const interviewId = Number.parseInt(params?.interviewId ?? "", 10);
  const validId = Number.isInteger(interviewId) && interviewId > 0;

  const [phase, setPhase] = useState<Phase>(validId ? "loading" : "ready");
  const [state, setState] = useState<HandoffState | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notes, setNotes] = useState("");
  const [now, setNow] = useState(() => Date.now());
  const [candidateVideo, setCandidateVideo] = useState<RemoteTrack | null>(null);
  const [candidateHere, setCandidateHere] = useState(false);
  const [micOn, setMicOn] = useState(true);
  // Off until the recruiter turns it on: no surprise live camera on join.
  const [camOn, setCamOn] = useState(false);
  const [audioBlocked, setAudioBlocked] = useState(false);

  const roomRef = useRef<Room | null>(null);
  const localTracks = useRef<LocalTrack[]>([]);
  const audioEls = useRef<HTMLMediaElement[]>([]);
  const videoRef = useRef<HTMLVideoElement>(null);
  const selfRef = useRef<HTMLVideoElement>(null);

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
      try {
        const tracks = await createLocalTracks({ audio: true, video: false });
        localTracks.current = tracks;
        for (const t of tracks) await room.localParticipant.publishTrack(t);
      } catch (mediaErr) {
        console.warn("handoff: joined without a microphone", mediaErr);
        setMicOn(false);
      }
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
    if (kind === "mic") {
      await lp.setMicrophoneEnabled(!micOn);
      setMicOn(!micOn);
    } else {
      const pub = await lp.setCameraEnabled(!camOn);
      if (!camOn && pub?.track && selfRef.current) pub.track.attach(selfRef.current);
      setCamOn(!camOn);
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
  if (phase === "ended") {
    return (
      <Shell title="Interview ended" body="Thanks — the candidate's call has ended.">
        <Button variant="outline" onClick={() => router.push("/notifications")}>Back to notifications</Button>
      </Shell>
    );
  }

  if (phase === "in_call") {
    return (
      <div className="max-w-5xl mx-auto p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-xl font-bold text-slate-900">Live with the candidate</h1>
          <span className="inline-flex items-center gap-2 text-sm font-medium text-emerald-700">
            <span className="w-2 h-2 rounded-full bg-emerald-500 animate-pulse" aria-hidden="true" />
            {candidateHere ? "Connected" : "Candidate reconnecting…"}
          </span>
        </div>
        {audioBlocked && (
          <Button onClick={() => void roomRef.current?.startAudio().then(() => setAudioBlocked(false))}>
            Click to turn on sound
          </Button>
        )}
        <div className="grid gap-4 md:grid-cols-[3fr_1fr]">
          <Card className="relative overflow-hidden bg-slate-900 aspect-video flex items-center justify-center">
            {candidateVideo ? (
              <video ref={videoRef} autoPlay playsInline muted aria-label="Candidate video" className="w-full h-full object-cover" />
            ) : (
              <div className="flex flex-col items-center gap-2 text-slate-300">
                <UserRound className="w-16 h-16" aria-hidden="true" />
                <span className="text-sm">Candidate is on audio</span>
              </div>
            )}
          </Card>
          <Card className="overflow-hidden bg-slate-900 aspect-video">
            <video ref={selfRef} autoPlay playsInline muted aria-label="Your camera" className="w-full h-full object-cover" />
          </Card>
        </div>
        <div className="flex flex-wrap items-center gap-3">
          <Button variant="outline" onClick={() => void toggle("mic")} aria-pressed={!micOn}>
            {micOn ? <Mic className="w-4 h-4" /> : <MicOff className="w-4 h-4" />} {micOn ? "Mute" : "Unmute"}
          </Button>
          <Button variant="outline" onClick={() => void toggle("cam")} aria-pressed={!camOn}>
            {camOn ? <Video className="w-4 h-4" /> : <VideoOff className="w-4 h-4" />} {camOn ? "Camera off" : "Camera on"}
          </Button>
          <input
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder="Notes for the record (optional)"
            aria-label="Notes"
            maxLength={5000}
            className="flex-1 min-w-[200px] h-9 px-3 rounded-md border border-slate-200 text-sm"
          />
          <Button variant="destructive" onClick={() => void end()}>
            <PhoneOff className="w-4 h-4" /> End interview
          </Button>
        </div>
        {error && <p role="alert" className="text-sm text-red-600">{error}</p>}
      </div>
    );
  }

  // ready / joining
  const view = state ? joinViewFor(state) : { kind: "none" as const };
  const left = state?.state === "requested" ? formatSecondsLeft(state.expires_at, now) : null;
  return (
    <Shell title="Recruiter handoff">
      {view.kind === "waiting" && (
        <>
          <p className="text-slate-600">
            The candidate finished the PAIR interview and is waiting on the call for you.
            {left && left !== "0:00" && <> They&apos;ll wait about <strong>{left}</strong> more.</>}
          </p>
          <Button onClick={() => void join()} disabled={phase === "joining"}>
            {phase === "joining" ? <Loader2 className="w-4 h-4 animate-spin" /> : <PhoneCall className="w-4 h-4" />}
            {phase === "joining" ? "Joining…" : "Join now"}
          </Button>
        </>
      )}
      {view.kind === "over" && <p className="text-slate-600">{view.reason}</p>}
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
