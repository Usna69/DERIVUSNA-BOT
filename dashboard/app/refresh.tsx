"use client";
import { useRouter } from "next/navigation";
import { useEffect } from "react";

// Re-run the server component every 15s so the page stays live without a full reload.
export default function Refresh() {
  const router = useRouter();
  useEffect(() => {
    const id = setInterval(() => router.refresh(), 15000);
    return () => clearInterval(id);
  }, [router]);
  return null;
}
