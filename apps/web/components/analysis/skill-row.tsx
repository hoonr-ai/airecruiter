import { Badge } from "@/components/ui/badge";
import { Progress } from "@/components/ui/progress";
import { cn } from "@/lib/utils";

interface SkillRowProps {
    slug: string;
    score: number; // 0.0 - 1.0
    status: "matched" | "missing" | "partial";
    priority: "required" | "preferred";
    level?: string;
}

export function SkillRow({ slug, score, status, priority, level }: SkillRowProps) {
    const percentage = Math.round(score * 100);

    let colorClass = "bg-slate-700";
    if (status === "matched") colorClass = "bg-emerald-500";
    else if (status === "partial") colorClass = "bg-amber-500";
    else if (status === "missing") colorClass = "bg-rose-500";

    return (
        <div className="flex items-center gap-4 py-3 border-b border-border last:border-0 hover:bg-muted/50 px-2 rounded-lg transition-colors">
            <div className="w-8 h-8 rounded-full bg-muted flex items-center justify-center border border-border text-xs font-bold text-muted-foreground">
                {priority === "required" ? "R" : "P"}
            </div>

            <div className="flex-1 min-w-0">
                <div className="flex items-center justify-between mb-1">
                    <div className="flex items-center gap-2">
                        <span className="font-medium text-foreground truncate capitalize">
                            {slug.replace(/_/g, " ")}
                        </span>
                        {level && level !== "unknown" && (
                            <Badge variant="outline" className="text-[10px] h-5 px-1.5 py-0 border-border text-muted-foreground capitalize">
                                {level}
                            </Badge>
                        )}
                    </div>
                    <span className={cn("text-xs font-mono",
                        status === "matched" ? "text-emerald-500" :
                            status === "missing" ? "text-rose-500" : "text-amber-500"
                    )}>
                        {percentage}%
                    </span>
                </div>

                <div className="h-1.5 w-full bg-muted rounded-full overflow-hidden">
                    <div
                        className={cn("h-full rounded-full transition-all duration-500", colorClass)}
                        style={{ width: `${percentage}%` }}
                    />
                </div>
            </div>
        </div>
    );
}
