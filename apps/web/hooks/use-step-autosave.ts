import { useEffect, useEffectEvent, useRef } from "react";

export type SaveJobDraft = (params: {
  currentStep: number;
  saveType?: string;
  skipToast?: boolean;
  keepalive?: boolean;
}) => Promise<{ ok: boolean, message?: string }>;

// Pulled out of the effect so it can be unit tested without a DOM/React renderer.
export function haveDepsChanged(deps: readonly unknown[], prevDeps: readonly unknown[]): boolean {
  return deps.some((dep, i) => dep !== prevDeps[i]);
}

export function shouldMarkDirty(params: {
  currentStep: number;
  targetStep: number;
  isReadOnly: boolean;
  isHydrated: boolean;
  depsChanged: boolean;
}): boolean {
  const { currentStep, targetStep, isReadOnly, isHydrated, depsChanged } = params;
  return currentStep === targetStep && !isReadOnly && isHydrated && depsChanged;
}

export function useStepAutosave(
  currentStep: number,
  targetStep: number,
  isReadOnly: boolean,
  isHydrated: boolean,
  saveJobDraft: SaveJobDraft,
  deps: any[]
) {
  const dirtyRef = useRef(false);
  const isFirstRender = useRef(true);

  // Use a ref to store previous dependencies so we only mark dirty on actual changes
  const prevDeps = useRef<any[]>(deps);

  // saveJobDraft is a fresh closure every render of the parent component, so
  // it can't live in the effect dependency arrays below directly — that tore
  // down (clearTimeout'ing the pending debounce / removing the beforeunload
  // listeners) and re-ran the effects on every render, not just when
  // currentStep/targetStep/isReadOnly/isHydrated/deps actually changed. That
  // spurious re-run cancelled the debounce timer and then saw depsChanged
  // === false (prevDeps.current was already updated by the run it just tore
  // down), so it never rescheduled: dirtyRef stayed true with no save
  // pending. useEffectEvent gives a stable identity that always reads the
  // latest saveJobDraft without needing it in the dependency arrays.
  const onAutoSave = useEffectEvent((step: number, opts?: { keepalive?: boolean }) =>
    saveJobDraft({ currentStep: step, saveType: "auto", skipToast: true, ...opts })
  );

  useEffect(() => {
    // 1. Check if any dependency actually changed (skip if identical or first run)
    const depsChanged = haveDepsChanged(deps, prevDeps.current);
    prevDeps.current = deps;

    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }

    if (!shouldMarkDirty({ currentStep, targetStep, isReadOnly, isHydrated, depsChanged })) return;

    // 2. Mark as dirty and set a debounce timer
    dirtyRef.current = true;
    const handle = setTimeout(async () => {
      const ok = await onAutoSave(targetStep);
      if (ok.ok) dirtyRef.current = false;
    }, 1500);

    return () => clearTimeout(handle);
  }, [currentStep, targetStep, isReadOnly, isHydrated, ...deps]);

  // 3. Flush on step change
  useEffect(() => {
    if (currentStep === targetStep) return;
    if (!dirtyRef.current) return;
    if (isReadOnly) return;
    if (!isHydrated) return;

    dirtyRef.current = false;
    onAutoSave(targetStep);
  }, [currentStep, targetStep, isReadOnly, isHydrated]);

  // 4. Tab close / refresh / backgrounding: warn while dirty, and best-effort
  // flush the pending save with keepalive so it has a chance to land after
  // the page starts unloading (beforeunload alone never sends the request).
  useEffect(() => {
    if (currentStep !== targetStep) return;
    if (isReadOnly) return;
    if (!isHydrated) return;

    const flush = () => {
      if (!dirtyRef.current) return;
      dirtyRef.current = false;
      onAutoSave(targetStep, { keepalive: true });
    };
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    const handleVisibilityChange = () => {
      if (document.visibilityState === "hidden") flush();
    };

    window.addEventListener("beforeunload", handleBeforeUnload);
    document.addEventListener("visibilitychange", handleVisibilityChange);
    window.addEventListener("pagehide", flush);
    return () => {
      window.removeEventListener("beforeunload", handleBeforeUnload);
      document.removeEventListener("visibilitychange", handleVisibilityChange);
      window.removeEventListener("pagehide", flush);
    };
  }, [currentStep, targetStep, isReadOnly, isHydrated]);
}
