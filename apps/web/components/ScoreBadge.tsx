import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { scoreColorClasses } from "@/lib/score-color";

export function ScoreBadge({ score, className }: { score: number; className?: string }) {
  return (
    <Badge className={cn(scoreColorClasses(score), "border-transparent", className)}>
      {Math.round(score)}
    </Badge>
  );
}
