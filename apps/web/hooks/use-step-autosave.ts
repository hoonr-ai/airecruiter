import { useEffect, useRef } from "react";

export function useStepAutosave(
  currentStep: number,
  targetStep: number,
  isReadOnly: boolean,
  isHydrated: boolean,
  saveJobDraft: (params: { currentStep: number; saveType?: string; skipToast?: boolean }) => Promise<boolean>,
  deps: any[]
) {
  const dirtyRef = useRef(false);
  const isFirstRender = useRef(true);
  
  // Use a ref to store previous dependencies so we only mark dirty on actual changes
  const prevDeps = useRef<any[]>(deps);

  useEffect(() => {
    // 1. Check if any dependency actually changed (skip if identical or first run)
    const depsChanged = deps.some((dep, i) => dep !== prevDeps.current[i]);
    prevDeps.current = deps;

    if (isFirstRender.current) {
      isFirstRender.current = false;
      return;
    }

    if (currentStep !== targetStep) return;
    if (isReadOnly) return;
    if (!isHydrated) return;
    if (!depsChanged) return; // Only autosave when the user actually modifies a monitored value

    // 2. Mark as dirty and set a debounce timer
    dirtyRef.current = true;
    const handle = setTimeout(async () => {
      const ok = await saveJobDraft({ currentStep: targetStep, saveType: "auto", skipToast: true });
      if (ok === true || ok) dirtyRef.current = false;
    }, 1500);

    return () => clearTimeout(handle);
  }, [currentStep, targetStep, isReadOnly, isHydrated, saveJobDraft, ...deps]);

  // 3. Flush on step change
  useEffect(() => {
    if (currentStep === targetStep) return;
    if (!dirtyRef.current) return;
    if (isReadOnly) return;
    if (!isHydrated) return;

    dirtyRef.current = false;
    saveJobDraft({ currentStep: targetStep, saveType: "auto", skipToast: true });
  }, [currentStep, targetStep, isReadOnly, isHydrated, saveJobDraft]);

  // 4. Warn user if trying to close the tab with unsaved changes
  useEffect(() => {
    const handleBeforeUnload = (e: BeforeUnloadEvent) => {
      if (dirtyRef.current) {
        e.preventDefault();
        e.returnValue = "";
      }
    };
    window.addEventListener("beforeunload", handleBeforeUnload);
    return () => window.removeEventListener("beforeunload", handleBeforeUnload);
  }, []);
}
