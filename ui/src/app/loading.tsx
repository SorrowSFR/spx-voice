import { Loader2 } from "lucide-react";

export default function Loading() {
  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="flex flex-col items-center gap-4">
        {/* Premium Logo Pulse */}
        <div className="relative">
          <div className="w-16 h-16 rounded-2xl bg-gradient-to-br from-blue-500 to-purple-500 shadow-2xl shadow-blue-500/30 flex items-center justify-center">
            <span className="text-white font-bold text-2xl">V</span>
          </div>
          {/* Pulse ring */}
          <div className="absolute inset-0 rounded-2xl border-2 border-blue-500/50 animate-ping" />
        </div>

        {/* Loading text */}
        <div className="flex items-center gap-2 text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" />
          <span className="text-sm font-medium">Loading...</span>
        </div>
      </div>
    </div>
  );
}
