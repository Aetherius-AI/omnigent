import type { MouseEvent } from "react";
import { Loader2Icon } from "lucide-react";
import { Link } from "@/lib/routing";
import { useSessionImportStore } from "@/store/sessionImport";

/** Sidebar status row for an in-flight Settings › Import; links back to the panel. */
export function SessionImportStatus({
  onNavigate,
}: {
  onNavigate?: (e: MouseEvent<HTMLAnchorElement>) => void;
}) {
  const importing = useSessionImportStore((s) => s.importing);
  const count = useSessionImportStore((s) => s.streamed.length);
  if (!importing) return null;
  return (
    <p
      role="status"
      data-testid="sidebar-import-progress"
      className="flex items-center gap-2 px-2 py-1 text-ui text-muted-foreground"
    >
      <Loader2Icon className="size-3.5 shrink-0 animate-spin" aria-hidden="true" />
      <Link
        to="/settings/import"
        onClick={onNavigate}
        className="truncate hover:underline"
        componentId="sidebar.import_progress"
      >
        Importing sessions{count > 0 ? `, ${count} so far` : ""}…
      </Link>
    </p>
  );
}
