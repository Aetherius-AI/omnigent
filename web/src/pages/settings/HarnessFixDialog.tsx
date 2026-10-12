import { useCallback, useEffect, useRef, useState } from "react";
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
  cancelHarnessLogin,
  configureIsaacCredential,
  fetchHarnessLoginStatus,
  installHarnessOnHost,
  startHarnessLogin,
  useInstallHarness,
} from "@/hooks/useHosts";
import { copyText } from "@/lib/clipboard";
import { cn } from "@/lib/utils";

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
type Phase = "choose" | "progress" | "error";

const PERSONAL_LOGIN_HARNESSES = new Set(["claude", "claude-native", "codex", "codex-native"]);
const LOGIN_POLL_MS = 1_000;

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
  const [phase, setPhase] = useState<Phase>("choose");
  const [choice, setChoice] = useState<CredentialChoice>("isaac");
  const [error, setError] = useState("");
  const [authUrl, setAuthUrl] = useState<string | null>(null);
  const [userCode, setUserCode] = useState<string | null>(null);
  const runId = useRef(0);
  const loginId = useRef<string | null>(null);
  const loginWindow = useRef<Window | null>(null);
  const install = useInstallHarness(hostId);
  const personalLoginSupported = PERSONAL_LOGIN_HARNESSES.has(harness);

  const finish = useCallback(
    (configuredHarnesses: Record<string, boolean | string>, credentialDisplayLabel?: string) => {
      onFixed({ configuredHarnesses, credentialLabel: credentialDisplayLabel });
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

  const pollLogin = useCallback(
    async (id: number, pendingLoginId: string) => {
      if (runId.current !== id) return;
      const result = await fetchHarnessLoginStatus(hostId, harness, pendingLoginId);
      if (runId.current !== id) return;
      if (result.status === "failed") {
        throw new Error(result.error || "The browser sign-in failed.");
      }
      if (result.status === "succeeded") {
        if (!result.configured_harnesses) {
          throw new Error("The host did not return refreshed harness status.");
        }
        finish(result.configured_harnesses, "Personal subscription");
        return;
      }
      await new Promise<void>((resolve) => {
        window.setTimeout(resolve, LOGIN_POLL_MS);
      });
      return pollLogin(id, pendingLoginId);
    },
    [finish, harness, hostId],
  );

  const authenticate = useCallback(
    async (selected: CredentialChoice, id: number) => {
      if (selected === "personal") {
        const started = await startHarnessLogin(hostId, harness);
        if (runId.current !== id) return;
        loginId.current = started.login_id;
        setAuthUrl(started.auth_url);
        setUserCode(started.user_code);
        if (started.auth_url) {
          if (loginWindow.current) loginWindow.current.location.href = started.auth_url;
          else window.open(started.auth_url, "_blank", "noopener,noreferrer");
        }
        await pollLogin(id, started.login_id);
        return;
      }

      const result = await configureIsaacCredential(hostId, harness);
      if (runId.current === id) finish(result.configured_harnesses, "Isaac");
    },
    [finish, harness, hostId, pollLogin],
  );

  const start = useCallback(async () => {
    const id = ++runId.current;
    loginId.current = null;
    setAuthUrl(null);
    setUserCode(null);
    setError("");
    setPhase("progress");
    if (choice === "personal") loginWindow.current = window.open("", "_blank");
    try {
      if (mode === "update") {
        const result = await installHarnessOnHost(hostId, harness, { upgrade: true });
        if (runId.current === id) finish(result.configured_harnesses);
        return;
      }
      if (mode === "install") {
        const result = await install.mutateAsync(harness);
        if (runId.current !== id) return;
        if (!personalLoginSupported) {
          finish(result.configured_harnesses);
          return;
        }
      }
      await authenticate(choice, id);
    } catch (reason) {
      fail(reason, id);
    }
  }, [authenticate, choice, fail, finish, harness, hostId, install, mode, personalLoginSupported]);
  const startRef = useRef(start);
  startRef.current = start;

  useEffect(() => {
    if (!open) return;
    runId.current += 1;
    loginId.current = null;
    setChoice("isaac");
    setError("");
    setAuthUrl(null);
    setUserCode(null);
    setPhase(mode === "update" ? "progress" : "choose");
    if (mode === "update") void startRef.current();
    return () => {
      runId.current += 1;
    };
  }, [open, mode, hostId, harness]);

  const setOpen = (nextOpen: boolean) => {
    if (!nextOpen) {
      runId.current += 1;
      const pendingLoginId = loginId.current;
      loginId.current = null;
      loginWindow.current?.close();
      loginWindow.current = null;
      if (pendingLoginId) void cancelHarnessLogin(hostId, harness, pendingLoginId).catch(() => {});
    }
    onOpenChange(nextOpen);
  };

  const retry = () => {
    runId.current += 1;
    loginId.current = null;
    setError("");
    setAuthUrl(null);
    setUserCode(null);
    setPhase(mode === "update" ? "progress" : "choose");
    if (mode === "update") void start();
  };

  const unsupported = mode !== "update" && !personalLoginSupported;

  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogContent className="gap-0 overflow-hidden rounded-[20px] p-0 sm:max-w-[440px]">
        <DialogHeader className="px-6 pt-6 pb-4">
          <DialogTitle>{dialogTitle(mode, phase, harnessLabel)}</DialogTitle>
        </DialogHeader>

        <div className="flex flex-col gap-5 px-6 pb-2">
          {unsupported ? (
            <Fallback hostName={hostName} harnessLabel={harnessLabel} />
          ) : phase === "choose" ? (
            <ChooseCredential
              mode={mode}
              choice={choice}
              onChoice={setChoice}
              hostName={hostName}
              harnessLabel={harnessLabel}
            />
          ) : phase === "error" ? (
            <ErrorState error={error} hostName={hostName} />
          ) : (
            <ProgressState
              mode={mode}
              choice={choice}
              harness={harness}
              hostName={hostName}
              harnessLabel={harnessLabel}
              currentVersion={currentVersion}
              authUrl={authUrl}
              userCode={userCode}
            />
          )}
        </div>

        <DialogFooter className="mx-0 -mb-0 mt-4 rounded-none border-t border-border px-6 py-4">
          {unsupported ? (
            <Button variant="outline" onClick={() => setOpen(false)}>
              Close
            </Button>
          ) : phase === "choose" ? (
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
}: {
  mode: HarnessFixMode;
  choice: CredentialChoice;
  onChoice: (choice: CredentialChoice) => void;
  hostName: string;
  harnessLabel: string;
}) {
  return (
    <>
      <DialogDescription>
        {mode === "install"
          ? `Choose the credential ${harnessLabel} will use to authenticate.`
          : `Choose the credential ${harnessLabel} will use to authenticate on ${hostName}.`}
      </DialogDescription>
      <div role="radiogroup" aria-label="Credential" className="flex flex-col gap-2">
        <CredentialCard
          selected={choice === "isaac"}
          title="Isaac"
          description="Databricks-managed credential via the Unity Gateway"
          onClick={() => onChoice("isaac")}
        />
        <CredentialCard
          selected={choice === "personal"}
          title="Personal subscription"
          description="Your own plan (e.g. Claude Pro/Max, ChatGPT Plus)"
          onClick={() => onChoice("personal")}
        />
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
  choice,
  harness,
  hostName,
  harnessLabel,
  currentVersion,
  authUrl,
  userCode,
}: {
  mode: HarnessFixMode;
  choice: CredentialChoice;
  harness: string;
  hostName: string;
  harnessLabel: string;
  currentVersion?: string;
  authUrl: string | null;
  userCode: string | null;
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

  const personal = choice === "personal";
  return (
    <>
      <DialogDescription>
        {personal
          ? `Finish signing in to your ${subscriptionName(harness)} in the browser tab that just opened. This updates when you’re done.`
          : `Setting up ${harnessLabel} on ${hostName}. This usually takes a few seconds.`}
      </DialogDescription>
      <Steps
        rows={
          personal
            ? [
                [`Started sign-in on ${hostName}`, "done"],
                ["Waiting for you to finish in the browser", "active"],
                [`Checking ${harnessLabel}`, "pending"],
              ]
            : [
                ["Connected to the Databricks Unity Gateway", "done"],
                [`Saving the credential on ${hostName}`, "active"],
                [`Checking ${harnessLabel}`, "pending"],
              ]
        }
      />
      {personal && (authUrl || userCode) && (
        <PersonalLoginHandoff authUrl={authUrl} userCode={userCode} />
      )}
    </>
  );
}

function PersonalLoginHandoff({
  authUrl,
  userCode,
}: {
  authUrl: string | null;
  userCode: string | null;
}) {
  return (
    <div className="flex flex-col gap-2 rounded-xl border border-border bg-muted/30 p-3">
      <p className="text-sm text-muted-foreground">Browser didn’t open?</p>
      <div className="flex flex-wrap items-center gap-2">
        {userCode && (
          <code className="mr-auto font-mono text-base font-semibold tracking-wide text-foreground">
            {userCode}
          </code>
        )}
        {userCode && (
          <Button
            variant="outline"
            size="sm"
            onClick={() => void copyText(userCode)}
            aria-label="Copy sign-in code"
          >
            <CopyIcon className="size-3.5" />
            Copy code
          </Button>
        )}
        {authUrl && (
          <Button asChild variant="outline" size="sm">
            <a href={authUrl} target="_blank" rel="noreferrer">
              Open the sign-in page
            </a>
          </Button>
        )}
      </div>
    </div>
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
      <SetupCommand />
    </>
  );
}

function Fallback({ hostName, harnessLabel }: { hostName: string; harnessLabel: string }) {
  return (
    <>
      <DialogDescription>
        Run <code className="font-mono text-foreground">omni setup</code> on {hostName} to finish
        setting up {harnessLabel}.
      </DialogDescription>
      <SetupCommand />
    </>
  );
}

function SetupCommand() {
  return (
    <div className="flex h-9 items-center gap-2 rounded-[10px] border border-border px-3">
      <code className="flex-1 font-mono text-sm text-foreground">omni setup</code>
      <Button
        variant="ghost"
        size="sm"
        onClick={() => void copyText("omni setup")}
        aria-label="Copy omni setup"
      >
        <CopyIcon className="size-3.5" />
        Copy
      </Button>
    </div>
  );
}

function dialogTitle(mode: HarnessFixMode, phase: Phase, harnessLabel: string): string {
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

function subscriptionName(harness: string): string {
  if (harness === "codex" || harness === "codex-native") return "ChatGPT account";
  if (harness === "claude" || harness === "claude-native") return "Claude account";
  return "subscription account";
}
