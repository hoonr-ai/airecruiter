import { useState, useCallback } from "react";

export function useClampedScoreInput(initialValue: number | "" = "") {
  const [score, setScore] = useState<number | "">(initialValue);

  const handleScoreChange = useCallback((value: string) => {
    if (value === "") {
      setScore("");
    } else {
      const n = Number.parseInt(value, 10);
      setScore(Number.isFinite(n) ? Math.max(0, Math.min(100, n)) : 0);
    }
  }, []);

  return [score, handleScoreChange, setScore] as const;
}
