"use client";

import { Moon, Sun } from "lucide-react";
import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

interface ThemeToggleProps {
  className?: string;
  showLabel?: boolean;
  variant?: "ghost" | "outline" | "default";
  size?: "default" | "sm" | "lg" | "icon";
}

export default function ThemeToggle({
  className,
  showLabel = false,
  variant = "ghost",
  size = "icon"
}: ThemeToggleProps) {
  // Start with null to avoid hydration mismatch - theme is set by inline script in layout.tsx
  const [theme, setTheme] = useState<"light" | "dark" | null>(null);

  useEffect(() => {
    // Read the current theme from the DOM (already set by inline script in layout.tsx)
    const isDark = document.documentElement.classList.contains("dark");
    setTheme(isDark ? "dark" : "light");
  }, []);

  const toggleTheme = () => {
    const newTheme = theme === "light" ? "dark" : "light";
    setTheme(newTheme);
    localStorage.setItem("theme", newTheme);
    document.documentElement.classList.toggle("dark", newTheme === "dark");
  };

  return (
    <Button
      variant={variant}
      size={size}
      className={cn(
        "relative overflow-hidden transition-all duration-300",
        showLabel && "w-full justify-start px-3",
        !showLabel && "rounded-xl",
        className
      )}
      onClick={toggleTheme}
    >
      <Sun className={cn(
        "h-4 w-4 rotate-0 scale-100 transition-all duration-300",
        theme === "dark" ? "opacity-0 scale-75" : "opacity-100 scale-100",
        showLabel && "absolute left-3"
      )} />
      <Moon className={cn(
        "h-4 w-4 absolute transition-all duration-300",
        theme === "light" ? "opacity-0 scale-75" : "opacity-100 scale-100",
        theme === "dark" ? "rotate-0" : "-rotate-90",
        !showLabel && "left-1/2 -translate-x-1/2"
      )} />
      {showLabel && theme && (
        <span className="ml-7">{theme === "light" ? "Light" : "Dark"}</span>
      )}
      <span className="sr-only">Toggle theme</span>
    </Button>
  );
}
