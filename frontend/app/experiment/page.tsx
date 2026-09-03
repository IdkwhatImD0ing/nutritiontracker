import type { Metadata } from "next";
import Experiment from "@/components/Experiment";

export const metadata: Metadata = { title: "Experiment · Tally" };

export default function ExperimentPage() {
  return <Experiment />;
}
