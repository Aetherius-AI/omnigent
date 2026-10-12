import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { CheckIcon, CircleIcon, CopyIcon, KeyRoundIcon, Loader2Icon } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { showToast } from "@/components/ui/toast";
import {
  installHarnessOnHost,
  refreshHarnessesOnHost,
  useInstallHarness,
  type Host,
} from "@/hooks/useHosts";
import { useHarnessSetupSteps } from "@/lib/agentLabels";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";
import { HarnessCredentialForm } from "@/shell/HarnessCredentialForm";

export type HarnessFixMode = "reconfigure" | "update" | "install";

export interface HarnessFixResult {
  configuredHarnesses: Record<string, boolean | string>;
  credentialLabel?: string;
}

export interface HarnessFixDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  mode: HarnessFixMode;
  hostId: string;
  hostName: string;
  harness: string;
  harnessLabel: string;
  currentVersion?: string;
  onFixed: (result: HarnessFixResult) => void;
}

type CredentialChoice = "isaac" | "personal";
type Phase = "choose" | "progress" | "isaac" | "personal" | "credentials" | "error";

const ISAAC_HARNESSES = new Set([
  "claude",
  "claude-native",
  "codex",
  "codex-native",
  "pi",
  "pi-native",
]);
const PERSONAL_HARNESSES = new Set([
  "claude",
  "claude-native",
  "codex",
  "codex-native",
  "pi",
  "pi-native",
]);
export function HarnessFixDialog(props: HarnessFixDialogProps) {
  const {
    open,
    onOpenChange,
    mode,
    hostId,
    hostName,
    harness,
    harnessLabel,
    currentVersion,
    onFixed,
  } = props;
  const setupSteps = useHarnessSetupSteps();
  const loginCommand = useMemo(() => {
    const command = setupSteps[harness]?.find((step) => step.kind === "auth")?.command ?? null;
    if (harness === "codex" || harness === "codex-native") {
      return command ? "codex login --device-auth" : null;
    }
    if (harness === "pi" || harness === "pi-native") return command ?? "omni setup";
    return command;
  }, [harness, setupSteps]);
  const isaacSupported = ISAAC_HARNESSES.has(harness);
  const personalSupported = PERSONAL_HARNESSES.has(harness) && loginCommand !== null;
  const hasCredentialCards = isaacSupported || personalSupported;
  const initialPhase: Phase =
    mode === "update" ? "progress" : hasCredentialCards ? "choose" : "credentials";

  const [phase, setPhase] = useState<Phase>(initialPhase);
  const [choice, setChoice] = useState<CredentialChoice>(isaacSupported ? "isaac" : "personal");
  const [error, setError] = useState("");
  const [stillNotSignedIn, setStillNotSignedIn] = useState(false);
  const runId = useRef(0);
  const install = useInstallHarness(hostId);

  const finish = useCallback(
    (configuredHarnesses: Record<string, boolean | string>, credentialLabel?: string) => {
      onFixed({ configuredHarnesses, credentialLabel });
      showToast(`${harnessLabel} is ready on ${hostName}`);
      onOpenChange(false);
    },
    [harnessLabel, hostName, onFixed, onOpenChange],
  );

  const fail = useCallback((reason: unknown, id: number) => {
    if (runId.current !== id) return;
    setError(reason instanceof Error ? reason.message : "Something went wrong.");
    setPhase("error");
  }, []);

  const authenticate = useCallback((selected: CredentialChoice) => {
    setStillNotSignedIn(false);
    setPhase(selected);
  }, []);

  const start = useCallback(async () => {
    const id = ++runId.current;
    setError("");
    setStillNotSignedIn(false);
    try {
      if (mode === "update") {
        setPhase("progress");
        const result = await installHarnessOnHost(hostId, harness);
        if (runId.current === id) finish(result.configured_harnesses);
        return;
      }
      if (mode === "install") {
        setPhase("progress");
        const result = await install.mutateAsync(harness);
        if (runId.current !== id) return;
        if (result.configured_harnesses[harness] === true) {
          finish(result.configured_harnesses);
          return;
        }
        if (!hasCredentialCards) {
          setPhase("credentials");
          return;
        }
      }
      authenticate(choice);
    } catch (reason) {
      fail(reason, id);
    }
  }, [authenticate, choice, fail, finish, harness, hasCredentialCards, hostId, install, mode]);
  const startRef = useRef(start);
  startRef.current = start;

  useEffect(() => {
    if (!open) return;
    runId.current += 1;
    setChoice(isaacSupported ? "isaac" : "personal");
    setError("");
    setStillNotSignedIn(false);
    setPhase(initialPhase);
    if (mode === "update") void startRef.current();
    return () => {
      runId.current += 1;
    };
  }, [harness, hostId, initialPhase, isaacSupported, mode, open]);

  const setOpen = (nextOpen: boolean) => {
    if (!nextOpen) runId.current += 1;
    onOpenChange(nextOpen);
  };

  const retry = () => {
    runId.current += 1;
    setError("");
    setStillNotSignedIn(false);
    setPhase(mode === "update" ? "progress" : hasCredentialCards ? "choose" : "credentials");
    if (mode === "update") void start();
  };

  const checkAgain = async () => {
    const id = ++runId.current;
    setStillNotSignedIn(false);
    try {
      const result = await refreshHarnessesOnHost(hostId);
      if (runId.current !== id) return;
      if (result.configured_harnesses[harness] === true) {
        const credentialLabel =
          phase === "isaac" && result.gateway_inference?.[harness]
            ? "Isaac"
            : phase === "personal"
              ? "Personal subscription"
              : undefined;
        finish(result.configured_harnesses, credentialLabel);
      } else {
        setStillNotSignedIn(true);
      }
    } catch (reason) {
      fail(reason, id);
    }
  };

  const credentialHost: Host = {
    host_id: hostId,
    name: hostName,
    owner: "",
    status: "online",
  };

  const onCredentialStored = () => {
    void checkAgain();
  };

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="gap-0 overflow-hidden rounded-[20px] p-0 sm:max-w-[440px]">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle>{dialogTitle(mode, phase, harnessLabel, hostName)}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-5 px-6 pb-2">
          {phase === "choose" ? (
            <ChooseCredential
              mode={mode}
              choice={choice}
              onChoice={setChoice}
              hostName={hostName}
              harnessLabel={harnessLabel}
              isaacSupported={isaacSupported}
              personalSupported={personalSupported}
            />
          ) : phase === "error" ? (
            <ErrorState error={error} hostName={hostName} />
          ) : phase === "personal" && loginCommand ? (
            <PersonalCommand
              command={loginCommand}
              harness={harness}
              hostName={hostName}
              stillNotSignedIn={stillNotSignedIn}
            />
          ) : phase === "isaac" ? (
            <IsaacCommand
              harnessLabel={harnessLabel}
              hostName={hostName}
              stillNotSignedIn={stillNotSignedIn}
            />
          ) : phase === "credentials" ? (
            <CredentialForms
              harnessLabel={harnessLabel}
              hostName={hostName}
              harness={harness}
              host={credentialHost}
              onDone={onCredentialStored}
            />
          ) : (
            <ProgressState
              mode={mode}
              hostName={hostName}
              harnessLabel={harnessLabel}
              currentVersion={currentVersion}
            />
          )}
        </div>

        <DialogFooter className="mx-0 -mb-0 mt-4 rounded-none border-t border-border px-6 py-4">
          {phase === "choose" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Cancel
              </Button>
              <Button onClick={() => void start()}>
                {mode === "install" ? "Install" : "Authenticate"}
              </Button>
            </>
          ) : phase === "error" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button onClick={retry}>Try again</Button>
            </>
          ) : phase === "personal" || phase === "isaac" ? (
            <>
              <Button variant="outline" onClick={() => setOpen(false)}>
                Close
              </Button>
              <Button onClick={() => void checkAgain()}>Check again</Button>
            </>
          ) : phase === "credentials" ? (
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
          ) : (
            <Button variant="outline" onClick={() => setOpen(false)}>
              Cancel
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function ChooseCredential({
  mode,
  choice,
  onChoice,
  hostName,
  harnessLabel,
  isaacSupported,
  personalSupported,
}: {
  mode: HarnessFixMode;
  choice: CredentialChoice;
  onChoice: (choice: CredentialChoice) => void;
  hostName: string;
  harnessLabel: string;
  isaacSupported: boolean;
  personalSupported: boolean;
}) {
  return (
    <>
      <DialogDescription>
        {mode === "install"
          ? `Choose the credential ${harnessLabel} will use to authenticate.`
          : `Choose the credential ${harnessLabel} will use to authenticate on ${hostName}.`}
      </DialogDescription>
      <div role="radiogroup" aria-label="Credential" className="flex flex-col gap-2">
        {isaacSupported && (
          <CredentialCard
            selected={choice === "isaac"}
            title="Isaac"
            description="Databricks-managed credential via the Unity Gateway"
            onClick={() => onChoice("isaac")}
          />
        )}
        {personalSupported && (
          <CredentialCard
            selected={choice === "personal"}
            title="Personal subscription"
            description="Your own plan (e.g. Claude Pro/Max, ChatGPT Plus)"
            onClick={() => onChoice("personal")}
          />
        )}
      </div>
    </>
  );
}

function CredentialCard({
  selected,
  title,
  description,
  onClick,
}: {
  selected: boolean;
  title: string;
  description: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      onClick={onClick}
      className={cn(
        "flex items-center gap-3 rounded-xl border px-3.5 py-3 text-left transition-colors",
        selected ? "border-foreground bg-foreground/5" : "border-border hover:bg-muted/50",
      )}
    >
      <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground">
        <KeyRoundIcon className="size-4" />
      </span>
      <span className="flex min-w-0 flex-1 flex-col gap-0.5">
        <span className="text-ui font-medium text-foreground">{title}</span>
        <span className="text-sm text-muted-foreground">{description}</span>
      </span>
      <span
        className={cn(
          "flex size-[18px] shrink-0 items-center justify-center rounded-full border",
          selected ? "border-foreground bg-foreground text-background" : "border-border-strong",
        )}
      >
        {selected && <CheckIcon className="size-3" />}
      </span>
    </button>
  );
}

function ProgressState({
  mode,
  hostName,
  harnessLabel,
  currentVersion,
}: {
  mode: HarnessFixMode;
  hostName: string;
  harnessLabel: string;
  currentVersion?: string;
}) {
  if (mode === "update") {
    return (
      <>
        <DialogDescription>
          Updating {harnessLabel} from {currentVersion ?? "the current version"} to the latest
          version on {hostName}.
        </DialogDescription>
        <Steps
          rows={[
            ["Downloaded the latest version", "done"],
            [`Installing on ${hostName}`, "active"],
            [`Checking ${harnessLabel}`, "pending"],
          ]}
        />
      </>
    );
  }

  return (
    <>
      <DialogDescription>
        Setting up {harnessLabel} on {hostName}. This usually takes a few seconds.
      </DialogDescription>
      <Steps
        rows={[
          ["Connected to the Databricks Unity Gateway", "done"],
          [`Saving the credential on ${hostName}`, "active"],
          [`Checking ${harnessLabel}`, "pending"],
        ]}
      />
    </>
  );
}

function PersonalCommand({
  command,
  harness,
  hostName,
  stillNotSignedIn,
}: {
  command: string;
  harness: string;
  hostName: string;
  stillNotSignedIn: boolean;
}) {
  return (
    <>
      <DialogDescription>Run this in a terminal on {hostName}, then come back.</DialogDescription>
      <CommandRow command={command} label="Copy login command" />
      <p className="text-sm text-muted-foreground">
        {harness.startsWith("claude")
          ? "Open the link it prints, then paste the code from your browser back into the terminal."
          : "Open the link it prints and enter the one-time code."}
      </p>
      {stillNotSignedIn && (
        <p role="status" className="text-sm text-destructive">
          Still not signed in on {hostName}.
        </p>
      )}
    </>
  );
}

function IsaacCommand({
  harnessLabel,
  hostName,
  stillNotSignedIn,
}: {
  harnessLabel: string;
  hostName: string;
  stillNotSignedIn: boolean;
}) {
  return (
    <>
      <DialogDescription>Run this in a terminal on {hostName}, then come back.</DialogDescription>
      <CommandRow command="omni setup" label="Copy omni setup" />
      <p className="text-sm text-muted-foreground">
        Choose {harnessLabel}, then the Databricks Unity Gateway credential.
      </p>
      {stillNotSignedIn && (
        <p role="status" className="text-sm text-destructive">
          Still not signed in on {hostName}.
        </p>
      )}
    </>
  );
}

function CredentialForms({
  harnessLabel,
  hostName,
  harness,
  host,
  onDone,
}: {
  harnessLabel: string;
  hostName: string;
  harness: string;
  host: Host;
  onDone: () => void;
}) {
  return (
    <>
      <DialogDescription>
        Add a credential {harnessLabel} can use to authenticate on {hostName}.
      </DialogDescription>
      <HarnessCredentialForm
        harness={harness}
        label={harnessLabel}
        host={host}
        command={null}
        onDone={onDone}
      />
    </>
  );
}

function Steps({ rows }: { rows: [string, "done" | "active" | "pending"][] }) {
  return (
    <div className="flex flex-col gap-2.5">
      {rows.map(([label, status]) => (
        <div
          key={label}
          className={cn(
            "flex items-center gap-2.5 text-ui",
            status === "pending" && "text-muted-foreground",
          )}
        >
          {status === "done" ? (
            <CheckIcon className="size-4 text-success" />
          ) : status === "active" ? (
            <Loader2Icon aria-label="In progress" className="size-4 animate-spin" />
          ) : (
            <CircleIcon className="size-3 fill-muted text-muted" />
          )}
          <span>{label}</span>
        </div>
      ))}
    </div>
  );
}

function ErrorState({ error, hostName }: { error: string; hostName: string }) {
  return (
    <>
      <DialogDescription>
        Nothing changed on {hostName}. Try again, or run setup in a terminal on that machine.
      </DialogDescription>
      <div className="rounded-xl border border-destructive/30 bg-destructive/5 px-3.5 py-2.5 text-ui text-destructive">
        {error}
      </div>
      <CommandRow command="omni setup" label="Copy omni setup" />
    </>
  );
}

function CommandRow({ command, label }: { command: string; label: string }) {
  return (
    <div className="flex min-h-9 items-center gap-2 rounded-[10px] border border-border px-3 py-1.5">
      <code className="min-w-0 flex-1 break-all font-mono text-sm text-foreground">{command}</code>
      <Button variant="ghost" size="sm" onClick={() => void copyText(command)} aria-label={label}>
        <CopyIcon className="size-3.5" />
        Copy
      </Button>
    </div>
  );
}

function dialogTitle(
  mode: HarnessFixMode,
  phase: Phase,
  harnessLabel: string,
  hostName: string,
): string {
  if (phase === "personal") return `Sign in to ${harnessLabel} on ${hostName}`;
  if (phase === "isaac") return `Set up Isaac for ${harnessLabel} on ${hostName}`;
  if (phase === "error") {
    if (mode === "update") return `Couldn’t update ${harnessLabel}`;
    if (mode === "install") return `Couldn’t install ${harnessLabel}`;
    return `Couldn’t reconfigure ${harnessLabel}`;
  }
  if (phase === "progress") {
    if (mode === "update") return `Updating ${harnessLabel}…`;
    if (mode === "install") return `Installing ${harnessLabel}…`;
    return `Reconfiguring ${harnessLabel}…`;
  }
  return mode === "install" ? `Install ${harnessLabel}` : `Reconfigure ${harnessLabel}`;
}
