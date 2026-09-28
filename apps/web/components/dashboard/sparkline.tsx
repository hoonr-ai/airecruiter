"use client";

// Inline SVG trend lines for the dashboard cards and the weekly-trend modal.
// No chart library: the app ships none, and a polyline is all a sparkline is.
// Missing weeks (null) break the line instead of drawing a fake zero.

import { formatWeekLabel } from "@/lib/dashboard";

type Point = { x: number; y: number } | null;

function toPoints(values: (number | null)[], width: number, height: number, pad: number): Point[] {
  const present = values.filter((v): v is number => v !== null && Number.isFinite(v));
  const max = Math.max(...present, 0);
  const min = Math.min(...present, 0);
  const span = max - min || 1;
  const step = values.length > 1 ? (width - pad * 2) / (values.length - 1) : 0;
  return values.map((v, i) =>
    v === null || !Number.isFinite(v)
      ? null
      : { x: pad + i * step, y: height - pad - ((v - min) / span) * (height - pad * 2) },
  );
}

function segments(points: Point[]): { x: number; y: number }[][] {
  const out: { x: number; y: number }[][] = [];
  let current: { x: number; y: number }[] = [];
  for (const p of points) {
    if (p === null) {
      if (current.length) out.push(current);
      current = [];
    } else {
      current.push(p);
    }
  }
  if (current.length) out.push(current);
  return out;
}

export function Sparkline({
  values,
  color,
  width = 96,
  height = 28,
  label,
}: {
  values: (number | null)[];
  color: string;
  width?: number;
  height?: number;
  label?: string;
}) {
  const points = toPoints(values, width, height, 2);
  const parts = segments(points);
  if (!parts.length) return <div style={{ width, height }} aria-hidden />;
  return (
    <svg width={width} height={height} viewBox={`0 0 ${width} ${height}`} role="img" aria-label={label ?? "Weekly trend"}>
      {parts.map((part, i) => {
        const line = part.map((p) => `${p.x},${p.y}`).join(" ");
        const area =
          part.length > 1 ? `M${part[0].x},${height} L${line.replace(/ /g, " L")} L${part[part.length - 1].x},${height} Z` : "";
        return (
          <g key={i}>
            {area && <path d={area} fill={color} opacity={0.12} />}
            {part.length > 1 ? (
              <polyline points={line} fill="none" stroke={color} strokeWidth={1.75} strokeLinejoin="round" strokeLinecap="round" />
            ) : (
              <circle cx={part[0].x} cy={part[0].y} r={1.75} fill={color} />
            )}
          </g>
        );
      })}
    </svg>
  );
}

/** The larger chart in the weekly-trend modal: axis labels and a value per week. */
export function TrendChart({
  weeks,
  values,
  color,
  format,
}: {
  weeks: string[];
  values: (number | null)[];
  color: string;
  format: (value: number | null) => string;
}) {
  const width = 640;
  const height = 180;
  const pad = 24;
  const points = toPoints(values, width, height - 18, pad);
  const parts = segments(points);
  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="w-full h-auto" role="img" aria-label="Weekly trend chart">
      <line x1={pad} x2={width - pad} y1={height - 18 - pad} y2={height - 18 - pad} stroke="#e2e8f0" />
      {parts.map((part, i) => (
        <g key={i}>
          {part.length > 1 && (
            <path
              d={`M${part[0].x},${height - 18 - pad} L${part.map((p) => `${p.x},${p.y}`).join(" L")} L${part[part.length - 1].x},${height - 18 - pad} Z`}
              fill={color}
              opacity={0.1}
            />
          )}
          <polyline
            points={part.map((p) => `${p.x},${p.y}`).join(" ")}
            fill="none"
            stroke={color}
            strokeWidth={2.25}
            strokeLinejoin="round"
          />
        </g>
      ))}
      {points.map((p, i) =>
        p ? (
          <g key={i}>
            <circle cx={p.x} cy={p.y} r={3} fill="#fff" stroke={color} strokeWidth={2} />
            <text x={p.x} y={p.y - 8} textAnchor="middle" fontSize={10} fontWeight={600} fill="#475569">
              {format(values[i])}
            </text>
          </g>
        ) : null,
      )}
      {weeks.map((week, i) => {
        const x = weeks.length > 1 ? pad + (i * (width - pad * 2)) / (weeks.length - 1) : pad;
        return (
          <text key={week} x={x} y={height - 4} textAnchor="middle" fontSize={10} fill="#94a3b8">
            {formatWeekLabel(week)}
          </text>
        );
      })}
    </svg>
  );
}
