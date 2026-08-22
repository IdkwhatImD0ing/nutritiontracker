import type { ReactNode } from "react";
import TabBar from "@/components/TabBar";
import "../library/m3.css";

export default function SettingsLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <TabBar />
    </>
  );
}
