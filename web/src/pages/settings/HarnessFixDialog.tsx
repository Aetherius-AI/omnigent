import type { Host } from "@/hooks/useHosts";

export interface HarnessFixDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: "reconfigure" | "update" | "install";
  hostId: string;
  hostName: string;
  harness: string;
  harnessLabel: string;
  currentVersion?: string;
  onFixed: (result: {
    configuredHarnesses: NonNullable<Host["configured_harnesses"]>;
    credentialLabel?: string;
  }) => void;
}

export function HarnessFixDialog(_props: HarnessFixDialogProps) {
  return null;
}
