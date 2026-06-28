"use client";

import {
  AlertTriangle,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Loader2,
  PhoneCall,
  PlugZap,
  RadioTower,
  Route,
  WandSparkles,
  XCircle,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { toast } from "sonner";

import { client } from "@/client/client.gen";
import {
  getLivekitSettingsApiV1LivekitSettingsGet,
  getWorkflowsSummaryApiV1WorkflowSummaryGet,
  setupVobizLivekitApiV1LivekitVobizSetupPost,
} from "@/client/sdk.gen";
import type {
  LiveKitSettingsResponse,
  VobizLiveKitSetupResponse,
  WorkflowSummaryResponse,
} from "@/client/types.gen";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Card,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { useAuth } from "@/lib/auth";

type LiveKitSettings = Pick<
  LiveKitSettingsResponse,
  | "livekit_url"
  | "livekit_client_url"
  | "livekit_api_key"
  | "livekit_api_secret_configured"
  | "livekit_agent_name"
  | "livekit_room_prefix"
  | "livekit_sip_inbound_host"
  | "livekit_token_ttl_seconds"
  | "livekit_sip_max_call_duration_seconds"
> & {
  voice_runtime: "livekit";
};

interface FormState {
  livekitUrl: string;
  livekitClientUrl: string;
  livekitApiKey: string;
  livekitApiSecret: string;
  livekitAgentName: string;
  livekitRoomPrefix: string;
  livekitSipInboundHost: string;
  maxCallSeconds: string;
  provisionLiveKitSip: boolean;
  configName: string;
  vobizAuthId: string;
  vobizAuthToken: string;
  vobizApplicationId: string;
  phoneNumbers: string;
  inboundWorkflowId: string;
  setDefaultOutbound: boolean;
}

const NO_WORKFLOW = "__none__";
const DEFAULT_WORKFLOW_NAME = "Default Voice Assistant";

const emptySettings: LiveKitSettings = {
  voice_runtime: "livekit",
  livekit_url: "",
  livekit_client_url: "",
  livekit_api_key: "",
  livekit_api_secret_configured: false,
  livekit_agent_name: "spx-voice",
  livekit_room_prefix: "spx-voice",
  livekit_sip_inbound_host: "",
  livekit_token_ttl_seconds: 3600,
  livekit_sip_max_call_duration_seconds: 1800,
};

const emptyForm: FormState = {
  livekitUrl: "",
  livekitClientUrl: "",
  livekitApiKey: "",
  livekitApiSecret: "",
  livekitAgentName: "spx-voice",
  livekitRoomPrefix: "spx-voice",
  livekitSipInboundHost: "",
  maxCallSeconds: "1800",
  provisionLiveKitSip: true,
  configName: "Vobiz LiveKit",
  vobizAuthId: "",
  vobizAuthToken: "",
  vobizApplicationId: "",
  phoneNumbers: "",
  inboundWorkflowId: NO_WORKFLOW,
  setDefaultOutbound: true,
};

const steps = [
  { label: "LiveKit", icon: RadioTower },
  { label: "Vobiz", icon: PhoneCall },
  { label: "Routing", icon: Route },
  { label: "Run", icon: CheckCircle2 },
];

export function VobizLiveKitSetupWizard({
  onSaved,
}: {
  onSaved?: () => void | Promise<void>;
}) {
  const { user, getAccessToken, loading: authLoading } = useAuth();
  const [open, setOpen] = useState(false);
  const [step, setStep] = useState(0);
  const [loading, setLoading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [secretConfigured, setSecretConfigured] = useState(false);
  const [workflows, setWorkflows] = useState<WorkflowSummaryResponse[]>([]);
  const [form, setForm] = useState<FormState>(emptyForm);
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<{ ok: boolean; detail: string } | null>(
    null,
  );
  const [result, setResult] = useState<VobizLiveKitSetupResponse | null>(null);
  const [resultError, setResultError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (authLoading || !user) return;
    setLoading(true);
    try {
      const token = await getAccessToken();
      const [settingsResponse, workflowsResponse] = await Promise.all([
        getLivekitSettingsApiV1LivekitSettingsGet({
          headers: { Authorization: `Bearer ${token}` },
        }),
        getWorkflowsSummaryApiV1WorkflowSummaryGet({
          headers: { Authorization: `Bearer ${token}` },
          query: { status: "active" },
        }),
      ]);

      if (settingsResponse.error) {
        throw new Error(detailFromError(settingsResponse.error));
      }
      const settings = {
        ...emptySettings,
        ...settingsResponse.data,
        voice_runtime: "livekit",
      };
      setSecretConfigured(Boolean(settings.livekit_api_secret_configured));
      setForm((current) => ({
        ...current,
        livekitUrl: settings.livekit_url,
        livekitClientUrl: settings.livekit_client_url || settings.livekit_url,
        livekitApiKey: settings.livekit_api_key,
        livekitApiSecret: "",
        livekitAgentName: settings.livekit_agent_name || "spx-voice",
        livekitRoomPrefix: settings.livekit_room_prefix || "spx-voice",
        livekitSipInboundHost: settings.livekit_sip_inbound_host,
        maxCallSeconds: String(settings.livekit_sip_max_call_duration_seconds || 1800),
      }));

      if (workflowsResponse.error) {
        throw new Error(detailFromError(workflowsResponse.error));
      }
      const workflowOptions = (workflowsResponse.data ?? []).map((workflow) => ({
          id: workflow.id,
          name: workflow.name,
        }));
      setWorkflows(workflowOptions);
      setForm((current) => {
        if (current.inboundWorkflowId !== NO_WORKFLOW) return current;
        const defaultWorkflow =
          workflowOptions.find((workflow) => workflow.name === DEFAULT_WORKFLOW_NAME) ??
          (workflowOptions.length === 1 ? workflowOptions[0] : undefined);
        if (!defaultWorkflow) return current;
        return {
          ...current,
          inboundWorkflowId: String(defaultWorkflow.id),
        };
      });
    } catch (error) {
      toast.error(error instanceof Error ? error.message : "Failed to load setup data");
    } finally {
      setLoading(false);
    }
  }, [authLoading, user, getAccessToken]);

  useEffect(() => {
    if (open) {
      setStep(0);
      setResult(null);
      setResultError(null);
      setTestResult(null);
      void load();
    }
  }, [open, load]);

  const phoneNumberCount = useMemo(
    () => splitPhoneNumbers(form.phoneNumbers).length,
    [form.phoneNumbers],
  );

  const update = (key: keyof FormState, value: string | boolean) => {
    if (key === "vobizAuthId" || key === "vobizAuthToken") {
      setTestResult(null);
    }
    setForm((current) => ({ ...current, [key]: value }));
  };

  const testVobiz = async () => {
    if (!form.vobizAuthId.trim() || !form.vobizAuthToken.trim()) {
      setTestResult({
        ok: false,
        detail: "Enter the Vobiz account ID and auth token first.",
      });
      return;
    }
    setTesting(true);
    setTestResult(null);
    try {
      const token = await getAccessToken();
      const res = await client.post({
        url: "/api/v1/livekit/vobiz/test-connection",
        headers: {
          Authorization: `Bearer ${token}`,
          "Content-Type": "application/json",
        },
        body: {
          vobiz_auth_id: form.vobizAuthId.trim(),
          vobiz_auth_token: form.vobizAuthToken.trim(),
        },
      });
      if (res.error) throw new Error(detailFromError(res.error));
      const data = res.data as { ok: boolean; detail: string };
      setTestResult({ ok: Boolean(data?.ok), detail: data?.detail ?? "" });
    } catch (error) {
      setTestResult({
        ok: false,
        detail: error instanceof Error ? error.message : "Connection test failed",
      });
    } finally {
      setTesting(false);
    }
  };

  const validateStep = (targetStep = step): string | null => {
    if (targetStep === 0) {
      if (!form.provisionLiveKitSip) return null;
      if (!form.livekitUrl.trim()) return "LiveKit URL is required";
      if (!form.livekitApiKey.trim()) return "LiveKit API key is required";
      if (!secretConfigured && !form.livekitApiSecret.trim()) {
        return "LiveKit API secret is required";
      }
      if (!form.livekitSipInboundHost.trim()) {
        return "LiveKit SIP inbound host is required";
      }
    }
    if (targetStep === 1) {
      if (!form.configName.trim()) return "Configuration name is required";
      if (!form.vobizAuthId.trim()) return "Vobiz account ID is required";
      if (!form.vobizAuthToken.trim()) return "Vobiz auth token is required";
    }
    return null;
  };

  const next = () => {
    const message = validateStep();
    if (message) {
      toast.error(message);
      return;
    }
    setStep((current) => Math.min(current + 1, steps.length - 1));
  };

  const submit = async () => {
    const livekitError = validateStep(0);
    const vobizError = validateStep(1);
    if (livekitError || vobizError) {
      toast.error(livekitError ?? vobizError ?? "Setup is incomplete");
      setStep(livekitError ? 0 : 1);
      return;
    }

    setSaving(true);
    setResult(null);
    setResultError(null);
    try {
      const token = await getAccessToken();
      const response = await setupVobizLivekitApiV1LivekitVobizSetupPost({
        headers: { Authorization: `Bearer ${token}` },
        body: {
          livekit_url: form.livekitUrl.trim(),
          livekit_client_url: form.livekitClientUrl.trim() || form.livekitUrl.trim(),
          livekit_api_key: form.livekitApiKey.trim(),
          livekit_api_secret: form.livekitApiSecret.trim() || undefined,
          livekit_agent_name: form.livekitAgentName.trim() || "spx-voice",
          livekit_room_prefix: form.livekitRoomPrefix.trim() || "spx-voice",
          livekit_sip_inbound_host: form.livekitSipInboundHost.trim(),
          livekit_sip_max_call_duration_seconds: Number(form.maxCallSeconds) || 1800,
          provision_livekit_sip: form.provisionLiveKitSip,
          config_name: form.configName.trim(),
          set_default_outbound: form.setDefaultOutbound,
          vobiz_auth_id: form.vobizAuthId.trim(),
          vobiz_auth_token: form.vobizAuthToken.trim(),
          vobiz_application_id: form.vobizApplicationId.trim() || undefined,
          phone_numbers: splitPhoneNumbers(form.phoneNumbers),
          inbound_workflow_id:
            form.inboundWorkflowId === NO_WORKFLOW
              ? undefined
              : Number(form.inboundWorkflowId),
        },
      });
      if (response.error) throw new Error(detailFromError(response.error));
      const data = response.data as VobizLiveKitSetupResponse;
      setResult(data);
      if (data.sync_ok) {
        toast.success(
          `${data.telephony_config_name} is ${data.telephony_config_created ? "created" : "updated"}`,
        );
      } else {
        toast.warning(
          `${data.telephony_config_name} was saved, but SIP provisioning needs attention`,
        );
      }
      // Keep the dialog open so the result (and any failure detail) stays
      // visible instead of vanishing with a toast.
      await onSaved?.();
    } catch (error) {
      const message = error instanceof Error ? error.message : "Setup failed";
      setResultError(message);
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <Card className="overflow-hidden border-emerald-200 bg-emerald-50/50 dark:border-emerald-900 dark:bg-emerald-950/20">
        <CardHeader className="flex flex-row items-start justify-between gap-4">
          <div className="space-y-1">
            <CardTitle className="flex items-center gap-2">
              <WandSparkles className="h-5 w-5 text-emerald-700 dark:text-emerald-300" />
              Vobiz + LiveKit setup
            </CardTitle>
            <CardDescription>
              Save LiveKit, create the Vobiz config, import CLIs, and provision SIP in one run.
            </CardDescription>
          </div>
          <Button onClick={() => setOpen(true)}>
            <WandSparkles className="h-4 w-4 mr-2" />
            Open setup
          </Button>
        </CardHeader>
      </Card>

      <Dialog open={open} onOpenChange={setOpen}>
        <DialogContent className="max-w-3xl max-h-[92vh] overflow-y-auto">
          <DialogHeader>
            <DialogTitle>Vobiz + LiveKit setup</DialogTitle>
            <DialogDescription>
              One guided run for runtime credentials, Vobiz credentials, numbers, and inbound routing.
            </DialogDescription>
          </DialogHeader>

          <div className="grid grid-cols-4 gap-2">
            {steps.map((item, index) => {
              const Icon = item.icon;
              const active = index === step;
              const done = index < step;
              return (
                <button
                  key={item.label}
                  type="button"
                  onClick={() => setStep(index)}
                  className={[
                    "flex min-h-12 items-center justify-center gap-2 rounded-md border px-2 text-sm transition-colors",
                    active
                      ? "border-emerald-500 bg-emerald-50 text-emerald-900 dark:bg-emerald-950 dark:text-emerald-100"
                      : done
                        ? "border-emerald-200 bg-background text-emerald-700 dark:border-emerald-900 dark:text-emerald-300"
                        : "bg-background text-muted-foreground",
                  ].join(" ")}
                >
                  <Icon className="h-4 w-4 shrink-0" />
                  <span className="truncate">{item.label}</span>
                </button>
              );
            })}
          </div>

          {loading ? (
            <div className="flex min-h-72 items-center justify-center rounded-md border">
              <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
            </div>
          ) : (
            <div className="min-h-72 rounded-md border p-4">
              {step === 0 && (
                <div className="grid gap-4 md:grid-cols-2">
                  <div className="flex items-center justify-between rounded-md border p-3 md:col-span-2">
                    <div className="space-y-0.5">
                      <Label>Provision LiveKit SIP</Label>
                      <p className="text-xs text-muted-foreground">
                        Turn this off when LiveKit/SIP is already set up and you only need local Vobiz credentials and numbers.
                      </p>
                    </div>
                    <Switch
                      checked={form.provisionLiveKitSip}
                      onCheckedChange={(checked) => update("provisionLiveKitSip", checked)}
                    />
                  </div>
                  <Field
                    label="LiveKit URL"
                    value={form.livekitUrl}
                    placeholder="wss://project.livekit.cloud"
                    onChange={(value) => update("livekitUrl", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="Client URL"
                    value={form.livekitClientUrl}
                    placeholder="wss://project.livekit.cloud"
                    onChange={(value) => update("livekitClientUrl", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="API key"
                    value={form.livekitApiKey}
                    onChange={(value) => update("livekitApiKey", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="API secret"
                    type="password"
                    value={form.livekitApiSecret}
                    placeholder={secretConfigured ? "Saved" : "Required"}
                    onChange={(value) => update("livekitApiSecret", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="SIP inbound host"
                    value={form.livekitSipInboundHost}
                    placeholder="sip.your-livekit-host.com"
                    onChange={(value) => update("livekitSipInboundHost", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="Max call seconds"
                    type="number"
                    value={form.maxCallSeconds}
                    onChange={(value) => update("maxCallSeconds", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="Agent name"
                    value={form.livekitAgentName}
                    onChange={(value) => update("livekitAgentName", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                  <Field
                    label="Room prefix"
                    value={form.livekitRoomPrefix}
                    onChange={(value) => update("livekitRoomPrefix", value)}
                    disabled={!form.provisionLiveKitSip}
                  />
                </div>
              )}

              {step === 1 && (
                <div className="grid gap-4 md:grid-cols-2">
                  <Field
                    label="Configuration name"
                    value={form.configName}
                    onChange={(value) => update("configName", value)}
                  />
                  <Field
                    label="Vobiz Account ID"
                    value={form.vobizAuthId}
                    placeholder="MA_..."
                    onChange={(value) => update("vobizAuthId", value)}
                  />
                  <Field
                    label="Vobiz Auth Token"
                    type="password"
                    value={form.vobizAuthToken}
                    onChange={(value) => update("vobizAuthToken", value)}
                  />
                  <Field
                    label="Application ID"
                    value={form.vobizApplicationId}
                    placeholder="Optional"
                    onChange={(value) => update("vobizApplicationId", value)}
                  />
                  <div className="space-y-2 md:col-span-2">
                    <Label htmlFor="vobiz-phone-numbers">CLIs / phone numbers</Label>
                    <Textarea
                      id="vobiz-phone-numbers"
                      value={form.phoneNumbers}
                      onChange={(event) => update("phoneNumbers", event.target.value)}
                      placeholder={"+910000000000\n+910000000001"}
                      rows={5}
                      className="field-sizing-fixed font-mono text-sm"
                    />
                    <p className="text-xs text-muted-foreground">
                      {phoneNumberCount > 0
                        ? `${phoneNumberCount} ${phoneNumberCount === 1 ? "number" : "numbers"} will be imported.`
                        : "Leave blank to import numbers from the Vobiz account."}
                    </p>
                  </div>
                  <div className="space-y-2 md:col-span-2">
                    <div className="flex items-center gap-3">
                      <Button
                        type="button"
                        variant="outline"
                        onClick={testVobiz}
                        disabled={testing}
                      >
                        {testing ? (
                          <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                        ) : (
                          <PlugZap className="h-4 w-4 mr-2" />
                        )}
                        Test Vobiz connection
                      </Button>
                      <p className="text-xs text-muted-foreground">
                        Checks the account ID and auth token before the full run.
                      </p>
                    </div>
                    {testResult && (
                      <div
                        className={[
                          "flex items-start gap-2 rounded-md border p-2 text-sm",
                          testResult.ok
                            ? "border-emerald-300 bg-emerald-50 text-emerald-900 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-100"
                            : "border-red-300 bg-red-50 text-red-900 dark:border-red-900 dark:bg-red-950/30 dark:text-red-100",
                        ].join(" ")}
                      >
                        {testResult.ok ? (
                          <CheckCircle2 className="mt-0.5 h-4 w-4 shrink-0" />
                        ) : (
                          <XCircle className="mt-0.5 h-4 w-4 shrink-0" />
                        )}
                        <span>{testResult.detail}</span>
                      </div>
                    )}
                  </div>
                </div>
              )}

              {step === 2 && (
                <div className="space-y-4">
                  <div className="space-y-2">
                    <Label htmlFor="vobiz-inbound-workflow">Inbound workflow</Label>
                    <Select
                      value={form.inboundWorkflowId}
                      onValueChange={(value) => update("inboundWorkflowId", value)}
                    >
                      <SelectTrigger id="vobiz-inbound-workflow">
                        <SelectValue placeholder="No workflow" />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value={NO_WORKFLOW}>No workflow yet</SelectItem>
                        {workflows.map((workflow) => (
                          <SelectItem key={workflow.id} value={String(workflow.id)}>
                            #{workflow.id} - {workflow.name}
                          </SelectItem>
                        ))}
                      </SelectContent>
                    </Select>
                  </div>
                  <div className="flex items-center justify-between rounded-md border p-3">
                    <div className="space-y-0.5">
                      <Label>Default outbound configuration</Label>
                      <p className="text-xs text-muted-foreground">
                        Campaigns and test calls use this unless another config is selected.
                      </p>
                    </div>
                    <Switch
                      checked={form.setDefaultOutbound}
                      onCheckedChange={(checked) => update("setDefaultOutbound", checked)}
                    />
                  </div>
                </div>
              )}

              {step === 3 && (
                <div className="space-y-4">
                  <div className="grid gap-3 md:grid-cols-2">
                    <ReviewItem
                      label="LiveKit SIP"
                      value={form.provisionLiveKitSip ? "Provision" : "Skip"}
                    />
                    <ReviewItem label="LiveKit" value={form.livekitUrl} />
                    <ReviewItem label="SIP host" value={form.livekitSipInboundHost} />
                    <ReviewItem label="Vobiz account" value={form.vobizAuthId} />
                    <ReviewItem label="Configuration" value={form.configName} />
                    <ReviewItem
                      label="Numbers"
                      value={
                        phoneNumberCount > 0
                          ? String(phoneNumberCount)
                          : "Import from account"
                      }
                    />
                    <ReviewItem
                      label="Inbound workflow"
                      value={
                        form.inboundWorkflowId === NO_WORKFLOW
                          ? "None"
                          : `#${form.inboundWorkflowId}`
                      }
                    />
                  </div>
                  {!result && !resultError && (
                    <div className="rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-100">
                      <div className="flex items-center gap-2 font-medium">
                        <CheckCircle2 className="h-4 w-4" />
                        Ready to configure
                      </div>
                    </div>
                  )}

                  {resultError && (
                    <div className="space-y-2 rounded-md border border-red-300 bg-red-50 p-3 text-sm text-red-900 dark:border-red-900 dark:bg-red-950/30 dark:text-red-100">
                      <div className="flex items-center gap-2 font-medium">
                        <XCircle className="h-4 w-4" />
                        Setup failed
                      </div>
                      <p className="break-words">{resultError}</p>
                    </div>
                  )}

                  {result && (
                    <div
                      className={[
                        "space-y-3 rounded-md border p-3 text-sm",
                        result.sync_ok
                          ? "border-emerald-300 bg-emerald-50 text-emerald-950 dark:border-emerald-900 dark:bg-emerald-950/30 dark:text-emerald-100"
                          : "border-amber-300 bg-amber-50 text-amber-950 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-100",
                      ].join(" ")}
                    >
                      <div className="flex items-center gap-2 font-medium">
                        {result.sync_ok ? (
                          <CheckCircle2 className="h-4 w-4" />
                        ) : (
                          <AlertTriangle className="h-4 w-4" />
                        )}
                        {result.sync_ok
                          ? "Setup complete"
                          : "Saved, but SIP provisioning needs attention"}
                      </div>
                      <div className="grid gap-2 md:grid-cols-2">
                        <ReviewItem
                          label="Configuration"
                          value={`${result.telephony_config_name} (${result.telephony_config_created ? "created" : "updated"})`}
                        />
                        <ReviewItem
                          label="Worker"
                          value={
                            result.livekit.worker_managed_by_api
                              ? result.livekit.worker_running
                                ? "Running"
                                : "Stopped"
                              : "External"
                          }
                        />
                        <ReviewItem
                          label="Imported numbers"
                          value={String(result.imported_phone_numbers)}
                        />
                        <ReviewItem
                          label="Active numbers"
                          value={String(result.active_phone_numbers)}
                        />
                        <ReviewItem
                          label="Inbound workflow"
                          value={
                            result.inbound_workflow_id
                              ? `#${result.inbound_workflow_id}`
                              : "None"
                          }
                        />
                      </div>
                      {result.sync_message && (
                        <p className="break-words border-t border-black/10 pt-2 dark:border-white/10">
                          {result.sync_message}
                        </p>
                      )}
                    </div>
                  )}
                </div>
              )}
            </div>
          )}

          <DialogFooter className="items-center justify-between gap-2 sm:justify-between">
            <div className="flex flex-wrap gap-2">
              <Badge variant="secondary">
                {!form.provisionLiveKitSip
                  ? "Vobiz local only"
                  : secretConfigured
                    ? "LiveKit secret saved"
                    : "LiveKit secret needed"}
              </Badge>
              {phoneNumberCount > 0 && (
                <Badge variant="outline">{phoneNumberCount} CLI import</Badge>
              )}
            </div>
            <div className="flex gap-2">
              <Button
                variant="outline"
                onClick={() => setStep((current) => Math.max(current - 1, 0))}
                disabled={step === 0 || saving}
              >
                <ChevronLeft className="h-4 w-4 mr-2" />
                Back
              </Button>
              {step < steps.length - 1 ? (
                <Button onClick={next} disabled={saving || loading}>
                  Next
                  <ChevronRight className="h-4 w-4 ml-2" />
                </Button>
              ) : (
                <>
                  <Button onClick={submit} disabled={saving || loading}>
                    {saving ? (
                      <Loader2 className="h-4 w-4 mr-2 animate-spin" />
                    ) : (
                      <WandSparkles className="h-4 w-4 mr-2" />
                    )}
                    {saving
                      ? "Configuring..."
                      : result || resultError
                        ? "Re-run setup"
                        : "Run setup"}
                  </Button>
                  {result?.sync_ok && !saving && (
                    <Button variant="secondary" onClick={() => setOpen(false)}>
                      Done
                    </Button>
                  )}
                </>
              )}
            </div>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}

function Field({
  label,
  value,
  onChange,
  placeholder,
  type = "text",
  disabled = false,
}: {
  label: string;
  value: string;
  onChange: (value: string) => void;
  placeholder?: string;
  type?: "text" | "password" | "number";
  disabled?: boolean;
}) {
  const id = `vobiz-lk-${label.toLowerCase().replace(/[^a-z0-9]+/g, "-")}`;
  return (
    <div className="space-y-2">
      <Label htmlFor={id}>{label}</Label>
      <Input
        id={id}
        type={type}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        autoComplete={type === "password" ? "current-password" : undefined}
        disabled={disabled}
      />
    </div>
  );
}

function ReviewItem({ label, value }: { label: string; value: string }) {
  return (
    <div className="min-h-16 rounded-md border p-3">
      <div className="text-xs text-muted-foreground">{label}</div>
      <div className="mt-1 truncate font-medium" title={value}>
        {value || "-"}
      </div>
    </div>
  );
}

function splitPhoneNumbers(value: string): string[] {
  return value
    .split(/[\s,]+/)
    .map((item) => item.trim())
    .filter(Boolean);
}

function detailFromError(err: unknown): string {
  if (typeof err === "string") return err;
  if (err && typeof err === "object" && "detail" in err) {
    const detail = (err as { detail?: unknown }).detail;
    if (typeof detail === "string") return detail;
    if (Array.isArray(detail) && detail.length > 0) {
      const first = detail[0] as { msg?: unknown };
      if (typeof first.msg === "string") return first.msg;
    }
  }
  return "Request failed";
}
