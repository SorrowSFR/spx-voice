import { NextResponse } from "next/server";

import { healthApiV1HealthGet } from "@/client/sdk.gen";
import type { HealthResponse } from "@/client/types.gen";

// Import version from package.json at build time
import packageJson from "../../../../../package.json";

export async function GET() {
  const uiVersion = packageJson.version || "dev";

  let apiVersion = "unknown";
  let deploymentMode = "oss";
  let authProvider = "local";
  let turnEnabled = false;
  let forceTurnRelay = false;
  let hostedServicesEnabled = false;
  let voiceRuntime = "livekit" as const;
  let livekitEnabled = false;

  try {
    const response = await healthApiV1HealthGet();
    if (response.data) {
      const data = response.data as HealthResponse & {
        hosted_services_enabled?: boolean;
        voice_runtime?: string;
        livekit_enabled?: boolean;
      };
      apiVersion = data.version;
      deploymentMode = data.deployment_mode;
      authProvider = data.auth_provider;
      turnEnabled = Boolean(data.turn_enabled);
      forceTurnRelay = Boolean(data.force_turn_relay);
      hostedServicesEnabled = Boolean(data.hosted_services_enabled);
      voiceRuntime = "livekit";
      livekitEnabled = Boolean(data.livekit_enabled);
    }
  } catch {
    apiVersion = "unavailable";
  }

  return NextResponse.json({
    ui: uiVersion,
    api: apiVersion,
    deploymentMode,
    authProvider,
    turnEnabled,
    forceTurnRelay,
    hostedServicesEnabled,
    voiceRuntime,
    livekitEnabled,
  });
}
