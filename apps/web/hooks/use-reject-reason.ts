import { useState } from 'react';
import { resolveRejectReason, isRejectReasonValid } from '../lib/rejection';

export function useRejectReason() {
  const [rejectReason, setRejectReason] = useState("");
  const [otherRejectText, setOtherRejectText] = useState("");

  const reset = () => {
    setRejectReason("");
    setOtherRejectText("");
  };

  const finalReason = resolveRejectReason(rejectReason, otherRejectText);
  const isReasonValid = isRejectReasonValid(rejectReason, otherRejectText);

  return {
    rejectReason,
    setRejectReason,
    otherRejectText,
    setOtherRejectText,
    reset,
    finalReason,
    isReasonValid,
  };
}
