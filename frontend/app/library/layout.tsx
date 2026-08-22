import type { ReactNode } from "react";
import TabBar from "@/components/TabBar";
import "./m3.css";

export default function LibraryLayout({ children }: { children: ReactNode }) {
  return (
    <>
      {children}
      <TabBar />
    </>
  );
}
