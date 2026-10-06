export type SourceLocation = { id: number; value: string; radius: string };

export type LocationQuestion = {
  id: number;
  question_text: string;
  pass_criteria: string;
  category: string;
  is_hard_filter?: boolean;
  generated_source?: "work-arrangement-location" | "note-location-commute";
  location_values?: string[];
  [key: string]: unknown;
};

const STATE_CODES: Record<string, string> = {
  alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR", california: "CA",
  colorado: "CO", connecticut: "CT", delaware: "DE", florida: "FL", georgia: "GA",
  hawaii: "HI", idaho: "ID", illinois: "IL", indiana: "IN", iowa: "IA", kansas: "KS",
  kentucky: "KY", louisiana: "LA", maine: "ME", maryland: "MD", massachusetts: "MA",
  michigan: "MI", minnesota: "MN", mississippi: "MS", missouri: "MO", montana: "MT",
  nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
  "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
  ohio: "OH", oklahoma: "OK", oregon: "OR", pennsylvania: "PA", "rhode island": "RI",
  "south carolina": "SC", "south dakota": "SD", tennessee: "TN", texas: "TX", utah: "UT",
  vermont: "VT", virginia: "VA", washington: "WA", "west virginia": "WV",
  wisconsin: "WI", wyoming: "WY", "district of columbia": "DC",
};

export function normalizeLocationKey(value: string): string {
  const clean = String(value || "")
    .replace(/\b\d{5}(?:-\d{4})?\b/g, "")
    .replace(/[.]/g, "")
    .replace(/\s+/g, " ")
    .trim();
  const parts = clean.split(",").map(part => part.trim()).filter(Boolean);
  if (parts.length > 1) {
    const state = parts[parts.length - 1];
    const canonicalState = STATE_CODES[state.toLowerCase()] || state.toUpperCase();
    parts[parts.length - 1] = canonicalState;
  }
  return parts.join(", ").toLowerCase();
}

export function mergeSourceLocations(args: {
  jobCity?: string;
  jobState?: string;
  jobZip?: string;
  existing: SourceLocation[];
  noteLocations: string[];
  defaultRadius: string;
  excludedLocationKeys?: string[];
  remote?: boolean;
}): SourceLocation[] {
  const merged: SourceLocation[] = [];
  const keys = new Set<string>();
  const excluded = new Set((args.excludedLocationKeys || []).map(normalizeLocationKey));
  let nextId = Math.max(Date.now(), ...args.existing.map(location => location.id + 1));
  const add = (value: string, radius: string, preferredId?: number) => {
    const clean = String(value || "").trim();
    const key = normalizeLocationKey(clean);
    if (!key || keys.has(key) || excluded.has(key)) return;
    merged.push({ id: preferredId ?? nextId++, value: clean, radius });
    keys.add(key);
  };

  const city = String(args.jobCity || "").trim();
  const state = String(args.jobState || "").trim();
  const cityState = [city, state].filter(Boolean).join(", ");
  if (cityState && !args.remote) {
    const primaryKey = normalizeLocationKey(cityState);
    const savedPrimary = args.existing.find(location => normalizeLocationKey(location.value) === primaryKey);
    add(
      savedPrimary?.value || [cityState, String(args.jobZip || "").trim()].filter(Boolean).join(" "),
      savedPrimary?.radius || args.defaultRadius,
      savedPrimary?.id,
    );
  }

  for (const location of args.existing) add(location.value, location.radius, location.id);
  for (const location of args.noteLocations) add(location, args.defaultRadius);
  return merged;
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

export function mergeArrangementQuestionLocations<T extends LocationQuestion>(
  question: T,
  locations: string[],
): T {
  const seen = new Set<string>();
  const uniqueLocations = locations.map(value => value.trim()).filter(value => {
    const key = normalizeLocationKey(value);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
  if (uniqueLocations.length === 0) return question;

  let text = question.question_text;
  const previous = question.location_values || [];
  if (previous.length > 0) {
    const oldText = previous.join(" or ");
    text = text.replace(new RegExp(escapeRegExp(oldText), "i"), uniqueLocations.join(" or "));
  } else {
    // Backward compatibility for arrangement questions saved before they had
    // structured location metadata.
    text = text.replace(
      /(work arrangement based in )(.+?)(\. are you open to working in this setup\?)/i,
      (_match, prefix: string, _oldLocations: string, suffix: string) =>
        `${prefix}${uniqueLocations.join(" or ")}${suffix}`,
    );
  }
  return {
    ...question,
    question_text: text,
    generated_source: "work-arrangement-location",
    location_values: uniqueLocations,
  };
}

export function isGeneratedCommuteQuestion(question: LocationQuestion): boolean {
  if (question.generated_source === "note-location-commute") return true;
  // Recognize the exact legacy auto-generated shape while retaining other
  // recruiter-authored questions with similar wording.
  return question.category === "Location"
    && question.is_hard_filter === true
    && question.pass_criteria.trim().toLowerCase() === "yes"
    && /^are you located in or able to commute to .+\?$/i.test(question.question_text.trim());
}
