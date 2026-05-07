// Phone normalization for DNC matching. Mirrors apps/api/utils/phone.py so
// the client- and server-side checks agree. Returns the digit-only North
// American 11-digit form (e.g. "14408405137") or null when the input
// cannot be normalized.

export function normalizePhone(raw: string | null | undefined): string | null {
  if (raw === null || raw === undefined) return null;
  const digits = String(raw).replace(/\D/g, "");
  if (!digits) return null;
  if (digits.length === 10) return "1" + digits;
  if (digits.length === 11 && digits.startsWith("1")) return digits;
  return null;
}
