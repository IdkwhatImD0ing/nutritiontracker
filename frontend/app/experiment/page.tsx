import type { Metadata } from "next";
import Experiment from "@/components/Experiment";

export const metadata: Metadata = { title: "Experiment · Usual" };

export default function ExperimentPage() {
  return <Experiment />;
}
