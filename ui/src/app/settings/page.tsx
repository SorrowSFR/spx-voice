"use client";

import { Activity,Cpu } from "lucide-react";

import { MCPSection } from "@/components/MCPSection";
import { TelemetrySection } from "@/components/TelemetrySection";
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from "@/components/ui/card";

export default function SettingsPage() {
  return (
    <div className="container mx-auto px-4 py-8 space-y-8">
      {/* Premium Header */}
      <div className="relative animate-fade-in-up">
        <div className="absolute -top-10 -right-10 w-64 h-64 bg-gradient-to-br from-slate-500/5 to-zinc-500/5 rounded-full blur-3xl -z-10" />

        <div>
          <div className="flex items-center gap-2 mb-2">
            <div className="h-1.5 w-8 bg-gradient-to-r from-slate-500 to-zinc-500 rounded-full" />
            <span className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">Configuration</span>
          </div>
          <h1 className="text-3xl font-bold tracking-tight">Platform Settings</h1>
          <p className="text-muted-foreground mt-1">Manage your platform configuration and integrations</p>
        </div>
      </div>

      {/* Settings Cards */}
      <div className="grid gap-6 max-w-3xl">
        <Card className="card-hover overflow-hidden">
          <CardHeader className="pb-4">
            <div className="flex items-center gap-3">
              <div className="bg-gradient-to-br from-blue-500 to-cyan-500 p-2.5 rounded-xl shadow-lg shadow-blue-500/20">
                <Cpu className="h-5 w-5 text-white" />
              </div>
              <div>
                <CardTitle className="text-lg">MCP Server</CardTitle>
                <CardDescription>
                  Let AI assistants access this workspace and its operational context via
                  the Model Context Protocol.
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <MCPSection />
          </CardContent>
        </Card>

        <Card className="card-hover overflow-hidden">
          <CardHeader className="pb-4">
            <div className="flex items-center gap-3">
              <div className="bg-gradient-to-br from-emerald-500 to-teal-500 p-2.5 rounded-xl shadow-lg shadow-emerald-500/20">
                <Activity className="h-5 w-5 text-white" />
              </div>
              <div>
                <CardTitle className="text-lg">Telemetry</CardTitle>
                <CardDescription>
                  Configure Langfuse tracing for your voice agent calls.
                </CardDescription>
              </div>
            </div>
          </CardHeader>
          <CardContent>
            <TelemetrySection />
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
