export function resolveRejectReason(reason: string, otherText: string): string | undefined {
  const trimmedReason = reason?.trim();
  const trimmedOther = otherText?.trim();

  if (trimmedReason === "__other__") {
    return trimmedOther || undefined;
  }
  
  return trimmedReason || undefined;
}

export function isRejectReasonValid(reason: string, otherText: string): boolean {
  return !!resolveRejectReason(reason, otherText);
}
