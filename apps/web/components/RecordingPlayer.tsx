"use client";

import { useCallback, useRef, useState } from "react";
import { Pause, Play, RotateCcw } from "lucide-react";
import { formatTimecode } from "@/lib/timecode";

/**
 * Compact audio player for call recordings, with a loader in place of the
 * play button until the audio has actually reached the browser.
 *
 * Ported from the livekit-airecruiter frontend's RecordingPlayer. Not the
 * native `<audio controls>`: that has a download button in its menu, and it
 * shows a live-looking play button while an Ogg file (no duration in its
 * header) is still fetching. Here the button is a spinner until
 * `loadedmetadata`, and again whenever playback stalls for data (`waiting`).
 * The `<audio>` element is rendered without `controls`, so there is no
 * download option and no right-click menu on it.
 *
 * The presigned URL is still visible in the network tab, so this keeps the UI
 * honest rather than enforcing anything; the link's short TTL is the control.
 */

interface RecordingPlayerProps {
  src: string;
  label?: string;
  className?: string;
  // Called instead of a bare remount when the viewer clicks Retry — e.g. to
  // refetch the recordings list and get a fresh presigned URL once the
  // 15-minute TTL on `src` has expired, which a remount alone can't fix.
  onRetry?: () => void;
}

/**
 * Remounts the inner player when `src` changes (a refreshed presigned URL or a
 * re-fetched list) or on retry, so loaded/failed/duration never outlive the
 * source they were measured on.
 */
export function RecordingPlayer(props: RecordingPlayerProps) {
  const [attempt, setAttempt] = useState(0);
  const handleRetry = useCallback(() => {
    if (props.onRetry) {
      props.onRetry();
    } else {
      setAttempt((n) => n + 1);
    }
  }, [props.onRetry]);
  return <PlayerInner key={`${props.src}#${attempt}`} {...props} onRetry={handleRetry} />;
}

function PlayerInner({
  src,
  label = "recording",
  className = "",
  onRetry,
}: RecordingPlayerProps & { onRetry: () => void }) {
  const audioRef = useRef<HTMLAudioElement | null>(null);

  // `loaded` flips once, on the first metadata; `buffering` covers every later
  // stall, so a stall mid-call does not reset the duration.
  const [loaded, setLoaded] = useState(false);
  const [buffering, setBuffering] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [failed, setFailed] = useState(false);
  const [current, setCurrent] = useState(0);
  const [duration, setDuration] = useState<number | null>(null);

  const readDuration = (audio: HTMLAudioElement) =>
    setDuration(Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null);

  const toggle = useCallback(() => {
    const audio = audioRef.current;
    if (!audio) return;
    if (audio.paused) {
      // The play promise stays pending until data arrives; show the loader
      // for that window instead of a button that looks ignored.
      setBuffering(true);
      void audio.play().catch(() => setBuffering(false));
    } else {
      audio.pause();
    }
  }, []);

  const seek = (e: React.ChangeEvent<HTMLInputElement>) => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.currentTime = Number(e.target.value);
    setCurrent(Math.floor(audio.currentTime));
  };

  if (failed) {
    return (
      <div className={`flex items-center gap-3 ${className}`}>
        <p className="text-[11px] text-slate-400 font-semibold">This recording could not be loaded.</p>
        <button
          type="button"
          onClick={onRetry}
          className="inline-flex items-center gap-1 text-[11px] font-bold text-indigo-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 rounded"
        >
          <RotateCcw size={12} />
          Retry
        </button>
      </div>
    );
  }

  const showLoader = !loaded || buffering;
  const action = !loaded ? `Loading ${label}` : playing ? `Pause ${label}` : `Play ${label}`;

  return (
    <div className={`flex items-center gap-3 min-w-0 ${className}`} onContextMenu={(e) => e.preventDefault()}>
      <audio
        ref={audioRef}
        src={src}
        preload="metadata"
        onLoadedMetadata={(e) => {
          readDuration(e.currentTarget);
          setLoaded(true);
        }}
        onDurationChange={(e) => readDuration(e.currentTarget)}
        onWaiting={() => setBuffering(true)}
        onSeeking={(e) => {
          if (!e.currentTarget.paused) setBuffering(true);
        }}
        onPlaying={() => setBuffering(false)}
        onCanPlay={(e) => {
          if (e.currentTarget.paused) setBuffering(false);
        }}
        onSeeked={(e) => {
          if (e.currentTarget.paused) setBuffering(false);
        }}
        onPlay={() => setPlaying(true)}
        onPause={() => {
          // A paused player is never "buffering": clearing here covers a Pause
          // clicked while play() was pending or during a stall, where no
          // later canplay/playing event is guaranteed.
          setPlaying(false);
          setBuffering(false);
        }}
        onEnded={() => {
          setPlaying(false);
          setBuffering(false);
        }}
        // Whole seconds: React skips the re-render when the value is unchanged.
        onTimeUpdate={(e) => setCurrent(Math.floor(e.currentTarget.currentTime))}
        onError={() => {
          setBuffering(false);
          setFailed(true);
        }}
      />
      <button
        type="button"
        onClick={toggle}
        disabled={!loaded}
        aria-label={action}
        aria-busy={showLoader}
        title={action}
        className="shrink-0 w-9 h-9 rounded-full flex items-center justify-center bg-indigo-50 text-indigo-700 border border-indigo-200 hover:bg-indigo-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-indigo-500 transition-colors disabled:cursor-default"
      >
        {showLoader ? (
          <span
            aria-hidden="true"
            className="block w-4 h-4 rounded-full border-2 border-indigo-200 border-t-indigo-700 animate-spin motion-reduce:animate-pulse"
          />
        ) : playing ? (
          <Pause size={16} fill="currentColor" />
        ) : (
          <Play size={16} fill="currentColor" className="translate-x-px" />
        )}
      </button>
      <input
        type="range"
        aria-label={`Seek ${label}`}
        min={0}
        max={duration ?? 0}
        step={1}
        aria-valuetext={`${formatTimecode(current)} of ${duration === null ? "unknown" : formatTimecode(duration)}`}
        value={Math.min(current, duration ?? 0)}
        onChange={seek}
        disabled={duration === null}
        className="flex-1 min-w-0 h-1.5 accent-indigo-600 cursor-pointer disabled:cursor-default disabled:opacity-50"
      />
      <span className="text-xs text-slate-500 tabular-nums shrink-0">
        {formatTimecode(current)} / {duration === null ? "–:––" : formatTimecode(duration)}
      </span>
    </div>
  );
}
