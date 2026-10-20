import { useEffect, useRef, useState } from "react";
import { BellIcon } from "lucide-react";
import { authenticatedFetch } from "@/lib/identity";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from "@/components/ui/dialog";

interface Settings {
  enabled: boolean;
  wake_on_message: boolean;
  can_manage: boolean;
  supported?: boolean;
}

/** Per-session owner consent for automatic peer-message processing. */
export function PeerNotificationSettings({ sessionId }: { sessionId: string }) {
  const currentEndpoint = useRef("");
  const [settings, setSettings] = useState<Settings | null>(null);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const endpoint = `/v1/sessions/${encodeURIComponent(sessionId)}/peer-notifications`;
  useEffect(() => {
    currentEndpoint.current = endpoint;
    const controller = new AbortController();
    setSettings(null);
    setBusy(false);
    setOpen(false);
    setError(null);
    void authenticatedFetch(endpoint, { signal: controller.signal })
      .then(async (response) => {
        if (response.ok) {
          const value = (await response.json()) as Settings;
          if (!controller.signal.aborted) setSettings(value);
        }
      })
      .catch(() => {});
    return () => controller.abort();
  }, [endpoint]);
  const update = async (next: Settings) => {
    setBusy(true);
    setError(null);
    try {
      const response = await authenticatedFetch(endpoint, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: next.enabled, wake_on_message: next.wake_on_message }),
      });
      if (!response.ok) throw new Error("unavailable");
      const result = (await response.json()) as Settings;
      if (currentEndpoint.current === endpoint) setSettings(result);
    } catch {
      if (currentEndpoint.current === endpoint)
        setError("Could not save message settings. Refresh and retry.");
    } finally {
      if (currentEndpoint.current === endpoint) setBusy(false);
    }
  };
  if (!settings || settings.supported === false) return null;
  return (
    <Dialog open={open} onOpenChange={setOpen}>
      <DialogTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          aria-label="Agent message notifications"
          title="Agent message notifications"
        >
          <BellIcon className="size-4" />
        </Button>
      </DialogTrigger>
      <DialogContent className="sm:max-w-md">
        <DialogHeader>
          <DialogTitle>Agent messages</DialogTitle>
          <DialogDescription>
            Choose when this session processes messages from other agents. Automatic turns and
            sandbox wakes can incur costs.
          </DialogDescription>
        </DialogHeader>
        <div className="space-y-4 text-sm">
          <label className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={settings.enabled}
              disabled={!settings.can_manage || busy}
              onChange={(event) => void update({ ...settings, enabled: event.target.checked })}
            />
            Process messages automatically while awake
          </label>
          <label className="flex items-center gap-3">
            <input
              type="checkbox"
              checked={settings.wake_on_message}
              disabled={!settings.can_manage || busy || !settings.enabled}
              onChange={(event) =>
                void update({ ...settings, wake_on_message: event.target.checked })
              }
            />
            Wake sleeping sandbox when a message arrives
          </label>
          <p className="text-muted-foreground">
            Messages remain in the inbox when automatic processing is off. Offline workstation hosts
            cannot be started remotely. Delivery is batched and rate limited.
          </p>
          {!settings.can_manage && <p>Only the session owner can change these settings.</p>}
          {error && <p role="alert">{error}</p>}
        </div>
      </DialogContent>
    </Dialog>
  );
}
